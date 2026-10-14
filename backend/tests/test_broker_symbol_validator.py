import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.broker_symbol_validator import BrokerSymbolValidator


class BrokerSymbolValidatorTests(unittest.TestCase):
    def _context(self, **overrides):
        info = SimpleNamespace(
            visible=True,
            trade_enabled=True,
            volume_min=0.01,
            volume_max=10.0,
            volume_step=0.01,
            point=0.01,
            trade_stops_level=10,
            digits=2,
            trade_contract_size=100.0,
        )
        tick = {"bid": 99.9, "ask": 100.0, "spread": 10, "time": time.time(), "time_msc": int(time.time() * 1000)}
        for key, value in overrides.pop("info", {}).items():
            setattr(info, key, value)
        tick.update(overrides.pop("tick", {}))
        return SimpleNamespace(symbol="US500", symbol_info=info, tick=tick, **overrides)

    def test_valid_context_order_has_no_diagnostics(self):
        issues = BrokerSymbolValidator().validate_context_order(
            context=self._context(),
            action="BUY",
            volume=0.02,
            stop_loss=99.0,
            take_profit=102.0,
            entry_price=100.0,
            margin_required=20.0,
        )
        self.assertEqual(issues, [])

    def test_invalid_volume_and_targets_return_structured_codes(self):
        issues = BrokerSymbolValidator().validate_context_order(
            context=self._context(),
            action="BUY",
            volume=0.015,
            stop_loss=100.5,
            take_profit=100.8,
            entry_price=100.0,
            margin_required=20.0,
        )
        codes = {issue.code for issue in issues}
        self.assertIn("volume_step_mismatch", codes)
        self.assertIn("buy_targets_invalid", codes)
        self.assertTrue(all(issue.message for issue in issues))

    def test_stale_quote_is_reported(self):
        issues = BrokerSymbolValidator(max_quote_age_seconds=5).validate_context_order(
            context=self._context(tick={"time": time.time() - 60, "time_msc": int((time.time() - 60) * 1000)}),
            action="SELL",
            volume=0.02,
            stop_loss=101.0,
            take_profit=98.0,
            entry_price=99.9,
            margin_required=20.0,
        )
        self.assertIn("quote_stale", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
