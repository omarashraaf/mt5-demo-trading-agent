import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.cloud_brain_decision_client import CloudBrainDecisionClient


class CloudBrainDecisionClientTests(unittest.TestCase):
    def test_non_finite_timeouts_fall_back_to_safe_defaults(self):
        client = CloudBrainDecisionClient(
            enabled=True,
            url="http://example.invalid/decision",
            timeout_seconds=float("nan"),
        )

        self.assertTrue(math.isfinite(client.timeout_seconds))
        self.assertEqual(client.timeout_seconds, 6.0)
        self.assertEqual(client._bounded_timeout(float("inf"), default=client.timeout_seconds, minimum=0.5), 6.0)


if __name__ == "__main__":
    unittest.main()
