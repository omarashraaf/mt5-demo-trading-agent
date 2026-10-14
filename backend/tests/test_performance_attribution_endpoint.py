import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import routes
from storage.db import Database


class PerformanceAttributionEndpointTests(unittest.TestCase):
    def test_performance_report_groups_existing_trade_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            old_db = routes.db
            old_auth_required = routes.config.AUTH_REQUIRED
            try:
                routes.db = db
                routes.config.AUTH_REQUIRED = False
                asyncio.run(
                    db.log_trade_outcome(
                        ticket=1,
                        signal_id=None,
                        symbol="XAUUSD",
                        action="BUY",
                        confidence=0.82,
                        profit=25.0,
                        exit_reason="tp",
                        symbol_category="Commodities",
                        strategy="trend-v1",
                        outcome_json={"risk_profile": "safe", "session": "london", "news_bias": "bullish"},
                    )
                )
                asyncio.run(
                    db.log_trade_outcome(
                        ticket=2,
                        signal_id=None,
                        symbol="EURUSD",
                        action="SELL",
                        confidence=0.58,
                        profit=-10.0,
                        exit_reason="sl",
                        symbol_category="Forex",
                        strategy="mean-v2",
                        outcome_json={"risk_profile": "balanced", "session": "new_york", "news_bias": "bearish"},
                    )
                )

                report = asyncio.run(routes.performance_attribution_reports(limit=100))
            finally:
                routes.db = old_db
                routes.config.AUTH_REQUIRED = old_auth_required
                asyncio.run(db.close())

        self.assertEqual(report["sample_size"], 2)
        self.assertEqual(report["by_symbol"]["XAUUSD"]["trades"], 1)
        self.assertEqual(report["by_symbol"]["XAUUSD"]["net_pnl"], 25.0)
        self.assertEqual(report["by_strategy"]["trend-v1"]["wins"], 1)
        self.assertEqual(report["by_confidence_bucket"]["80_100"]["trades"], 1)
        self.assertEqual(report["by_session"]["london"]["trades"], 1)
        self.assertEqual(report["by_risk_profile"]["safe"]["trades"], 1)
        self.assertEqual(report["news_gemini_impact"]["bullish"]["trades"], 1)


if __name__ == "__main__":
    unittest.main()
