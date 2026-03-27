import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5.execution import PositionInfo
from position_manager import PositionManager


class _Connector:
    connected = True


class _MarketData:
    def get_symbol_info(self, _symbol):
        return {"point": 0.0001, "trade_contract_size": 100000}


class _Execution:
    def get_positions(self):
        return []


class _RiskEngine:
    panic_stopped = False
    auto_trade_enabled = True


class PositionManagerTimeStopTests(unittest.TestCase):
    def setUp(self):
        self.manager = PositionManager(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={},
        )

    def test_time_exit_reason_closes_stale_trade_without_progress(self):
        pos = PositionInfo(
            ticket=1,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1001,
            stop_loss=1.0950,
            take_profit=1.1100,
            profit=0.0,
            time=int(time.time() - 70 * 60),
            comment="TradingAgent",
        )

        decision = self.manager._time_exit_reason(
            pos=pos,
            plan={
                "planned_hold_minutes": 90,
                "stale_after_minutes": 45,
                "min_progress_r_multiple": 0.25,
                "target_progress_r_multiple": 0.40,
            },
            plan_meta={},
            profit_in_price=0.0002,
            original_risk=0.0050,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision[0], "time_stop")
        self.assertIn("stale trade", decision[1])

    def test_session_close_triggers_inside_buffer(self):
        with patch("position_manager.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 3, 26, 16, 50, tzinfo=timezone.utc)
            should_close = self.manager._should_end_session_close(
                {"close_before_session_end": True, "session_close_buffer_minutes": 15},
                {"close_before_session_end": True, "sessions": ["US Open"]},
            )

        self.assertTrue(should_close)

    def test_passive_exit_classifies_breakeven(self):
        reason = self.manager._classify_passive_exit(
            {
                "price_open": 1.1000,
                "stop_loss": 1.10005,
                "take_profit": 1.1100,
                "last_price": 1.10004,
                "point": 0.0001,
                "profit": 0.01,
                "action": "BUY",
            }
        )

        self.assertEqual(reason, "breakeven")
