import asyncio
import importlib.util
import math
import os
import tempfile
import unittest

from research.model_trainer import EmpiricalRulesModel, ModelTrainer
from storage.db import Database


class ModelTrainerDependencyTests(unittest.TestCase):
    def test_numeric_feature_coercion_sanitizes_bad_and_non_finite_values(self):
        pandas_spec = importlib.util.find_spec("pandas")
        if not pandas_spec:
            self.skipTest("pandas unavailable")

        import pandas as pd

        frame = pd.DataFrame(
            [
                {"f_reward_risk": "Infinity", "f_confidence": "bad-value", "f_symbol": "XAUUSD"},
                {"f_reward_risk": "-Infinity", "f_confidence": "0.72", "f_symbol": None},
                {"f_reward_risk": "NaN", "f_confidence": "", "f_symbol": "EURUSD"},
            ]
        )

        coerced = ModelTrainer._coerce_feature_types(
            frame,
            numeric_columns=["f_reward_risk", "f_confidence"],
            categorical_columns=["f_symbol"],
        )

        numeric_values = []
        for column in ("f_reward_risk", "f_confidence"):
            numeric_values.extend(coerced[column].tolist())

        self.assertTrue(all(math.isfinite(float(value)) for value in numeric_values))
        self.assertEqual(coerced["f_reward_risk"].tolist(), [0.0, 0.0, 0.0])
        self.assertEqual(coerced["f_confidence"].tolist(), [0.0, 0.72, 0.0])
        self.assertEqual(coerced["f_symbol"].tolist(), ["XAUUSD", "", "EURUSD"])

    def test_numeric_detection_keeps_mostly_numeric_features_numeric(self):
        pandas_spec = importlib.util.find_spec("pandas")
        if not pandas_spec:
            self.skipTest("pandas unavailable")

        import pandas as pd

        frame = pd.DataFrame(
            [
                {"f_confidence": "0.61", "f_reward_risk": "2.1", "f_symbol": "XAUUSD"},
                {"f_confidence": "bad-value", "f_reward_risk": "Infinity", "f_symbol": "EURUSD"},
                {"f_confidence": "0.74", "f_reward_risk": "1.9", "f_symbol": "GBPUSD"},
            ]
        )

        numeric_columns = ModelTrainer._numeric_columns(frame)

        self.assertIn("f_confidence", numeric_columns)
        self.assertIn("f_reward_risk", numeric_columns)
        self.assertNotIn("f_symbol", numeric_columns)

    def test_empirical_rules_model_scores_known_weak_slice_low(self):
        pandas_spec = importlib.util.find_spec("pandas")
        if not pandas_spec:
            self.skipTest("pandas unavailable")

        import pandas as pd

        model = EmpiricalRulesModel()
        frame = pd.DataFrame(
            [
                {
                    "f_quality_score": 0.82,
                    "f_portfolio_free_margin_pct": 99.95,
                    "f_spread_at_eval": 4.0,
                    "f_confidence": 0.72,
                    "f_portfolio_margin_utilization_pct": 0.1,
                    "f_atr_regime": "high",
                    "f_trend_alignment_score": 0.95,
                },
                {
                    "f_quality_score": 0.66,
                    "f_portfolio_free_margin_pct": 99.0,
                    "f_spread_at_eval": 31.0,
                    "f_confidence": 0.50,
                    "f_portfolio_margin_utilization_pct": 1.0,
                    "f_atr_regime": "medium",
                    "f_trend_alignment_score": 0.70,
                },
            ]
        )

        probabilities = model.predict_proba(frame)

        self.assertGreaterEqual(probabilities[0][1], 0.60)
        self.assertLess(probabilities[1][1], 0.35)

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
