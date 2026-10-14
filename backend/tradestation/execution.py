from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from zlib import adler32

from mt5.execution import OrderRequest, OrderResult, PositionInfo


class TradeStationExecutionEngine:
    def __init__(self, connector, market_data):
        self.connector = connector
        self.market_data = market_data
        self._shadow_positions: dict[int, dict] = {}
        self._closed_trades: list[dict] = []
        self._realized_profit_by_ticket: dict[int, float] = {}

    def get_recent_history_order_rows(self, limit: int = 100, lookback_days: int = 120) -> list[dict]:
        rows = self._broker_history_rows(limit=limit, lookback_days=lookback_days)
        if not rows:
            rows = list(self._closed_trades)
        rows.sort(key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0), reverse=True)
        return rows[: max(1, int(limit))]

    def get_recent_closed_deal_rows(self, limit: int = 100, lookback_days: int = 120) -> list[dict]:
        return self.get_recent_history_order_rows(limit=limit, lookback_days=lookback_days)

    def get_recent_closed_trades(self, limit: int = 100, lookback_days: int = 120) -> list[dict]:
        return self.get_recent_history_order_rows(limit=limit, lookback_days=lookback_days)

    def get_recent_commission_by_symbol(self) -> dict:
        rows = self._broker_history_rows(limit=400, lookback_days=45)
        agg: dict[str, dict[str, float]] = {}
        for row in rows:
            commission = abs(float(self._to_float(row.get("commission_usd"), default=0.0) or 0.0))
            volume = float(self._to_float(row.get("volume"), default=0.0) or 0.0)
            symbol = str(row.get("symbol") or "").upper()
            if commission <= 0 or volume <= 0 or not symbol:
                continue
            item = agg.setdefault(symbol, {"sum_abs_commission": 0.0, "sum_volume": 0.0, "deals": 0.0})
            item["sum_abs_commission"] += commission
            item["sum_volume"] += volume
            item["deals"] += 1.0

        out: dict[str, dict] = {}
        for symbol, item in agg.items():
            if float(item.get("sum_volume") or 0.0) <= 0:
                continue
            per_lot_side = float(item["sum_abs_commission"]) / float(item["sum_volume"])
            out[symbol] = {
                "commission_per_lot_side": round(per_lot_side, 6),
                "commission_round_turn_per_lot": round(per_lot_side * 2.0, 6),
                "commission_model": "tradestation_broker_history",
                "commission_percent_rate": None,
                "commission_notional_1lot": None,
                "commission_samples": int(item.get("deals", 0.0) or 0.0),
            }
        return out

    def estimate_margin(self, symbol: str, action: str, volume: float, price: Optional[float] = None) -> Optional[float]:
        del action
        entry = float(price or 0.0)
        if entry <= 0:
            tick = self.market_data.get_tick(symbol)
            if tick is not None:
                entry = tick.ask or tick.bid or 0.0
        quantity = max(1.0, float(volume or 0.0))
        if entry <= 0 or quantity <= 0:
            return None
        leverage = max(int(getattr(self.connector.account_info, "leverage", 1) or 1), 1)
        return (entry * quantity) / leverage

    def get_tradeability(self, symbol: str) -> dict:
        tick = self.market_data.get_tick(symbol)
        info = self.market_data.get_symbol_info(symbol) or {}
        return {
            "trade_enabled": bool(info and tick and tick.bid > 0 and tick.ask > 0),
            "visible": bool(info.get("visible", True)),
            "has_tick": bool(tick and tick.bid > 0 and tick.ask > 0),
            "reason": "" if tick else "No live TradeStation quote available.",
        }

    def get_realized_profit_map(self, ticket_list: list[int], lookback_days: int = 90) -> dict[int, float]:
        del lookback_days
        return {int(ticket): float(self._realized_profit_by_ticket.get(int(ticket), 0.0)) for ticket in ticket_list}

    def get_position_realized_profit(self, ticket: int) -> Optional[float]:
        return self._realized_profit_by_ticket.get(int(ticket))

    def place_order(self, req: OrderRequest) -> OrderResult:
        if not self.connector.connected:
            return OrderResult(success=False, retcode=-1, retcode_desc="TradeStation not connected", comment=req.comment)
        info = self.market_data.get_symbol_info(req.symbol) or {}
        broker_symbol = str(info.get("broker_symbol") or req.symbol).upper()
        quantity = max(1, int(round(float(req.volume or 0.0))))
        body = {
            "AccountID": self.connector.active_account_id,
            "Symbol": broker_symbol,
            "Quantity": str(quantity),
            "OrderType": "Market",
            "TradeAction": str(req.action or "").upper(),
            "TimeInForce": {"Duration": "DAY"},
            "Route": "Intelligent",
        }
        response = self.connector.place_order(body)
        success = bool(response.get("success"))
        ticket = int(response.get("ticket") or 0) if response.get("ticket") else None
        fill_price = float(response.get("price") or 0.0) or self._reference_price(req.symbol, req.action)
        if success and ticket:
            self._shadow_positions[int(ticket)] = {
                "ticket": int(ticket),
                "symbol": str(req.symbol or "").upper(),
                "type": str(req.action or "").upper(),
                "volume": float(quantity),
                "price_open": fill_price,
                "stop_loss": float(req.stop_loss or 0.0),
                "take_profit": float(req.take_profit or 0.0),
                "time": int(time.time()),
                "comment": req.comment or "TradeStation",
            }
            if float(req.stop_loss or 0.0) > 0 or float(req.take_profit or 0.0) > 0:
                try:
                    self._sync_broker_exit_orders_for_symbol(
                        symbol=str(req.symbol or "").upper(),
                        position_side=str(req.action or "").upper(),
                        stop_loss=float(req.stop_loss or 0.0),
                        take_profit=float(req.take_profit or 0.0),
                    )
                except Exception:
                    pass
        return OrderResult(
            success=success,
            retcode=int(response.get("retcode", 0 if success else -1)),
            retcode_desc=str(response.get("retcode_desc") or ("Order placed" if success else "Order rejected")),
            ticket=ticket,
            volume=float(quantity),
            price=fill_price if fill_price > 0 else None,
            stop_loss=float(req.stop_loss or 0.0) or None,
            take_profit=float(req.take_profit or 0.0) or None,
            comment=req.comment,
        )

    def close_position(self, ticket: int) -> OrderResult:
        open_position = next((p for p in self.get_positions() if int(p.ticket) == int(ticket)), None)
        if open_position is None:
            return OrderResult(success=False, retcode=-1, retcode_desc=f"Position {ticket} not found", comment="")
        try:
            self._cancel_broker_exit_orders(open_position)
        except Exception:
            pass
        close_side = "SELL" if open_position.type == "BUY" else "BUY"
        result = self.place_order(
            OrderRequest(
                symbol=open_position.symbol,
                action=close_side,
                volume=open_position.volume,
                stop_loss=0.0,
                take_profit=0.0,
                comment=f"Close {ticket}",
            )
        )
        if result.success:
            exit_price = float(result.price or self._reference_price(open_position.symbol, close_side) or 0.0)
            profit = self._closed_profit(open_position, exit_price)
            self._realized_profit_by_ticket[int(ticket)] = profit
            self._closed_trades.append(
                {
                    "ticket": int(ticket),
                    "symbol": open_position.symbol,
                    "action": open_position.type,
                    "status": "closed",
                    "opened_at": float(open_position.time),
                    "closed_at": float(time.time()),
                    "duration_minutes": max(0.0, (time.time() - float(open_position.time or 0.0)) / 60.0),
                    "volume": float(open_position.volume),
                    "entry_price": float(open_position.price_open),
                    "stop_loss": float(open_position.stop_loss) if float(open_position.stop_loss or 0.0) > 0 else None,
                    "take_profit": float(open_position.take_profit) if float(open_position.take_profit or 0.0) > 0 else None,
                    "profit_usd": profit,
                    "profit_pct": ((profit / max(float(open_position.price_open or 1.0) * float(open_position.volume or 1.0), 1.0)) * 100.0),
                    "started_with_usd": float(open_position.price_open or 0.0) * float(open_position.volume or 0.0),
                    "ended_with_usd": (float(open_position.price_open or 0.0) * float(open_position.volume or 0.0)) + profit,
                    "entry_market_value_usd": float(open_position.price_open or 0.0) * float(open_position.volume or 0.0),
                    "sl_amount_usd": None,
                    "tp_amount_usd": None,
                    "sl_pct_of_start": None,
                    "tp_pct_of_start": None,
                    "started_with_source": "estimated",
                    "signal_id": None,
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": "tradestation_local_close",
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": "manual_close",
                    "close_price": exit_price,
                }
            )
            self._shadow_positions.pop(int(ticket), None)
            if result.ticket is not None:
                self._shadow_positions.pop(int(result.ticket), None)
        return result

    def modify_position(self, ticket: int, stop_loss: float, take_profit: float) -> OrderResult:
        position = self._shadow_positions.get(int(ticket))
        if position is None:
            live_position = next((row for row in self.get_positions() if int(row.ticket) == int(ticket)), None)
            if live_position is None:
                return OrderResult(success=False, retcode=-1, retcode_desc=f"Position {ticket} not found", comment="")
            position = {
                "ticket": int(live_position.ticket),
                "symbol": str(live_position.symbol or "").upper(),
                "type": str(live_position.type or "BUY").upper(),
                "volume": float(live_position.volume or 0.0),
                "price_open": float(live_position.price_open or 0.0),
                "stop_loss": float(live_position.stop_loss or 0.0),
                "take_profit": float(live_position.take_profit or 0.0),
                "time": int(live_position.time or 0),
                "comment": str(live_position.comment or "TradeStation"),
            }
            self._shadow_positions[int(ticket)] = position
        position["stop_loss"] = float(stop_loss or 0.0)
        position["take_profit"] = float(take_profit or 0.0)
        retcode_desc = "Local TradeStation risk targets updated"
        try:
            live_position = next((row for row in self.get_positions() if int(row.ticket) == int(ticket)), None)
            if live_position is not None:
                self._apply_broker_exit_orders(live_position, stop_loss=float(stop_loss or 0.0), take_profit=float(take_profit or 0.0))
                retcode_desc = "TradeStation broker risk targets updated"
        except Exception:
            retcode_desc = "TradeStation local risk targets updated"
        return OrderResult(
            success=True,
            retcode=0,
            retcode_desc=retcode_desc,
            ticket=int(ticket),
            volume=float(position.get("volume", 0.0) or 0.0),
            price=float(position.get("price_open", 0.0) or 0.0) or None,
            stop_loss=float(stop_loss or 0.0) or None,
            take_profit=float(take_profit or 0.0) or None,
            comment=str(position.get("comment") or ""),
        )

    def get_positions(self, symbol: Optional[str] = None) -> list[PositionInfo]:
        requested = str(symbol or "").strip().upper()
        rows: list[PositionInfo] = []
        seen_keys: set[tuple[str, str, int]] = set()

        for broker_row in self._broker_positions():
            composite = self._position_identity(
                symbol=broker_row.symbol,
                side=broker_row.type,
                volume=broker_row.volume,
            )
            shadow = self._matching_shadow_position(
                symbol=broker_row.symbol,
                side=broker_row.type,
                volume=broker_row.volume,
            )
            if shadow is not None:
                broker_row.stop_loss = float(shadow.get("stop_loss") or broker_row.stop_loss or 0.0)
                broker_row.take_profit = float(shadow.get("take_profit") or broker_row.take_profit or 0.0)
                broker_row.comment = str(shadow.get("comment") or broker_row.comment or "TradeStation")
            if requested and str(broker_row.symbol or "").upper() != requested:
                continue
            seen_keys.add(composite)
            rows.append(broker_row)

        for shadow in self._shadow_positions.values():
            if requested and str(shadow.get("symbol") or "").upper() != requested:
                continue
            composite = self._position_identity(
                symbol=str(shadow.get("symbol") or ""),
                side=str(shadow.get("type") or "BUY"),
                volume=float(shadow.get("volume") or 0.0),
            )
            if composite in seen_keys:
                continue
            tick = self.market_data.get_tick(str(shadow.get("symbol") or ""))
            current_price = float(shadow.get("price_open") or 0.0)
            if tick is not None:
                current_price = tick.bid if str(shadow.get("type") or "").upper() == "BUY" else tick.ask
            rows.append(
                PositionInfo(
                    ticket=int(shadow.get("ticket") or 0),
                    symbol=str(shadow.get("symbol") or "").upper(),
                    type=str(shadow.get("type") or "BUY").upper(),
                    volume=float(shadow.get("volume") or 0.0),
                    price_open=float(shadow.get("price_open") or 0.0),
                    price_current=current_price,
                    stop_loss=float(shadow.get("stop_loss") or 0.0),
                    take_profit=float(shadow.get("take_profit") or 0.0),
                    profit=self._open_profit(shadow, current_price),
                    time=int(shadow.get("time") or 0),
                    comment=str(shadow.get("comment") or "TradeStation"),
                )
            )
        return rows

    def _sync_broker_exit_orders_for_symbol(self, *, symbol: str, position_side: str, stop_loss: float, take_profit: float) -> None:
        live_position = next(
            (
                row for row in self.get_positions(symbol)
                if str(row.symbol or "").upper() == str(symbol or "").upper()
                and str(row.type or "").upper() == str(position_side or "").upper()
            ),
            None,
        )
        if live_position is None:
            return
        self._apply_broker_exit_orders(live_position, stop_loss=stop_loss, take_profit=take_profit)

    def _apply_broker_exit_orders(self, position: PositionInfo, *, stop_loss: float, take_profit: float) -> None:
        self._cancel_broker_exit_orders(position)
        if float(stop_loss or 0.0) <= 0 and float(take_profit or 0.0) <= 0:
            return

        info = self.market_data.get_symbol_info(position.symbol) or {}
        broker_symbol = str(info.get("broker_symbol") or position.symbol).upper()
        trade_action = self._closing_trade_action(position.type)
        quantity = max(1, int(round(float(position.volume or 0.0))))
        asset_type = self._asset_type_for_symbol(position.symbol)
        account_key = str(self.connector.active_account_id or "").strip()
        if not account_key:
            return

        orders: list[dict] = []
        if float(take_profit or 0.0) > 0:
            orders.append(
                {
                    "AccountKey": account_key,
                    "AssetType": asset_type,
                    "Symbol": broker_symbol,
                    "Quantity": str(quantity),
                    "OrderType": "Limit",
                    "Duration": "GTC",
                    "TradeAction": trade_action,
                    "Route": "Intelligent",
                    "LimitPrice": str(float(take_profit)),
                }
            )
        if float(stop_loss or 0.0) > 0:
            orders.append(
                {
                    "AccountKey": account_key,
                    "AssetType": asset_type,
                    "Symbol": broker_symbol,
                    "Quantity": str(quantity),
                    "OrderType": "StopMarket",
                    "Duration": "GTC",
                    "TradeAction": trade_action,
                    "Route": "Intelligent",
                    "StopPrice": str(float(stop_loss)),
                }
            )
        if not orders:
            return
        self.connector.submit_group_order(
            {
                "Type": "BRK",
                "Orders": orders,
            }
        )

    def _cancel_broker_exit_orders(self, position: PositionInfo) -> None:
        for order in self._open_broker_exit_orders(position):
            order_id = order.get("OrderID") or order.get("orderID") or order.get("OrderId") or order.get("orderId")
            if order_id in (None, ""):
                continue
            try:
                self.connector.cancel_order(order_id)
            except Exception:
                continue

    def _open_broker_exit_orders(self, position: PositionInfo) -> list[dict]:
        info = self.market_data.get_symbol_info(position.symbol) or {}
        broker_symbol = str(info.get("broker_symbol") or position.symbol).upper()
        closing_action = self._closing_order_type_label(position.type)
        open_statuses = {"OPN", "ACK", "UCN", "FPR", "DON"}
        rows: list[dict] = []
        try:
            raw_orders = self.connector.get_orders(limit=200)
        except Exception:
            raw_orders = []
        for order in raw_orders:
            symbol = str(order.get("Symbol") or "").upper()
            if symbol != broker_symbol:
                continue
            status = str(order.get("Status") or "").upper()
            if status not in open_statuses:
                continue
            order_type = str(order.get("Type") or "").strip()
            if order_type and order_type != closing_action:
                continue
            rows.append(order)
        return rows

    def _broker_history_rows(self, *, limit: int, lookback_days: int) -> list[dict]:
        del lookback_days
        if not self.connector.connected:
            return []
        try:
            raw_orders = self.connector.get_orders(limit=max(50, min(int(limit) * 4, 500)))
        except Exception:
            return []

        rows: list[dict] = []
        for item in raw_orders:
            parsed = self._trade_row_from_broker_order(item)
            if parsed is not None:
                rows.append(parsed)
        rows.sort(key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0), reverse=True)
        return rows[: max(1, int(limit))]

    def _trade_row_from_broker_order(self, item: dict) -> dict | None:
        status_text = str(
            item.get("StatusDescription")
            or item.get("statusDescription")
            or item.get("Status")
            or item.get("status")
            or ""
        ).strip()
        normalized_status = status_text.lower()
        if normalized_status and not any(token in normalized_status for token in ("filled", "executed", "closed")):
            return None

        legs = item.get("Legs") if isinstance(item.get("Legs"), list) else item.get("legs")
        first_leg = legs[0] if isinstance(legs, list) and legs and isinstance(legs[0], dict) else {}
        broker_symbol = str(
            item.get("Symbol")
            or item.get("symbol")
            or first_leg.get("Symbol")
            or first_leg.get("symbol")
            or ""
        ).strip().upper()
        if not broker_symbol:
            return None
        symbol = self._canonical_symbol(broker_symbol)
        action = self._normalize_side(
            item.get("TradeAction")
            or item.get("tradeAction")
            or item.get("BuyOrSell")
            or item.get("buyOrSell")
            or item.get("Side")
            or item.get("side")
        )
        quantity = abs(float(self._to_float(
            item.get("FilledQuantity")
            or item.get("filledQuantity")
            or item.get("Quantity")
            or item.get("quantity")
            or item.get("Qty")
            or item.get("qty")
            or 0.0
        ) or 0.0))
        if quantity <= 0:
            return None

        closed_at = self._to_timestamp(
            item.get("ClosedDateTime")
            or item.get("closedDateTime")
            or item.get("FilledDateTime")
            or item.get("filledDateTime")
            or item.get("LastUpdateTime")
            or item.get("lastUpdateTime")
            or item.get("OpenedDateTime")
            or item.get("openedDateTime")
        )
        opened_at = self._to_timestamp(
            item.get("OpenedDateTime")
            or item.get("openedDateTime")
            or item.get("EnteredTime")
            or item.get("enteredTime")
            or item.get("CreatedDateTime")
            or item.get("createdDateTime")
            or closed_at
        )
        entry_price = float(self._to_float(
            item.get("OpenPrice")
            or item.get("openPrice")
            or item.get("AverageOpenPrice")
            or item.get("averageOpenPrice")
            or item.get("PriceUsedForBuyingPower")
            or item.get("priceUsedForBuyingPower")
            or item.get("LimitPrice")
            or item.get("limitPrice")
            or 0.0
        ) or 0.0)
        close_price = float(self._to_float(
            item.get("FilledPrice")
            or item.get("filledPrice")
            or item.get("AverageFilledPrice")
            or item.get("averageFilledPrice")
            or item.get("ExecutionPrice")
            or item.get("executionPrice")
            or entry_price
        ) or 0.0)
        commission = float(self._to_float(
            item.get("Commission")
            or item.get("commission")
            or item.get("CommissionFee")
            or item.get("commissionFee")
            or 0.0
        ) or 0.0)
        realized = self._to_float(
            item.get("ProfitLoss")
            or item.get("profitLoss")
            or item.get("RealizedProfitLoss")
            or item.get("realizedProfitLoss")
            or item.get("NetAmount")
            or item.get("netAmount"),
            default=None,
        )
        if realized is None:
            if entry_price <= 0 or close_price <= 0:
                return None
            gross = (close_price - entry_price) * quantity if action == "BUY" else (entry_price - close_price) * quantity
            realized = gross - abs(commission)

        started_with = (entry_price * quantity) if entry_price > 0 else None
        profit_pct = ((float(realized) / started_with) * 100.0) if started_with and started_with > 0 else None
        ticket = self._to_int(
            item.get("OrderID")
            or item.get("orderID")
            or item.get("OrderId")
            or item.get("orderId")
            or item.get("ID")
            or item.get("id"),
            default=0,
        )
        if ticket <= 0:
            ticket = self._synthetic_ticket(symbol, action, quantity)

        return {
            "ticket": int(ticket),
            "symbol": symbol,
            "action": action,
            "status": "closed",
            "opened_at": float(opened_at),
            "closed_at": float(closed_at),
            "duration_minutes": max(0.0, (float(closed_at) - float(opened_at)) / 60.0),
            "volume": float(quantity),
            "entry_price": entry_price if entry_price > 0 else None,
            "stop_loss": None,
            "take_profit": None,
            "profit_usd": float(realized or 0.0),
            "commission_usd": abs(float(commission or 0.0)),
            "profit_pct": profit_pct,
            "started_with_usd": started_with,
            "ended_with_usd": (started_with + float(realized)) if started_with is not None else None,
            "entry_market_value_usd": started_with,
            "sl_amount_usd": None,
            "tp_amount_usd": None,
            "sl_pct_of_start": None,
            "tp_pct_of_start": None,
            "started_with_source": "estimated",
            "signal_id": None,
            "signal_confidence": None,
            "signal_reason": None,
            "risk_approved": None,
            "risk_reason": None,
            "decision_reason": "tradestation_broker_history",
            "trade_reasons": [],
            "gemini_response": None,
            "meta_model": None,
            "post_analysis": None,
            "exit_reason": "broker_closed",
            "close_price": close_price if close_price > 0 else None,
            "status_text": status_text,
        }

    def _broker_positions(self) -> list[PositionInfo]:
        if not self.connector.connected:
            return []
        try:
            raw_positions = self.connector.get_positions()
        except Exception:
            return []

        rows: list[PositionInfo] = []
        for item in raw_positions:
            parsed = self._position_from_broker_row(item)
            if parsed is not None:
                rows.append(parsed)
        return rows

    def _position_from_broker_row(self, item: dict) -> PositionInfo | None:
        broker_symbol = str(
            item.get("Symbol")
            or item.get("symbol")
            or item.get("AssetSymbol")
            or item.get("assetSymbol")
            or ""
        ).strip().upper()
        if not broker_symbol:
            return None

        symbol = self._canonical_symbol(broker_symbol)
        side = self._normalize_side(
            item.get("LongShort")
            or item.get("longShort")
            or item.get("Side")
            or item.get("side")
            or item.get("MarketPosition")
            or item.get("marketPosition")
        )
        quantity = abs(float(self._to_float(
            item.get("Quantity")
            or item.get("quantity")
            or item.get("NetQty")
            or item.get("netQty")
            or item.get("OpenQuantity")
            or item.get("openQuantity")
            or item.get("Qty")
            or item.get("qty")
            or 0.0
        )))
        if quantity <= 0:
            return None

        price_open = float(self._to_float(
            item.get("AveragePrice")
            or item.get("averagePrice")
            or item.get("AvgPrice")
            or item.get("avgPrice")
            or item.get("CostBasis")
            or item.get("costBasis")
            or 0.0
        ))
        tick = self.market_data.get_tick(symbol)
        price_current = float(price_open or 0.0)
        if tick is not None:
            price_current = float(tick.bid if side == "BUY" else tick.ask)

        profit = self._to_float(
            item.get("UnrealizedProfitLoss")
            or item.get("unrealizedProfitLoss")
            or item.get("OpenProfitLoss")
            or item.get("openProfitLoss")
            or item.get("ProfitLoss")
            or item.get("profitLoss"),
            default=None,
        )
        if profit is None:
            profit = self._open_profit(
                {
                    "price_open": price_open,
                    "volume": quantity,
                    "type": side,
                },
                price_current,
            )

        ticket = self._to_int(
            item.get("PositionID")
            or item.get("positionID")
            or item.get("PositionId")
            or item.get("positionId")
            or item.get("ID")
            or item.get("id"),
            default=0,
        )
        if ticket <= 0:
            ticket = self._synthetic_ticket(symbol, side, quantity)

        opened_at = self._to_timestamp(
            item.get("OpenDateTime")
            or item.get("openDateTime")
            or item.get("Timestamp")
            or item.get("timestamp")
            or item.get("TradeDate")
            or item.get("tradeDate")
        )

        return PositionInfo(
            ticket=int(ticket),
            symbol=symbol,
            type=side,
            volume=quantity,
            price_open=price_open,
            price_current=price_current,
            stop_loss=0.0,
            take_profit=0.0,
            profit=float(profit or 0.0),
            time=opened_at,
            comment="TradeStation broker position",
        )

    def _matching_shadow_position(self, *, symbol: str, side: str, volume: float) -> dict | None:
        target_key = self._position_identity(symbol=symbol, side=side, volume=volume)
        for shadow in self._shadow_positions.values():
            shadow_key = self._position_identity(
                symbol=str(shadow.get("symbol") or ""),
                side=str(shadow.get("type") or "BUY"),
                volume=float(shadow.get("volume") or 0.0),
            )
            if shadow_key == target_key:
                return shadow
        return None

    def _position_identity(self, *, symbol: str, side: str, volume: float) -> tuple[str, str, int]:
        return (
            str(symbol or "").strip().upper(),
            self._normalize_side(side),
            int(round(float(volume or 0.0) * 10000.0)),
        )

    def _canonical_symbol(self, symbol: str) -> str:
        helper = getattr(self.market_data, "_canonical_symbol", None)
        if callable(helper):
            return str(helper(symbol or "")).strip().upper()
        return str(symbol or "").strip().upper()

    def _asset_type_for_symbol(self, symbol: str) -> str:
        canonical = self._canonical_symbol(symbol)
        if canonical in {"US500", "US100", "US30", "GER40", "UK100", "WTI", "BRENT"}:
            return "EQ"
        return "EQ"

    def _closing_trade_action(self, position_side: str) -> str:
        side = self._normalize_side(position_side)
        return "SELL" if side == "BUY" else "BUYTOCOVER"

    def _closing_order_type_label(self, position_side: str) -> str:
        return "Sell" if self._normalize_side(position_side) == "BUY" else "Buy to Cover"

    def _normalize_side(self, value) -> str:
        text = str(value or "").strip().upper()
        if text in {"SELL", "SHORT", "SHORTSELL"}:
            return "SELL"
        return "BUY"

    def _synthetic_ticket(self, symbol: str, side: str, volume: float) -> int:
        payload = f"{self.connector.active_account_id}|{symbol}|{side}|{round(float(volume or 0.0), 4)}"
        return max(1, int(adler32(payload.encode("utf-8"))))

    def _to_timestamp(self, value) -> int:
        if value is None:
            return int(time.time())
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return int(ts)
        text = str(value).strip()
        if not text:
            return int(time.time())
        try:
            return int(float(text))
        except Exception:
            pass
        try:
            normalized = text.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp())
        except Exception:
            return int(time.time())

    def _to_float(self, value, default: float | None = 0.0) -> float | None:
        try:
            return float(value)
        except Exception:
            return default

    def _to_int(self, value, default: int = 0) -> int:
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def _reference_price(self, symbol: str, action: str) -> float:
        tick = self.market_data.get_tick(symbol)
        if tick is None:
            return 0.0
        return float(tick.ask if str(action or "").upper() == "BUY" else tick.bid)

    def _open_profit(self, shadow: dict, current_price: float) -> float:
        entry = float(shadow.get("price_open") or 0.0)
        qty = float(shadow.get("volume") or 0.0)
        if entry <= 0 or qty <= 0 or current_price <= 0:
            return 0.0
        return (current_price - entry) * qty if str(shadow.get("type") or "").upper() == "BUY" else (entry - current_price) * qty

    def _closed_profit(self, position: PositionInfo, close_price: float) -> float:
        if position.type == "BUY":
            return (close_price - float(position.price_open or 0.0)) * float(position.volume or 0.0)
        return (float(position.price_open or 0.0) - close_price) * float(position.volume or 0.0)
