import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.interface import AgentInput
from agent.smart_agent import SmartAgent, get_asset_class


def _make_bar(close: float, drift: float = 0.1):
    return {
        "time": 1,
        "open": close - drift,
        "high": close + drift,
        "low": close - drift,
        "close": close,
        "volume": 150,
    }


class SmartAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = SmartAgent()

    def test_asset_class_recognizes_indices_and_commodities(self):
        self.assertEqual(get_asset_class("US500"), "index")
        self.assertEqual(get_asset_class("WTI"), "commodity")
        self.assertEqual(get_asset_class("TSLA"), "stock")

    def test_stock_can_emit_continuation_buy_without_m15_reversal(self):
        h4 = [_make_bar(100 + i * 0.18 + ((i % 5) - 2) * 0.04) for i in range(80)]
        h1 = []
        base = 120.0
        for i in range(80):
            if i % 3 == 0:
                base -= 0.07
            else:
                base += 0.12
            h1.append(_make_bar(base + ((i % 3) - 1) * 0.02))
        m15 = []
        micro = 128.0
        for i in range(40):
            if i % 6 == 0:
                micro -= 0.015
            else:
                micro += 0.02
            m15.append(_make_bar(micro + ((i % 3) - 1) * 0.004, drift=0.05))
        m15[-2]["open"] = m15[-2]["close"] - 0.01
        m15[-1]["open"] = m15[-1]["close"] - 0.01

        signal = self.agent.evaluate(
            AgentInput(
                symbol="TSLA",
                timeframe="H1",
                bars=h1,
                multi_tf_bars={"M15": m15, "H1": h1, "H4": h4},
                open_positions=[],
                spread=5,
                account_equity=100000,
            )
        )

        self.assertEqual(signal.action, "BUY")
        self.assertIn(signal.metadata.get("entry_signal"), {"trend_continuation_buy", "buy", "neutral_lean_buy"})
        self.assertGreaterEqual(signal.confidence, 0.58)
