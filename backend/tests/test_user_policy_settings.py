import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.rules import RiskEngine, UserPolicySettings, build_policy_preset


class UserPolicySettingsTests(unittest.TestCase):
    def test_balanced_is_default_policy(self):
        engine = RiskEngine()

        self.assertEqual(engine.user_policy.mode, "balanced")
        self.assertTrue(engine.user_policy.demo_only_default)
        self.assertEqual(engine.user_policy.gemini_role, "advisory")
        self.assertFalse(engine.user_policy.allow_overnight_holding)
        self.assertEqual(engine.settings.policy_mode, "balanced")
        self.assertAlmostEqual(engine.settings.max_margin_utilization_pct, 18.0)

    def test_updating_user_policy_changes_compiled_runtime_limits(self):
        engine = RiskEngine()
        policy = build_policy_preset("balanced").model_copy(
            update={
                "max_margin_utilization": 22.0,
                "min_free_margin": 55.0,
                "max_open_trades": 4,
            }
        )

        engine.update_user_policy(policy)

        self.assertEqual(engine.settings.policy_mode, "balanced")
        self.assertEqual(engine.settings.max_open_positions_total, 4)
        self.assertAlmostEqual(engine.settings.max_margin_utilization_pct, 22.0)
        self.assertAlmostEqual(engine.settings.min_free_margin_pct, 55.0)

    def test_aggressive_preset_defaults_to_24_7_sessions(self):
        policy = build_policy_preset("aggressive")
        self.assertEqual(policy.session_filters, ["24/7"])

    def test_mode_runtime_confidence_thresholds(self):
        engine = RiskEngine()

        engine.apply_policy_preset("safe")
        self.assertAlmostEqual(engine.settings.min_confidence_threshold, 0.70)
        self.assertAlmostEqual(engine.settings.auto_trade_min_confidence, 0.72)

        engine.apply_policy_preset("balanced")
        self.assertAlmostEqual(engine.settings.min_confidence_threshold, 0.60)
        self.assertAlmostEqual(engine.settings.auto_trade_min_confidence, 0.62)

        engine.apply_policy_preset("aggressive")
        self.assertAlmostEqual(engine.settings.min_confidence_threshold, 0.45)
        self.assertAlmostEqual(engine.settings.auto_trade_min_confidence, 0.45)
