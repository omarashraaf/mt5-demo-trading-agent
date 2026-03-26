import logging
from pydantic import BaseModel
from agent.interface import TradeSignal
from mt5.execution import PositionInfo

logger = logging.getLogger(__name__)


class RiskSettings(BaseModel):
    risk_percent_per_trade: float = 0.3  # 0.3% per trade = ~$270 risk → smaller positions, more diversification
    max_daily_loss_percent: float = 3.0
    max_concurrent_positions: int = 15  # Allow more trades with smaller sizes for better diversification
    min_confidence_threshold: float = 0.55
    max_spread_threshold: float = 15.0
    # Preferred symbols: stocks (swing) + major forex (day) + commodities (swing)
    allowed_symbols: list[str] = [
        # Top Stocks — swing trade (hold days)
        "NVDA", "AMD", "MSFT", "INTC", "AAPL", "GOOG", "AMZN", "TSLA", "META", "NFLX",
        "AVGO", "CRM", "ORCL", "ADBE", "QCOM",
        # Major Forex — day trade (hold hours)
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
        # Metals — swing trade (gold, silver, platinum, palladium)
        "XAUUSD", "XAGUSD", "XAUEUR", "XAGEUR", "XPTUSD", "XPDUSD",
        # Commodity ETFs — swing trade (oil, gold, silver, broad commodities)
        "GLD", "SLV", "USO", "DBC",
    ]
    require_stop_loss: bool = True
    use_fixed_lot: bool = False  # Dynamic sizing based on equity
    fixed_lot_size: float = 0.01
    # Auto-trading settings
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.55
    auto_trade_scan_interval_seconds: int = 60


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    adjusted_volume: float = 0.0
    warnings: list[str] = []  # Warnings shown to user but don't block


class RiskEngine:
    def __init__(self):
        self.settings = RiskSettings()
        self._daily_loss: float = 0.0
        self._daily_start_equity: float = 0.0
        self._panic_stop: bool = False

    def update_settings(self, settings: RiskSettings):
        self.settings = settings
        logger.info(f"Risk settings updated: {settings.model_dump()}")

    def set_daily_start_equity(self, equity: float):
        self._daily_start_equity = equity
        self._daily_loss = 0.0

    def set_panic_stop(self, active: bool):
        self._panic_stop = active
        logger.warning(f"Panic stop {'ACTIVATED' if active else 'deactivated'}")

    @property
    def panic_stopped(self) -> bool:
        return self._panic_stop

    def evaluate(
        self,
        signal: TradeSignal,
        symbol: str,
        spread: float,
        equity: float,
        open_positions: list[PositionInfo],
        is_auto_trade: bool = False,
        entry_price: float = 0.0,
    ) -> RiskDecision:
        s = self.settings
        warnings: list[str] = []

        # Only panic stop actually blocks
        if self._panic_stop:
            return RiskDecision(
                approved=False,
                reason="Panic stop is active. All trading halted.",
            )

        # HOLD signals can't be executed
        if signal.action == "HOLD":
            return RiskDecision(
                approved=False,
                reason="Signal is HOLD, no trade to execute.",
            )

        # --- Hard blocks for auto-trade mode ---

        # Max positions — hard block in auto-trade
        if len(open_positions) >= s.max_concurrent_positions:
            if is_auto_trade:
                return RiskDecision(
                    approved=False,
                    reason=f"Max {s.max_concurrent_positions} positions reached. Waiting for exits.",
                )
            warnings.append(f"You have {len(open_positions)} positions open (max recommended: {s.max_concurrent_positions})")

        # Daily loss — hard block in auto-trade
        if self._daily_start_equity > 0:
            current_loss_pct = (
                (self._daily_start_equity - equity) / self._daily_start_equity * 100
            )
            if current_loss_pct >= s.max_daily_loss_percent:
                if is_auto_trade:
                    return RiskDecision(
                        approved=False,
                        reason=f"Daily loss {current_loss_pct:.1f}% hit limit ({s.max_daily_loss_percent}%). Auto-trading paused.",
                    )
                warnings.append(f"Daily loss is {current_loss_pct:.1f}% (limit: {s.max_daily_loss_percent}%) - consider stopping")

        # --- Reward-to-Risk check (hard block for auto-trade) ---
        if signal.stop_loss and signal.take_profit:
            entry_est = entry_price if entry_price > 0 else self._get_entry_estimate(signal)
            if entry_est > 0:
                sl_distance = abs(entry_est - signal.stop_loss)
                tp_distance = abs(signal.take_profit - entry_est)
                if sl_distance > 0:
                    rr_ratio = tp_distance / sl_distance
                    logger.info(f"R:R check {symbol} {signal.action}: entry={entry_est:.5f} SL={signal.stop_loss:.5f} TP={signal.take_profit:.5f} => RR={rr_ratio:.2f}:1 (entry_price_param={entry_price:.5f})")
                    if rr_ratio < 1.5 and is_auto_trade:
                        return RiskDecision(
                            approved=False,
                            reason=f"Reward:Risk ratio {rr_ratio:.1f}:1 too low (need 1.5:1 minimum). TP target not worth the risk.",
                        )
                    if rr_ratio < 1.5:
                        warnings.append(f"Low R:R ratio ({rr_ratio:.1f}:1) - TP target may not justify the risk")

        # --- Warnings (never block) ---

        if s.allowed_symbols and symbol not in s.allowed_symbols:
            warnings.append(f"Symbol {symbol} is not in your usual watchlist")

        if signal.confidence < s.min_confidence_threshold:
            warnings.append(f"Low confidence ({signal.confidence:.0%}) - below {s.min_confidence_threshold:.0%} threshold")

        if spread > s.max_spread_threshold:
            warnings.append(f"High spread ({spread}) - trading costs may be higher")

        if s.require_stop_loss and signal.stop_loss is None:
            warnings.append("No stop loss set - your losses are not limited on this trade")

        # Warn about averaging down
        same_symbol_positions = [p for p in open_positions if p.symbol == symbol]
        for pos in same_symbol_positions:
            if pos.profit < 0:
                if (signal.action == "BUY" and pos.type == "BUY") or (
                    signal.action == "SELL" and pos.type == "SELL"
                ):
                    warnings.append(f"Adding to a losing {pos.type} position on {symbol} ({pos.profit:.2f})")

        # Calculate volume with position-count scaling
        from risk.sizing import calculate_position_size

        volume = calculate_position_size(
            equity=equity,
            risk_percent=s.risk_percent_per_trade,
            stop_loss_distance=abs(signal.stop_loss - self._get_entry_estimate(signal))
            if signal.stop_loss
            else 0,
            symbol=symbol,
            use_fixed=s.use_fixed_lot,
            fixed_lot=s.fixed_lot_size,
            open_position_count=len(open_positions),
            max_concurrent=s.max_concurrent_positions,
            action=signal.action,
        )

        if volume <= 0:
            volume = s.fixed_lot_size  # Fallback to fixed lot
            warnings.append("Could not calculate position size, using minimum lot")

        if warnings:
            logger.warning(f"Risk warnings for {symbol} {signal.action}: {warnings}")

        reason = "All checks passed" if not warnings else f"{len(warnings)} warning(s)"

        return RiskDecision(
            approved=True,
            reason=reason,
            adjusted_volume=volume,
            warnings=warnings,
        )

    def _get_entry_estimate(self, signal: TradeSignal) -> float:
        """Estimate entry price from signal action and SL/TP positions."""
        if not signal.stop_loss or not signal.take_profit:
            return 0.0
        # For BUY: SL is below entry, TP is above → SL < entry < TP
        # For SELL: SL is above entry, TP is below → TP < entry < SL
        # Use the SL distance as the smaller leg (SL is always closer to entry than TP)
        if signal.action == "BUY":
            # Entry is above SL: entry = SL + sl_distance
            # We don't know sl_distance, but we know SL < TP
            # Estimate: entry ≈ SL + (TP - SL) * 0.33  (SL is ~1/3 of total range for 1:2 R:R)
            return signal.stop_loss + abs(signal.take_profit - signal.stop_loss) * 0.33
        elif signal.action == "SELL":
            # Entry is below SL: entry = SL - sl_distance
            return signal.stop_loss - abs(signal.stop_loss - signal.take_profit) * 0.33
        return (signal.stop_loss + signal.take_profit) / 2
