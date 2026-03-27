from __future__ import annotations

from domain.models import (
    GeminiAssessment,
    MarketContext,
    TechnicalSignal,
    TradeDecisionAssessment,
)
from services.trade_quality_service import TradeQualityService


class TradeDecisionService:
    """Combines deterministic technical ideas with bounded Gemini news analysis.

    Gemini can confirm, weaken, or flag contradictions. It cannot create an
    opposite-side executable trade on its own.
    """

    def __init__(self, trade_quality_service: TradeQualityService | None = None):
        self.trade_quality_service = trade_quality_service or TradeQualityService()

    def decide(
        self,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        *,
        portfolio_fit_score: float = 0.55,
        threshold_boost: float = 0.0,
    ) -> TradeDecisionAssessment:
        user_policy = context.user_policy or {}
        gemini_role = str(user_policy.get("gemini_role", "advisory")).lower()
        effective_assessment = None if gemini_role == "off" else gemini_assessment
        adjusted_signal = self._apply_news_advisory(technical_signal, effective_assessment)
        quality = self.trade_quality_service.assess(
            context=context,
            signal=adjusted_signal,
            gemini_assessment=effective_assessment,
            portfolio_fit_score=portfolio_fit_score,
            threshold_boost=threshold_boost,
        )

        reasons: list[str] = []
        if technical_signal.reason:
            reasons.append(technical_signal.reason)
        if effective_assessment and effective_assessment.summary_reason:
            reasons.append(effective_assessment.summary_reason)

        if adjusted_signal.action not in {"BUY", "SELL"}:
            final_signal = adjusted_signal.model_copy(update={"action": "HOLD"})
            reasons.append("Technical structure did not produce an executable direction.")
            return TradeDecisionAssessment(
                trade=False,
                final_direction="HOLD",
                final_signal=final_signal,
                trade_quality_assessment=quality,
                reasons=reasons,
            )

        if gemini_role == "confirmation-required":
            if not effective_assessment or not effective_assessment.used or effective_assessment.degraded:
                final_signal = adjusted_signal.model_copy(deep=True)
                final_signal.action = "HOLD"
                reasons.append("User policy requires Gemini confirmation, but Gemini was unavailable.")
                return TradeDecisionAssessment(
                    trade=False,
                    final_direction="HOLD",
                    final_signal=final_signal,
                    trade_quality_assessment=quality,
                    reasons=list(dict.fromkeys(reasons)),
                )
            if not effective_assessment.confirmed or effective_assessment.contradiction_flag:
                final_signal = adjusted_signal.model_copy(deep=True)
                final_signal.action = "HOLD"
                reasons.append("User policy requires explicit Gemini confirmation for this trade.")
                return TradeDecisionAssessment(
                    trade=False,
                    final_direction="HOLD",
                    final_signal=final_signal,
                    trade_quality_assessment=quality,
                    reasons=list(dict.fromkeys(reasons)),
                )

        if quality.no_trade_zone:
            final_signal = adjusted_signal.model_copy(deep=True)
            final_signal.action = "HOLD"
            final_signal.reason = f"{adjusted_signal.reason} Blocked: {' '.join(quality.no_trade_reasons)}"
            reasons.extend(quality.no_trade_reasons)
            return TradeDecisionAssessment(
                trade=False,
                final_direction="HOLD",
                final_signal=final_signal,
                trade_quality_assessment=quality,
                reasons=list(dict.fromkeys(reasons)),
            )

        return TradeDecisionAssessment(
            trade=True,
            final_direction=adjusted_signal.action,
            final_signal=adjusted_signal,
            trade_quality_assessment=quality,
            reasons=list(dict.fromkeys(reasons)),
        )

    def _apply_news_advisory(
        self,
        technical_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
    ) -> TechnicalSignal:
        if not gemini_assessment or not gemini_assessment.used:
            return technical_signal.model_copy(deep=True)

        final_signal = technical_signal.model_copy(deep=True)
        final_signal.confidence = round(
            max(0.0, min(0.95, final_signal.confidence + gemini_assessment.confidence_adjustment)),
            2,
        )

        metadata = dict(final_signal.metadata)
        metadata["gemini_assessment"] = gemini_assessment.model_dump()
        metadata["technical_confidence"] = round(technical_signal.confidence, 3)
        final_signal.metadata = metadata

        # Gemini is advisory only. It may weaken confidence or warn, but it does
        # not create a reverse-direction trade.
        if gemini_assessment.degraded:
            final_signal.reason = (
                f"{final_signal.reason} Gemini news analysis degraded; using technical logic only."
            )
        elif gemini_assessment.contradiction_flag:
            final_signal.reason = f"{final_signal.reason} News flow contradicts the technical thesis."
        elif gemini_assessment.news_bias != "neutral":
            final_signal.reason = (
                f"{final_signal.reason} News flow is {gemini_assessment.news_bias} for this setup."
            )

        return final_signal
