from __future__ import annotations

import math
from types import SimpleNamespace

from domain.models import (
    MarketContext,
    RiskApprovalDecision,
    RiskEvaluation,
    TechnicalSignal,
    TradeQualityAssessment,
)
from services.anti_churn_service import AntiChurnService
from services.portfolio_risk_service import PortfolioRiskService


class RiskService:
    """Final deterministic approval gate before execution."""

    def __init__(
        self,
        risk_engine,
        portfolio_risk_service: PortfolioRiskService,
        anti_churn_service: AntiChurnService,
        execution_engine=None,
    ):
        self.risk_engine = risk_engine
        self.portfolio_risk_service = portfolio_risk_service
        self.anti_churn_service = anti_churn_service
        self.execution_engine = execution_engine

    def preview_portfolio_fit(self, context: MarketContext) -> float:
        account = SimpleNamespace(
            equity=context.account_equity,
            margin=context.account_margin,
            free_margin=context.account_free_margin,
        )
        snapshot = self.portfolio_risk_service.snapshot(account, context.all_open_positions)
        margin_score = max(0.0, min(1.0, 1.0 - snapshot["margin_utilization_pct"] / 100.0))
        free_margin_score = max(0.0, min(1.0, snapshot["free_margin_pct"] / 100.0))
        position_score = max(0.0, min(1.0, 1.0 - snapshot["open_positions_total"] / max(self.risk_engine.settings.max_open_positions_total or 1, 1)))
        return round(margin_score * 0.4 + free_margin_score * 0.4 + position_score * 0.2, 3)

    def assess(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        trade_quality: TradeQualityAssessment,
        evaluation_mode: str,
        recent_outcomes: list[dict] | None = None,
        recent_evaluations: list[dict] | None = None,
        scan_window_id: str | None = None,
        entry_price: float | None = None,
    ) -> RiskApprovalDecision:
        live_entry_price = entry_price if entry_price is not None else self._entry_price(context, signal.action)
        live_entry_reason = self._live_entry_block_reason(
            signal=signal,
            evaluation_mode=evaluation_mode,
            entry_price=live_entry_price,
        )
        protective_targets_reason = self._protective_targets_block_reason(
            signal=signal,
            evaluation_mode=evaluation_mode,
        )

        context_positions = list(context.all_open_positions or [])
        live_positions = (
            context_positions
            if context_positions
            else ([] if self.execution_engine is None else self.execution_engine.get_positions())
        )

        risk_decision = self.risk_engine.evaluate(
            signal=self._risk_engine_trade_signal(signal),
            symbol=context.symbol,
            spread=context.tick.get("spread", 999.0) if context.tick else 999.0,
            equity=context.account_equity,
            open_positions=live_positions,
            is_auto_trade=evaluation_mode == "auto",
            entry_price=live_entry_price,
        )
        risk_evaluation = RiskEvaluation.from_risk_decision(risk_decision, evaluation_mode)
        executable_signal = str(signal.action or "").upper() in {"BUY", "SELL"}
        if executable_signal and hasattr(self.portfolio_risk_service, "fit_volume_to_margin_limits"):
            fitted_volume, fit_reason = self.portfolio_risk_service.fit_volume_to_margin_limits(
                context=context,
                proposed_volume=risk_evaluation.adjusted_volume,
                entry_price=live_entry_price,
                settings=self.risk_engine.settings,
            )
        elif executable_signal:
            # Backward compatibility: older/mocked portfolio services may not expose
            # margin-fit scaling yet, so keep the proposed volume unchanged.
            fitted_volume, fit_reason = risk_evaluation.adjusted_volume, None
        else:
            fitted_volume, fit_reason = 0.0, None
        if fit_reason and fitted_volume > 0:
            risk_evaluation.warnings.append(fit_reason)
        if fit_reason and fitted_volume <= 0:
            risk_evaluation.approved = False
            risk_evaluation.reason = fit_reason
        risk_evaluation.adjusted_volume = fitted_volume

        portfolio_risk = self.portfolio_risk_service.assess(
            context=context,
            signal=signal,
            proposed_volume=risk_evaluation.adjusted_volume,
            entry_price=live_entry_price,
            settings=self.risk_engine.settings,
            recent_outcomes=recent_outcomes,
            is_auto_trade=evaluation_mode == "auto",
        )
        anti_churn = self.anti_churn_service.assess(
            context=context,
            signal=signal,
            trade_quality=trade_quality,
            cooldown_minutes=self.risk_engine.settings.cooldown_minutes_per_symbol,
            max_trades_per_symbol=self.risk_engine.settings.max_trades_per_symbol,
            recent_outcomes=recent_outcomes,
            recent_evaluations=recent_evaluations,
            scan_window_id=scan_window_id,
        )
        force_execute_any_conditions = bool((context.user_policy or {}).get("force_execute_any_conditions", False))

        reasons: list[str] = []
        if live_entry_reason:
            reasons.append(live_entry_reason)
        if protective_targets_reason:
            reasons.append(protective_targets_reason)
        auto_confidence_reason = None if force_execute_any_conditions else self._auto_confidence_gate(
            signal=signal,
            trade_quality=trade_quality,
            evaluation_mode=evaluation_mode,
        )
        if auto_confidence_reason:
            reasons.append(auto_confidence_reason)
        if not risk_evaluation.approved:
            reasons.append(risk_evaluation.reason)
        if not portfolio_risk.allow_execute:
            reasons.extend(portfolio_risk.blocking_reasons)
        if anti_churn.blocked and not force_execute_any_conditions:
            reasons.extend(anti_churn.reasons)

        risk_evaluation.approved = risk_evaluation.approved and not reasons
        risk_evaluation.reason = "Risk approved." if not reasons else reasons[0]
        risk_evaluation.status = "pass" if not reasons else "block"
        risk_evaluation.warnings = list(dict.fromkeys(risk_evaluation.warnings + portfolio_risk.warnings))
        risk_evaluation.machine_reasons = list(dict.fromkeys(risk_evaluation.machine_reasons + reasons))
        risk_evaluation.metrics_snapshot = {
            **portfolio_risk.metrics_snapshot,
            "trade_quality_score": trade_quality.final_trade_quality_score,
            "trade_quality_threshold": trade_quality.threshold,
            "anti_churn_threshold_boost": anti_churn.threshold_boost,
        }

        return RiskApprovalDecision(
            approved=not reasons,
            reasons=list(dict.fromkeys(reasons)),
            risk_evaluation=risk_evaluation,
            portfolio_risk_assessment=portfolio_risk,
            anti_churn_assessment=anti_churn,
        )

    def _live_entry_block_reason(
        self,
        *,
        signal: TechnicalSignal,
        evaluation_mode: str,
        entry_price: float,
    ) -> str | None:
        if evaluation_mode != "auto" or signal.action not in {"BUY", "SELL"}:
            return None
        parsed_entry = self._safe_float(entry_price, default=0.0)
        if math.isfinite(parsed_entry) and parsed_entry > 0:
            return None
        return "Auto-trade blocked because no valid live entry price is available."

    def _protective_targets_block_reason(
        self,
        *,
        signal: TechnicalSignal,
        evaluation_mode: str,
    ) -> str | None:
        if evaluation_mode != "auto" or signal.action not in {"BUY", "SELL"}:
            return None
        stop_loss = self._safe_float(signal.stop_loss, default=0.0)
        take_profit = self._safe_float(signal.take_profit, default=0.0)
        if (
            math.isfinite(stop_loss)
            and math.isfinite(take_profit)
            and stop_loss > 0.0
            and take_profit > 0.0
        ):
            return None
        return "Auto-trade blocked because stop-loss and take-profit are required."

    def _auto_confidence_gate(
        self,
        *,
        signal: TechnicalSignal,
        trade_quality: TradeQualityAssessment,
        evaluation_mode: str,
    ) -> str | None:
        if evaluation_mode != "auto" or signal.action not in {"BUY", "SELL"}:
            return None

        settings = self.risk_engine.settings
        base_required = self._safe_float(settings.auto_trade_min_confidence, default=0.72)
        floor_required = self._safe_float(settings.min_confidence_threshold, default=0.7)
        if not math.isfinite(base_required) or base_required <= 0:
            base_required = 0.72
        if not math.isfinite(floor_required) or floor_required <= 0:
            floor_required = 0.7
        signal_confidence = self._safe_float(signal.confidence, default=float("nan"))
        if not math.isfinite(signal_confidence):
            return "Auto-trade confidence is invalid for this setup."

        # Let exceptional trade quality earn a modest confidence discount,
        # but never below the mode's base technical floor.
        required = base_required
        if trade_quality.final_trade_quality_score >= max(trade_quality.threshold + 0.08, 0.82):
            required = max(floor_required, base_required - 0.03)

        if signal_confidence < required:
            return (
                f"Auto-trade confidence {signal_confidence:.0%} is below the required "
                f"{required:.0%} for this setup."
            )
        return None

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        try:
            if action == "BUY":
                price = float(context.tick.get("ask", 0.0))
            elif action == "SELL":
                price = float(context.tick.get("bid", 0.0))
            else:
                return 0.0
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(price):
            return 0.0
        return price

    def _risk_engine_trade_signal(self, signal: TechnicalSignal):
        safe_signal = signal.model_copy(
            update={
                "confidence": self._safe_float(signal.confidence, default=0.0),
                "stop_loss": self._optional_positive_float(signal.stop_loss),
                "take_profit": self._optional_positive_float(signal.take_profit),
            },
            deep=True,
        )
        return safe_signal.to_trade_signal()

    def _optional_positive_float(self, value):
        parsed = self._safe_float(value, default=float("nan"))
        return parsed if math.isfinite(parsed) and parsed > 0.0 else None

    def _safe_float(self, value, default: float = float("nan")) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if math.isfinite(parsed) else default
