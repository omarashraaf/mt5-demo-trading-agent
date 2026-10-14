import logging
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, timedelta
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception:  # pragma: no cover - handled for non-Windows/cloud runtimes
    class _MT5Stub:
        def __getattr__(self, name):
            if name.isupper():
                return 0
            return lambda *args, **kwargs: None

    mt5 = _MT5Stub()
    MT5_AVAILABLE = False

logger = logging.getLogger(__name__)


def _history_window(lookback_days: int) -> tuple[datetime, datetime]:
    """
    Build an MT5 history window with a small forward buffer.
    Some brokers expose server times ahead of local OS time; without this,
    newest rows can be missing until local clock catches up.
    """
    now_local = datetime.now()
    date_to = now_local + timedelta(hours=12)
    date_from = now_local - timedelta(days=max(1, int(lookback_days)))
    return date_from, date_to


def _sanitize_mt5_comment(comment: str, max_len: int = 31) -> str:
    # MT5 comments are broker-sensitive; keep short ASCII-safe text.
    raw = (comment or "TradingAgent").strip()
    ascii_only = "".join(ch for ch in raw if 32 <= ord(ch) <= 126)
    compact = " ".join(ascii_only.split())
    safe_chars = []
    for ch in compact:
        if ch.isalnum() or ch in {" ", "_", "-"}:
            safe_chars.append(ch)
        else:
            safe_chars.append("_")
    compact = "".join(safe_chars)
    safe = compact[:max_len].strip()
    return safe or "TradingAgent"


class OrderRequest(BaseModel):
    symbol: str
    action: str  # BUY or SELL
    volume: float
    stop_loss: float
    take_profit: float
    comment: str = "TradingAgent"
    idempotency_key: str = ""
    account_key: str = ""
    execution_context: str = ""
    execution_mode: str = "live"


class OrderResult(BaseModel):
    success: bool
    retcode: int
    retcode_desc: str
    ticket: Optional[int] = None
    volume: Optional[float] = None
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    comment: str = ""


class PositionInfo(BaseModel):
    ticket: int
    symbol: str
    type: str  # BUY or SELL
    volume: float
    price_open: float
    price_current: float
    stop_loss: float
    take_profit: float
    profit: float
    time: int
    comment: str


RETCODE_DESCRIPTIONS = {
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request canceled by trader",
    10008: "Order placed",
    10009: "Request completed",
    10010: "Only part of request completed",
    10011: "Request processing error",
    10012: "Request canceled by timeout",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stops",
    10017: "Trade disabled",
    10018: "Market closed",
    10019: "Not enough money",
    10020: "Prices changed",
    10021: "No quotes to process",
    10022: "Invalid order expiration",
    10023: "Order state changed",
    10024: "Too frequent requests",
    10025: "No changes in request",
    10026: "Autotrading not allowed by server",
    10027: "Autotrading disabled in terminal - enable it in MT5: Tools > Options > Expert Advisors > Allow Algo Trading",
    10028: "Request locked for processing",
    10029: "Order or position frozen",
    10030: "Invalid order filling type",
    10031: "No connection with trade server",
    10032: "Operation allowed only for live accounts",
    10033: "Pending orders limit reached",
    10034: "Volume limit for symbol reached",
    10035: "Incorrect or prohibited order type",
    10036: "Position with specified ID already closed",
    10038: "Close volume exceeds current position volume",
    10039: "Close order already exists for position",
    10040: "Positions limit reached",
    10041: "Pending order activation request rejected, order canceled",
    10042: "Only long positions allowed",
    10043: "Only short positions allowed",
    10044: "Only position close allowed",
}


def _mt5_unavailable_message() -> str:
    return "MetaTrader5 Python package is unavailable in this runtime."


class ExecutionEngine:
    def get_recent_history_order_rows(
        self,
        limit: int = 100,
        lookback_days: int = 120,
    ) -> list[dict]:
        """Return MT5 history rows in order-style (closer to MT5 History tab)."""
        date_from, date_to = _history_window(lookback_days)
        orders = mt5.history_orders_get(date_from, date_to) or []
        deals = mt5.history_deals_get(date_from, date_to) or []

        if not orders:
            return []

        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0

        profit_by_order: dict[int, float] = {}
        last_price_by_order: dict[int, float] = {}
        entry_flags_by_order: dict[int, set[int]] = {}
        for deal in deals:
            order_ticket = int(getattr(deal, "order", 0) or 0)
            if order_ticket <= 0:
                continue
            raw_entry = getattr(deal, "entry", None)
            entry_flag = int(raw_entry) if raw_entry is not None else -1
            entry_flags_by_order.setdefault(order_ticket, set()).add(entry_flag)
            profit = float(getattr(deal, "profit", 0.0) or 0.0)
            commission = float(getattr(deal, "commission", 0.0) or 0.0)
            swap = float(getattr(deal, "swap", 0.0) or 0.0)
            fee = float(getattr(deal, "fee", 0.0) or 0.0)
            profit_by_order[order_ticket] = float(profit_by_order.get(order_ticket, 0.0)) + profit + commission + swap + fee
            px = float(getattr(deal, "price", 0.0) or 0.0)
            if px > 0:
                last_price_by_order[order_ticket] = px

        out: list[dict] = []
        for order in orders:
            order_type = int(getattr(order, "type", -1) or -1)
            if order_type == int(mt5.ORDER_TYPE_BUY):
                action = "BUY"
            elif order_type == int(mt5.ORDER_TYPE_SELL):
                action = "SELL"
            else:
                continue

            done_time = float((getattr(order, "time_done", 0) or 0) or (getattr(order, "time_setup", 0) or 0))
            if done_time <= 0:
                continue

            ticket = int(getattr(order, "ticket", 0) or 0)
            if ticket <= 0:
                continue
            entry_flags = entry_flags_by_order.get(ticket) or set()
            close_flags = {
                int(mt5.DEAL_ENTRY_OUT),
                int(mt5.DEAL_ENTRY_OUT_BY),
                int(mt5.DEAL_ENTRY_INOUT),
            }
            # Closed-only history: keep orders that have at least one close-side deal.
            if not any(flag in close_flags for flag in entry_flags):
                continue

            symbol = str(getattr(order, "symbol", "") or "").upper()
            if not symbol:
                continue

            volume = abs(float(getattr(order, "volume_initial", 0.0) or 0.0))
            if volume <= 0:
                continue

            entry_price = float(getattr(order, "price_open", 0.0) or 0.0)
            close_price = float(getattr(order, "price_current", 0.0) or 0.0)
            if close_price <= 0:
                close_price = float(last_price_by_order.get(ticket, 0.0) or 0.0)
            profit_usd = float(profit_by_order.get(ticket, 0.0) or 0.0)

            contract_size = 1.0
            info = mt5.symbol_info(symbol)
            if info is not None:
                try:
                    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
                except Exception:
                    contract_size = 1.0

            started_with = None
            if entry_price > 0 and volume > 0:
                margin_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
                margin_required = mt5.order_calc_margin(margin_type, symbol, volume, entry_price)
                if margin_required is not None and float(margin_required) > 0:
                    started_with = float(margin_required)
                else:
                    started_with = (entry_price * volume * contract_size) / leverage
            ended_with = (started_with + profit_usd) if started_with is not None else None
            profit_pct = ((profit_usd / started_with) * 100.0) if (started_with is not None and started_with != 0) else None

            sl = float(getattr(order, "sl", 0.0) or 0.0)
            tp = float(getattr(order, "tp", 0.0) or 0.0)
            sl = sl if sl > 0 else None
            tp = tp if tp > 0 else None

            out.append(
                {
                    "ticket": ticket,
                    "symbol": symbol,
                    "action": action,
                    "status": "closed",
                    "opened_at": done_time,
                    "closed_at": done_time,
                    "duration_minutes": 0.0,
                    "volume": volume,
                    "entry_price": entry_price if entry_price > 0 else None,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "profit_usd": profit_usd,
                    "profit_pct": profit_pct,
                    "started_with_usd": started_with,
                    "ended_with_usd": ended_with,
                    "entry_market_value_usd": (entry_price * volume * contract_size) if entry_price > 0 else None,
                    "sl_amount_usd": None,
                    "tp_amount_usd": None,
                    "sl_pct_of_start": None,
                    "tp_pct_of_start": None,
                    "started_with_source": "estimated" if started_with is not None else "unknown",
                    "signal_id": None,
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": "mt5_broker_order_history",
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": "broker_closed",
                    "close_price": close_price if close_price > 0 else None,
                }
            )

        out.sort(key=lambda row: float(row.get("closed_at") or 0.0), reverse=True)
        return out[: max(1, int(limit))]

    def get_recent_closed_deal_rows(
        self,
        limit: int = 100,
        lookback_days: int = 120,
    ) -> list[dict]:
        """Return MT5 closed deal rows (1:1 with history deals)."""
        date_from, date_to = _history_window(lookback_days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        orders = mt5.history_orders_get(date_from, date_to) or []
        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0
        contract_size_by_symbol: dict[str, float] = {}

        def contract_size_for(symbol: str) -> float:
            key = str(symbol or "").upper()
            if not key:
                return 1.0
            if key in contract_size_by_symbol:
                return contract_size_by_symbol[key]
            size = 1.0
            try:
                info = mt5.symbol_info(key)
                if info is not None:
                    size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            except Exception:
                size = 1.0
            if size <= 0:
                size = 1.0
            contract_size_by_symbol[key] = size
            return size

        # Recover SL/TP and entry hints from orders because many deal rows have sl/tp=0.
        order_hint_by_position: dict[int, dict] = {}
        for order in orders:
            position_id = int(getattr(order, "position_id", 0) or 0)
            if position_id <= 0:
                continue
            symbol = str(getattr(order, "symbol", "") or "").upper()
            if not symbol:
                continue
            done_time = float(
                (getattr(order, "time_done", 0) or 0)
                or (getattr(order, "time_setup", 0) or 0)
                or 0
            )
            sl = float(getattr(order, "sl", 0.0) or 0.0)
            tp = float(getattr(order, "tp", 0.0) or 0.0)
            price_open = float(getattr(order, "price_open", 0.0) or 0.0)
            volume_initial = abs(float(getattr(order, "volume_initial", 0.0) or 0.0))
            raw_type = getattr(order, "type", None)
            order_type = int(raw_type) if raw_type is not None else -1
            action_hint = "BUY" if order_type == int(mt5.ORDER_TYPE_BUY) else ("SELL" if order_type == int(mt5.ORDER_TYPE_SELL) else "")

            hint = order_hint_by_position.setdefault(
                position_id,
                {
                    "symbol": symbol,
                    "time": done_time,
                    "sl": None,
                    "tp": None,
                    "entry_price": None,
                    "volume": 0.0,
                    "action": action_hint,
                },
            )
            if done_time > float(hint.get("time") or 0.0):
                hint["time"] = done_time
                hint["symbol"] = symbol
            if sl > 0:
                hint["sl"] = sl
            if tp > 0:
                hint["tp"] = tp
            if price_open > 0 and hint.get("entry_price") is None:
                hint["entry_price"] = price_open
            if volume_initial > 0 and float(hint.get("volume") or 0.0) <= 0:
                hint["volume"] = volume_initial
            if action_hint and not hint.get("action"):
                hint["action"] = action_hint

        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0

        rows: list[dict] = []
        for deal in deals:
            raw_entry = getattr(deal, "entry", None)
            entry_flag = int(raw_entry) if raw_entry is not None else -1
            position_id = int(getattr(deal, "position_id", 0) or 0)
            order_hint = order_hint_by_position.get(position_id) or {}

            symbol = str(getattr(deal, "symbol", "") or "").upper()
            if not symbol:
                symbol = str(order_hint.get("symbol") or "").upper()
            if not symbol:
                continue

            raw_type = getattr(deal, "type", None)
            deal_type = int(raw_type) if raw_type is not None else -1
            if deal_type == int(mt5.DEAL_TYPE_BUY):
                action = "BUY"
            elif deal_type == int(mt5.DEAL_TYPE_SELL):
                action = "SELL"
            else:
                # Skip non-trade deal rows (balance, credit, etc.).
                continue
            position_action = str(order_hint.get("action") or "").upper()
            if position_action in {"BUY", "SELL"}:
                action = position_action
            is_exit_deal = entry_flag in {
                int(mt5.DEAL_ENTRY_OUT),
                int(mt5.DEAL_ENTRY_OUT_BY),
                int(mt5.DEAL_ENTRY_INOUT),
            }

            volume = abs(float(getattr(deal, "volume", 0.0) or 0.0))
            price = float(getattr(deal, "price", 0.0) or 0.0)
            deal_time = float(getattr(deal, "time", 0.0) or 0.0)
            profit = float(getattr(deal, "profit", 0.0) or 0.0)
            commission = float(getattr(deal, "commission", 0.0) or 0.0)
            swap = float(getattr(deal, "swap", 0.0) or 0.0)
            fee = float(getattr(deal, "fee", 0.0) or 0.0)
            net_profit = profit + commission + swap + fee

            entry_price = float(order_hint.get("entry_price") or 0.0)
            if entry_price <= 0:
                entry_price = price
            calc_volume = volume if volume > 0 else float(order_hint.get("volume") or 0.0)
            if calc_volume <= 0:
                calc_volume = volume

            contract_size = 1.0
            info = mt5.symbol_info(symbol)
            if info is not None:
                try:
                    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
                except Exception:
                    contract_size = 1.0

            started_with = None
            if entry_price > 0 and calc_volume > 0:
                order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
                margin_required = mt5.order_calc_margin(order_type, symbol, calc_volume, entry_price)
                if margin_required is not None and float(margin_required) > 0:
                    started_with = float(margin_required)
                else:
                    started_with = (entry_price * calc_volume * contract_size) / leverage

            ended_with = (started_with + net_profit) if started_with is not None else None
            profit_pct = ((net_profit / started_with) * 100.0) if (started_with is not None and started_with != 0) else None

            sl = float(getattr(deal, "sl", 0.0) or 0.0)
            tp = float(getattr(deal, "tp", 0.0) or 0.0)
            if sl <= 0:
                sl = float(order_hint.get("sl") or 0.0)
            if tp <= 0:
                tp = float(order_hint.get("tp") or 0.0)
            sl = sl if sl > 0 else None
            tp = tp if tp > 0 else None

            sl_amount = None
            tp_amount = None
            sl_pct = None
            tp_pct = None
            if entry_price > 0 and calc_volume > 0:
                if isinstance(sl, (int, float)) and float(sl) > 0:
                    sl_amount = abs(entry_price - float(sl)) * calc_volume * contract_size
                if isinstance(tp, (int, float)) and float(tp) > 0:
                    tp_amount = abs(float(tp) - entry_price) * calc_volume * contract_size
            if started_with and started_with > 0:
                if sl_amount is not None:
                    sl_pct = (sl_amount / started_with) * 100.0
                if tp_amount is not None:
                    tp_pct = (tp_amount / started_with) * 100.0

            rows.append(
                {
                    "ticket": int(getattr(deal, "ticket", 0) or 0),
                    "symbol": symbol,
                    "action": action,
                    "status": "closed" if is_exit_deal else "opened",
                    "opened_at": deal_time,
                    "closed_at": deal_time if is_exit_deal else None,
                    "duration_minutes": 0.0,
                    "volume": calc_volume,
                    "entry_price": entry_price if entry_price > 0 else None,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "profit_usd": net_profit,
                    "profit_pct": profit_pct,
                    "started_with_usd": started_with,
                    "ended_with_usd": ended_with,
                    "entry_market_value_usd": (entry_price * calc_volume * contract_size) if (entry_price > 0 and calc_volume > 0) else None,
                    "sl_amount_usd": sl_amount,
                    "tp_amount_usd": tp_amount,
                    "sl_pct_of_start": sl_pct,
                    "tp_pct_of_start": tp_pct,
                    "started_with_source": "estimated" if started_with is not None else "unknown",
                    "signal_id": None,
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": "mt5_broker_deal_history",
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": "broker_closed" if is_exit_deal else "broker_opened",
                    "close_price": price if (is_exit_deal and price > 0) else None,
                }
            )

        rows.sort(key=lambda row: float(row.get("closed_at") or 0.0), reverse=True)
        return rows[: max(1, int(limit))]

    def get_recent_closed_trades(
        self,
        limit: int = 100,
        lookback_days: int = 120,
    ) -> list[dict]:
        """Return closed trades from MT5 broker history, grouped by position."""
        date_from, date_to = _history_window(lookback_days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        orders = mt5.history_orders_get(date_from, date_to) or []
        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0
        contract_size_by_symbol: dict[str, float] = {}

        def contract_size_for(symbol: str) -> float:
            key = str(symbol or "").upper()
            if not key:
                return 1.0
            if key in contract_size_by_symbol:
                return contract_size_by_symbol[key]
            size = 1.0
            try:
                info = mt5.symbol_info(key)
                if info is not None:
                    size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            except Exception:
                size = 1.0
            if size <= 0:
                size = 1.0
            contract_size_by_symbol[key] = size
            return size

        order_by_position: dict[int, dict] = {}
        for order in orders:
            position_id = int(getattr(order, "position_id", 0) or 0)
            if position_id <= 0:
                continue
            symbol = str(getattr(order, "symbol", "") or "").upper()
            if not symbol:
                continue
            done_time = float(
                (getattr(order, "time_done", 0) or 0)
                or (getattr(order, "time_setup", 0) or 0)
                or 0
            )
            price_open = float(getattr(order, "price_open", 0.0) or 0.0)
            volume_initial = float(getattr(order, "volume_initial", 0.0) or 0.0)
            sl = float(getattr(order, "sl", 0.0) or 0.0)
            tp = float(getattr(order, "tp", 0.0) or 0.0)
            raw_type = getattr(order, "type", None)
            order_type = int(raw_type) if raw_type is not None else -1

            entry = order_by_position.setdefault(
                position_id,
                {
                    "symbol": symbol,
                    "opened_at": None,
                    "entry_price": None,
                    "volume": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "action": None,
                },
            )
            if entry["opened_at"] is None or (done_time > 0 and done_time < float(entry["opened_at"])):
                entry["opened_at"] = done_time if done_time > 0 else entry["opened_at"]
            if price_open > 0 and entry["entry_price"] is None:
                entry["entry_price"] = price_open
            if volume_initial > 0 and float(entry.get("volume") or 0.0) <= 0:
                entry["volume"] = volume_initial
            if sl > 0:
                entry["stop_loss"] = sl
            if tp > 0:
                entry["take_profit"] = tp
            if entry["action"] is None:
                if order_type == int(mt5.ORDER_TYPE_BUY):
                    entry["action"] = "BUY"
                elif order_type == int(mt5.ORDER_TYPE_SELL):
                    entry["action"] = "SELL"

        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0

        open_positions = mt5.positions_get() or []
        open_position_ids = {int(getattr(pos, "ticket", 0) or 0) for pos in open_positions}

        grouped: dict[int, dict] = {}
        for deal in deals:
            position_id = int(getattr(deal, "position_id", 0) or 0)
            if position_id <= 0:
                continue
            raw_entry = getattr(deal, "entry", None)
            entry_flag = int(raw_entry) if raw_entry is not None else -1

            bucket = grouped.setdefault(
                position_id,
                {
                    "ticket": position_id,
                    "symbol": str(getattr(deal, "symbol", "") or "").upper(),
                    "opened_at": None,
                    "closed_at": None,
                    "volume": 0.0,
                    "entry_price": None,
                    "close_price": None,
                    "stop_loss": None,
                    "take_profit": None,
                    "profit_usd": 0.0,
                    "entry_value_sum": 0.0,
                    "entry_volume_sum": 0.0,
                    "close_value_sum": 0.0,
                    "close_volume_sum": 0.0,
                    "action_type": None,
                    "deal_ids": [],
                    "has_exit_leg": False,
                },
            )

            volume = abs(float(getattr(deal, "volume", 0.0) or 0.0))
            price = float(getattr(deal, "price", 0.0) or 0.0)
            deal_time = float(getattr(deal, "time", 0.0) or 0.0)
            profit = float(getattr(deal, "profit", 0.0) or 0.0)
            commission = float(getattr(deal, "commission", 0.0) or 0.0)
            swap = float(getattr(deal, "swap", 0.0) or 0.0)
            fee = float(getattr(deal, "fee", 0.0) or 0.0)

            bucket["profit_usd"] += profit + commission + swap + fee
            bucket["deal_ids"].append(int(getattr(deal, "ticket", 0) or 0))
            if bucket["closed_at"] is None or deal_time > float(bucket["closed_at"]):
                bucket["closed_at"] = deal_time
            if bucket["opened_at"] is None or deal_time < float(bucket["opened_at"]):
                bucket["opened_at"] = deal_time

            raw_type = getattr(deal, "type", None)
            deal_type = int(raw_type) if raw_type is not None else -1
            if bucket["action_type"] is None and entry_flag in {int(mt5.DEAL_ENTRY_IN), int(mt5.DEAL_ENTRY_INOUT)}:
                if deal_type == int(mt5.DEAL_TYPE_BUY):
                    bucket["action_type"] = "BUY"
                elif deal_type == int(mt5.DEAL_TYPE_SELL):
                    bucket["action_type"] = "SELL"

            if volume > 0 and price > 0:
                if entry_flag in {int(mt5.DEAL_ENTRY_IN), int(mt5.DEAL_ENTRY_INOUT)}:
                    bucket["entry_value_sum"] += price * volume
                    bucket["entry_volume_sum"] += volume
                    bucket["volume"] = max(float(bucket["volume"]), float(bucket["entry_volume_sum"]))
                if entry_flag in {int(mt5.DEAL_ENTRY_OUT), int(mt5.DEAL_ENTRY_OUT_BY), int(mt5.DEAL_ENTRY_INOUT)}:
                    bucket["has_exit_leg"] = True
                    # For a fully closed position, exit-deals weighted average is a robust close proxy.
                    bucket["close_value_sum"] += price * volume
                    bucket["close_volume_sum"] += volume
                    bucket["volume"] = max(float(bucket["volume"]), float(bucket["close_volume_sum"]))

            sl = float(getattr(deal, "sl", 0.0) or 0.0)
            tp = float(getattr(deal, "tp", 0.0) or 0.0)
            if sl > 0:
                bucket["stop_loss"] = sl
            if tp > 0:
                bucket["take_profit"] = tp

        results: list[dict] = []
        for position_id, item in grouped.items():
            if not bool(item.get("has_exit_leg")):
                continue
            if position_id in open_position_ids:
                continue
            close_volume_sum = float(item.get("close_volume_sum") or 0.0)
            close_price = (
                float(item["close_value_sum"]) / close_volume_sum
                if close_volume_sum > 0
                else None
            )
            entry_volume_sum = float(item.get("entry_volume_sum") or 0.0)
            entry_price = (
                float(item["entry_value_sum"]) / entry_volume_sum
                if entry_volume_sum > 0
                else None
            )
            opened_at = float(item.get("opened_at") or 0.0)
            closed_at = float(item.get("closed_at") or 0.0)
            if closed_at <= 0:
                continue
            action = str(item.get("action_type") or "BUY").upper()
            if action not in {"BUY", "SELL"}:
                action = "BUY"
            symbol = str(item.get("symbol") or "").upper()
            order_hint = order_by_position.get(position_id) or {}
            if not symbol:
                symbol = str(order_hint.get("symbol") or "").upper()
            contract_size = 1.0
            info = mt5.symbol_info(symbol)
            if info is not None:
                try:
                    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
                except Exception:
                    contract_size = 1.0

            if entry_price is None:
                hint_entry = order_hint.get("entry_price")
                if isinstance(hint_entry, (int, float)) and float(hint_entry) > 0:
                    entry_price = float(hint_entry)
            if opened_at <= 0:
                hint_opened = order_hint.get("opened_at")
                if isinstance(hint_opened, (int, float)) and float(hint_opened) > 0:
                    opened_at = float(hint_opened)
            if action not in {"BUY", "SELL"}:
                action = str(order_hint.get("action") or "BUY").upper()
            if action not in {"BUY", "SELL"}:
                action = "BUY"

            stop_loss = item.get("stop_loss")
            take_profit = item.get("take_profit")
            if not isinstance(stop_loss, (int, float)) or float(stop_loss) <= 0:
                hint_sl = order_hint.get("stop_loss")
                stop_loss = float(hint_sl) if isinstance(hint_sl, (int, float)) and float(hint_sl) > 0 else None
            if not isinstance(take_profit, (int, float)) or float(take_profit) <= 0:
                hint_tp = order_hint.get("take_profit")
                take_profit = float(hint_tp) if isinstance(hint_tp, (int, float)) and float(hint_tp) > 0 else None

            if entry_volume_sum <= 0:
                hint_vol = order_hint.get("volume")
                if isinstance(hint_vol, (int, float)) and float(hint_vol) > 0:
                    entry_volume_sum = float(hint_vol)

            started_with = None
            if entry_price and entry_volume_sum > 0:
                order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
                margin_required = mt5.order_calc_margin(order_type, symbol, entry_volume_sum, float(entry_price))
                if margin_required is not None and float(margin_required) > 0:
                    started_with = float(margin_required)
                else:
                    started_with = (float(entry_price) * entry_volume_sum * contract_size) / leverage
            profit_usd = float(item.get("profit_usd") or 0.0)
            ended_with = (started_with + profit_usd) if started_with is not None else None
            profit_pct = ((profit_usd / started_with) * 100.0) if (started_with is not None and started_with != 0) else None

            sl_amount = None
            tp_amount = None
            sl_pct = None
            tp_pct = None
            if started_with is not None and entry_price and entry_volume_sum > 0:
                if isinstance(stop_loss, (int, float)) and float(stop_loss) > 0:
                    sl_amount = abs(float(entry_price) - float(stop_loss)) * entry_volume_sum * contract_size
                    if started_with > 0:
                        sl_pct = (sl_amount / started_with) * 100.0
                if isinstance(take_profit, (int, float)) and float(take_profit) > 0:
                    tp_amount = abs(float(take_profit) - float(entry_price)) * entry_volume_sum * contract_size
                    if started_with > 0:
                        tp_pct = (tp_amount / started_with) * 100.0

            results.append(
                {
                    "ticket": int(position_id),
                    "symbol": symbol,
                    "action": action,
                    "status": "closed",
                    "opened_at": opened_at if opened_at > 0 else closed_at,
                    "closed_at": closed_at,
                    "duration_minutes": max(0.0, (closed_at - (opened_at if opened_at > 0 else closed_at)) / 60.0),
                    "volume": float(item.get("volume") or 0.0) if float(item.get("volume") or 0.0) > 0 else entry_volume_sum,
                    "entry_price": entry_price,
                    "stop_loss": stop_loss if isinstance(stop_loss, (int, float)) and float(stop_loss) > 0 else None,
                    "take_profit": take_profit if isinstance(take_profit, (int, float)) and float(take_profit) > 0 else None,
                    "profit_usd": profit_usd,
                    "profit_pct": profit_pct,
                    "started_with_usd": started_with,
                    "ended_with_usd": ended_with,
                    "entry_market_value_usd": (float(entry_price) * entry_volume_sum * contract_size) if (entry_price and entry_volume_sum > 0) else None,
                    "sl_amount_usd": sl_amount,
                    "tp_amount_usd": tp_amount,
                    "sl_pct_of_start": sl_pct,
                    "tp_pct_of_start": tp_pct,
                    "started_with_source": "estimated" if started_with is not None else "unknown",
                    "signal_id": None,
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": "mt5_broker_history",
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": "broker_closed",
                    "close_price": close_price,
                }
            )

        results.sort(key=lambda row: float(row.get("closed_at") or 0.0), reverse=True)
        return results[: max(1, int(limit))]

    def get_recent_closed_trades_fast(
        self,
        limit: int = 50,
        lookback_days: int = 30,
    ) -> list[dict]:
        """
        Return recent closed trades for the currently connected MT5 account.

        This intentionally avoids per-symbol `symbol_info` and `order_calc_margin`
        calls so the UI history page can load quickly while still being scoped to
        the connected terminal account.
        """
        date_from, date_to = _history_window(lookback_days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        orders = mt5.history_orders_get(date_from, date_to) or []
        account_info = mt5.account_info()
        leverage = float(getattr(account_info, "leverage", 100.0) or 100.0)
        if leverage <= 0:
            leverage = 100.0
        contract_size_by_symbol: dict[str, float] = {}

        def contract_size_for(symbol: str) -> float:
            key = str(symbol or "").upper()
            if not key:
                return 1.0
            if key in contract_size_by_symbol:
                return contract_size_by_symbol[key]
            size = 1.0
            try:
                info = mt5.symbol_info(key)
                if info is not None:
                    size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            except Exception:
                size = 1.0
            if size <= 0:
                size = 1.0
            contract_size_by_symbol[key] = size
            return size

        order_by_position: dict[int, dict] = {}
        for order in orders:
            position_id = int(getattr(order, "position_id", 0) or 0)
            if position_id <= 0:
                continue
            symbol = str(getattr(order, "symbol", "") or "").upper()
            raw_type = getattr(order, "type", None)
            order_type = int(raw_type) if raw_type is not None else -1
            done_time = float(
                (getattr(order, "time_done", 0) or 0)
                or (getattr(order, "time_setup", 0) or 0)
                or 0
            )
            hint = order_by_position.setdefault(
                position_id,
                {
                    "symbol": symbol,
                    "opened_at": None,
                    "entry_price": None,
                    "volume": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "action": None,
                },
            )
            if done_time > 0 and (hint["opened_at"] is None or done_time < float(hint["opened_at"])):
                hint["opened_at"] = done_time
            price_open = float(getattr(order, "price_open", 0.0) or 0.0)
            if price_open > 0 and hint["entry_price"] is None:
                hint["entry_price"] = price_open
            volume_initial = abs(float(getattr(order, "volume_initial", 0.0) or 0.0))
            if volume_initial > 0 and float(hint.get("volume") or 0.0) <= 0:
                hint["volume"] = volume_initial
            sl = float(getattr(order, "sl", 0.0) or 0.0)
            tp = float(getattr(order, "tp", 0.0) or 0.0)
            if sl > 0:
                hint["stop_loss"] = sl
            if tp > 0:
                hint["take_profit"] = tp
            if hint["action"] is None:
                if order_type == int(mt5.ORDER_TYPE_BUY):
                    hint["action"] = "BUY"
                elif order_type == int(mt5.ORDER_TYPE_SELL):
                    hint["action"] = "SELL"

        open_positions = mt5.positions_get() or []
        open_position_ids = {int(getattr(pos, "ticket", 0) or 0) for pos in open_positions}

        grouped: dict[int, dict] = {}
        for deal in deals:
            position_id = int(getattr(deal, "position_id", 0) or 0)
            if position_id <= 0:
                continue
            entry_flag = int(getattr(deal, "entry", -1) if getattr(deal, "entry", None) is not None else -1)
            deal_type = int(getattr(deal, "type", -1) if getattr(deal, "type", None) is not None else -1)
            bucket = grouped.setdefault(
                position_id,
                {
                    "ticket": position_id,
                    "symbol": str(getattr(deal, "symbol", "") or "").upper(),
                    "opened_at": None,
                    "closed_at": None,
                    "volume": 0.0,
                    "entry_value_sum": 0.0,
                    "entry_volume_sum": 0.0,
                    "close_value_sum": 0.0,
                    "close_volume_sum": 0.0,
                    "profit_usd": 0.0,
                    "action": None,
                    "stop_loss": None,
                    "take_profit": None,
                    "has_exit_leg": False,
                },
            )
            volume = abs(float(getattr(deal, "volume", 0.0) or 0.0))
            price = float(getattr(deal, "price", 0.0) or 0.0)
            deal_time = float(getattr(deal, "time", 0.0) or 0.0)
            bucket["profit_usd"] += (
                float(getattr(deal, "profit", 0.0) or 0.0)
                + float(getattr(deal, "commission", 0.0) or 0.0)
                + float(getattr(deal, "swap", 0.0) or 0.0)
                + float(getattr(deal, "fee", 0.0) or 0.0)
            )
            if deal_time > 0:
                if bucket["opened_at"] is None or deal_time < float(bucket["opened_at"]):
                    bucket["opened_at"] = deal_time
                if bucket["closed_at"] is None or deal_time > float(bucket["closed_at"]):
                    bucket["closed_at"] = deal_time
            if bucket["action"] is None and entry_flag in {int(mt5.DEAL_ENTRY_IN), int(mt5.DEAL_ENTRY_INOUT)}:
                if deal_type == int(mt5.DEAL_TYPE_BUY):
                    bucket["action"] = "BUY"
                elif deal_type == int(mt5.DEAL_TYPE_SELL):
                    bucket["action"] = "SELL"
            if volume > 0 and price > 0:
                if entry_flag in {int(mt5.DEAL_ENTRY_IN), int(mt5.DEAL_ENTRY_INOUT)}:
                    bucket["entry_value_sum"] += price * volume
                    bucket["entry_volume_sum"] += volume
                if entry_flag in {int(mt5.DEAL_ENTRY_OUT), int(mt5.DEAL_ENTRY_OUT_BY), int(mt5.DEAL_ENTRY_INOUT)}:
                    bucket["has_exit_leg"] = True
                    bucket["close_value_sum"] += price * volume
                    bucket["close_volume_sum"] += volume
                bucket["volume"] = max(float(bucket["volume"] or 0.0), volume)
            sl = float(getattr(deal, "sl", 0.0) or 0.0)
            tp = float(getattr(deal, "tp", 0.0) or 0.0)
            if sl > 0:
                bucket["stop_loss"] = sl
            if tp > 0:
                bucket["take_profit"] = tp

        results: list[dict] = []
        for position_id, item in grouped.items():
            if not bool(item.get("has_exit_leg")) or position_id in open_position_ids:
                continue
            hint = order_by_position.get(position_id) or {}
            symbol = str(item.get("symbol") or hint.get("symbol") or "").upper()
            opened_at = float(item.get("opened_at") or hint.get("opened_at") or item.get("closed_at") or 0.0)
            closed_at = float(item.get("closed_at") or 0.0)
            if closed_at <= 0:
                continue
            entry_volume = float(item.get("entry_volume_sum") or 0.0)
            close_volume = float(item.get("close_volume_sum") or 0.0)
            entry_price = (
                float(item["entry_value_sum"]) / entry_volume
                if entry_volume > 0
                else hint.get("entry_price")
            )
            close_price = (
                float(item["close_value_sum"]) / close_volume
                if close_volume > 0
                else None
            )
            volume = max(float(item.get("volume") or 0.0), entry_volume, close_volume, float(hint.get("volume") or 0.0))
            action = str(item.get("action") or hint.get("action") or "BUY").upper()
            if action not in {"BUY", "SELL"}:
                action = "BUY"
            stop_loss = item.get("stop_loss") or hint.get("stop_loss")
            take_profit = item.get("take_profit") or hint.get("take_profit")
            profit_usd = float(item.get("profit_usd") or 0.0)
            entry_price_value = float(entry_price) if isinstance(entry_price, (int, float)) and float(entry_price) > 0 else None
            contract_size = contract_size_for(symbol)
            entry_market_value = (
                entry_price_value * volume * contract_size
                if entry_price_value is not None and volume > 0
                else None
            )
            started_with = (
                entry_market_value / leverage
                if entry_market_value is not None
                else None
            )
            ended_with = (
                started_with + profit_usd
                if started_with is not None
                else None
            )
            profit_pct = (
                (profit_usd / started_with) * 100.0
                if started_with not in {None, 0}
                else None
            )
            stop_loss_value = float(stop_loss) if isinstance(stop_loss, (int, float)) and float(stop_loss) > 0 else None
            take_profit_value = float(take_profit) if isinstance(take_profit, (int, float)) and float(take_profit) > 0 else None
            sl_amount = (
                abs(entry_price_value - stop_loss_value) * volume * contract_size
                if entry_price_value is not None and stop_loss_value is not None and volume > 0
                else None
            )
            tp_amount = (
                abs(take_profit_value - entry_price_value) * volume * contract_size
                if entry_price_value is not None and take_profit_value is not None and volume > 0
                else None
            )
            sl_pct = (
                (sl_amount / started_with) * 100.0
                if sl_amount is not None and started_with not in {None, 0}
                else None
            )
            tp_pct = (
                (tp_amount / started_with) * 100.0
                if tp_amount is not None and started_with not in {None, 0}
                else None
            )
            results.append(
                {
                    "ticket": int(position_id),
                    "signal_id": None,
                    "symbol": symbol,
                    "origin": "mt5_connected_account",
                    "action": action,
                    "side": action,
                    "status": "closed",
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "duration_minutes": max(0.0, (closed_at - opened_at) / 60.0),
                    "volume": volume if volume > 0 else None,
                    "entry_price": entry_price_value,
                    "close_price": close_price,
                    "stop_loss": stop_loss_value,
                    "take_profit": take_profit_value,
                    "profit_usd": profit_usd,
                    "profit_pct": profit_pct,
                    "started_with_usd": started_with,
                    "ended_with_usd": ended_with,
                    "entry_market_value_usd": entry_market_value,
                    "sl_amount_usd": sl_amount,
                    "tp_amount_usd": tp_amount,
                    "sl_pct_of_start": sl_pct,
                    "tp_pct_of_start": tp_pct,
                    "started_with_source": "broker_history_estimated_margin",
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": "MT5 connected account broker history",
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": "broker_closed",
                    "audit_trail": [
                        {
                            "time": closed_at,
                            "type": "close",
                            "title": "Closed on connected MT5 account",
                            "detail": f"Realized P/L {profit_usd:.2f}",
                        }
                    ],
                }
            )
        results.sort(key=lambda row: float(row.get("closed_at") or 0.0), reverse=True)
        return results[: max(1, int(limit))]

    def get_recent_commission_by_symbol(
        self,
        lookback_days: int = 45,
    ) -> dict[str, dict]:
        """
        Derive effective commission from realized MT5 deals.
        Returns per-symbol weighted average commission per lot per side.
        """
        date_from, date_to = _history_window(lookback_days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return {}

        agg: dict[str, dict[str, float]] = {}
        for deal in deals:
            symbol = str(getattr(deal, "symbol", "") or "").strip()
            if not symbol:
                continue
            volume = float(getattr(deal, "volume", 0.0) or 0.0)
            commission = float(getattr(deal, "commission", 0.0) or 0.0)
            if volume <= 0:
                continue
            if abs(commission) <= 0:
                continue
            item = agg.setdefault(symbol, {"sum_abs_commission": 0.0, "sum_volume": 0.0, "deals": 0.0})
            item["sum_abs_commission"] += abs(commission)
            item["sum_volume"] += volume
            item["deals"] += 1.0

        out: dict[str, dict] = {}
        for symbol, item in agg.items():
            sum_volume = float(item.get("sum_volume", 0.0) or 0.0)
            if sum_volume <= 0:
                continue
            per_lot_side = float(item["sum_abs_commission"]) / sum_volume
            out[symbol] = {
                "commission_per_lot_side": round(per_lot_side, 6),
                "commission_round_turn_per_lot": round(per_lot_side * 2.0, 6),
                "commission_model": "realized_history",
                "commission_percent_rate": None,
                "commission_notional_1lot": None,
                "commission_samples": int(item.get("deals", 0.0) or 0.0),
            }
        return out

    def estimate_margin(self, symbol: str, action: str, volume: float, price: Optional[float] = None) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        if price is None:
            price = tick.ask if action == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        margin = mt5.order_calc_margin(order_type, symbol, volume, price)
        return None if margin is None else float(margin)

    def get_tradeability(self, symbol: str) -> dict:
        if not MT5_AVAILABLE:
            return {"symbol": symbol, "trade_enabled": False, "has_tick": False, "reason": _mt5_unavailable_message()}
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        return {
            "symbol": symbol,
            "exists": info is not None,
            "visible": bool(info.visible) if info is not None else False,
            "trade_mode": getattr(info, "trade_mode", 0) if info is not None else 0,
            "trade_enabled": getattr(info, "trade_mode", 0) != 0 if info is not None else False,
            "has_tick": tick is not None,
        }

    def get_realized_profit_map(
        self,
        position_tickets: list[int],
        lookback_days: int = 30,
    ) -> dict[int, float]:
        tickets = {int(t) for t in position_tickets if t is not None}
        if not tickets:
            return {}
        date_from, date_to = _history_window(lookback_days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return {}
        pnl_by_position: dict[int, float] = {}
        for deal in deals:
            pos_id = int(getattr(deal, "position_id", 0) or 0)
            if pos_id not in tickets:
                continue
            profit = float(getattr(deal, "profit", 0.0) or 0.0)
            commission = float(getattr(deal, "commission", 0.0) or 0.0)
            swap = float(getattr(deal, "swap", 0.0) or 0.0)
            fee = float(getattr(deal, "fee", 0.0) or 0.0)
            pnl_by_position[pos_id] = pnl_by_position.get(pos_id, 0.0) + profit + commission + swap + fee
        return pnl_by_position

    def get_position_realized_profit(
        self,
        position_ticket: int,
        lookback_days: int = 30,
    ) -> Optional[float]:
        pnl_map = self.get_realized_profit_map([position_ticket], lookback_days=lookback_days)
        return pnl_map.get(int(position_ticket))

    def place_order(self, req: OrderRequest) -> OrderResult:
        if not MT5_AVAILABLE:
            return OrderResult(success=False, retcode=-1, retcode_desc=_mt5_unavailable_message(), comment=req.comment)
        tick = mt5.symbol_info_tick(req.symbol)
        if tick is None:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"Cannot get tick for {req.symbol}",
            )

        if req.action == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif req.action == "SELL":
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"Invalid action: {req.action}",
            )

        # Auto-detect the correct filling mode for this symbol
        filling_type = mt5.ORDER_FILLING_IOC  # Default
        sym_info = mt5.symbol_info(req.symbol)
        if sym_info is not None:
            # filling_mode is a bitmask: bit 0 = FOK, bit 1 = IOC, bit 2 = RETURN
            fm = sym_info.filling_mode
            if fm & 2:  # IOC supported
                filling_type = mt5.ORDER_FILLING_IOC
            elif fm & 1:  # FOK supported
                filling_type = mt5.ORDER_FILLING_FOK
            else:  # RETURN as fallback
                filling_type = mt5.ORDER_FILLING_RETURN

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": req.symbol,
            "volume": req.volume,
            "type": order_type,
            "price": price,
            "sl": req.stop_loss,
            "tp": req.take_profit,
            "deviation": 20,
            "magic": 234000,
            "comment": _sanitize_mt5_comment(req.comment),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        result = mt5.order_send(request)
        if result is None:
            error = mt5.last_error()
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"order_send returned None: {error}",
            )

        desc = RETCODE_DESCRIPTIONS.get(result.retcode, f"Unknown retcode {result.retcode}")

        return OrderResult(
            success=result.retcode == 10009,
            retcode=result.retcode,
            retcode_desc=desc,
            ticket=result.order if result.retcode == 10009 else None,
            volume=result.volume,
            price=result.price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            comment=result.comment,
        )

    def close_position(self, ticket: int) -> OrderResult:
        if not MT5_AVAILABLE:
            return OrderResult(success=False, retcode=-1, retcode_desc=_mt5_unavailable_message(), comment="")
        transient_retcodes = {10004, 10020, 10021, 10028}
        last_result = None
        for deviation in (20, 50, 100):
            positions = mt5.positions_get(ticket=ticket)
            if positions is None or len(positions) == 0:
                return OrderResult(
                    success=True,
                    retcode=10009,
                    retcode_desc="Position already closed",
                    ticket=ticket,
                )

            pos = positions[0]
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                return OrderResult(
                    success=False,
                    retcode=-1,
                    retcode_desc=f"Cannot get tick for {pos.symbol}",
                )

            if pos.type == mt5.ORDER_TYPE_BUY:
                close_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                close_type = mt5.ORDER_TYPE_BUY
                price = tick.ask

            # Auto-detect filling mode for close order
            filling_type = mt5.ORDER_FILLING_IOC
            sym_info = mt5.symbol_info(pos.symbol)
            if sym_info is not None:
                fm = sym_info.filling_mode
                if fm & 2:
                    filling_type = mt5.ORDER_FILLING_IOC
                elif fm & 1:
                    filling_type = mt5.ORDER_FILLING_FOK
                else:
                    filling_type = mt5.ORDER_FILLING_RETURN

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": deviation,
                "magic": 234000,
                "comment": "TradingAgent close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_type,
            }

            result = mt5.order_send(request)
            if result is None:
                error = mt5.last_error()
                return OrderResult(
                    success=False,
                    retcode=-1,
                    retcode_desc=f"Close order_send returned None: {error}",
                )
            last_result = result
            if result.retcode in {10009, 10008}:
                desc = RETCODE_DESCRIPTIONS.get(result.retcode, f"Unknown retcode {result.retcode}")
                return OrderResult(
                    success=True,
                    retcode=result.retcode,
                    retcode_desc=desc,
                    ticket=result.order if int(result.order or 0) > 0 else ticket,
                    volume=result.volume,
                    price=result.price,
                )
            if result.retcode in transient_retcodes:
                continue
            desc = RETCODE_DESCRIPTIONS.get(result.retcode, f"Unknown retcode {result.retcode}")
            return OrderResult(
                success=False,
                retcode=result.retcode,
                retcode_desc=desc,
                ticket=ticket,
                volume=result.volume,
                price=result.price,
            )

        if last_result is not None:
            desc = RETCODE_DESCRIPTIONS.get(last_result.retcode, f"Unknown retcode {last_result.retcode}")
            return OrderResult(
                success=False,
                retcode=last_result.retcode,
                retcode_desc=desc,
                ticket=ticket,
                volume=last_result.volume,
                price=last_result.price,
            )
        return OrderResult(
            success=False,
            retcode=-1,
            retcode_desc="Close failed without broker response",
            ticket=ticket,
        )

    def modify_position(self, ticket: int, stop_loss: float, take_profit: float) -> OrderResult:
        """Modify SL/TP on an existing open position."""
        if not MT5_AVAILABLE:
            return OrderResult(success=False, retcode=-1, retcode_desc=_mt5_unavailable_message(), comment="")
        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            return OrderResult(
                success=False, retcode=-1,
                retcode_desc=f"Position {ticket} not found for modification",
            )

        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": stop_loss,
            "tp": take_profit,
        }

        result = mt5.order_send(request)
        if result is None:
            error = mt5.last_error()
            return OrderResult(
                success=False, retcode=-1,
                retcode_desc=f"modify_position returned None: {error}",
            )

        desc = RETCODE_DESCRIPTIONS.get(result.retcode, f"Unknown retcode {result.retcode}")
        return OrderResult(
            success=result.retcode == 10009,
            retcode=result.retcode,
            retcode_desc=desc,
            ticket=ticket,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def get_positions(self, symbol: Optional[str] = None) -> list[PositionInfo]:
        if not MT5_AVAILABLE:
            return []
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()

        if positions is None:
            return []

        result = []
        for p in positions:
            result.append(
                PositionInfo(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    type="BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                    volume=p.volume,
                    price_open=p.price_open,
                    price_current=p.price_current,
                    stop_loss=p.sl,
                    take_profit=p.tp,
                    profit=p.profit,
                    time=p.time,
                    comment=p.comment,
                )
            )
        return result
