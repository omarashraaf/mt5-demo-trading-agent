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


class _DatasetBuilderStub:
    async def build_dataset(self, limit: int = 2000, include_unexecuted: bool = True):
        return [], {}


class _AttributionDbStub:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.saved_report = None

    async def get_trade_outcomes(self, limit: int = 2000):
        return self.outcomes[:limit]

    async def list_model_versions(self, limit: int = 10):
        return []

    async def save_attribution_report(
        self,
        *,
        report_id,
        report_type,
        data_range_start,
        data_range_end,
        report,
    ):
        self.saved_report = report


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
                self.assertIn("model_quality", snapshot)
                self.assertGreaterEqual(len(snapshot["recent_attribution_reports"]), 1)

                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(_run())

    def test_approve_model_rejects_zero_precision_model(self):
        async def _run():
            fd, db_path = tempfile.mkstemp(prefix="ta-research-approve-", suffix=".db")
            os.close(fd)
            try:
                db = Database(db_path)
                await db.initialize()
                await db.register_model_version(
                    version_id="bad-model",
                    algorithm="logistic_regression",
                    target_definition="profitable_after_costs_180m",
                    feature_schema_version="v2",
                    training_date=time.time(),
                    data_range_start=time.time() - 1000,
                    data_range_end=time.time(),
                    evaluation_metrics={
                        "sample_count": 200,
                        "accuracy": 0.96,
                        "precision": 0.0,
                        "recall": 0.0,
                        "f1": 0.0,
                        "confusion_matrix": {"tp": 0, "fn": 20, "fp": 0, "tn": 180},
                    },
                    walk_forward_metrics={},
                    approval_status="candidate",
                    notes="",
                )

                service = ResearchCycleService(db, meta_model_service=_MetaStub())
                with self.assertRaisesRegex(ValueError, "approval gate"):
                    await service.approve_model("bad-model")

                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(_run())

    def test_attribution_report_reads_alternate_pnl_fields(self):
        async def _run():
            db = _AttributionDbStub([
                {
                    "symbol": "XAUUSD",
                    "confidence": 0.80,
                    "net_pnl": -120.0,
                    "holding_minutes": 20.0,
                    "timestamp": 100.0,
                    "closed_at": 200.0,
                },
                {
                    "symbol": "XAUUSD",
                    "confidence": 0.40,
                    "profit_usd": 45.0,
                    "holding_minutes": 90.0,
                    "timestamp": 110.0,
                    "closed_at": 220.0,
                },
                {
                    "symbol": "EURUSD",
                    "confidence": 0.60,
                    "pnl": 30.0,
                    "holding_minutes": 8.0,
                    "timestamp": 120.0,
                    "closed_at": 230.0,
                },
            ])
            service = ResearchCycleService(db, meta_model_service=_MetaStub())
            service.dataset_builder = _DatasetBuilderStub()

            report = await service.generate_attribution_report(report_type="full", limit=500)

            self.assertEqual(report["symbol_profitability_report"]["XAUUSD"]["net_pnl"], -75.0)
            self.assertEqual(report["hold_duration_report"]["15_30m"]["pnl"], -120.0)
            self.assertEqual(report["spread_cost_impact_report"]["wide"]["pnl"], -120.0)
            self.assertIs(db.saved_report, report)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
