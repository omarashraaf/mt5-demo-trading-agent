import math
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, TechnicalSignal
import services.gemini_strategy_advisor_service as strategy_advisor_module
from services.gemini_strategy_advisor_service import GeminiStrategyAdvisorService


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)


class _FakeTypes:
    GenerateContentConfig = _FakeGenerateContentConfig


class _FakeResponse:
    text = """
    {
      "strategy_type": "intraday",
      "stop_loss_price": 1.095,
      "take_profit_price": 1.112,
      "sl_atr_multiplier": 1.5,
      "tp_atr_multiplier": 2.5,
      "max_hold_minutes": 90,
      "confidence_adjustment": 0.01,
      "contradiction_flag": false,
      "summary_reason": "clean payload"
    }
    """


class _FakeModels:
    def __init__(self):
        self.contents = ""

    def generate_content(self, *, model, contents, config):
        self.contents = contents
        return _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


class GeminiStrategyAdvisorServiceTests(unittest.TestCase):
    def test_parse_response_sanitizes_non_finite_numeric_guidance(self):
        service = GeminiStrategyAdvisorService(api_key="")

        parsed = service._parse_response(
            """
            {
              "strategy_type": "intraday",
              "stop_loss_price": NaN,
              "take_profit_price": Infinity,
              "sl_atr_multiplier": "NaN",
              "tp_atr_multiplier": "Infinity",
              "max_hold_minutes": "NaN",
              "confidence_adjustment": "NaN",
              "contradiction_flag": false,
              "summary_reason": "malformed Gemini advisor output"
            }
            """
        )

        self.assertIsNone(parsed["stop_loss_price"])
        self.assertIsNone(parsed["take_profit_price"])
        self.assertEqual(parsed["sl_atr_multiplier"], 1.5)
        self.assertEqual(parsed["tp_atr_multiplier"], 2.5)
        self.assertEqual(parsed["max_hold_minutes"], 120)
        self.assertEqual(parsed["confidence_adjustment"], 0.0)
        self.assertTrue(math.isfinite(parsed["sl_atr_multiplier"]))
        self.assertTrue(math.isfinite(parsed["tp_atr_multiplier"]))

    def test_chart_summary_sanitizes_malformed_bar_values(self):
        service = GeminiStrategyAdvisorService(api_key="")
        context = MarketContext(
            symbol="EURUSD",
            bars_by_timeframe={
                "H1": [
                    {"close": "bad-close", "high": "bad-high", "low": "bad-low"},
                    {"close": float("nan"), "high": float("inf"), "low": "-Infinity"},
                    {"close": 1.102, "high": 1.105, "low": 1.099},
                ]
            },
        )

        summary = service._build_chart_summary(context)

        self.assertEqual(summary["H1"]["bars"], 1)
        self.assertEqual(summary["H1"]["last_close"], 1.102)
        self.assertEqual(summary["H1"]["high"], 1.105)
        self.assertEqual(summary["H1"]["low"], 1.099)

    def test_assess_sync_sends_valid_json_when_inputs_contain_non_finite_values(self):
        original_types = strategy_advisor_module.types
        strategy_advisor_module.types = _FakeTypes
        try:
            service = GeminiStrategyAdvisorService(api_key="")
            fake_client = _FakeClient()
            service._client = fake_client
            context = MarketContext(
                symbol="EURUSD",
                tick={"bid": float("nan"), "ask": 1.1002, "spread": float("inf")},
                user_policy={"min_reward_risk": float("nan"), "mode": "balanced"},
                strategy_context={"score": float("inf")},
            )
            signal = TechnicalSignal(
                agent_name="SmartAgent",
                action="BUY",
                confidence=float("nan"),
                metadata={"atr": float("nan"), "reward_risk_ratio": float("inf")},
            )

            service._assess_sync(
                context=context,
                technical_signal=signal,
                gemini_assessment=None,
                event_context={"event_risk": float("inf")},
            )

            marker = "Input payload (JSON):\n"
            payload_text = fake_client.models.contents.split(marker, 1)[1]
            self.assertNotIn("NaN", payload_text)
            self.assertNotIn("Infinity", payload_text)
            json.loads(payload_text, parse_constant=lambda value: self.fail(f"invalid JSON constant {value}"))
        finally:
            strategy_advisor_module.types = original_types


if __name__ == "__main__":
    unittest.main()
