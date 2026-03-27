import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.background_state import BackgroundServiceState


class BackgroundServiceStateTests(unittest.TestCase):
    def test_state_tracks_start_cycle_and_stop(self):
        state = BackgroundServiceState(name="scanner")

        state.mark_started()
        self.assertTrue(state.running)
        self.assertTrue(state.desired_running)
        self.assertGreater(state.last_started_at, 0)

        time.sleep(0.01)
        state.mark_cycle()
        self.assertGreaterEqual(state.last_cycle_at, state.last_started_at)

        state.mark_error("boom")
        self.assertEqual(state.last_error, "boom")

        state.mark_stopped()
        self.assertFalse(state.running)
        self.assertFalse(state.desired_running)
        self.assertGreater(state.last_stopped_at, 0)
