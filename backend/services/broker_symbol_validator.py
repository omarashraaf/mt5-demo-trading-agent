from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BrokerDiagnosticIssue:
    code: str
    message: str
    details: dict[str, Any]
    suggested_fix: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "suggested_fix": self.suggested_fix,
        }


class BrokerSymbolValidator:
    """Best-effort broker rule diagnostics before an order can reach MT5."""

    def __init__(self, *, max_quote_age_seconds: float = 10.0):
        self.max_quote_age_seconds = max(1.0, float(max_quote_age_seconds))

    def validate_context_order(
        self,
        *,
        context,
        action: str,
        volume: float,
        stop_loss: float | None,
        take_profit: float | None,
        entry_price: float,
        margin_required: float,
    ) -> list[BrokerDiagnosticIssue]:
        issues: list[BrokerDiagnosticIssue] = []
        symbol = str(getattr(context, "symbol", "") or "").upper()
        info = getattr(context, "symbol_info", None)
        tick = getattr(context, "tick", None) or {}
        action = str(action or "").upper()

        if info is None:
            issues.append(self._issue("symbol_info_missing", symbol, "Broker symbol metadata is unavailable.", {}, "Refresh market data or select a broker-supported symbol."))
            return issues

        if getattr(info, "visible", None) is False and self._has_explicit_field(info, "visible"):
            issues.append(self._issue("symbol_not_visible", symbol, "Symbol is not visible in Market Watch.", {"visible": False}, "Enable/select the symbol in MT5 Market Watch."))
        if getattr(info, "trade_enabled", None) is False and self._has_explicit_field(info, "trade_enabled"):
            issues.append(self._issue("symbol_trade_disabled", symbol, "Broker reports this symbol is not trade-enabled.", {"trade_enabled": False}, "Choose a trade-enabled symbol or wait for the market session to open."))

        bid = self._num(tick.get("bid"))
        ask = self._num(tick.get("ask"))
        if bid <= 0 or ask <= 0 or ask < bid:
            issues.append(self._issue("quote_invalid", symbol, "Live bid/ask quote is invalid.", {"bid": bid, "ask": ask}, "Wait for a fresh quote from the broker."))
        tick_ts = self._tick_timestamp(tick)
        if tick_ts > 0 and (time.time() - tick_ts) > self.max_quote_age_seconds:
            issues.append(self._issue("quote_stale", symbol, "Live quote is stale.", {"quote_time": tick_ts, "max_age_seconds": self.max_quote_age_seconds}, "Wait for the broker feed to update."))

        min_lot = self._num(getattr(info, "volume_min", 0.0))
        max_lot = self._num(getattr(info, "volume_max", 0.0))
        lot_step = self._num(getattr(info, "volume_step", 0.0))
        vol = self._num(volume)
        if vol <= 0:
            issues.append(self._issue("volume_invalid", symbol, "Order volume must be positive.", {"volume": volume}, "Use a positive lot size."))
        if min_lot > 0 and vol < min_lot - 1e-9:
            issues.append(self._issue("volume_below_min", symbol, "Order volume is below broker minimum.", {"volume": vol, "min_lot": min_lot}, f"Use at least {min_lot:g} lots."))
        if max_lot > 0 and vol > max_lot + 1e-9:
            issues.append(self._issue("volume_above_max", symbol, "Order volume is above broker maximum.", {"volume": vol, "max_lot": max_lot}, f"Use no more than {max_lot:g} lots."))
        if lot_step > 0 and vol > 0:
            steps = round((vol - min_lot) / lot_step) if min_lot > 0 else round(vol / lot_step)
            expected = (min_lot + steps * lot_step) if min_lot > 0 else steps * lot_step
            if abs(vol - expected) > max(1e-8, lot_step / 1000.0):
                issues.append(self._issue("volume_step_mismatch", symbol, "Order volume does not match broker lot step.", {"volume": vol, "lot_step": lot_step, "nearest_valid_volume": expected}, "Round volume to the broker lot step."))

        sl = self._num(stop_loss)
        tp = self._num(take_profit)
        if sl <= 0 or tp <= 0:
            issues.append(self._issue("protective_targets_missing", symbol, "Stop-loss and take-profit are required.", {"stop_loss": stop_loss, "take_profit": take_profit}, "Set both SL and TP before execution."))
        elif entry_price > 0:
            if action == "BUY" and not (sl < entry_price < tp):
                issues.append(self._issue("buy_targets_invalid", symbol, "BUY SL/TP are not around entry price.", {"entry": entry_price, "stop_loss": sl, "take_profit": tp}, "For BUY, SL must be below entry and TP above entry."))
            if action == "SELL" and not (tp < entry_price < sl):
                issues.append(self._issue("sell_targets_invalid", symbol, "SELL SL/TP are not around entry price.", {"entry": entry_price, "stop_loss": sl, "take_profit": tp}, "For SELL, TP must be below entry and SL above entry."))
            point = self._num(getattr(info, "point", 0.0))
            stops_level = self._num(getattr(info, "trade_stops_level", 0.0))
            if point > 0 and stops_level > 0:
                min_distance = point * stops_level
                if abs(entry_price - sl) < min_distance or abs(tp - entry_price) < min_distance:
                    issues.append(self._issue("stops_level_violation", symbol, "SL/TP are closer than broker stops level.", {"entry": entry_price, "stop_loss": sl, "take_profit": tp, "min_distance": min_distance, "stops_level": stops_level}, "Move SL/TP farther from current price."))

        digits = int(getattr(info, "digits", 0) or 0)
        contract_size = self._num(getattr(info, "trade_contract_size", 0.0))
        if digits <= 0:
            issues.append(self._issue("digits_invalid", symbol, "Broker returned invalid symbol digits.", {"digits": digits}, "Refresh symbol metadata or avoid this symbol."))
        if contract_size <= 0:
            issues.append(self._issue("contract_size_invalid", symbol, "Broker returned invalid contract size.", {"contract_size": contract_size}, "Refresh symbol metadata or avoid this symbol."))
        if margin_required <= 0 or not math.isfinite(float(margin_required)):
            issues.append(self._issue("margin_required_invalid", symbol, "Margin requirement could not be verified.", {"margin_required": margin_required}, "Wait for broker margin calculation or reduce order size."))
        return issues

    def _issue(self, code: str, symbol: str, message: str, details: dict[str, Any], suggested_fix: str) -> BrokerDiagnosticIssue:
        payload = {"symbol": symbol}
        payload.update(details)
        return BrokerDiagnosticIssue(code=code, message=message, details=payload, suggested_fix=suggested_fix)

    def _num(self, value: Any) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return 0.0
        return out if math.isfinite(out) else 0.0

    def _tick_timestamp(self, tick: dict[str, Any]) -> float:
        raw_msc = self._num(tick.get("time_msc"))
        if raw_msc > 0:
            return raw_msc / 1000.0
        return self._num(tick.get("time"))

    def _has_explicit_field(self, info: Any, field: str) -> bool:
        fields_set = getattr(info, "model_fields_set", None)
        if fields_set is not None:
            return field in fields_set
        return hasattr(info, field)
