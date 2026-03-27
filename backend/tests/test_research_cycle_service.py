import asyncio
import os
import tempfile
import time
import unittest

from services.research_cycle_service import ResearchCycleService
from storage.db import Database


class _MetaStub:
    is_active = False
    active_version_id = ""

    async def refresh_active_model(self, force: bool = False):
        return None


class ResearchCycleServiceTests(unittest.TestCase):
    def test_status_snapshot_and_attribution_report(self):
        async def _run():
            fd, db_path = tempfile.mkstemp(prefix="ta-research-cycle-", suffix=".db")
            os.close(fd)
            try:
                db = Database(db_path)
                await db.initialize()

                await db.register_model_version(
                    version_id="model-1",
                    algorithm="logistic_regression",
                    target_definition="profitable_after_costs_90m",
                    feature_schema_version="v2",
                    training_date=time.time(),
                    data_range_start=time.time() - 2000,
                    data_range_end=time.time(),
                    evaluation_metrics={"accuracy": 0.62, "feature_importance": [{"feature": "f_quality_score", "importance": 0.4}]},
                    walk_forward_metrics={},
                    approval_status="candidate",
                    notes="",
                )
                await db.log_model_run(
                    run_id="run-1",
                    version_id="model-1",
                    status="candidate",
                    params={"algorithm": "logistic_regression"},
                    metrics={"accuracy": 0.62},
                )
                await db.log_trade_outcome(
                    ticket=123,
                    signal_id=1,
                    symbol="XAUUSD",
                    action="BUY",
                    confidence=0.7,
                    profit=45.0,
                    exit_reason="take_profit",
                    holding_minutes=22.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={},
                )

                service = ResearchCycleService(db, meta_model_service=_MetaStub())
                report = await service.generate_attribution_report(report_type="full", limit=500)
                self.assertIn("report_id", report)
                self.assertIn("symbol_profitability_report", report)

                snapshot = await service.status_snapshot()
                self.assertIn("meta_model_active", snapshot)
                self.assertIn("best_candidate_model", snapshot)
                self.assertIn("recent_attribution_reports", snapshot)
                self.assertGreaterEqual(len(snapshot["recent_attribution_reports"]), 1)

                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
