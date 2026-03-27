import asyncio
import importlib.util
import os
import tempfile
import unittest

from research.model_trainer import ModelTrainer
from storage.db import Database


class ModelTrainerDependencyTests(unittest.TestCase):
    def test_trainer_dependency_handling(self):
        async def _run():
            fd, path = tempfile.mkstemp(prefix="ta-model-trainer-", suffix=".db")
            os.close(fd)
            try:
                db = Database(path)
                await db.initialize()
                trainer = ModelTrainer(db, artifacts_dir=tempfile.gettempdir())

                sklearn_available = bool(importlib.util.find_spec("sklearn"))
                if sklearn_available:
                    self.assertTrue(True)
                else:
                    with self.assertRaises(RuntimeError):
                        await trainer.train_candidate_model(min_rows=1)
                await db.close()
            finally:
                if os.path.exists(path):
                    os.remove(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
