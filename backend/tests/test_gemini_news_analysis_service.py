import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, NormalizedNewsItem, NormalizedSymbolInfo, SymbolProfile, TechnicalSignal
from services.gemini_news_analysis_service import GeminiNewsAnalysisService


class _Agent:
    available = True
    unavailable_reason = ""

    def assess_news_items(self, *, context, technical_signal, normalized_news):
        return {
            "affected_symbols": [context.symbol],
            "news_bias": "bullish",
            "macro_relevance": "medium",
            "event_risk": "low",
            "contradiction_flag": False,
            "confidence_adjustment": 0.03,
            "summary_reason": "Recent headlines support the technical thesis.",
            "source_quality_score": 0.8,
        }


class GeminiNewsAnalysisServiceTests(unittest.TestCase):
    def _context(self) -> MarketContext:
        return MarketContext(
            symbol="EURUSD",
            requested_timeframe="H1",
            evaluation_mode="auto",
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex"),
            profile=SymbolProfile(
                profile_name="Forex Majors",
                category="Forex",
                max_spread=12,
                min_atr_pct=0.2,
                max_hold_minutes=360,
                min_reward_risk=1.8,
                max_positions_per_category=2,
                quality_threshold=0.72,
                cooldown_minutes=120,
                news_weight=0.1,
                default_sl_atr_multiplier=1.2,
                default_tp_atr_multiplier=2.4,
            ),
        )

    def test_assessment_reads_normalized_news_and_returns_bounded_schema(self):
        service = GeminiNewsAnalysisService(_Agent(), timeout_seconds=1, max_retries=0)
        assessment = self._run(
            service.assess(
                self._context(),
                TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.7, reason="Trend"),
                [
                    NormalizedNewsItem(
                        source="wire",
                        category="headline",
                        title="EUR firms on softer dollar",
                        published_at=1.0,
                        received_at=2.0,
                        affected_symbols=["EURUSD"],
                    )
                ],
            )
        )

        self.assertTrue(assessment.used)
        self.assertEqual(assessment.news_bias, "bullish")
        self.assertEqual(assessment.affected_symbols, ["EURUSD"])
        self.assertEqual(assessment.advisory_action, "HOLD")

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)
