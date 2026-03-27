import asyncio
import csv
import os
import tempfile
import unittest

from research.label_builder import build_labels, hold_bucket_from_minutes
from research.trade_dataset_builder import TradeDatasetBuilder
from storage.db import Database


class ResearchDatasetBuilderTests(unittest.TestCase):
    def test_label_builder_no_trade_and_executed_paths(self):
        no_trade = build_labels(
            candidate={"executed": 0},
            outcome=None,
        )
        self.assertEqual(no_trade["no_trade_label"], 1)
        self.assertEqual(no_trade["profitable_after_costs_180m"], 0)

        executed = build_labels(
            candidate={
                "executed": 1,
                "net_pnl": 125.0,
                "spread_at_eval": 20.0,
                "execution_slippage_est": 1.0,
                "hold_duration_minutes": 40.0,
                "exit_reason": "take_profit",
            },
            outcome={"profit": 125.0, "holding_minutes": 40.0},
        )
        self.assertEqual(executed["no_trade_label"], 0)
        self.assertEqual(executed["hit_target_before_stop"], 1)
        self.assertEqual(executed["hold_bucket"], "30_60m")
        self.assertLess(executed["expected_return_180m"], 125.0)
        self.assertGreater(executed["expected_return_30m"], 0.0)

    def test_hold_bucket_boundaries(self):
        self.assertEqual(hold_bucket_from_minutes(5), "0_15m")
        self.assertEqual(hold_bucket_from_minutes(20), "15_30m")
        self.assertEqual(hold_bucket_from_minutes(45), "30_60m")
        self.assertEqual(hold_bucket_from_minutes(120), "60_180m")
        self.assertEqual(hold_bucket_from_minutes(300), "180m_plus")

    def test_build_dataset_and_export_csv(self):
        async def _run():
            fd, path = tempfile.mkstemp(prefix="ta-research-dataset-", suffix=".db")
            os.close(fd)
            csv_fd, csv_path = tempfile.mkstemp(prefix="ta-research-dataset-", suffix=".csv")
            os.close(csv_fd)
            try:
                db = Database(path)
                await db.initialize()

                await db.log_trade_candidate(
                    candidate_id="cand-1",
                    signal_id=11,
                    symbol="XAGUSD",
                    asset_class="Commodities",
                    strategy_mode="balanced",
                    session="london,newyork",
                    day_of_week=4,
                    technical_direction="BUY",
                    smart_agent_summary="trend aligned",
                    gemini_summary="event neutral",
                    quality_score=0.71,
                    confidence_score=0.66,
                    trend_h1="bullish",
                    trend_h4="bullish",
                    stop_loss=29.8,
                    take_profit=31.4,
                    reward_risk=2.0,
                    spread_at_eval=15.0,
                    atr_regime="medium",
                    support_resistance_context="support bounce",
                    event_id="ev-2",
                    event_type="macro_event",
                    event_importance="medium",
                    contradiction_flag=False,
                    risk_decision="approved",
                    rejection_reasons=[],
                    executed=True,
                    gemini_changed_decision=False,
                    meta_model_changed_decision=False,
                )
                await db.log_feature_snapshot(
                    candidate_id="cand-1",
                    signal_id=11,
                    schema_version="v2",
                    features={
                        "symbol": "XAGUSD",
                        "asset_class": "Commodities",
                        "trend_alignment_score": 0.8,
                        "multi_timeframe_agreement": 1.0,
                        "rsi_proxy": 58.0,
                        "volatility_percentile": 0.42,
                        "spread_percentile": 0.15,
                    },
                )
                await db.mark_trade_candidate_execution(
                    signal_id=11,
                    executed=True,
                    ticket=444,
                    fill_price=30.55,
                    slippage_estimate=0.2,
                    margin_snapshot={"equity": 10000},
                )
                await db.log_trade_outcome(
                    ticket=444,
                    signal_id=11,
                    symbol="XAGUSD",
                    action="BUY",
                    confidence=0.66,
                    profit=55.0,
                    exit_reason="take_profit",
                    holding_minutes=25.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={},
                )

                builder = TradeDatasetBuilder(db)
                rows, metadata = await builder.build_dataset(limit=1000, include_unexecuted=True)
                self.assertEqual(len(rows), 1)
                self.assertEqual(metadata["default_feature_schema_version"], "v2")
                self.assertIn("f_trend_alignment_score", rows[0])
                self.assertEqual(rows[0]["profitable_after_costs_90m"], 1)

                await builder.export_csv(csv_path, limit=1000, include_unexecuted=True)
                with open(csv_path, "r", encoding="utf-8") as handle:
                    read_rows = list(csv.DictReader(handle))
                self.assertEqual(len(read_rows), 1)
                self.assertIn("label_schema_version", read_rows[0])
                self.assertIn("f_feature_schema_version", read_rows[0])

                await db.close()
            finally:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(csv_path):
                    os.remove(csv_path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
