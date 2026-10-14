import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, TechnicalSignal
import services.gemini_trade_brain_service as trade_brain_module
from services.gemini_trade_brain_service import GeminiTradeBrainService


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)


class _FakeTypes:
    GenerateContentConfig = _FakeGenerateContentConfig


class _FakeResponse:
    parsed = {
        "action": "BUY",
        "confidence": 0.71,
        "stop_loss_price": 1.095,
        "take_profit_price": 1.112,
        "max_hold_minutes": 90,
        "strategy": "test",
        "summary_reason": "clean payload",
        "market_regime": "trend",
        "risk_note": "bounded",
    }


class _FakeModels:
    def __init__(self):
        self.contents = ""

    def generate_content(self, *, model, contents, config):
        self.contents = contents
        return _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


class GeminiTradeBrainServiceTests(unittest.TestCase):
    def test_recent_trade_summary_reads_alternate_pnl_fields(self):
        service = GeminiTradeBrainService(api_key="")

        summary = service._build_recent_trade_summary(
            [
                {"action": "SELL", "net_pnl": -120.0, "closed_at": 1710000100.0},
                {"action": "SELL", "profit_usd": -80.0, "closed_at": 1710000000.0},
                {"action": "BUY", "pnl": 45.0, "closed_at": 1709999900.0},
            ]
        )

        self.assertEqual(summary["net_profit"], -155.0)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 2)
        self.assertEqual(summary["recent"][0]["profit"], -120.0)
        self.assertEqual(summary["recent"][1]["profit"], -80.0)
        self.assertEqual(summary["recent"][2]["profit"], 45.0)

    def test_normalize_response_sanitizes_non_finite_confidence_and_targets(self):
        service = GeminiTradeBrainService(api_key="")
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.63,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=90,
            reason="baseline",
            strategy="trend_follow",
        )

        normalized = service._normalize_response(
            context,
            baseline,
            {
                "action": "BUY",
                "confidence": float("nan"),
                "stop_loss_price": float("nan"),
                "take_profit_price": float("inf"),
                "max_hold_minutes": "nan",
                "summary_reason": "malformed Gemini output",
            },
        )

        self.assertEqual(normalized["confidence"], 0.63)
        self.assertEqual(normalized["stop_loss_price"], baseline.stop_loss)
        self.assertEqual(normalized["take_profit_price"], baseline.take_profit)
        self.assertEqual(normalized["max_hold_minutes"], baseline.max_holding_minutes)

    def test_normalize_response_holds_when_gemini_and_baseline_targets_are_invalid(self):
        service = GeminiTradeBrainService(api_key="")
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.63,
            stop_loss=float("nan"),
            take_profit=float("inf"),
            max_holding_minutes=90,
            reason="baseline",
            strategy="trend_follow",
        )

        normalized = service._normalize_response(
            context,
            baseline,
            {
                "action": "BUY",
                "confidence": 0.8,
                "stop_loss_price": float("nan"),
                "take_profit_price": float("inf"),
                "summary_reason": "malformed Gemini and baseline targets",
            },
        )

        self.assertEqual(normalized["action"], "HOLD")
        self.assertIsNone(normalized["stop_loss_price"])
        self.assertIsNone(normalized["take_profit_price"])

    def test_chart_summary_sanitizes_malformed_bar_values(self):
        service = GeminiTradeBrainService(api_key="")
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

        self.assertEqual(summary["H1"]["close"], 1.102)
        self.assertEqual(summary["H1"]["high"], 1.105)
        self.assertEqual(summary["H1"]["low"], 0.0)

    def test_assess_sync_sends_valid_json_when_inputs_contain_non_finite_values(self):
        original_types = trade_brain_module.types
        trade_brain_module.types = _FakeTypes
        try:
            service = GeminiTradeBrainService(api_key="")
            fake_client = _FakeClient()
            service._client = fake_client
            context = MarketContext(
                symbol="EURUSD",
                tick={"bid": float("nan"), "ask": 1.1002, "spread": float("inf")},
                user_policy={"min_reward_risk": float("nan"), "mode": "balanced"},
            )
            baseline = TechnicalSignal(
                agent_name="SmartAgent",
                action="BUY",
                confidence=float("nan"),
                stop_loss=float("nan"),
                take_profit=float("inf"),
                max_holding_minutes=90,
                metadata={"atr": float("nan"), "reward_risk_ratio": float("inf")},
            )

            service._assess_sync(
                context=context,
                baseline_signal=baseline,
                gemini_assessment=None,
                meta_model_insight={"profit_probability": float("nan")},
                recent_outcomes=[],
                recent_evaluations=[],
            )

            marker = "Input payload (JSON):\n"
            payload_text = fake_client.models.contents.split(marker, 1)[1]
            self.assertNotIn("NaN", payload_text)
            self.assertNotIn("Infinity", payload_text)
            json.loads(payload_text, parse_constant=lambda value: self.fail(f"invalid JSON constant {value}"))
        finally:
            trade_brain_module.types = original_types


if __name__ == "__main__":
    unittest.main()
