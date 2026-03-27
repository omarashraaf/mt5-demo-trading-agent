from __future__ import annotations

import asyncio
import logging
import time

from domain.models import GeminiAssessment, MarketContext, NormalizedNewsItem, TechnicalSignal

logger = logging.getLogger(__name__)


class GeminiNewsAnalysisService:
    """Bounded news-intelligence layer.

    Gemini reads normalized news only. It cannot emit executable trade commands,
    lot sizes, or bypass later deterministic decision and risk services.
    """

    def __init__(
        self,
        gemini_agent,
        timeout_seconds: float = 12.0,
        max_retries: int = 1,
    ):
        self.gemini_agent = gemini_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._last_error: str | None = None
        self._last_failure_at: float = 0.0

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def degraded(self) -> bool:
        return bool(self._last_error)

    async def assess(
        self,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        normalized_news: list[NormalizedNewsItem],
    ) -> GeminiAssessment:
        if self.gemini_agent is None:
            return GeminiAssessment(
                used=False,
                available=False,
                summary_reason="Gemini news analysis is not configured.",
                reason="Gemini news analysis is not configured.",
            )

        if hasattr(self.gemini_agent, "available") and not self.gemini_agent.available:
            return GeminiAssessment(
                used=False,
                available=False,
                summary_reason=getattr(self.gemini_agent, "unavailable_reason", "Gemini unavailable."),
                reason=getattr(self.gemini_agent, "unavailable_reason", "Gemini unavailable."),
            )

        if not normalized_news:
            return GeminiAssessment(
                used=False,
                available=bool(getattr(self.gemini_agent, "available", False)),
                summary_reason="No normalized news inputs available.",
                reason="No normalized news inputs available.",
                affected_symbols=[context.symbol],
            )

        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                assessment = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._call_agent,
                        context,
                        technical_signal,
                        normalized_news,
                    ),
                    timeout=self.timeout_seconds,
                )
                self._last_error = None
                return assessment
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning(
                    "Gemini news analysis timed out after %.1fs (attempt %s/%s)",
                    self.timeout_seconds,
                    attempt,
                    attempts,
                )
            except Exception as exc:  # pragma: no cover - exercised by integration tests
                last_error = exc
                logger.warning(
                    "Gemini news analysis failed (attempt %s/%s): %s",
                    attempt,
                    attempts,
                    exc,
                )

        self._last_error = str(last_error) if last_error else "Gemini news analysis failed"
        self._last_failure_at = time.time()
        return GeminiAssessment(
            used=True,
            available=False,
            degraded=True,
            summary_reason="Gemini news analysis failed; continuing with technical logic only.",
            reason="Gemini news analysis failed; continuing with technical logic only.",
            error=self._last_error,
            affected_symbols=[context.symbol],
        )

    def _call_agent(
        self,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        normalized_news: list[NormalizedNewsItem],
    ) -> GeminiAssessment:
        if hasattr(self.gemini_agent, "assess_news_items"):
            result = self.gemini_agent.assess_news_items(
                context=context,
                technical_signal=technical_signal.to_trade_signal(),
                normalized_news=[item.model_dump() for item in normalized_news],
            )
        elif hasattr(self.gemini_agent, "assess"):
            result = self.gemini_agent.assess(
                self._agent_input_fallback(context),
                technical_signal=technical_signal.to_trade_signal(),
            )
        else:
            raise TypeError("Gemini agent does not support bounded news assessment")

        if isinstance(result, GeminiAssessment):
            assessment = result
        elif hasattr(result, "model_dump"):
            assessment = GeminiAssessment(**result.model_dump())
        elif isinstance(result, dict):
            assessment = GeminiAssessment(**result)
        else:
            raise TypeError("Gemini news analysis returned an unsupported response type")

        assessment.used = True
        assessment.available = True
        if not assessment.affected_symbols:
            assessment.affected_symbols = [context.symbol]
        return assessment

    def _agent_input_fallback(self, context: MarketContext):
        from agent.interface import AgentInput

        return AgentInput(
            symbol=context.symbol,
            timeframe=context.requested_timeframe,
            bars=context.bars_by_timeframe.get(context.requested_timeframe.upper(), []),
            spread=context.tick.get("spread", 0.0) if context.tick else 0.0,
            account_equity=context.account_equity,
            open_positions=context.symbol_open_positions,
            multi_tf_bars=context.bars_by_timeframe,
        )
