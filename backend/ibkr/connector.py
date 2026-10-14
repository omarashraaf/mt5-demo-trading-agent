from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import socket
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
import re
from typing import Optional

from pydantic import BaseModel

from mt5.connector import AccountInfo, TerminalInfo

logger = logging.getLogger(__name__)


def _is_valid_resolved_contract(contract, *, expect_stock: bool = False) -> bool:
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    con_id = int(getattr(contract, "conId", 0) or 0)
    if con_id <= 0 or not sec_type:
        return False
    if expect_stock and sec_type != "STK":
        return False
    return True


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        num = float(value or 0.0)
    except Exception:
        return default
    if not math.isfinite(num):
        return default
    return num


class IBKRConnectionParams(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account_id: Optional[str] = None
    readonly: bool = False
    environment: str = "paper"


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
        self._environment = "paper"
        self._ib = None
        self._ib_class = None
        self._contract_cache: dict[str, object] = {}
        self._snapshot_cache: dict[str, tuple[float, dict[str, float | str]]] = {}
        self._position_targets: dict[int, dict[str, object]] = {}
        try:
            mdt = int(os.getenv("IBKR_MARKET_DATA_TYPE", "3") or 3)
        except Exception:
            mdt = 1
        # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed Frozen.
        self._market_data_type = mdt if mdt in {1, 2, 3, 4} else 1

    def _position_ticket_for_contract(self, contract, account: str = "", symbol: str = "") -> int:
        con_id = int(getattr(contract, "conId", 0) or 0) if contract is not None else 0
        if con_id > 0:
            return con_id
        sym = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or symbol or "").upper()
        return abs(hash(f"{account}:{sym}")) % 10_000_000

    def _position_order_ref(self, ticket: int, role: str) -> str:
        return f"TA:{int(ticket)}:{role}"[:32]

    def _cancel_position_exit_orders(self, ticket: int) -> None:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return
        prefix = f"TA:{int(ticket)}:"
        try:
            open_trades = list(self._ib.openTrades() or [])
        except Exception:
            open_trades = []
        for trade in open_trades:
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ref = str(getattr(order, "orderRef", "") or "")
            if not ref.startswith(prefix):
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status in {"Cancelled", "Filled", "Inactive", "ApiCancelled"}:
                continue
            try:
                self._ib.cancelOrder(order)
            except Exception:
                continue

    def _submit_position_exit_orders(
        self,
        *,
        contract,
        ticket: int,
        position_side: str,
        quantity: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> None:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return
        qty = float(quantity or 0.0)
        if qty <= 0:
            return
        exit_side = "SELL" if str(position_side or "").upper() == "BUY" else "BUY"
        orders = []
        oca_group = f"TA-OCA-{int(ticket)}"
        try:
            from ib_insync import StopOrder, LimitOrder  # type: ignore
        except Exception:
            return

        if stop_loss is not None and float(stop_loss or 0.0) > 0:
            stop_order = StopOrder(exit_side, qty, float(stop_loss))
            stop_order.tif = "GTC"
            stop_order.outsideRth = True
            stop_order.ocaGroup = oca_group
            stop_order.ocaType = 1
            stop_order.orderRef = self._position_order_ref(ticket, "SL")
            if self._active_account_id:
                stop_order.account = self._active_account_id
            orders.append(stop_order)

        if take_profit is not None and float(take_profit or 0.0) > 0:
            tp_order = LimitOrder(exit_side, qty, float(take_profit))
            tp_order.tif = "GTC"
            tp_order.outsideRth = True
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order.orderRef = self._position_order_ref(ticket, "TP")
            if self._active_account_id:
                tp_order.account = self._active_account_id
            orders.append(tp_order)

        for order in orders:
            self._ib.placeOrder(contract, order)

        if orders:
            deadline = time.time() + 4.0
            while time.time() < deadline:
                try:
                    self._ib.sleep(0.1)
                except Exception:
                    break

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
        if sys.platform.startswith("win"):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            except Exception:
                pass
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
            # Fallback: when backend is launched with system python instead of
            # backend venv, try loading venv site-packages dynamically.
            venv_site = Path(__file__).resolve().parents[1] / "venv" / "Lib" / "site-packages"
            if venv_site.exists():
                venv_site_str = str(venv_site)
                if venv_site_str not in sys.path:
                    sys.path.insert(0, venv_site_str)
                try:
                    from ib_insync import IB, util  # type: ignore
                    try:
                        util.patchAsyncio()
                    except Exception:
                        pass
                except Exception:
                    raise RuntimeError(
                        "ib_insync is required for IBKR integration. Install backend requirements."
                    ) from exc
            else:
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
            self._environment = str(params.environment or "paper").strip().lower()
            if self._environment not in {"paper", "live"}:
                raise ValueError("IBKR environment must be paper or live")
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
            # Keep connection snappy: try at most one nearby clientId.
            for offset in (1,):
                candidate_client_ids.append(int(params.client_id) + offset)

            last_connect_error: Exception | None = None
            connected = False
            selected_client_id = int(params.client_id)
            connect_deadline = time.monotonic() + 12.0
            for candidate_port in fallback_ports:
                if time.monotonic() >= connect_deadline:
                    break
                if not self._tcp_port_reachable(params.host, candidate_port, timeout_seconds=1.0):
                    continue
                for candidate_client_id in candidate_client_ids:
                    if time.monotonic() >= connect_deadline:
                        break
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
                            account=self._active_account_id,
                            timeout=3,
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
                        account=self._active_account_id,
                        timeout=3,
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
                # Default to live feed for MT5-like responsiveness.
                # Can be overridden by IBKR_MARKET_DATA_TYPE env var.
                self._ib.reqMarketDataType(self._market_data_type)
            except Exception:
                pass

            self._connected = True
            self._last_error = None
            refreshed_account = self.refresh_account()
            if refreshed_account is None:
                raise RuntimeError(self._last_error or "IBKR connected, but account details could not be loaded")
            detected_environment = "paper" if self.is_demo() else "live"
            if detected_environment != self._environment:
                raise RuntimeError(
                    f"Connected IBKR account is {detected_environment}, but {self._environment} was selected. "
                    "Log in to the matching TWS / IB Gateway session and use its configured API port."
                )
            trade_mode = refreshed_account.trade_mode
            self._account_info = AccountInfo(
                login=refreshed_account.login,
                name=refreshed_account.name,
                server=f"{self._host}:{self._port}",
                balance=refreshed_account.balance,
                equity=refreshed_account.equity,
                margin=refreshed_account.margin,
                free_margin=refreshed_account.free_margin,
                leverage=refreshed_account.leverage,
                currency=refreshed_account.currency,
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
            try:
                if self._ib is not None and self._ib.isConnected():
                    self._ib.disconnect()
            except Exception:
                pass
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
            self._position_targets.clear()
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

    def trade_server_connected(self) -> bool:
        return bool(
            self._connected
            and self._ib is not None
            and self._ib.isConnected()
        )

    def _normalize_symbol_for_ibkr(self, symbol: str) -> str:
        raw = (symbol or "").upper().strip()
        if not raw:
            return ""
        # Map CFD/index/commodity aliases to liquid IBKR-tradable proxies.
        aliases = {
            "US500": "SPY",
            "SPX500": "SPY",
            "US100": "QQQ",
            "NAS100": "QQQ",
            "US30": "DIA",
            "DJ30": "DIA",
            "GER40": "EWG",
            "DAX40": "EWG",
            "UK100": "EWU",
            "FTSE100": "EWU",
            "XAUUSD": "GLD",
            "GOLD": "GLD",
            "XAGUSD": "SLV",
            "SILVER": "SLV",
            "WTI": "USO",
            "USOIL": "USO",
            "XTIUSD": "USO",
            "BRENT": "BNO",
            "UKOIL": "BNO",
            "XBRUSD": "BNO",
        }
        return aliases.get(raw, raw)

    def _resolve_contract(self, symbol: str):
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return None
        key = self._normalize_symbol_for_ibkr(symbol)
        if not key:
            return None
        cached = self._contract_cache.get(key)
        if cached is not None:
            return cached

        try:
            from ib_insync import Contract, Crypto, Forex, Stock  # type: ignore
        except Exception:
            return None

        candidates = []
        forex_currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}
        is_forex = len(key) == 6 and key[:3] in forex_currencies and key[3:] in forex_currencies
        is_crypto = key in {"BTCUSD", "ETHUSD"}
        is_stock_like = bool(re.fullmatch(r"[A-Z]{1,6}", key)) and not is_forex
        try:
            if is_forex:
                candidates.append(Forex(key))
            elif is_crypto:
                candidates.append(Crypto(key[:3], "PAXOS", "USD"))
            else:
                # Fast path for common US stocks/ETFs.
                candidates.append(Stock(key, "SMART", "USD"))
        except Exception:
            pass

        # Try generic symbol matching only for non stock-like symbols
        # (for stock/ETF tickers this can add timeouts and invalid contracts).
        if not is_stock_like:
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
                    if not _is_valid_resolved_contract(resolved, expect_stock=is_stock_like):
                        continue
                    self._contract_cache[key] = resolved
                    return resolved
            except Exception:
                continue

        # Last fallback, generic contract object (only for non stock-like symbols).
        if not is_stock_like:
            try:
                generic = Contract(symbol=key)
                qualified = self._ib.qualifyContracts(generic)
                if qualified:
                    resolved = qualified[0]
                    if not _is_valid_resolved_contract(resolved, expect_stock=is_stock_like):
                        return None
                    self._contract_cache[key] = resolved
                    return resolved
            except Exception:
                pass

        return None

    def _finalize_snapshot_fields(self, bid: float, ask: float, last: float, close: float) -> tuple[float, float, float]:
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
        if bid <= 0 and ask > 0:
            bid = ask
        if ask <= 0 and bid > 0:
            ask = bid
        return bid, ask, (last or close or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0))

    def get_snapshots(
        self,
        symbols: list[str],
        max_wait_seconds: float = 0.22,
        allow_data_type_fallback: bool = True,
    ) -> dict[str, dict]:
        """
        Batch quote snapshots to reduce per-symbol API overhead and UI latency.
        Returns payload keyed by the requested symbol token.
        """
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return {}
        requested = [str(s or "").strip().upper() for s in (symbols or []) if str(s or "").strip()]
        if not requested:
            return {}

        now = time.time()
        out: dict[str, dict] = {}
        pending: list[tuple[str, object]] = []
        for req_sym in requested:
            cached = self._snapshot_cache.get(req_sym)
            if cached and (now - float(cached[0])) <= 0.9:
                out[req_sym] = dict(cached[1])
                continue
            contract = self._resolve_contract(req_sym)
            if contract is None:
                continue
            try:
                ticker = self._ib.reqMktData(contract, "", True, False)
                pending.append((req_sym, ticker))
            except Exception:
                continue

        if pending:
            wait_seconds = max(0.08, min(float(max_wait_seconds or 0.22), 0.4))
            wait_seconds = max(wait_seconds, min(0.35, 0.08 + (0.01 * len(pending))))
            self._ib.sleep(wait_seconds)

        for req_sym, ticker in pending:
            try:
                bid = float(getattr(ticker, "bid", 0.0) or 0.0)
                ask = float(getattr(ticker, "ask", 0.0) or 0.0)
                last = float(getattr(ticker, "last", 0.0) or 0.0)
                close = float(getattr(ticker, "close", 0.0) or 0.0)
                try:
                    market_price = float(ticker.marketPrice() or 0.0)
                except Exception:
                    market_price = 0.0
                if not math.isfinite(market_price):
                    market_price = 0.0
                if last <= 0 and market_price > 0:
                    last = market_price
                if close <= 0 and market_price > 0:
                    close = market_price
                bid, ask, last_px = self._finalize_snapshot_fields(bid, ask, last, close)
                if bid > 0 and ask > 0:
                    now_msc = int(time.time() * 1000)
                    payload = {
                        "symbol": req_sym,
                        "bid": bid,
                        "ask": ask,
                        "last": last_px,
                        "time": int(now_msc / 1000),
                        "time_msc": now_msc,
                        "market_data_type": int(getattr(ticker, "marketDataType", 0) or self._market_data_type),
                    }
                    out[req_sym] = payload
                    self._snapshot_cache[req_sym] = (now_msc / 1000.0, dict(payload))
                else:
                    cached = self._snapshot_cache.get(req_sym)
                    if cached and (now - float(cached[0])) <= 20.0:
                        out[req_sym] = dict(cached[1])
            except Exception:
                cached = self._snapshot_cache.get(req_sym)
                if cached and (time.time() - float(cached[0])) <= 20.0:
                    out[req_sym] = dict(cached[1])
        if not out and allow_data_type_fallback and self._market_data_type != 3:
            # If live/frozen feed returns nothing (missing entitlement or competing session),
            # degrade to delayed stream so dashboard prices still move.
            try:
                self._ib.reqMarketDataType(3)
                self._market_data_type = 3
                return self.get_snapshots(
                    requested,
                    max_wait_seconds=max_wait_seconds,
                    allow_data_type_fallback=False,
                )
            except Exception:
                pass
        return out

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """Best-effort quote snapshot for any supported contract type."""
        cache_key = str(symbol or "").strip().upper()
        if not cache_key:
            return None
        now = time.time()
        cached = self._snapshot_cache.get(cache_key)
        if cached and (now - float(cached[0])) <= 1.2:
            return dict(cached[1])
        try:
            snap = self.get_snapshots([cache_key], max_wait_seconds=0.2)
            if cache_key in snap:
                return dict(snap[cache_key])
            cached = self._snapshot_cache.get(cache_key)
            if cached and (time.time() - float(cached[0])) <= 20.0:
                return dict(cached[1])
            return None
        except Exception as exc:
            self._last_error = f"IBKR snapshot failed for {symbol}: {exc}"
            logger.debug(self._last_error)
            cached = self._snapshot_cache.get(cache_key)
            if cached and (time.time() - float(cached[0])) <= 20.0:
                return dict(cached[1])
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
                    timeout=10.0,
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
            self._ib.sleep(0.12)
            bid = float(getattr(ticker, "bid", 0.0) or 0.0)
            ask = float(getattr(ticker, "ask", 0.0) or 0.0)
            last = float(getattr(ticker, "last", 0.0) or 0.0)
            close = float(getattr(ticker, "close", 0.0) or 0.0)
            bid, ask, last_px = self._finalize_snapshot_fields(bid, ask, last, close)
            if bid <= 0 and ask <= 0:
                return None
            return {"bid": bid, "ask": ask, "last": last_px}
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
            order.outsideRth = True
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
            position_ticket = self._position_ticket_for_contract(
                contract,
                account=self._active_account_id,
                symbol=symbol,
            )

            success = status in {"Filled", "Submitted", "PreSubmitted"} and (filled_qty > 0 or status != "Filled")
            if success:
                self._position_targets[int(position_ticket)] = {
                    "stop_loss": float(stop_loss or 0.0),
                    "take_profit": float(take_profit or 0.0),
                    "comment": comment or "",
                }
                if float(stop_loss or 0.0) > 0 or float(take_profit or 0.0) > 0:
                    try:
                        self._cancel_position_exit_orders(position_ticket)
                        self._submit_position_exit_orders(
                            contract=contract,
                            ticket=position_ticket,
                            position_side=side,
                            quantity=qty,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                        )
                    except Exception as exc:
                        logger.warning("IBKR protective exit order submission failed for %s: %s", symbol, exc)
            return {
                "success": success,
                "retcode": 0 if success else -1,
                "retcode_desc": status or "Order rejected",
                "ticket": position_ticket if position_ticket > 0 else (ticket if ticket > 0 else None),
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
            # PortfolioItem is account-scoped and carries IBKR's own market mark
            # and unrealized P/L. Do not recompute P/L from delayed snapshots.
            rows = self._ib.portfolio(account=self._active_account_id) or []
        except Exception as exc:
            self._last_error = f"IBKR portfolio failed: {exc}"
            return []

        pending_closes: dict[int, dict[str, object]] = {}
        try:
            for trade in list(self._ib.openTrades() or []):
                order = getattr(trade, "order", None)
                ref = str(getattr(order, "orderRef", "") or "") if order is not None else ""
                match = re.fullmatch(r"Close (\d+)", ref)
                if not match:
                    continue
                status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
                if status in {"Cancelled", "Filled", "Inactive", "ApiCancelled"}:
                    continue
                pending_closes[int(match.group(1))] = {
                    "order_id": int(getattr(order, "orderId", 0) or 0),
                    "status": status or "PendingSubmit",
                }
        except Exception:
            pending_closes = {}

        result: list[dict] = []
        now_ts = int(time.time())
        symbol_filter = (symbol or "").upper().strip()
        for row in rows:
            account = str(getattr(row, "account", "") or "")
            if self._active_account_id and account != self._active_account_id:
                continue
            contract = getattr(row, "contract", None)
            if contract is None:
                continue
            pos_qty = _safe_float(getattr(row, "position", 0.0), 0.0)
            if abs(pos_qty) < 1e-9:
                continue
            sym = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
            if symbol_filter and sym != symbol_filter:
                continue
            avg_cost = _safe_float(getattr(row, "averageCost", 0.0), 0.0)
            current_price = _safe_float(getattr(row, "marketPrice", 0.0), avg_cost)
            profit = _safe_float(getattr(row, "unrealizedPNL", 0.0), 0.0)
            normalized_ticket = self._position_ticket_for_contract(contract, account=account, symbol=sym)
            target_meta = self._position_targets.get(int(normalized_ticket), {})
            pending_close = pending_closes.get(int(normalized_ticket))
            result.append(
                {
                    "ticket": normalized_ticket,
                    "symbol": sym,
                    "type": "BUY" if pos_qty > 0 else "SELL",
                    "volume": abs(pos_qty),
                    "price_open": avg_cost,
                    "price_current": current_price or avg_cost,
                    "stop_loss": _safe_float(target_meta.get("stop_loss", 0.0), 0.0),
                    "take_profit": _safe_float(target_meta.get("take_profit", 0.0), 0.0),
                    "profit": profit,
                    "time": now_ts,
                    "comment": str(target_meta.get("comment") or f"IBKR {account}"),
                    "close_pending": bool(pending_close),
                    "close_order_id": int((pending_close or {}).get("order_id") or 0) or None,
                    "close_order_status": str((pending_close or {}).get("status") or "") or None,
                }
            )

        return result

    def cancel_position_close(self, ticket: int) -> dict:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return {"success": False, "retcode": -1, "retcode_desc": "IBKR not connected"}
        close_ref = f"Close {int(ticket)}"[:32]
        cancelled = 0
        matched = 0
        for trade in list(self._ib.openTrades() or []):
            order = getattr(trade, "order", None)
            if order is None or str(getattr(order, "orderRef", "") or "") != close_ref:
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status in {"Cancelled", "Filled", "Inactive", "ApiCancelled"}:
                continue
            matched += 1
            self._ib.cancelOrder(order)
            cancelled += 1
        if cancelled:
            self._ib.sleep(0.5)
            return {
                "success": True, "retcode": 0,
                "retcode_desc": f"Cancellation sent to IBKR for {cancelled} pending close order(s)",
                "ticket": int(ticket), "volume": None, "price": None,
                "comment": "Cancel pending close",
            }
        return {
            "success": False, "retcode": -1,
            "retcode_desc": "No cancellable LinkTrade close order found at IBKR",
            "ticket": int(ticket), "volume": None, "price": None,
            "comment": "Cancel pending close",
        }

    def close_position(self, ticket: int) -> dict:
        if not self._connected or self._ib is None or not self._ib.isConnected():
            return {"success": False, "retcode": -1, "retcode_desc": "IBKR not connected"}
        try:
            rows = self._ib.positions() or []
        except Exception as exc:
            self._last_error = f"IBKR positions failed: {exc}"
            return {"success": False, "retcode": -1, "retcode_desc": str(exc)}

        matched_contract = None
        matched_qty = 0.0
        matched_symbol = ""
        strict_filtering = bool(self._active_account_id)
        for row in rows:
            account = str(getattr(row, "account", "") or "")
            if strict_filtering and account and account != self._active_account_id:
                continue
            contract = getattr(row, "contract", None)
            if contract is None:
                continue
            con_id = int(getattr(contract, "conId", 0) or 0)
            if con_id != int(ticket):
                continue
            qty = _safe_float(getattr(row, "position", 0.0), 0.0)
            if abs(qty) < 1e-9:
                continue
            matched_contract = contract
            matched_qty = qty
            matched_symbol = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").upper()
            break

        if matched_contract is None:
            return {
                "success": False,
                "retcode": -1,
                "retcode_desc": f"Live position {ticket} not found. Refusing to open opposite order.",
            }

        side = "SELL" if matched_qty > 0 else "BUY"
        qty = abs(matched_qty)

        # A regular-hours market close can remain PreSubmitted until the US
        # session opens. Repeated clicks must not stack additional opposite
        # orders while that first close is still working at IBKR.
        close_ref = f"Close {ticket}"[:32]
        try:
            open_trades = list(self._ib.openTrades() or [])
        except Exception:
            open_trades = []
        for existing_trade in open_trades:
            existing_order = getattr(existing_trade, "order", None)
            if existing_order is None:
                continue
            if str(getattr(existing_order, "orderRef", "") or "") != close_ref:
                continue
            existing_status = str(
                getattr(getattr(existing_trade, "orderStatus", None), "status", "") or ""
            )
            if existing_status in {"Cancelled", "Filled", "Inactive", "ApiCancelled"}:
                continue
            existing_ticket = int(
                getattr(existing_order, "permId", 0) or getattr(existing_order, "orderId", 0) or 0
            )
            return {
                "success": True,
                "retcode": 0,
                "retcode_desc": f"{existing_status or 'PendingSubmit'} — close order already pending at IBKR",
                "ticket": existing_ticket if existing_ticket > 0 else None,
                "volume": qty,
                "price": None,
                "comment": f"Close {matched_symbol} pending",
            }
        try:
            from ib_insync import MarketOrder  # type: ignore
        except Exception:
            return {"success": False, "retcode": -1, "retcode_desc": "ib_insync not available"}

        try:
            self._cancel_position_exit_orders(int(ticket))
            order = MarketOrder(side, qty)
            # Be explicit so TWS presets do not modify and cancel API orders
            # (error 10349). Stock exits should use SMART instead of the
            # position's reporting exchange, which may trigger direct-route
            # precautionary rejection 10311.
            order.tif = "DAY"
            order.outsideRth = False
            if self._active_account_id:
                order.account = self._active_account_id
            order.orderRef = close_ref
            routed_contract = copy.deepcopy(matched_contract)
            if str(getattr(routed_contract, "secType", "") or "").upper() == "STK":
                routed_contract.exchange = "SMART"
            trade = self._ib.placeOrder(routed_contract, order)
            deadline = time.time() + 8.0
            while not trade.isDone() and time.time() < deadline:
                self._ib.sleep(0.2)
            status = str(getattr(trade.orderStatus, "status", "") or "")
            filled_qty = _safe_float(getattr(trade.orderStatus, "filled", 0.0), 0.0)
            avg_fill = _safe_float(getattr(trade.orderStatus, "avgFillPrice", 0.0), 0.0)
            if avg_fill <= 0 and trade.fills:
                try:
                    avg_fill = _safe_float(trade.fills[-1].execution.avgPrice, 0.0)
                except Exception:
                    avg_fill = 0.0
            success = status in {"Filled", "Submitted", "PreSubmitted"} and (filled_qty > 0 or status != "Filled")
            ticket_out = int(getattr(trade.order, "permId", 0) or getattr(trade.order, "orderId", 0) or 0)
            if success:
                self._position_targets.pop(int(ticket), None)
            return {
                "success": success,
                "retcode": 0 if success else -1,
                "retcode_desc": (
                    "Filled"
                    if status == "Filled"
                    else f"{status} — accepted by IBKR and waiting to fill"
                    if success
                    else status or "Close order rejected"
                ),
                "ticket": ticket_out if ticket_out > 0 else None,
                "volume": filled_qty if filled_qty > 0 else qty,
                "price": avg_fill if avg_fill > 0 else None,
                "comment": f"Close {matched_symbol}",
            }
        except Exception as exc:
            self._last_error = f"IBKR close_position failed: {exc}"
            logger.exception(self._last_error)
            return {"success": False, "retcode": -1, "retcode_desc": str(exc)}

    def modify_position(self, ticket: int, stop_loss: float, take_profit: float) -> dict:
        positions = self.get_positions()
        matched = next((row for row in positions if int(row.get("ticket") or 0) == int(ticket)), None)
        if matched is None:
            return {
                "success": False,
                "retcode": -1,
                "retcode_desc": f"Position {ticket} not found for modification",
            }
        self._position_targets[int(ticket)] = {
            "stop_loss": float(stop_loss or 0.0),
            "take_profit": float(take_profit or 0.0),
            "comment": str(matched.get("comment") or "IBKR"),
        }
        contract = self._resolve_contract(str(matched.get("symbol") or ""))
        if contract is not None:
            try:
                self._cancel_position_exit_orders(int(ticket))
                self._submit_position_exit_orders(
                    contract=contract,
                    ticket=int(ticket),
                    position_side=str(matched.get("type") or "BUY"),
                    quantity=float(matched.get("volume") or 0.0),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
            except Exception as exc:
                self._last_error = f"IBKR modify_position protective update failed: {exc}"
                logger.warning(self._last_error)
        return {
            "success": True,
            "retcode": 0,
            "retcode_desc": "IBKR risk targets updated",
            "ticket": int(ticket),
            "volume": float(matched.get("volume") or 0.0),
            "price": float(matched.get("price_open") or 0.0) or None,
            "stop_loss": float(stop_loss or 0.0) or None,
            "take_profit": float(take_profit or 0.0) or None,
            "comment": str(matched.get("comment") or "IBKR"),
        }

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
