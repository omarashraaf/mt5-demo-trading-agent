from __future__ import annotations

import asyncio
import logging
import math
import socket
import time
from datetime import datetime, timezone
import re
from typing import Optional

from pydantic import BaseModel

from mt5.connector import AccountInfo, TerminalInfo

logger = logging.getLogger(__name__)


class IBKRConnectionParams(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account_id: Optional[str] = None
    readonly: bool = False


class IBKRConnector:
    def __init__(self):
        self._connected = False
        self._last_error: Optional[str] = None
        self._account_info: Optional[AccountInfo] = None
        self._terminal_info: Optional[TerminalInfo] = None
        self._active_account_id: str = ""
        self._host = "127.0.0.1"
        self._port = 7497
        self._client_id = 1
        self._ib = None
        self._ib_class = None
        self._contract_cache: dict[str, object] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def account_info(self) -> Optional[AccountInfo]:
        return self._account_info

    def _ensure_ib(self):
        if self._ib is not None:
            return
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            from ib_insync import IB, util  # type: ignore
            try:
                util.patchAsyncio()
            except Exception:
                pass
        except Exception as exc:
            raise RuntimeError(
                "ib_insync is required for IBKR integration. Install backend requirements."
            ) from exc
        self._ib_class = IB
        self._ib = IB()

    def connect(self, params: IBKRConnectionParams) -> bool:
        try:
            self._ensure_ib()
            self._host = params.host
            self._port = params.port
            self._client_id = params.client_id
            self._active_account_id = (params.account_id or "").strip()

            if self._ib.isConnected():
                self._ib.disconnect()

            selected_port = int(params.port)
            fallback_ports: list[int] = []
            if selected_port == 7497:
                fallback_ports = [7497, 4002]
            elif selected_port == 7496:
                fallback_ports = [7496, 4001]
            elif selected_port in {4001, 4002}:
                fallback_ports = [selected_port]
            else:
                fallback_ports = [selected_port]

            candidate_client_ids = [int(params.client_id)]
            # Reduce friction: auto-probe nearby client IDs when default is occupied.
            for offset in (1, 2, 3, 4, 5):
                candidate_client_ids.append(int(params.client_id) + offset)

            last_connect_error: Exception | None = None
            connected = False
            selected_client_id = int(params.client_id)
            for candidate_port in fallback_ports:
                if not self._tcp_port_reachable(params.host, candidate_port, timeout_seconds=1.0):
                    continue
                for candidate_client_id in candidate_client_ids:
                    try:
                        if self._ib is not None and self._ib.isConnected():
                            self._ib.disconnect()
                        # Fresh IB client per attempt avoids stale clientId/session state.
                        self._ib = self._ib_class()
                        self._ib.connect(
                            host=params.host,
                            port=candidate_port,
                            clientId=candidate_client_id,
                            readonly=bool(params.readonly),
                            timeout=10,
                        )
                        if self._ib.isConnected():
                            selected_port = candidate_port
                            selected_client_id = candidate_client_id
                            connected = True
                            break
                    except Exception as exc:
                        last_connect_error = exc
                        continue
                if connected:
                    break

            if not connected:
                # Try one direct connect attempt on requested port to keep backward behavior
                # and surface detailed ib_insync exception when available.
                try:
                    if self._ib is not None and self._ib.isConnected():
                        self._ib.disconnect()
                    self._ib = self._ib_class()
                    self._ib.connect(
                        host=params.host,
                        port=params.port,
                        clientId=params.client_id,
                        readonly=bool(params.readonly),
                        timeout=10,
                    )
                    connected = bool(self._ib.isConnected())
                except Exception as exc:
                    last_connect_error = exc

            if connected and self._ib.isConnected():
                self._port = selected_port
                self._client_id = selected_client_id
            else:
                if last_connect_error is not None:
                    raise last_connect_error
                raise RuntimeError(
                    f"No reachable IBKR API listener found at {params.host} on expected ports {fallback_ports} "
                    f"using candidate client IDs {candidate_client_ids}."
                )

            if not self._ib.isConnected():
                self._last_error = "IBKR connection failed."
                return False
            try:
                # Prefer delayed stream when live subscriptions are missing.
                # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed Frozen.
                self._ib.reqMarketDataType(3)
            except Exception:
                pass

            self._connected = True
            self._last_error = None
            trade_mode = 0 if (self._active_account_id or "").upper().startswith("DU") else 2
            self._account_info = AccountInfo(
                login=0,
                name=f"IBKR {(self._active_account_id or '').strip() or 'Account'}",
                server=f"{self._host}:{self._port}",
                balance=0.0,
                equity=0.0,
                margin=0.0,
                free_margin=0.0,
                leverage=1,
                currency="USD",
                trade_mode=trade_mode,
            )
            self._terminal_info = TerminalInfo(
                connected=True,
                path="Interactive Brokers (TWS/IB Gateway)",
                data_path="",
                community_account=False,
                build=0,
                name="IBKR",
            )
            logger.info(
                "Connected to IBKR at %s:%s (client_id=%s, account=%s)",
                params.host,
                selected_port,
                selected_client_id,
                self._active_account_id or "auto",
            )
            return True
        except Exception as exc:
            self._connected = False
            self._account_info = None
            self._terminal_info = None
            self._last_error = (
                f"IBKR connect failed: {exc}. "
                f"Verify TWS/Gateway API is enabled and listening on {params.host}:{params.port} "
                f"(Paper: 7497 or 4002, Live: 7496 or 4001), and allow localhost/trusted clients."
            )
            logger.exception(self._last_error)
            return False

    def _tcp_port_reachable(self, host: str, port: int, timeout_seconds: float = 1.0) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_seconds)
        try:
            sock.connect((host, int(port)))
            return True
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def disconnect(self) -> bool:
        try:
            if self._ib is not None and self._ib.isConnected():
                self._ib.disconnect()
            self._connected = False
            self._account_info = None
            self._terminal_info = None
            self._last_error = None
            self._active_account_id = ""
            self._contract_cache.clear()
            logger.info("Disconnected from IBKR")
            return True
        except Exception as exc:
            self._last_error = f"IBKR disconnect failed: {exc}"
            logger.exception(self._last_error)
            return False

    def refresh_account(self) -> Optional[AccountInfo]:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return None
        try:
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
            accounts = list(self._ib.managedAccounts() or [])
            account_id = self._active_account_id or (accounts[0] if accounts else "")
            if not account_id:
                self._last_error = "IBKR connected, but no managed account was returned."
                return None
            self._active_account_id = account_id

            rows = self._ib.accountSummary(account=account_id)
            if not rows:
                # Some IBKR sessions return data only through unscoped summary.
                rows = self._ib.accountSummary()

            # If no explicit account was provided, prefer the account with highest
            # net liquidation to avoid binding to an empty/placeholder account.
            if not self._active_account_id and rows:
                candidate_accounts = {}
                for item in rows:
                    acct = str(getattr(item, "account", "") or "").strip()
                    if not acct:
                        continue
                    if str(getattr(item, "tag", "") or "") != "NetLiquidation":
                        continue
                    candidate_accounts[acct] = self._parse_float(getattr(item, "value", 0.0), 0.0)
                if candidate_accounts:
                    best_account = max(candidate_accounts.items(), key=lambda x: x[1])[0]
                    if best_account:
                        account_id = best_account
                        self._active_account_id = best_account

            filtered_rows = [
                item for item in rows
                if str(getattr(item, "account", "") or "").strip() in {"", account_id}
            ] if account_id else list(rows)
            by_tag = {str(item.tag): item for item in filtered_rows}

            def _f(tag: str, default: float = 0.0) -> float:
                row = by_tag.get(tag)
                if row is None:
                    return default
                return self._parse_float(getattr(row, "value", None), default)

            currency = "USD"
            row_currency = by_tag.get("NetLiquidation") or by_tag.get("TotalCashValue")
            if row_currency is not None and getattr(row_currency, "currency", None):
                currency = str(row_currency.currency)

            balance = _f("TotalCashValue", _f("CashBalance", 0.0))
            equity = _f("NetLiquidation", balance)
            margin = _f("MaintMarginReq", 0.0)
            free_margin = _f("AvailableFunds", max(0.0, equity - margin))
            gross_position = _f("GrossPositionValue", 0.0)
            leverage = 1
            if equity > 0:
                leverage = max(1, int(round(gross_position / equity)))

            # IBKR paper accounts usually start with DU.
            trade_mode = 0 if account_id.upper().startswith("DU") else 2
            self._account_info = AccountInfo(
                login=0,
                name=f"IBKR {account_id}",
                server=f"{self._host}:{self._port}",
                balance=balance,
                equity=equity,
                margin=margin,
                free_margin=free_margin,
                leverage=leverage,
                currency=currency,
                trade_mode=trade_mode,
            )
            self._terminal_info = TerminalInfo(
                connected=True,
                path="Interactive Brokers (TWS/IB Gateway)",
                data_path="",
                community_account=False,
                build=0,
                name="IBKR",
            )
            self._last_error = None
            return self._account_info
        except Exception as exc:
            self._last_error = f"IBKR account refresh failed: {exc}"
            logger.exception(self._last_error)
            return None

    def _parse_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value).strip()
            if not text:
                return default
            # Handle formats like "100,000.25", "$100000", "USD 100000"
            text = text.replace(",", "")
            text = re.sub(r"[^0-9.\-]", "", text)
            if text in {"", "-", ".", "-."}:
                return default
            return float(text)
        except Exception:
            return default

    def get_terminal_info(self) -> Optional[TerminalInfo]:
        if not self._connected:
            return None
        return self._terminal_info

    def debug_account_summary(self) -> dict:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return {"connected": False, "error": "IBKR not connected"}
        try:
            managed = list(self._ib.managedAccounts() or [])
            rows = self._ib.accountSummary() or []
            compact_rows = [
                {
                    "account": str(getattr(item, "account", "") or ""),
                    "tag": str(getattr(item, "tag", "") or ""),
                    "value": str(getattr(item, "value", "") or ""),
                    "currency": str(getattr(item, "currency", "") or ""),
                }
                for item in rows
            ]
            return {
                "connected": True,
                "active_account_id": self._active_account_id,
                "managed_accounts": managed,
                "rows_count": len(compact_rows),
                "rows": compact_rows,
            }
        except Exception as exc:
            return {
                "connected": True,
                "active_account_id": self._active_account_id,
                "error": str(exc),
            }

    def is_demo(self) -> bool:
        if self._account_info is None:
            return False
        return self._account_info.trade_mode == 0

    def _resolve_contract(self, symbol: str):
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return None
        key = (symbol or "").upper().strip()
        if not key:
            return None
        cached = self._contract_cache.get(key)
        if cached is not None:
            return cached

        try:
            from ib_insync import Contract, Stock  # type: ignore
        except Exception:
            return None

        candidates = []
        try:
            # Fast path for common US stocks/ETFs.
            candidates.append(Stock(key, "SMART", "USD"))
        except Exception:
            pass

        # Try generic symbol matching for indices/commodities/custom feeds.
        try:
            matches = self._ib.reqMatchingSymbols(key) or []
            for match in matches:
                contract = getattr(match, "contract", None)
                if contract is not None:
                    candidates.append(contract)
        except Exception:
            matches = []

        sec_type_priority = {
            "CFD": 0,
            "STK": 1,
            "ETF": 2,
            "IND": 3,
            "FUT": 4,
            "CMDTY": 5,
        }
        is_stock_like = key.isalpha() and 1 <= len(key) <= 6

        def _score(contract):
            sec_type = str(getattr(contract, "secType", "")).upper()
            symbol = str(getattr(contract, "symbol", "")).upper()
            currency = str(getattr(contract, "currency", "")).upper()
            exchange = str(getattr(contract, "exchange", "")).upper()
            stock_like_boost = 0
            if is_stock_like and sec_type == "STK":
                stock_like_boost = -2
            exact_symbol_boost = -1 if symbol == key else 0
            return (
                stock_like_boost,
                exact_symbol_boost,
                sec_type_priority.get(sec_type, 99),
                0 if currency in {"USD", ""} else 1,
                0 if exchange in {"SMART", "IDEALPRO", "NYMEX", "COMEX", "CME", "EUREX", ""} else 1,
            )

        # Deduplicate candidates by a lightweight key.
        unique = {}
        for c in candidates:
            dedupe_key = (
                str(getattr(c, "symbol", "")).upper(),
                str(getattr(c, "secType", "")).upper(),
                str(getattr(c, "exchange", "")).upper(),
                str(getattr(c, "currency", "")).upper(),
                str(getattr(c, "lastTradeDateOrContractMonth", "")).upper(),
            )
            unique[dedupe_key] = c
        ranked = sorted(unique.values(), key=_score)

        for candidate in ranked:
            try:
                qualified = self._ib.qualifyContracts(candidate)
                if qualified:
                    resolved = qualified[0]
                    self._contract_cache[key] = resolved
                    return resolved
            except Exception:
                continue

        # Last fallback, generic contract object.
        try:
            generic = Contract(symbol=key)
            qualified = self._ib.qualifyContracts(generic)
            if qualified:
                resolved = qualified[0]
                self._contract_cache[key] = resolved
                return resolved
        except Exception:
            pass

        return None

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """Best-effort quote snapshot for any supported contract type."""
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return None
        contract = self._resolve_contract(symbol)
        if contract is None:
            return None
        try:
            ticker = self._ib.reqMktData(contract, "", True, False)
            self._ib.sleep(0.8)
            bid = float(getattr(ticker, "bid", 0.0) or 0.0)
            ask = float(getattr(ticker, "ask", 0.0) or 0.0)
            last = float(getattr(ticker, "last", 0.0) or 0.0)
            close = float(getattr(ticker, "close", 0.0) or 0.0)
            if not math.isfinite(bid):
                bid = 0.0
            if not math.isfinite(ask):
                ask = 0.0
            if not math.isfinite(last):
                last = 0.0
            if not math.isfinite(close):
                close = 0.0
            self._ib.cancelMktData(contract)
            base = last or close
            if bid <= 0 and base > 0:
                bid = base
            if ask <= 0 and base > 0:
                ask = base
            if bid <= 0 or ask <= 0:
                return None
            return {
                "symbol": symbol.upper(),
                "bid": bid,
                "ask": ask,
                "last": last or close or ((bid + ask) / 2.0),
            }
        except Exception as exc:
            self._last_error = f"IBKR snapshot failed for {symbol}: {exc}"
            logger.debug(self._last_error)
            return None

    def get_stock_snapshot(self, symbol: str) -> Optional[dict]:
        """Backward-compatible alias for existing routes."""
        return self.get_snapshot(symbol)

    def get_bars(self, symbol: str, timeframe: str, count: int = 120) -> list[dict]:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return []
        contract = self._resolve_contract(symbol)
        if contract is None:
            return []

        tf = (timeframe or "H1").upper()
        bar_size_map = {
            "M15": ("15 mins", 15),
            "H1": ("1 hour", 60),
            "H4": ("4 hours", 240),
        }
        bar_size, minutes = bar_size_map.get(tf, ("1 hour", 60))
        duration_days = max(2, int(math.ceil((count * minutes) / (60 * 24))) + 1)
        duration = f"{duration_days} D"

        try:
            bars = []
            for what_to_show in ("TRADES", "MIDPOINT", "BID_ASK"):
                bars = self._ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow=what_to_show,
                    useRTH=False,
                    formatDate=2,
                    keepUpToDate=False,
                    timeout=3.0,
                )
                if bars:
                    break
            if not bars:
                return []
            rows: list[dict] = []
            for bar in bars[-count:]:
                ts = int(getattr(bar, "date").timestamp())
                rows.append(
                    {
                        "time": ts,
                        "open": float(getattr(bar, "open", 0.0) or 0.0),
                        "high": float(getattr(bar, "high", 0.0) or 0.0),
                        "low": float(getattr(bar, "low", 0.0) or 0.0),
                        "close": float(getattr(bar, "close", 0.0) or 0.0),
                        "volume": int(getattr(bar, "volume", 0) or 0),
                    }
                )
            return rows
        except Exception as exc:
            self._last_error = f"IBKR bars failed for {symbol} {timeframe}: {exc}"
            logger.debug(self._last_error)
            return []

    def get_stock_bars(self, symbol: str, timeframe: str, count: int = 120) -> list[dict]:
        # Backward compatible name used by existing routes.
        return self.get_bars(symbol, timeframe, count)

    def _snapshot_for_contract(self, contract) -> Optional[dict]:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return None
        try:
            ticker = self._ib.reqMktData(contract, "", True, False)
            self._ib.sleep(0.6)
            bid = float(getattr(ticker, "bid", 0.0) or 0.0)
            ask = float(getattr(ticker, "ask", 0.0) or 0.0)
            last = float(getattr(ticker, "last", 0.0) or 0.0)
            close = float(getattr(ticker, "close", 0.0) or 0.0)
            self._ib.cancelMktData(contract)
            if not math.isfinite(bid):
                bid = 0.0
            if not math.isfinite(ask):
                ask = 0.0
            if not math.isfinite(last):
                last = 0.0
            if not math.isfinite(close):
                close = 0.0
            base = last or close
            if bid <= 0 and base > 0:
                bid = base
            if ask <= 0 and base > 0:
                ask = base
            if bid <= 0 and ask <= 0:
                return None
            if bid <= 0:
                bid = ask
            if ask <= 0:
                ask = bid
            return {"bid": bid, "ask": ask, "last": last or close or ((bid + ask) / 2.0)}
        except Exception:
            return None

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        comment: str = "",
    ) -> dict:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return {"success": False, "retcode": -1, "retcode_desc": "IBKR not connected"}
        contract = self._resolve_contract(symbol)
        if contract is None:
            return {"success": False, "retcode": -1, "retcode_desc": f"Cannot resolve contract for {symbol}"}

        side = str(action or "").upper()
        if side not in {"BUY", "SELL"}:
            return {"success": False, "retcode": -1, "retcode_desc": "Invalid order side"}
        qty = float(quantity or 0.0)
        if qty <= 0:
            return {"success": False, "retcode": -1, "retcode_desc": "Quantity must be > 0"}

        try:
            from ib_insync import MarketOrder  # type: ignore
        except Exception:
            return {"success": False, "retcode": -1, "retcode_desc": "ib_insync not available"}

        try:
            order = MarketOrder("BUY" if side == "BUY" else "SELL", qty)
            if self._active_account_id:
                order.account = self._active_account_id
            if comment:
                order.orderRef = str(comment)[:32]

            trade = self._ib.placeOrder(contract, order)
            deadline = time.time() + 8.0
            while not trade.isDone() and time.time() < deadline:
                self._ib.sleep(0.2)

            status = str(getattr(trade.orderStatus, "status", "") or "")
            filled_qty = float(getattr(trade.orderStatus, "filled", 0.0) or 0.0)
            avg_fill = float(getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0)
            if avg_fill <= 0 and trade.fills:
                try:
                    avg_fill = float(trade.fills[-1].execution.avgPrice or 0.0)
                except Exception:
                    avg_fill = 0.0
            ticket = int(getattr(trade.order, "permId", 0) or getattr(trade.order, "orderId", 0) or 0)

            success = status in {"Filled", "Submitted", "PreSubmitted"} and (filled_qty > 0 or status != "Filled")
            return {
                "success": success,
                "retcode": 0 if success else -1,
                "retcode_desc": status or "Order rejected",
                "ticket": ticket if ticket > 0 else None,
                "volume": filled_qty if filled_qty > 0 else qty,
                "price": avg_fill if avg_fill > 0 else None,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "comment": comment or "",
            }
        except Exception as exc:
            self._last_error = f"IBKR place_order failed: {exc}"
            logger.exception(self._last_error)
            return {"success": False, "retcode": -1, "retcode_desc": str(exc)}

    def get_positions(self, symbol: Optional[str] = None) -> list[dict]:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return []
        try:
            rows = self._ib.positions() or []
        except Exception as exc:
            self._last_error = f"IBKR positions failed: {exc}"
            return []

        result: list[dict] = []
        added_tickets: set[int] = set()
        now_ts = int(time.time())
        symbol_filter = (symbol or "").upper().strip()
        strict_filtering = bool(self._active_account_id)
        for row in rows:
            account = str(getattr(row, "account", "") or "")
            if strict_filtering and account and account != self._active_account_id:
                continue
            contract = getattr(row, "contract", None)
            if contract is None:
                continue
            pos_qty = float(getattr(row, "position", 0.0) or 0.0)
            if abs(pos_qty) < 1e-9:
                continue
            sym = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
            if symbol_filter and sym != symbol_filter:
                continue
            avg_cost = float(getattr(row, "avgCost", 0.0) or 0.0)
            snap = self._snapshot_for_contract(contract)
            current_price = float((snap or {}).get("last", 0.0) or 0.0)
            multiplier_raw = getattr(contract, "multiplier", None)
            try:
                multiplier = float(multiplier_raw) if multiplier_raw else 1.0
            except Exception:
                multiplier = 1.0
            profit = (current_price - avg_cost) * pos_qty * multiplier if current_price > 0 else 0.0
            ticket = int(getattr(contract, "conId", 0) or 0)
            normalized_ticket = ticket if ticket > 0 else abs(hash(f"{account}:{sym}")) % 10_000_000
            result.append(
                {
                    "ticket": normalized_ticket,
                    "symbol": sym,
                    "type": "BUY" if pos_qty > 0 else "SELL",
                    "volume": abs(pos_qty),
                    "price_open": avg_cost,
                    "price_current": current_price or avg_cost,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "profit": profit,
                    "time": now_ts,
                    "comment": "IBKR",
                }
            )
            added_tickets.add(int(normalized_ticket))

        # If the selected account filter produced no positions, fall back to all
        # managed-account positions to avoid hiding valid active trades.
        if strict_filtering and not result:
            for row in rows:
                contract = getattr(row, "contract", None)
                if contract is None:
                    continue
                pos_qty = float(getattr(row, "position", 0.0) or 0.0)
                if abs(pos_qty) < 1e-9:
                    continue
                sym = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
                if symbol_filter and sym != symbol_filter:
                    continue
                avg_cost = float(getattr(row, "avgCost", 0.0) or 0.0)
                snap = self._snapshot_for_contract(contract)
                current_price = float((snap or {}).get("last", 0.0) or 0.0)
                multiplier_raw = getattr(contract, "multiplier", None)
                try:
                    multiplier = float(multiplier_raw) if multiplier_raw else 1.0
                except Exception:
                    multiplier = 1.0
                account = str(getattr(row, "account", "") or "")
                profit = (current_price - avg_cost) * pos_qty * multiplier if current_price > 0 else 0.0
                ticket = int(getattr(contract, "conId", 0) or 0)
                normalized_ticket = ticket if ticket > 0 else abs(hash(f"{account}:{sym}")) % 10_000_000
                result.append(
                    {
                        "ticket": normalized_ticket,
                        "symbol": sym,
                        "type": "BUY" if pos_qty > 0 else "SELL",
                        "volume": abs(pos_qty),
                        "price_open": avg_cost,
                        "price_current": current_price or avg_cost,
                        "stop_loss": 0.0,
                        "take_profit": 0.0,
                        "profit": profit,
                        "time": now_ts,
                        "comment": f"IBKR ({account or 'managed'})",
                    }
                )
                added_tickets.add(int(normalized_ticket))
            logger.info(
                "IBKR positions fallback used: active account %s had no rows, showing managed-account positions instead.",
                self._active_account_id or "auto",
            )

        # Include submitted/presubmitted orders so users can see active trades
        # immediately even before fill.
        try:
            open_trades = list(self._ib.openTrades() or [])
        except Exception:
            open_trades = []
        for trade in open_trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if order is None or contract is None:
                continue
            if status not in {"Submitted", "PreSubmitted", "PendingSubmit"}:
                continue
            sym = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
            if symbol_filter and sym != symbol_filter:
                continue
            action = str(getattr(order, "action", "") or "").upper()
            total_qty = float(getattr(order, "totalQuantity", 0.0) or 0.0)
            limit_price = float(getattr(order, "lmtPrice", 0.0) or 0.0)
            ticket = int(getattr(order, "permId", 0) or getattr(order, "orderId", 0) or 0)
            normalized_ticket = ticket if ticket > 0 else abs(hash(f"{sym}:{status}:{total_qty}")) % 10_000_000
            if int(normalized_ticket) in added_tickets:
                continue
            result.append(
                {
                    "ticket": normalized_ticket,
                    "symbol": sym,
                    "type": "BUY" if action == "BUY" else "SELL",
                    "volume": abs(total_qty),
                    "price_open": limit_price,
                    "price_current": limit_price,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "profit": 0.0,
                    "time": now_ts,
                    "comment": f"IBKR pending ({status})",
                }
            )
            added_tickets.add(int(normalized_ticket))
        return result

    def close_position(self, ticket: int) -> dict:
        positions = self.get_positions()
        pos = next((p for p in positions if int(p.get("ticket", 0)) == int(ticket)), None)
        if pos is None:
            return {"success": False, "retcode": -1, "retcode_desc": f"Position {ticket} not found"}
        close_side = "SELL" if str(pos.get("type", "")).upper() == "BUY" else "BUY"
        return self.place_order(
            symbol=str(pos.get("symbol", "")),
            action=close_side,
            quantity=float(pos.get("volume", 0.0) or 0.0),
            comment=f"Close {ticket}",
        )

    def get_recent_executions(self, limit: int = 100) -> list[dict]:
        """Return recent executed fills from IBKR for the active account.

        This is intended for user-facing trade history views where broker-side
        executions are preferred over local app logs.
        """
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return []
        try:
            fills = list(self._ib.reqExecutions() or [])
        except Exception as exc:
            self._last_error = f"IBKR executions failed: {exc}"
            logger.warning(self._last_error)
            return []

        rows: list[dict] = []
        for fill in fills:
            execution = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None)
            if execution is None or contract is None:
                continue

            account = str(getattr(execution, "acctNumber", "") or "")
            if self._active_account_id and account and account != self._active_account_id:
                continue

            side = str(getattr(execution, "side", "") or "").upper()
            action = "BUY" if side in {"BOT", "BUY"} else "SELL"
            symbol = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
            shares = float(getattr(execution, "shares", 0.0) or 0.0)
            if shares <= 0:
                continue
            price = float(getattr(execution, "price", 0.0) or 0.0)
            exec_id = str(getattr(execution, "execId", "") or "")
            perm_id = int(getattr(execution, "permId", 0) or 0)

            ts = getattr(execution, "time", None)
            if isinstance(ts, datetime):
                opened_at = ts.timestamp()
            elif isinstance(ts, str) and ts.strip():
                try:
                    opened_at = datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    opened_at = time.time()
            else:
                opened_at = time.time()

            rows.append(
                {
                    "ticket": perm_id if perm_id > 0 else (abs(hash(exec_id)) % 10_000_000),
                    "signal_id": None,
                    "symbol": symbol,
                    "action": action,
                    "status": "closed",
                    "opened_at": opened_at,
                    "closed_at": opened_at,
                    "duration_minutes": 0.0,
                    "volume": shares,
                    "entry_price": price if price > 0 else None,
                    "stop_loss": None,
                    "take_profit": None,
                    "profit_usd": None,
                    "profit_pct": None,
                    "started_with_usd": None,
                    "ended_with_usd": None,
                    "entry_market_value_usd": (price * shares) if price > 0 else None,
                    "sl_amount_usd": None,
                    "tp_amount_usd": None,
                    "sl_pct_of_start": None,
                    "tp_pct_of_start": None,
                    "started_with_source": "broker_execution",
                    "agent_name": "IBKR",
                    "signal_confidence": None,
                    "signal_reason": "Broker execution record",
                    "risk_approved": None,
                    "risk_reason": None,
                    "exit_reason": "filled",
                }
            )

        rows.sort(key=lambda r: float(r.get("opened_at", 0.0) or 0.0), reverse=True)
        return rows[: max(1, min(int(limit), 1000))]
