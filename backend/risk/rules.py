import logging
from typing import Any
from typing import Literal
from pydantic import BaseModel, Field
from agent.interface import TradeSignal
from mt5.execution import PositionInfo

logger = logging.getLogger(__name__)


PolicyMode = Literal["safe", "balanced", "aggressive", "custom"]
GeminiRole = Literal["off", "advisory", "confirmation-required"]


DEFAULT_ALLOWED_SYMBOLS: list[str] = []


class UserPolicySettings(BaseModel):
    mode: PolicyMode = "balanced"
    allowed_symbols: list[str] = Field(default_factory=lambda: DEFAULT_ALLOWED_SYMBOLS.copy())
    max_risk_per_trade: float = 0.25
    max_daily_drawdown: float = 2.5
    max_weekly_loss: float = 6.0
    max_drawdown: float = 10.0
    max_open_trades: int = 3
    max_trades_per_symbol: int = 1
    max_correlated_exposure: int = 2
    max_lot_size: float = 0.10
    max_leverage_exposure: float = 2.0
    max_trades_per_day: int = 6
    max_margin_utilization: float = 12.0
    min_free_margin: float = 70.0
    min_reward_risk: float = 2.2
    max_spread: float = 12.0
    min_confidence: float = 0.70
    allowed_asset_classes: list[str] = Field(default_factory=lambda: ["Forex", "Indices", "Commodities"])
    news_blackout_behavior: Literal["block", "reduce_size", "warn"] = "block"
    auto_trading_allowed: bool = False
    live_execution_allowed: bool = False
    allow_counter_trend_trades: bool = False
    allow_overnight_holding: bool = False
    gemini_role: GeminiRole = "confirmation-required"
    force_execute_any_conditions: bool = False
    session_filters: list[str] = Field(default_factory=lambda: ["Europe", "US Open", "New York"])
    demo_only_default: bool = True


def build_policy_preset(mode: PolicyMode) -> UserPolicySettings:
    presets: dict[PolicyMode, UserPolicySettings] = {
        "safe": UserPolicySettings(
            mode="safe",
            max_risk_per_trade=0.25,
            max_daily_drawdown=2.5,
            max_weekly_loss=6.0,
            max_drawdown=10.0,
            max_open_trades=3,
            max_trades_per_symbol=1,
            max_correlated_exposure=2,
            max_lot_size=0.05,
            max_leverage_exposure=1.0,
            max_trades_per_day=4,
            max_margin_utilization=12.0,
            min_free_margin=70.0,
            min_reward_risk=2.2,
            max_spread=12.0,
            min_confidence=0.70,
            allowed_asset_classes=["Forex", "Indices", "Commodities"],
            news_blackout_behavior="block",
            auto_trading_allowed=False,
            live_execution_allowed=False,
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
            max_weekly_loss=10.0,
            max_drawdown=16.0,
            max_open_trades=5,
            max_trades_per_symbol=2,
            max_correlated_exposure=3,
            max_lot_size=0.10,
            max_leverage_exposure=2.0,
            max_trades_per_day=8,
            max_margin_utilization=18.0,
            min_free_margin=60.0,
            min_reward_risk=1.8,
            max_spread=15.0,
            min_confidence=0.60,
            allowed_asset_classes=["Forex", "Indices", "Commodities", "Stocks"],
            news_blackout_behavior="block",
            auto_trading_allowed=True,
            live_execution_allowed=True,
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
            max_weekly_loss=12.0,
            max_drawdown=20.0,
            max_open_trades=12,
            max_trades_per_symbol=4,
            max_correlated_exposure=6,
            max_lot_size=0.25,
            max_leverage_exposure=3.0,
            max_trades_per_day=18,
            max_margin_utilization=55.0,
            min_free_margin=35.0,
            min_reward_risk=1.6,
            max_spread=22.0,
            min_confidence=0.45,
            allowed_asset_classes=["Forex", "Indices", "Commodities", "Stocks", "Crypto"],
            news_blackout_behavior="warn",
            auto_trading_allowed=True,
            live_execution_allowed=True,
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
    force_execute_any_conditions: bool = False
    allow_counter_trend_trades: bool = False
    allow_overnight_holding: bool = False
    session_filters: list[str] = Field(default_factory=list)
    demo_only_default: bool = True
    risk_percent_per_trade: float = 0.25
    max_daily_loss_percent: float = 3.0
    max_weekly_loss_percent: float = 6.0
    max_drawdown_percent: float = 10.0
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
    max_lot_size: float = 0.10
    max_leverage_exposure: float = 2.0
    max_trades_per_day: int = 6
    allowed_asset_classes: list[str] = Field(default_factory=lambda: ["Forex", "Indices", "Commodities"])
    news_blackout_behavior: Literal["block", "reduce_size", "warn"] = "block"
    auto_trading_allowed: bool = False
    live_execution_allowed: bool = False
    # Preferred symbols: stocks (swing) + major forex (day) + commodities (swing)
    allowed_symbols: list[str] = Field(default_factory=lambda: DEFAULT_ALLOWED_SYMBOLS.copy())
    require_stop_loss: bool = True
    use_fixed_lot: bool = False
    fixed_lot_size: float = 0.01
    # Auto-trading settings
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.65
    auto_trade_scan_interval_seconds: int = 10
    auto_trade_budget_mode: Literal["full_margin", "custom_budget"] = "full_margin"
    auto_trade_budget_usd: float | None = None


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    adjusted_volume: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class RiskEngine:
    def __init__(self):
        self.user_policy = build_policy_preset("balanced")
        self.auto_trade_enabled: bool = False
        self.auto_trade_session_armed: bool = False
        self.auto_trade_scan_interval_seconds: int = 10
        self.auto_trade_budget_mode: Literal["full_margin", "custom_budget"] = "full_margin"
        self.auto_trade_budget_usd: float | None = None
        self.active_strategy_context: dict[str, Any] = {}
        self._daily_loss: float = 0.0
        self._daily_start_equity: float = 0.0
        self._panic_stop: bool = False

    def update_settings(self, settings: RiskSettings):
        self.user_policy = UserPolicySettings(
            mode=settings.policy_mode,
            allowed_symbols=list(settings.allowed_symbols),
            max_risk_per_trade=settings.risk_percent_per_trade,
            max_daily_drawdown=settings.max_daily_loss_percent,
            max_weekly_loss=settings.max_weekly_loss_percent,
            max_drawdown=settings.max_drawdown_percent,
            max_open_trades=settings.max_open_positions_total or settings.max_concurrent_positions,
            max_trades_per_symbol=settings.max_trades_per_symbol,
            max_correlated_exposure=settings.max_correlated_positions,
            max_lot_size=settings.max_lot_size,
            max_leverage_exposure=settings.max_leverage_exposure,
            max_trades_per_day=settings.max_trades_per_day,
            max_margin_utilization=settings.max_margin_utilization_pct,
            min_free_margin=settings.min_free_margin_pct,
            min_reward_risk=settings.min_reward_risk_ratio,
            max_spread=settings.max_spread_threshold,
            min_confidence=settings.min_confidence_threshold,
            allowed_asset_classes=list(settings.allowed_asset_classes),
            news_blackout_behavior=settings.news_blackout_behavior,
            auto_trading_allowed=settings.auto_trading_allowed,
            live_execution_allowed=settings.live_execution_allowed,
            allow_counter_trend_trades=settings.allow_counter_trend_trades,
            allow_overnight_holding=settings.allow_overnight_holding,
            gemini_role=settings.gemini_role,
            force_execute_any_conditions=settings.force_execute_any_conditions,
            session_filters=list(settings.session_filters),
            demo_only_default=settings.demo_only_default,
        )
        self.auto_trade_enabled = settings.auto_trade_enabled
        self.auto_trade_session_armed = False
        self.auto_trade_scan_interval_seconds = settings.auto_trade_scan_interval_seconds
        self.auto_trade_budget_mode = str(getattr(settings, "auto_trade_budget_mode", "full_margin") or "full_margin")  # type: ignore[assignment]
        raw_budget = getattr(settings, "auto_trade_budget_usd", None)
        self.auto_trade_budget_usd = float(raw_budget) if raw_budget is not None else None
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
            "auto_trade_session_armed": self.auto_trade_session_armed,
            "auto_trade_scan_interval_seconds": self.auto_trade_scan_interval_seconds,
            "auto_trade_budget_mode": self.auto_trade_budget_mode,
            "auto_trade_budget_usd": self.auto_trade_budget_usd,
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
        def _pos_field(position: PositionInfo | dict, key: str, default=None):
            if isinstance(position, dict):
                return position.get(key, default)
            return getattr(position, key, default)

        max_positions = s.max_open_positions_total or s.max_concurrent_positions
        if len(open_positions) >= max_positions:
            return RiskDecision(
                approved=False,
                reason=f"Open position hard cap reached ({len(open_positions)}/{max_positions}).",
            )

        same_symbol_positions = [
            p for p in open_positions
            if str(_pos_field(p, "symbol", "") or "").upper() == str(symbol or "").upper()
        ]
        if len(same_symbol_positions) >= max(1, int(s.max_positions_per_symbol or 1)):
            return RiskDecision(
                approved=False,
                reason=f"Per-symbol hard cap reached for {symbol} ({len(same_symbol_positions)}/{s.max_positions_per_symbol}).",
            )

        # Daily loss — hard block in auto-trade
        if self._daily_start_equity > 0:
            current_loss_pct = (
                (self._daily_start_equity - equity) / self._daily_start_equity * 100
            )
            if current_loss_pct >= s.max_daily_loss_percent:
                return RiskDecision(
                    approved=False,
                    reason=f"Daily loss {current_loss_pct:.1f}% hit hard limit ({s.max_daily_loss_percent}%).",
                )

        # --- Reward-to-Risk check (hard block for auto-trade) ---
        if signal.stop_loss and signal.take_profit:
            entry_est = entry_price if entry_price > 0 else self._get_entry_estimate(signal)
            if entry_est > 0:
                sl_distance = abs(entry_est - signal.stop_loss)
                tp_distance = abs(signal.take_profit - entry_est)
                if sl_distance > 0:
                    rr_ratio = tp_distance / sl_distance
                    logger.info(f"R:R check {symbol} {signal.action}: entry={entry_est:.5f} SL={signal.stop_loss:.5f} TP={signal.take_profit:.5f} => RR={rr_ratio:.2f}:1 (entry_price_param={entry_price:.5f})")
                    if rr_ratio < s.min_reward_risk_ratio:
                        return RiskDecision(
                            approved=False,
                            reason=f"Reward:Risk ratio {rr_ratio:.1f}:1 too low (need {s.min_reward_risk_ratio:.1f}:1 minimum). TP target not worth the risk.",
                        )
        elif s.require_stop_loss:
            return RiskDecision(
                approved=False,
                reason="Stop-loss and take-profit are required by the active risk profile.",
            )

        # --- Additional hard policy gates ---

        if s.allowed_symbols and symbol not in s.allowed_symbols:
            warnings.append(f"Symbol {symbol} is not in your usual watchlist")

        try:
            signal_confidence = float(signal.confidence)
        except Exception:
            signal_confidence = 0.0
        if signal_confidence < s.min_confidence_threshold:
            return RiskDecision(
                approved=False,
                reason=f"Confidence {signal_confidence:.0%} is below hard minimum {s.min_confidence_threshold:.0%}.",
            )

        if spread > s.max_spread_threshold:
            return RiskDecision(
                approved=False,
                reason=f"Spread {float(spread):.1f} exceeds hard maximum {s.max_spread_threshold:.1f}.",
            )

        if s.require_stop_loss and signal.stop_loss is None:
            return RiskDecision(
                approved=False,
                reason="Stop loss is required by the active risk profile.",
            )

        # Prevent averaging down/up in auto-trading when the current
        # same-direction position is already losing. This was previously a
        # warning-only path, which allowed the execution layer to keep
        # compounding a bad thesis.
        for pos in same_symbol_positions:
            pos_profit = float(_pos_field(pos, "profit", 0.0) or 0.0)
            pos_type = str(_pos_field(pos, "type", "") or "").upper()
            if pos_profit < 0:
                if (signal.action == "BUY" and pos_type == "BUY") or (
                    signal.action == "SELL" and pos_type == "SELL"
                ):
                    message = f"Adding to a losing {pos_type} position on {symbol} ({pos_profit:.2f})"
                    if is_auto_trade:
                        return RiskDecision(
                            approved=False,
                            reason=message,
                            adjusted_volume=0.0,
                            warnings=[message],
                        )
                    warnings.append(message)

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
        volume = min(float(volume), float(s.max_lot_size or volume))

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
        normalized.allowed_asset_classes = [
            str(asset).strip()
            for asset in normalized.allowed_asset_classes
            if str(asset).strip()
        ] or ["Forex", "Indices", "Commodities"]
        normalized.session_filters = normalized.session_filters or ["24/7"]
        # Lift legacy aggressive profiles that were saved with an older 30% cap.
        if normalized.mode == "aggressive":
            if float(normalized.max_margin_utilization or 0.0) < 55.0:
                normalized.max_margin_utilization = 55.0
            if int(normalized.max_open_trades or 0) < 12:
                normalized.max_open_trades = 12
            if int(normalized.max_trades_per_symbol or 0) < 4:
                normalized.max_trades_per_symbol = 4
            if float(normalized.min_free_margin or 0.0) > 35.0:
                normalized.min_free_margin = 35.0
        return normalized

    def _compile_settings(self, policy: UserPolicySettings) -> RiskSettings:
        policy = self._normalize_policy(policy)
        tuning_mode = policy.mode if policy.mode in {"safe", "balanced", "aggressive"} else "balanced"
        mode_tuning = {
            "safe": {"min_confidence": 0.70, "max_spread": 12.0, "cooldown": 180, "auto_confidence": 0.72},
            "balanced": {"min_confidence": 0.60, "max_spread": 15.0, "cooldown": 120, "auto_confidence": 0.62},
            "aggressive": {"min_confidence": 0.45, "max_spread": 22.0, "cooldown": 30, "auto_confidence": 0.45},
        }[tuning_mode]
        return RiskSettings(
            policy_mode=policy.mode,
            gemini_role=policy.gemini_role,
            force_execute_any_conditions=policy.force_execute_any_conditions,
            allow_counter_trend_trades=policy.allow_counter_trend_trades,
            allow_overnight_holding=policy.allow_overnight_holding,
            session_filters=list(policy.session_filters),
            demo_only_default=policy.demo_only_default,
            risk_percent_per_trade=policy.max_risk_per_trade,
            max_daily_loss_percent=policy.max_daily_drawdown,
            max_weekly_loss_percent=policy.max_weekly_loss,
            max_drawdown_percent=policy.max_drawdown,
            max_concurrent_positions=policy.max_open_trades,
            max_open_positions_total=policy.max_open_trades,
            # Keep per-symbol open-position cap aligned with user policy/strategy.
            max_positions_per_symbol=max(1, int(policy.max_trades_per_symbol)),
            max_trades_per_symbol=policy.max_trades_per_symbol,
            cooldown_minutes_per_symbol=mode_tuning["cooldown"],
            min_confidence_threshold=policy.min_confidence,
            max_spread_threshold=policy.max_spread,
            min_reward_risk_ratio=policy.min_reward_risk,
            max_margin_utilization_pct=policy.max_margin_utilization,
            min_free_margin_pct=policy.min_free_margin,
            max_sector_exposure_pct=30.0 if policy.mode == "aggressive" else 20.0,
            max_usd_beta_exposure_pct=28.0 if policy.mode == "aggressive" else 18.0,
            max_equity_exposure_pct_for_stocks=25.0,
            max_correlated_positions=policy.max_correlated_exposure,
            max_lot_size=policy.max_lot_size,
            max_leverage_exposure=policy.max_leverage_exposure,
            max_trades_per_day=policy.max_trades_per_day,
            allowed_asset_classes=list(policy.allowed_asset_classes),
            news_blackout_behavior=policy.news_blackout_behavior,
            auto_trading_allowed=policy.auto_trading_allowed,
            live_execution_allowed=policy.live_execution_allowed,
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
