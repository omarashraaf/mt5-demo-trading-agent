import asyncio
import os
import tempfile
import time
import unittest

from research.model_registry import ModelRegistry
from storage.db import Database


class ModelRegistryTests(unittest.TestCase):
    def test_register_list_and_approve(self):
        async def _run():
            fd, path = tempfile.mkstemp(prefix="ta-model-registry-", suffix=".db")
            os.close(fd)
            try:
                db = Database(path)
                await db.initialize()
                registry = ModelRegistry(db)

                now = time.time()
                await registry.register_candidate(
                    version_id="model-a",
                    algorithm="logistic_regression",
                    target_definition="profitable_after_costs_90m",
                    feature_schema_version="v2",
                    training_date=now - 10,
                    data_range_start=now - 1000,
                    data_range_end=now - 20,
                    evaluation_metrics={"accuracy": 0.61},
                )
                await registry.register_candidate(
                    version_id="model-b",
                    algorithm="gradient_boosting",
                    target_definition="profitable_after_costs_90m",
                    feature_schema_version="v2",
                    training_date=now,
                    data_range_start=now - 1200,
                    data_range_end=now - 5,
                    evaluation_metrics={"accuracy": 0.66},
                )

                versions = await registry.list_versions(limit=10)
                self.assertEqual(len(versions), 2)
                self.assertTrue(all(v["approval_status"] == "candidate" for v in versions))

                await registry.set_status(version_id="model-a", status="approved")
                active = await registry.get_active_approved()
                self.assertIsNotNone(active)
                self.assertEqual(active["version_id"], "model-a")

                await registry.set_status(version_id="model-b", status="approved")
                active2 = await registry.get_active_approved()
                self.assertEqual(active2["version_id"], "model-b")
                v1 = await db.get_model_version("model-a")
                self.assertEqual(v1["approval_status"], "archived")

                await db.close()
            finally:
                if os.path.exists(path):
                    os.remove(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
