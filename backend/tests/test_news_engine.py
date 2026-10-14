import unittest
import time

from domain.models import MarketContext, NormalizedNewsItem, TechnicalSignal
from services.news_engine import NewsEngine


class NewsEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = NewsEngine()

    def _context_with_news(self, items):
        return MarketContext(
            symbol="AAPL",
            requested_timeframe="H1",
            evaluation_mode="manual",
            normalized_news=items,
        )

    def test_aligned_news_increases_confidence(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Apple reports stronger demand",
                    summary="",
                    published_at=now - 120,
                    received_at=now - 90,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bullish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.9,
                        "is_recent": True,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.62, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)
        self.assertEqual(out["sentiment"], "bullish")
        self.assertGreater(out["confidence_adjustment"], 0.0)
        self.assertFalse(out["recommend_hold"])

    def test_fallback_news_does_not_boost_trade_confidence(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Apple reports stronger demand",
                    summary="",
                    published_at=now - 120,
                    received_at=now - 90,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bullish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.9,
                        "is_recent": True,
                        "fallback_used": True,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.72, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertTrue(out["fallback_used"])
        self.assertLessEqual(out["confidence_adjustment"], 0.0)
        self.assertNotEqual(out["reason_code"], "aligned")

    def test_non_finite_news_confidence_does_not_boost_trade_confidence(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Apple reports stronger demand",
                    summary="",
                    published_at=now - 120,
                    received_at=now - 90,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bullish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": float("nan"),
                        "is_recent": True,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.72, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertLessEqual(out["confidence_adjustment"], 0.0)
        self.assertLess(out["strength"], 0.01)
        self.assertNotEqual(out["reason_code"], "aligned")

    def test_malformed_news_timestamps_do_not_crash_analysis(self):
        item = NormalizedNewsItem.model_construct(
            source="benzinga",
            category="headline",
            title="Apple reports stronger demand",
            summary="",
            published_at="bad-published-at",
            received_at="bad-received-at",
            affected_symbols=["AAPL"],
            metadata={
                "sentiment": "bullish",
                "impact_level": "high",
                "relevance": "direct",
                "confidence": 0.9,
                "event_timestamp": "bad-event-time",
            },
        )
        ctx = self._context_with_news([item])
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.72, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertEqual(out["items_count"], 1)
        self.assertFalse(out["imminent_high_impact"])

    def test_conflicting_high_impact_can_hold(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="macro_event",
                    title="Fed signals surprise rate hike",
                    summary="",
                    published_at=now - 300,
                    received_at=now - 240,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bearish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.92,
                        "is_recent": True,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.55, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)
        self.assertTrue(out["risk_flag"])
        self.assertTrue(out["recommend_hold"])
        self.assertLess(out["confidence_adjustment"], 0.0)

    def test_major_event_headline_without_impact_metadata_can_hold(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Fed signals unexpected rate hike",
                    summary="Policy shock pressures risk assets.",
                    published_at=now - 180,
                    received_at=now - 120,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bearish",
                        "relevance": "direct",
                        "confidence": 0.9,
                        "is_recent": True,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.56, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertEqual(out["impact"], "high")
        self.assertTrue(out["risk_flag"])
        self.assertTrue(out["recommend_hold"])
        self.assertEqual(out["reason_code"], "high_impact_uncertain")

    def test_mixed_high_impact_direct_news_recommends_hold(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Apple raises revenue guidance after demand surge",
                    summary="",
                    published_at=now - 120,
                    received_at=now - 90,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bullish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.9,
                        "is_recent": True,
                    },
                ),
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Apple supplier warns of production disruption",
                    summary="",
                    published_at=now - 150,
                    received_at=now - 120,
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bearish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.88,
                        "is_recent": True,
                    },
                ),
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.76, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertTrue(out["risk_flag"])
        self.assertTrue(out["recommend_hold"])
        self.assertEqual(out["reason_code"], "high_impact_conflict")
        self.assertLess(out["confidence_adjustment"], 0.0)

    def test_stale_high_impact_news_does_not_force_hold(self):
        now = time.time()
        ctx = self._context_with_news(
            [
                NormalizedNewsItem(
                    source="benzinga",
                    category="headline",
                    title="Fed signals surprise rate hike",
                    summary="Old headline from a prior session.",
                    published_at=now - (3 * 24 * 3600),
                    received_at=now - (3 * 24 * 3600),
                    affected_symbols=["AAPL"],
                    metadata={
                        "sentiment": "bearish",
                        "impact_level": "high",
                        "relevance": "direct",
                        "confidence": 0.9,
                    },
                )
            ]
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.56, reason="test")

        out = self.engine.analyze(context=ctx, signal=signal)

        self.assertFalse(out["recommend_hold"])
        self.assertNotEqual(out["reason_code"], "high_impact_uncertain")
        self.assertLess(out["strength"], 0.4)

    def test_unavailable_snapshot_marks_degraded_mode(self):
        snap = self.engine.unavailable_snapshot("Decision made without news data.")
        self.assertTrue(snap["news_unavailable"])
        self.assertFalse(snap["risk_flag"])
        self.assertEqual(snap["sentiment"], "neutral")


if __name__ == "__main__":
    unittest.main()
