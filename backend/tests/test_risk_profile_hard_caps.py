import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.interface import TradeSignal
from risk.rules import RiskEngine


class RiskProfileHardCapTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine()
        self.engine.apply_policy_preset("balanced")

    def _signal(self, **overrides):
        data = {
            "action": "BUY",
            "confidence": 0.85,
            "stop_loss": 99.0,
            "take_profit": 103.0,
        }
        data.update(overrides)
        return TradeSignal(agent_name="test", **data)

    def test_low_confidence_is_hard_block(self):
        result = self.engine.evaluate(
            signal=self._signal(confidence=0.20),
            symbol="XAUUSD",
            spread=1.0,
            equity=100000.0,
            open_positions=[],
            entry_price=100.0,
        )
        self.assertFalse(result.approved)
        self.assertIn("Confidence", result.reason)

    def test_high_spread_is_hard_block(self):
        result = self.engine.evaluate(
            signal=self._signal(),
            symbol="XAUUSD",
            spread=999.0,
            equity=100000.0,
            open_positions=[],
            entry_price=100.0,
        )
        self.assertFalse(result.approved)
        self.assertIn("Spread", result.reason)

    def test_per_symbol_position_cap_is_hard_block(self):
        result = self.engine.evaluate(
            signal=self._signal(),
            symbol="XAUUSD",
            spread=1.0,
            equity=100000.0,
            open_positions=[
                {"symbol": "XAUUSD", "type": "BUY", "profit": 10.0},
                {"symbol": "XAUUSD", "type": "SELL", "profit": 5.0},
            ],
            entry_price=100.0,
        )
        self.assertFalse(result.approved)
        self.assertIn("Per-symbol hard cap", result.reason)

    def test_adjusted_volume_is_capped_by_profile_max_lot_size(self):
        policy = self.engine.user_policy.model_copy(update={"max_lot_size": 0.03})
        self.engine.update_user_policy(policy)
        result = self.engine.evaluate(
            signal=self._signal(),
            symbol="XAUUSD",
            spread=1.0,
            equity=1_000_000.0,
            open_positions=[],
            entry_price=100.0,
        )
        self.assertTrue(result.approved)
        self.assertLessEqual(result.adjusted_volume, 0.03)


if __name__ == "__main__":
    unittest.main()
