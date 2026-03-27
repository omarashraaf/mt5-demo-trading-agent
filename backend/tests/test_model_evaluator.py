import unittest

from research.model_evaluator import evaluate_binary_classifier, evaluate_regression


class ModelEvaluatorTests(unittest.TestCase):
    def test_binary_classifier_metrics(self):
        metrics = evaluate_binary_classifier(
            y_true=[1, 0, 1, 0, 1],
            y_pred_prob=[0.9, 0.2, 0.7, 0.6, 0.8],
            realized_returns=[10.0, -5.0, 4.0, -2.0, 6.0],
        )
        self.assertEqual(metrics["sample_count"], 5)
        self.assertGreater(metrics["accuracy"], 0.5)
        self.assertIn("confusion_matrix", metrics)
        self.assertIn("calibration", metrics)
        self.assertIn("expected_value_by_score_bucket", metrics)

    def test_regression_metrics(self):
        metrics = evaluate_regression(
            y_true=[1.0, 2.0, 3.0, 4.0],
            y_pred=[1.2, 1.9, 2.8, 4.4],
        )
        self.assertEqual(metrics["sample_count"], 4)
        self.assertGreaterEqual(metrics["mae"], 0.0)
        self.assertGreaterEqual(metrics["rmse"], 0.0)


if __name__ == "__main__":
    unittest.main()
