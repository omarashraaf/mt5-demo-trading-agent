import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import CandidateAssetMapping, ExternalEvent
from llm.gemini_event_classifier import GeminiEventClassifier


class GeminiEventClassifierTests(unittest.TestCase):
    def test_fallback_is_used_when_gemini_is_unavailable(self):
        classifier = GeminiEventClassifier(api_key="")
        classifier._client = None
        classifier._unavailable_reason = "forced unavailable for test"
        event = ExternalEvent(
            source="finnhub",
            source_event_id="1",
            dedupe_key="event-1",
            title="OPEC hints at supply cuts",
            summary="Oil market supply risk rises",
            timestamp_utc=1710000000,
            event_type="market_news",
            importance="high",
            affected_assets=["WTI", "BRENT"],
            raw_payload={},
            fetched_at=1710000001,
        )
        result = asyncio.run(
            classifier.classify_event(
                event=event,
                candidate_mappings=[
                    CandidateAssetMapping(symbol="WTI", baseline_bias="bullish", tradable=True, mapping_score=0.8, reason="oil"),
                    CandidateAssetMapping(symbol="BRENT", baseline_bias="bullish", tradable=True, mapping_score=0.8, reason="oil"),
                ],
            )
        )
        self.assertTrue(result.degraded)
        self.assertEqual(result.bias_by_asset["WTI"], "bullish")
        self.assertEqual(result.bias_by_asset["BRENT"], "bullish")

    def test_parses_structured_response(self):
        classifier = GeminiEventClassifier(api_key="")
        event = ExternalEvent(
            source="finnhub",
            source_event_id="2",
            dedupe_key="event-2",
            title="US CPI",
            summary="",
            timestamp_utc=1710000000,
            event_type="economic_calendar",
            importance="high",
            affected_assets=["US100", "US500"],
            raw_payload={},
            fetched_at=1710000001,
        )
        parsed = classifier._parse_response(
            """{
              "event_type": "economic_calendar",
              "affected_assets": ["US100", "US500"],
              "importance": "high",
              "bias_by_asset": {"US100": "bearish", "US500": "bearish", "US30": "neutral", "GER40": "neutral", "GOLD": "neutral", "WTI": "neutral", "BRENT": "neutral"},
              "persistence_horizon": "short",
              "event_risk": "high",
              "confidence_adjustment": -0.05,
              "contradiction_flag": false,
              "summary_reason": "Hot CPI pressures growth indices."
            }""",
            event=event,
        )
        self.assertTrue(parsed.available)
        self.assertEqual(parsed.bias_by_asset["US100"], "bearish")
        self.assertEqual(parsed.event_risk, "high")
