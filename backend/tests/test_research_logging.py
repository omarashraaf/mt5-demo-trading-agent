import asyncio
import os
import tempfile
import unittest

from storage.db import Database


class ResearchLoggingTests(unittest.TestCase):
    def test_candidate_feature_and_outcome_logging(self):
        async def _run():
            fd, path = tempfile.mkstemp(prefix="ta-research-", suffix=".db")
            os.close(fd)
            try:
                db = Database(path)
                await db.initialize()

                await db.log_trade_candidate(
                    candidate_id="cand-1",
                    signal_id=101,
                    symbol="XAUUSD",
                    asset_class="Commodities",
                    strategy_mode="aggressive",
                    session="24/7",
                    day_of_week=4,
                    technical_direction="SELL",
                    smart_agent_summary="test summary",
                    gemini_summary="gemini summary",
                    quality_score=0.72,
                    confidence_score=0.68,
                    trend_h1="bearish",
                    trend_h4="bearish",
                    stop_loss=4470.0,
                    take_profit=4400.0,
                    reward_risk=2.0,
                    spread_at_eval=22.0,
                    atr_regime="medium",
                    support_resistance_context="near resistance",
                    event_id="ev-1",
                    event_type="headline",
                    event_importance="high",
                    contradiction_flag=False,
                    risk_decision="approved",
                    rejection_reasons=[],
                    executed=False,
                    gemini_changed_decision=True,
                    meta_model_changed_decision=False,
                )

                await db.log_feature_snapshot(
                    candidate_id="cand-1",
                    signal_id=101,
                    schema_version="v1",
                    features={"f1": 1.0, "f2": "x"},
                )

                await db.mark_trade_candidate_execution(
                    signal_id=101,
                    executed=True,
                    ticket=555,
                    fill_price=4422.5,
                    slippage_estimate=0.0,
                    margin_snapshot={"equity": 10000},
                )

                await db.log_trade_outcome(
                    ticket=555,
                    signal_id=101,
                    symbol="XAUUSD",
                    action="SELL",
                    confidence=0.68,
                    profit=25.0,
                    exit_reason="manual_close",
                    holding_minutes=12.0,
                    symbol_category="Commodities",
                    strategy="trend_follow",
                    planned_hold_minutes=90,
                    outcome_json={"retcode": 10009},
                )

                cur = await db._db.execute(
                    "SELECT executed, execution_ticket, execution_fill_price, net_pnl, exit_reason FROM trade_candidates WHERE signal_id = 101"
                )
                row = await cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], 1)
                self.assertEqual(row[1], 555)
                self.assertAlmostEqual(float(row[2]), 4422.5, places=6)
                self.assertAlmostEqual(float(row[3]), 25.0, places=6)
                self.assertEqual(row[4], "manual_close")

                cur = await db._db.execute(
                    "SELECT COUNT(*) FROM feature_snapshots WHERE candidate_id = 'cand-1'"
                )
                count = (await cur.fetchone())[0]
                self.assertEqual(count, 1)

                await db.close()
            finally:
                if os.path.exists(path):
                    os.remove(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
