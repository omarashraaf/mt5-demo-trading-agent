import asyncio
import csv
import math
import os
import tempfile
import unittest

from research.label_builder import build_labels, hold_bucket_from_minutes
from research.trade_dataset_builder import _post_analysis_features
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

    def test_label_builder_reads_alternate_outcome_pnl_fields(self):
        loss = build_labels(
            candidate={
                "executed": 1,
                "net_pnl": 0.0,
                "spread_at_eval": 0.0,
                "execution_slippage_est": 0.0,
                "hold_duration_minutes": 30.0,
            },
            outcome={"profit_usd": -42.0, "holding_minutes": 30.0},
        )
        self.assertEqual(loss["expected_return_90m"], -42.0)
        self.assertEqual(loss["profitable_after_costs_90m"], 0)

        win = build_labels(
            candidate={
                "executed": 1,
                "spread_at_eval": 0.0,
                "execution_slippage_est": 0.0,
                "hold_duration_minutes": 30.0,
            },
            outcome={"pnl": 27.0, "holding_minutes": 30.0},
        )
        self.assertEqual(win["expected_return_90m"], 27.0)
        self.assertEqual(win["profitable_after_costs_90m"], 1)

        net = build_labels(
            candidate={
                "executed": 1,
                "spread_at_eval": 0.0,
                "execution_slippage_est": 0.0,
                "hold_duration_minutes": 30.0,
            },
            outcome={"net_pnl": -15.0, "holding_minutes": 30.0},
        )
        self.assertEqual(net["expected_return_90m"], -15.0)

    def test_post_analysis_features_sanitize_bad_numeric_values(self):
        features = _post_analysis_features(
            {
                "generated_by": "gemini_assisted",
                "future_confidence_adjustment": "nan",
                "quality_scores": {
                    "execution_quality": "Infinity",
                    "risk_discipline": "-Infinity",
                    "timing_quality": "bad-value",
                },
                "mistakes": ["late_entry"],
                "improvement_actions": ["wait_for_pullback"],
                "root_causes": ["execution_delay"],
                "diagnostics": {
                    "late_entry_flag": "bad-value",
                    "spread_stress_flag": float("inf"),
                    "trend_conflict_flag": 1,
                },
                "recommendation": {
                    "expected_win_rate_adjustment": "NaN",
                    "suggested_sl_pct_of_start": "Infinity",
                    "suggested_tp_pct_of_start": "-Infinity",
                    "next_trade_size_adjustment_pct": "bad-value",
                },
            }
        )

        numeric_values = [
            value for key, value in features.items()
            if key != "f_post_generated_by"
        ]
        self.assertTrue(all(math.isfinite(float(value)) for value in numeric_values))
        self.assertEqual(features["f_post_execution_quality"], 0.0)
        self.assertEqual(features["f_post_diag_spread_stress"], 0.0)
        self.assertEqual(features["f_post_diag_trend_conflict"], 1.0)

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

    def test_executed_training_dataset_uses_only_clean_app_owned_outcomes(self):
        async def _run():
            fd, path = tempfile.mkstemp(prefix="ta-research-clean-dataset-", suffix=".db")
            os.close(fd)
            db = None
            try:
                db = Database(path)
                await db.initialize()

                async def add_candidate(candidate_id: str, *, executed: bool = True) -> int:
                    signal_id = await db.log_signal(
                        agent_name="SmartAgent",
                        symbol="XAUUSD",
                        timeframe="M15",
                        action="BUY",
                        confidence=0.68,
                        stop_loss=2300.0,
                        take_profit=2350.0,
                        max_holding_minutes=90,
                        reason="trend aligned",
                    )
                    await db.log_trade_candidate(
                        candidate_id=candidate_id,
                        signal_id=signal_id,
                        symbol="XAUUSD",
                        asset_class="Commodities",
                        strategy_mode="balanced",
                        session="london",
                        day_of_week=3,
                        technical_direction="BUY",
                        smart_agent_summary="trend aligned",
                        gemini_summary="neutral",
                        quality_score=0.72,
                        confidence_score=0.68,
                        trend_h1="bullish",
                        trend_h4="bullish",
                        stop_loss=2300.0,
                        take_profit=2350.0,
                        reward_risk=2.0,
                        spread_at_eval=12.0,
                        atr_regime="medium",
                        support_resistance_context="support",
                        event_id="",
                        event_type="none",
                        event_importance="low",
                        contradiction_flag=False,
                        risk_decision="approved",
                        rejection_reasons=[],
                        executed=executed,
                        gemini_changed_decision=False,
                        meta_model_changed_decision=False,
                    )
                    await db.log_feature_snapshot(
                        candidate_id=candidate_id,
                        signal_id=signal_id,
                        schema_version="v2",
                        features={"quality_score": 0.72},
                    )
                    return int(signal_id)

                clean_signal_id = await add_candidate("clean")
                await db.mark_trade_candidate_execution(signal_id=clean_signal_id, executed=True, ticket=1001)
                await db.log_trade_outcome(
                    ticket=1001,
                    signal_id=clean_signal_id,
                    symbol="XAUUSD",
                    action="BUY",
                    confidence=0.68,
                    profit=25.0,
                    exit_reason="take_profit",
                    holding_minutes=35.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={},
                )

                ticket_only_signal_id = await add_candidate("ticket-only-outcome")
                await db.mark_trade_candidate_execution(signal_id=ticket_only_signal_id, executed=True, ticket=1004)
                await db.log_trade_outcome(
                    ticket=1004,
                    signal_id=None,
                    symbol="XAUUSD",
                    action="BUY",
                    confidence=0.68,
                    profit=15.0,
                    exit_reason="take_profit",
                    holding_minutes=20.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={},
                )

                missing_signal_id = await add_candidate("missing-outcome")
                await db.mark_trade_candidate_execution(signal_id=missing_signal_id, executed=True, ticket=1002)

                manual_signal_id = await add_candidate("manual-close")
                await db.mark_trade_candidate_execution(signal_id=manual_signal_id, executed=True, ticket=1003)
                await db.log_trade_outcome(
                    ticket=1003,
                    signal_id=manual_signal_id,
                    symbol="XAUUSD",
                    action="BUY",
                    confidence=0.68,
                    profit=5.0,
                    exit_reason="manual_close",
                    holding_minutes=10.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={},
                )

                await add_candidate("not-executed", executed=False)

                builder = TradeDatasetBuilder(db)
                rows, metadata = await builder.build_dataset(limit=1000, include_unexecuted=False)

                self.assertEqual(metadata["row_count"], 3)
                self.assertEqual(
                    {row["candidate_id"] for row in rows},
                    {"clean", "ticket-only-outcome", "manual-close"},
                )
                self.assertEqual(await db.count_clean_app_trade_outcomes(), 3)
            finally:
                if db is not None:
                    await db.close()
                if os.path.exists(path):
                    os.remove(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
