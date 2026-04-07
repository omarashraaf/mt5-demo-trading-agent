import logging
from typing import Literal
from pydantic import BaseModel, Field
from agent.interface import TradeSignal
from mt5.execution import PositionInfo

logger = logging.getLogger(__name__)


PolicyMode = Literal["safe", "balanced", "aggressive"]
GeminiRole = Literal["off", "advisory", "confirmation-required"]


DEFAULT_ALLOWED_SYMBOLS: list[str] = []


class UserPolicySettings(BaseModel):
    mode: PolicyMode = "balanced"
    allowed_symbols: list[str] = Field(default_factory=lambda: DEFAULT_ALLOWED_SYMBOLS.copy())
    max_risk_per_trade: float = 0.25
    max_daily_drawdown: float = 2.5
    max_open_trades: int = 3
    max_trades_per_symbol: int = 1
    max_margin_utilization: float = 12.0
    min_free_margin: float = 70.0
    min_reward_risk: float = 2.2
    allow_counter_trend_trades: bool = False
    allow_overnight_holding: bool = False
    gemini_role: GeminiRole = "confirmation-required"
    session_filters: list[str] = Field(default_factory=lambda: ["Europe", "US Open", "New York"])
    demo_only_default: bool = True


def build_policy_preset(mode: PolicyMode) -> UserPolicySettings:
    presets: dict[PolicyMode, UserPolicySettings] = {
        "safe": UserPolicySettings(
            mode="safe",
            max_risk_per_trade=0.25,
            max_daily_drawdown=2.5,
            max_open_trades=3,
            max_trades_per_symbol=1,
            max_margin_utilization=12.0,
            min_free_margin=70.0,
            min_reward_risk=2.2,
            allow_counter_trend_trades=False,
            allow_overnight_holding=False,
            gemini_role="confirmation-required",
            session_filters=["Europe", "US Open", "New York"],
            demo_only_default=True,
        ),
        "balanced": UserPolicySettings(
            mode="balanced",
            max_risk_per_trade=0.5,
            max_daily_drawdown=4.0,
            max_open_trades=5,
            max_trades_per_symbol=2,
            max_margin_utilization=18.0,
            min_free_margin=60.0,
            min_reward_risk=1.8,
            allow_counter_trend_trades=False,
            allow_overnight_holding=False,
            gemini_role="advisory",
            session_filters=["Europe", "US Open", "New York"],
            demo_only_default=True,
        ),
        "aggressive": UserPolicySettings(
            mode="aggressive",
            max_risk_per_trade=0.75,
            max_daily_drawdown=4.5,
            max_open_trades=8,
            max_trades_per_symbol=3,
            max_margin_utilization=30.0,
            min_free_margin=45.0,
            min_reward_risk=0.8,
            allow_counter_trend_trades=True,
            allow_overnight_holding=False,
            gemini_role="advisory",
            session_filters=["24/7"],
            demo_only_default=True,
        ),
    }
    return presets[mode].model_copy(deep=True)


def available_policy_presets() -> dict[str, dict]:
    return {
        mode: build_policy_preset(mode).model_dump()
        for mode in ("safe", "balanced", "aggressive")
    }


class RiskSettings(BaseModel):
    policy_mode: PolicyMode = "balanced"
    gemini_role: GeminiRole = "confirmation-required"
    allow_counter_trend_trades: bool = False
    allow_overnight_holding: bool = False
    session_filters: list[str] = Field(default_factory=list)
    demo_only_default: bool = True
    risk_percent_per_trade: float = 0.25
    max_daily_loss_percent: float = 3.0
    max_concurrent_positions: int = 6
    max_open_positions_total: int = 6
    max_positions_per_symbol: int = 1
    max_trades_per_symbol: int = 1
    cooldown_minutes_per_symbol: int = 120
    min_confidence_threshold: float = 0.60
    max_spread_threshold: float = 15.0
    min_reward_risk_ratio: float = 1.8
    max_margin_utilization_pct: float = 18.0
    min_free_margin_pct: float = 60.0
    max_sector_exposure_pct: float = 20.0
    max_usd_beta_exposure_pct: float = 18.0
    max_equity_exposure_pct_for_stocks: float = 25.0
    max_correlated_positions: int = 2
    # Preferred symbols: stocks (swing) + major forex (day) + commodities (swing)
    allowed_symbols: list[str] = Field(default_factory=lambda: DEFAULT_ALLOWED_SYMBOLS.copy())
    require_stop_loss: bool = True
    use_fixed_lot: bool = False
    fixed_lot_size: float = 0.01
    # Auto-trading settings
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.65
    auto_trade_scan_interval_seconds: int = 10


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    adjusted_volume: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class RiskEngine:
    def __init__(self):
        self.user_policy = build_policy_preset("balanced")
        self.auto_trade_enabled: bool = False
        self.auto_trade_scan_interval_seconds: int = 10
        self._daily_loss: float = 0.0
        self._daily_start_equity: float = 0.0
        self._panic_stop: bool = False

    def update_settings(self, settings: RiskSettings):
        self.user_policy = UserPolicySettings(
            mode=settings.policy_mode,
            allowed_symbols=list(settings.allowed_symbols),
            max_risk_per_trade=settings.risk_percent_per_trade,
            max_daily_drawdown=settings.max_daily_loss_percent,
            max_open_trades=settings.max_open_positions_total or settings.max_concurrent_positions,
            max_trades_per_symbol=settings.max_trades_per_symbol,
            max_margin_utilization=settings.max_margin_utilization_pct,
            min_free_margin=settings.min_free_margin_pct,
            min_reward_risk=settings.min_reward_risk_ratio,
            allow_counter_trend_trades=settings.allow_counter_trend_trades,
            allow_overnight_holding=settings.allow_overnight_holding,
            gemini_role=settings.gemini_role,
            session_filters=list(settings.session_filters),
            demo_only_default=settings.demo_only_default,
        )
        self.auto_trade_enabled = settings.auto_trade_enabled
        self.auto_trade_scan_interval_seconds = settings.auto_trade_scan_interval_seconds
        logger.info(f"Risk settings updated from runtime snapshot: {settings.model_dump()}")

    def update_user_policy(self, policy: UserPolicySettings):
        self.user_policy = self._normalize_policy(policy)
        logger.info(f"User policy updated: {self.user_policy.model_dump()}")

    def apply_policy_preset(self, mode: PolicyMode):
        self.user_policy = build_policy_preset(mode)
        logger.info("Applied user policy preset: %s", mode)

    @property
    def settings(self) -> RiskSettings:
        return self._compile_settings(self.user_policy)

    def set_daily_start_equity(self, equity: float):
        self._daily_start_equity = equity
        self._daily_loss = 0.0

    def set_panic_stop(self, active: bool):
        self._panic_stop = active
        logger.warning(f"Panic stop {'ACTIVATED' if active else 'deactivated'}")

    @property
    def panic_stopped(self) -> bool:
        return self._panic_stop

    def runtime_controls(self) -> dict:
        return {
            "auto_trade_enabled": self.auto_trade_enabled,
            "auto_trade_scan_interval_seconds": self.auto_trade_scan_interval_seconds,
            "panic_stop": self.panic_stopped,
        }

    def policy_presets(self) -> dict[str, dict]:
        return available_policy_presets()

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
        max_positions = s.max_open_positions_total or s.max_concurrent_positions
        if len(open_positions) >= max_positions:
            if is_auto_trade:
                return RiskDecision(
                    approved=False,
                    reason=f"Max {max_positions} positions reached. Waiting for exits.",
                )
            warnings.append(f"You have {len(open_positions)} positions open (max recommended: {max_positions})")

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
                    if rr_ratio < s.min_reward_risk_ratio and is_auto_trade:
                        return RiskDecision(
                            approved=False,
                            reason=f"Reward:Risk ratio {rr_ratio:.1f}:1 too low (need {s.min_reward_risk_ratio:.1f}:1 minimum). TP target not worth the risk.",
                        )
                    if rr_ratio < s.min_reward_risk_ratio:
                        warnings.append(f"Low R:R ratio ({rr_ratio:.1f}:1) - TP target may not justify the risk")

        # --- Warnings (never block) ---

        if s.allowed_symbols and symbol not in s.allowed_symbols:
            warnings.append(f"Symbol {symbol} is not in your usual watchlist")

        if signal.confidence < s.min_confidence_threshold:
            warnings.append(f"Low confidence ({signal.confidence:.0%}) - below {s.min_confidence_threshold:.0%} threshold")

        if spread > s.max_spread_threshold:
            warnings.append(
                f"High spread ({float(spread):.1f} pts) - trading costs may be higher"
            )

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
            max_concurrent=max_positions,
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

    def _normalize_policy(self, policy: UserPolicySettings) -> UserPolicySettings:
        normalized = policy.model_copy(deep=True)
        normalized.mode = normalized.mode or "balanced"
        normalized.allowed_symbols = [symbol.upper() for symbol in normalized.allowed_symbols if symbol]
        normalized.session_filters = normalized.session_filters or ["24/7"]
        return normalized

    def _compile_settings(self, policy: UserPolicySettings) -> RiskSettings:
        policy = self._normalize_policy(policy)
        mode_tuning = {
            "safe": {"min_confidence": 0.70, "max_spread": 12.0, "cooldown": 180, "auto_confidence": 0.72},
            "balanced": {"min_confidence": 0.60, "max_spread": 15.0, "cooldown": 120, "auto_confidence": 0.62},
            "aggressive": {"min_confidence": 0.45, "max_spread": 22.0, "cooldown": 30, "auto_confidence": 0.45},
        }[policy.mode]
        return RiskSettings(
            policy_mode=policy.mode,
            gemini_role=policy.gemini_role,
            allow_counter_trend_trades=policy.allow_counter_trend_trades,
            allow_overnight_holding=policy.allow_overnight_holding,
            session_filters=list(policy.session_filters),
            demo_only_default=policy.demo_only_default,
            risk_percent_per_trade=policy.max_risk_per_trade,
            max_daily_loss_percent=policy.max_daily_drawdown,
            max_concurrent_positions=policy.max_open_trades,
            max_open_positions_total=policy.max_open_trades,
            # Keep per-symbol open-position cap aligned with user policy/strategy.
            max_positions_per_symbol=max(1, int(policy.max_trades_per_symbol)),
            max_trades_per_symbol=policy.max_trades_per_symbol,
            cooldown_minutes_per_symbol=mode_tuning["cooldown"],
            min_confidence_threshold=mode_tuning["min_confidence"],
            max_spread_threshold=mode_tuning["max_spread"],
            min_reward_risk_ratio=policy.min_reward_risk,
            max_margin_utilization_pct=policy.max_margin_utilization,
            min_free_margin_pct=policy.min_free_margin,
            max_sector_exposure_pct=20.0,
            max_usd_beta_exposure_pct=18.0,
            max_equity_exposure_pct_for_stocks=25.0,
            max_correlated_positions=2 if policy.mode != "aggressive" else 3,
            allowed_symbols=list(policy.allowed_symbols),
            require_stop_loss=True,
            use_fixed_lot=False,
            fixed_lot_size=0.01,
            auto_trade_enabled=self.auto_trade_enabled,
            auto_trade_min_confidence=mode_tuning["auto_confidence"],
            auto_trade_scan_interval_seconds=self.auto_trade_scan_interval_seconds,
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
