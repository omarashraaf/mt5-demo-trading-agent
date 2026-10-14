import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5.execution import OrderRequest
from services.execution_service import ExecutionService
from storage.db import Database


class P1OrderIdempotencyTests(unittest.TestCase):
    def test_order_request_accepts_idempotency_key(self):
        req = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss=99.0,
            take_profit=101.0,
            idempotency_key="manual-123",
        )
        self.assertEqual(req.idempotency_key, "manual-123")

    def test_log_order_enforces_unique_idempotency_key_per_account_context(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "orders.db"))
                await db.initialize()
                try:
                    await db.log_order(
                        signal_id=None,
                        symbol="US500",
                        action="BUY",
                        volume=0.01,
                        price=100.0,
                        stop_loss=99.0,
                        take_profit=101.0,
                        ticket=111,
                        retcode=10009,
                        retcode_desc="sent",
                        success=True,
                        comment="first",
                        idempotency_key="same-key",
                        account_key="demo:123",
                        execution_context="manual",
                    )
                    await db.log_order(
                        signal_id=None,
                        symbol="US500",
                        action="BUY",
                        volume=0.01,
                        price=100.0,
                        stop_loss=99.0,
                        take_profit=101.0,
                        ticket=222,
                        retcode=10009,
                        retcode_desc="duplicate",
                        success=True,
                        comment="second",
                        idempotency_key="same-key",
                        account_key="demo:123",
                        execution_context="manual",
                    )
                    rows = await db.get_orders_by_idempotency_key(
                        idempotency_key="same-key",
                        account_key="demo:123",
                        execution_context="manual",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["ticket"], 111)
                finally:
                    await db.close()

        asyncio.run(scenario())

    def test_shadow_log_order_is_mirrored_to_simulated_results(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "orders.db"))
                await db.initialize()
                try:
                    await db.log_order(
                        signal_id=None,
                        symbol="US500",
                        action="BUY",
                        volume=0.01,
                        price=100.0,
                        stop_loss=99.0,
                        take_profit=102.5,
                        ticket=None,
                        retcode=0,
                        retcode_desc="Shadow execution recorded; broker order_send was not called.",
                        success=True,
                        comment="shadow",
                        idempotency_key="shadow-key",
                        account_key="demo:123",
                        execution_context="auto",
                        execution_mode="shadow",
                    )
                    rows = await db.get_recent_simulated_execution_results(limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["execution_mode"], "shadow")
                    self.assertEqual(rows[0]["idempotency_key"], "shadow-key")
                finally:
                    await db.close()

        asyncio.run(scenario())

    def test_execution_service_schedules_preflight_snapshot(self):
        class Engine:
            def __init__(self):
                self.called = False

            def place_order(self, _order):
                self.called = True
                from mt5.execution import OrderResult

                return OrderResult(success=True, retcode=10009, retcode_desc="sent")

        class FakeDb:
            def __init__(self):
                self.snapshots = []

            async def log_execution_preflight_snapshot(self, **kwargs):
                self.snapshots.append(kwargs)

        async def scenario():
            engine = Engine()
            db = FakeDb()
            service = ExecutionService(
                execution_engine=engine,
                risk_service=type("RiskService", (), {"risk_engine": type("RiskEngine", (), {"settings": type("Settings", (), {"min_reward_risk_ratio": 1.2})()})()})(),
                signal_pipeline=None,
                db=db,
            )
            result = service.place_order_if_approved(
                OrderRequest(
                    symbol="US500",
                    action="BUY",
                    volume=0.01,
                    stop_loss=99.0,
                    take_profit=102.5,
                    idempotency_key="snap-key",
                    account_key="demo:123",
                    execution_context="manual",
                ),
                type(
                    "Preflight",
                    (),
                    {
                        "approved": True,
                        "reason": "ok",
                        "entry_price": 100.0,
                        "required_reward_risk": 1.2,
                        "spread_cost": 0.0,
                        "model_dump": lambda self: {"approved": True, "reason": "ok"},
                    },
                )(),
            )
            await asyncio.sleep(0)
            self.assertTrue(result.success)
            self.assertTrue(engine.called)
            self.assertEqual(len(db.snapshots), 1)
            self.assertEqual(db.snapshots[0]["idempotency_key"], "snap-key")

        asyncio.run(scenario())

    def test_shadow_execution_does_not_call_underlying_order_engine(self):
        class Engine:
            def __init__(self):
                self.called = False

            def place_order(self, _order):
                self.called = True
                raise AssertionError("order_send path must not be called in shadow mode")

        engine = Engine()
        service = ExecutionService(
            execution_engine=engine,
            risk_service=type("RiskService", (), {"risk_engine": type("RiskEngine", (), {"settings": type("Settings", (), {"min_reward_risk_ratio": 1.2})()})()})(),
            signal_pipeline=None,
        )
        result = service.place_order_if_approved(
            OrderRequest(
                symbol="US500",
                action="BUY",
                volume=0.01,
                stop_loss=99.0,
                take_profit=102.5,
                execution_mode="shadow",
            ),
            type(
                "Preflight",
                (),
                {
                    "approved": True,
                    "reason": "ok",
                    "entry_price": 100.0,
                    "required_reward_risk": 1.2,
                    "spread_cost": 0.0,
                },
            )(),
        )
        self.assertTrue(result.success)
        self.assertFalse(engine.called)
        self.assertIn("order_send was not called", result.retcode_desc)


if __name__ == "__main__":
    unittest.main()
