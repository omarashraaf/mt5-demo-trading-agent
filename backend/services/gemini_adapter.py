from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from agent.interface import AgentInput, TradeSignal
from domain.models import GeminiAssessment, TechnicalSignal

logger = logging.getLogger(__name__)


class GeminiAdapter:
    def __init__(
        self,
        gemini_agent,
        timeout_seconds: float = 12.0,
        max_retries: int = 1,
    ):
        self.gemini_agent = gemini_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._last_error: Optional[str] = None
        self._last_failure_at: float = 0.0

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def last_failure_at(self) -> float:
        return self._last_failure_at

    @property
    def degraded(self) -> bool:
        return bool(self._last_error)

    def _mark_failure(self, error: Exception | str):
        self._last_error = str(error)
        self._last_failure_at = time.time()

    def _clear_failure(self):
        self._last_error = None

    async def confirm(
        self,
        input_data: AgentInput,
        primary_signal: TechnicalSignal,
    ) -> GeminiAssessment:
        if self.gemini_agent is None:
            return GeminiAssessment(
                used=False,
                available=False,
                summary_reason="Gemini advisor not registered.",
                reason="Gemini advisor not registered.",
            )

        if hasattr(self.gemini_agent, "available") and not self.gemini_agent.available:
            return GeminiAssessment(
                used=False,
                available=False,
                summary_reason=getattr(self.gemini_agent, "unavailable_reason", "Gemini advisor unavailable."),
                reason=getattr(self.gemini_agent, "unavailable_reason", "Gemini advisor unavailable."),
            )

        attempts = max(1, self.max_retries + 1)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                assessment = await asyncio.wait_for(
                    asyncio.to_thread(self._call_agent, input_data, primary_signal),
                    timeout=self.timeout_seconds,
                )
                self._clear_failure()
                return assessment
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning(
                    "Gemini confirmation timed out after %.1fs (attempt %s/%s)",
                    self.timeout_seconds,
                    attempt,
                    attempts,
                )
            except Exception as exc:  # pragma: no cover - exercised via tests
                last_error = exc
                logger.warning(
                    "Gemini confirmation failed (attempt %s/%s): %s",
                    attempt,
                    attempts,
                    exc,
                )

        self._mark_failure(last_error or "Gemini confirmation failed")
        return GeminiAssessment(
            used=True,
            available=False,
            degraded=True,
            summary_reason="Proceeding with deterministic technical logic only.",
            reason="Proceeding with deterministic technical logic only.",
            error=str(last_error) if last_error else "Gemini confirmation failed",
        )

    def _call_agent(
        self,
        input_data: AgentInput,
        primary_signal: TechnicalSignal,
    ) -> GeminiAssessment:
        if hasattr(self.gemini_agent, "assess"):
            result = self.gemini_agent.assess(
                input_data,
                technical_signal=primary_signal.to_trade_signal(),
            )
            if isinstance(result, GeminiAssessment):
                return result
            if hasattr(result, "model_dump"):
                return GeminiAssessment(**result.model_dump())
            if isinstance(result, dict):
                return GeminiAssessment(**result)

        trade_signal = self.gemini_agent.evaluate(input_data)
        if not isinstance(trade_signal, TradeSignal):
            raise TypeError("Gemini fallback evaluate() must return TradeSignal")
        return self._assessment_from_trade_signal(trade_signal, primary_signal)

    def _assessment_from_trade_signal(
        self,
        advisory_signal: TradeSignal,
        primary_signal: TechnicalSignal,
    ) -> GeminiAssessment:
        same_side = advisory_signal.action == primary_signal.action and advisory_signal.action in {"BUY", "SELL"}
        contradicted = (
            advisory_signal.action in {"BUY", "SELL"}
            and primary_signal.action in {"BUY", "SELL"}
            and advisory_signal.action != primary_signal.action
        )
        delta = 0.0
        if same_side:
            delta = 0.05
        elif contradicted:
            delta = -0.08

        if contradicted:
            news_bias = "bearish" if primary_signal.action == "BUY" else "bullish"
        elif same_side:
            news_bias = "bullish" if primary_signal.action == "BUY" else "bearish"
        else:
            news_bias = "neutral"

        return GeminiAssessment(
            used=True,
            available=True,
            degraded=False,
            confirmed=same_side,
            contradicted=contradicted,
            news_bias=news_bias,
            macro_relevance="medium",
            event_risk="medium",
            confidence_adjustment=delta,
            contradiction_flag=contradicted,
            summary_reason=advisory_signal.reason,
            source_quality_score=max(0.0, min(1.0, advisory_signal.confidence)),
            advisory_action=advisory_signal.action,
            advisory_confidence=advisory_signal.confidence,
            confidence_delta=delta,
            reason=advisory_signal.reason,
            raw_payload={"fallback_from_trade_signal": advisory_signal.model_dump()},
        )
