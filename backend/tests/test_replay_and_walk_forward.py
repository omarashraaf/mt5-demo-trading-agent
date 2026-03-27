import asyncio
import json
import os
import pickle
import tempfile
import time
import unittest
from pathlib import Path

from research.replay_runner import ReplayRunner
from research.walk_forward_runner import WalkForwardRunner
from storage.db import Database


class _MockProbModel:
    def predict_proba(self, frame):
        values = []
        for _, row in frame.iterrows():
            q = row.get("f_quality_score", 0.0)
            try:
                q = float(q)
            except Exception:
                q = 0.0
            p = max(0.01, min(0.99, q))
            values.append([1.0 - p, p])
        import numpy as np

        return np.array(values)


class ReplayAndWalkForwardTests(unittest.TestCase):
    def test_replay_and_walk_forward(self):
        async def _run():
            fd, db_path = tempfile.mkstemp(prefix="ta-replay-", suffix=".db")
            os.close(fd)
            artifacts_dir = Path(tempfile.mkdtemp(prefix="ta-artifacts-"))
            try:
                db = Database(db_path)
                await db.initialize()

                base_ts = time.time() - 86400
                for i in range(40):
                    signal_id = i + 1
                    executed = (i % 3) != 0
                    quality = 0.25 + (i % 10) * 0.07
                    await db.log_trade_candidate(
                        candidate_id=f"cand-{signal_id}",
                        signal_id=signal_id,
                        symbol="XAUUSD" if i % 2 == 0 else "XAGUSD",
                        asset_class="Commodities",
                        strategy_mode="balanced",
                        session="london,newyork",
                        day_of_week=4,
                        technical_direction="BUY",
                        smart_agent_summary="summary",
                        gemini_summary="",
                        quality_score=quality,
                        confidence_score=quality,
                        trend_h1="bullish",
                        trend_h4="bullish",
                        stop_loss=100.0,
                        take_profit=120.0,
                        reward_risk=2.0,
                        spread_at_eval=10.0,
                        atr_regime="medium",
                        support_resistance_context="",
                        event_id="ev",
                        event_type="macro_event" if i % 2 == 0 else "headline",
                        event_importance="medium",
                        contradiction_flag=False,
                        risk_decision="approved" if executed else "blocked",
                        rejection_reasons=[] if executed else ["blocked"],
                        executed=executed,
                        gemini_changed_decision=False,
                        meta_model_changed_decision=False,
                    )
                    await db._db.execute(
                        "UPDATE trade_candidates SET timestamp_utc = ? WHERE signal_id = ?",
                        (base_ts + i * 60.0, signal_id),
                    )
                    await db.log_feature_snapshot(
                        candidate_id=f"cand-{signal_id}",
                        signal_id=signal_id,
                        schema_version="v2",
                        features={
                            "feature_schema_version": "v2",
                            "quality_score": quality,
                            "trend_alignment_score": quality,
                            "multi_timeframe_agreement": 1.0,
                            "symbol": "XAUUSD" if i % 2 == 0 else "XAGUSD",
                        },
                    )
                    if executed:
                        pnl = 30.0 if quality >= 0.6 else -20.0
                        await db.mark_trade_candidate_execution(
                            signal_id=signal_id,
                            executed=True,
                            ticket=1000 + signal_id,
                            fill_price=110.0,
                            slippage_estimate=0.1,
                            margin_snapshot={"equity": 10000},
                        )
                        await db.log_trade_outcome(
                            ticket=1000 + signal_id,
                            signal_id=signal_id,
                            symbol="XAUUSD" if i % 2 == 0 else "XAGUSD",
                            action="BUY",
                            confidence=quality,
                            profit=pnl,
                            exit_reason="take_profit" if pnl > 0 else "stop_loss",
                            holding_minutes=45.0,
                            symbol_category="Commodities",
                            strategy="trend_follow",
                            planned_hold_minutes=90,
                            outcome_json={},
                        )

                await db._db.commit()

                version_id = "model-test-replay"
                artifact_name = f"{version_id}.pkl"
                summary_name = f"{version_id}.json"
                with (artifacts_dir / artifact_name).open("wb") as handle:
                    pickle.dump(_MockProbModel(), handle)
                (artifacts_dir / summary_name).write_text(
                    json.dumps(
                        {
                            "feature_columns": [
                                "f_quality_score",
                                "f_trend_alignment_score",
                                "f_multi_timeframe_agreement",
                                "f_symbol",
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                await db.register_model_version(
                    version_id=version_id,
                    algorithm="logistic_regression",
                    target_definition="profitable_after_costs_90m",
                    feature_schema_version="v2",
                    training_date=time.time(),
                    data_range_start=base_ts,
                    data_range_end=base_ts + 9999,
                    evaluation_metrics={},
                    walk_forward_metrics={},
                    approval_status="candidate",
                    notes=f"artifact={artifact_name};summary={summary_name}",
                )

                replay = ReplayRunner(db, artifacts_dir=str(artifacts_dir))
                replay_report = await replay.run_replay(
                    version_id=version_id,
                    score_threshold=0.55,
                    include_unexecuted=True,
                    limit=1000,
                )
                self.assertGreaterEqual(replay_report["sample_size_executed_baseline"], 1)
                self.assertIn("baseline", replay_report)
                self.assertIn("with_meta_model_filter", replay_report)

                walk_forward = WalkForwardRunner(db)
                wf_report = await walk_forward.run_walk_forward(
                    algorithm="logistic_regression",
                    target_column="profitable_after_costs_90m",
                    score_threshold=0.55,
                    windows=3,
                    include_unexecuted=True,
                    limit=1000,
                )
                self.assertGreaterEqual(wf_report["aggregate"]["window_count"], 1)
                self.assertIn("mean_accuracy", wf_report["aggregate"])

                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)
                for child in artifacts_dir.glob("*"):
                    child.unlink(missing_ok=True)
                artifacts_dir.rmdir()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
