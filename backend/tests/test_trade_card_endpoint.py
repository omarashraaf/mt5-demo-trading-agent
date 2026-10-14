import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import routes
from storage.db import Database


class TradeCardEndpointTests(unittest.TestCase):
    def test_trade_card_combines_signal_risk_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            old_db = routes.db
            try:
                signal_id = asyncio.run(
                    db.log_signal(
                        agent_name="SmartAgent",
                        symbol="XAUUSD",
                        timeframe="H1",
                        action="BUY",
                        confidence=0.82,
                        stop_loss=1990.0,
                        take_profit=2030.0,
                        max_holding_minutes=90,
                        reason="trend aligned",
                    )
                )
                asyncio.run(db.log_risk_decision(signal_id, True, "Risk approved.", 0.01))
                asyncio.run(
                    db.log_order(
                        signal_id=signal_id,
                        symbol="XAUUSD",
                        action="BUY",
                        volume=0.01,
                        price=2000.0,
                        stop_loss=1990.0,
                        take_profit=2030.0,
                        ticket=123456,
                        retcode=10009,
                        retcode_desc="Request completed",
                        success=True,
                        execution_mode="shadow",
                        idempotency_key="test-key",
                    )
                )
                routes.db = db

                card = asyncio.run(routes.get_trade_card(signal_id))
            finally:
                routes.db = old_db
                asyncio.run(db.close())

        self.assertEqual(card["symbol"], "XAUUSD")
        self.assertEqual(card["action"], "BUY")
        self.assertEqual(card["final_decision"], "executed")
        self.assertEqual(card["broker_preflight_status"]["approved"], True)
        self.assertEqual(card["execution_status"]["ticket"], 123456)
        self.assertEqual(card["execution_status"]["execution_mode"], "shadow")


if __name__ == "__main__":
    unittest.main()
