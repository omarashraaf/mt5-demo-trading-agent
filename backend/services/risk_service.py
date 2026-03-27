from __future__ import annotations

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

        risk_decision = self.risk_engine.evaluate(
            signal=signal.to_trade_signal(),
            symbol=context.symbol,
            spread=context.tick.get("spread", 999.0) if context.tick else 999.0,
            equity=context.account_equity,
            open_positions=[] if self.execution_engine is None else self.execution_engine.get_positions(),
            is_auto_trade=evaluation_mode == "auto",
            entry_price=live_entry_price,
        )
        risk_evaluation = RiskEvaluation.from_risk_decision(risk_decision, evaluation_mode)
        fitted_volume, fit_reason = self.portfolio_risk_service.fit_volume_to_margin_limits(
            context=context,
            proposed_volume=risk_evaluation.adjusted_volume,
            entry_price=live_entry_price,
            settings=self.risk_engine.settings,
        )
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

        reasons: list[str] = []
        auto_confidence_reason = self._auto_confidence_gate(
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
        if anti_churn.blocked:
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
        base_required = float(settings.auto_trade_min_confidence)
        floor_required = float(settings.min_confidence_threshold)

        # Let exceptional trade quality earn a modest confidence discount,
        # but never below the mode's base technical floor.
        required = base_required
        if trade_quality.final_trade_quality_score >= max(trade_quality.threshold + 0.08, 0.82):
            required = max(floor_required, base_required - 0.03)

        if signal.confidence < required:
            return (
                f"Auto-trade confidence {signal.confidence:.0%} is below the required "
                f"{required:.0%} for this setup."
            )
        return None

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        if action == "BUY":
            return float(context.tick.get("ask", 0.0))
        if action == "SELL":
            return float(context.tick.get("bid", 0.0))
        return 0.0
