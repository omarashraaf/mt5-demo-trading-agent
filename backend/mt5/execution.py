import MetaTrader5 as mt5
import logging
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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


class ExecutionEngine:
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

    def place_order(self, req: OrderRequest) -> OrderResult:
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
        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"Position {ticket} not found",
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
            "deviation": 20,
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

        desc = RETCODE_DESCRIPTIONS.get(result.retcode, f"Unknown retcode {result.retcode}")
        return OrderResult(
            success=result.retcode == 10009,
            retcode=result.retcode,
            retcode_desc=desc,
            ticket=result.order if result.retcode == 10009 else None,
            volume=result.volume,
            price=result.price,
        )

    def modify_position(self, ticket: int, stop_loss: float, take_profit: float) -> OrderResult:
        """Modify SL/TP on an existing open position."""
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
