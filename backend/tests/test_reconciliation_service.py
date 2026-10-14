import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.reconciliation_service import ReconciliationService


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def get_recent_successful_orders(self, *, limit=300):
        return list(self.rows)


class _Execution:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        return list(self.positions)


class ReconciliationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconciliation_ok_when_local_and_broker_match(self):
        service = ReconciliationService(
            db=_Db([
                {"ticket": 1, "symbol": "US500", "action": "BUY", "volume": 0.1, "stop_loss": 99.0, "take_profit": 105.0}
            ]),
            execution_engine=_Execution([
                SimpleNamespace(ticket=1, symbol="US500", type="BUY", volume=0.1, stop_loss=99.0, take_profit=105.0)
            ]),
        )
        report = await service.run()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["issue_count"], 0)

    async def test_reconciliation_reports_missing_and_mismatch_issues(self):
        service = ReconciliationService(
            db=_Db([
                {"ticket": 1, "symbol": "US500", "action": "BUY", "volume": 0.1, "stop_loss": 99.0, "take_profit": 105.0},
                {"ticket": 2, "symbol": "EURUSD", "action": "SELL", "volume": 0.2, "stop_loss": 1.12, "take_profit": 1.08},
            ]),
            execution_engine=_Execution([
                SimpleNamespace(ticket=2, symbol="EURUSD", type="SELL", volume=0.3, stop_loss=1.12, take_profit=1.08),
                SimpleNamespace(ticket=3, symbol="XAUUSD", type="BUY", volume=0.01, stop_loss=1900.0, take_profit=1950.0),
            ]),
        )
        report = await service.run()
        codes = {issue["code"] for issue in report["issues"]}
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("local_order_missing_in_broker_open_positions", codes)
        self.assertIn("volume_mismatch", codes)
        self.assertIn("broker_position_missing_local_order", codes)


if __name__ == "__main__":
    unittest.main()
