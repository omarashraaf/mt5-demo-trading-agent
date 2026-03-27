import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.db import Database


class RuntimeStateDbTests(unittest.TestCase):
    def test_save_and_restore_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state_test.db")
            database = Database(db_path)
            asyncio.run(database.initialize())
            payload = {
                "user_policy": {"mode": "aggressive", "min_reward_risk": 1.6},
                "auto_trade_enabled": True,
                "auto_trade_scan_interval_seconds": 45,
            }
            asyncio.run(database.save_runtime_state("runtime_controls_v1", payload))
            restored = asyncio.run(database.get_runtime_state("runtime_controls_v1"))
            asyncio.run(database.close())

            self.assertIsNotNone(restored)
            self.assertEqual(restored["user_policy"]["mode"], "aggressive")
            self.assertTrue(restored["auto_trade_enabled"])
            self.assertEqual(restored["auto_trade_scan_interval_seconds"], 45)

