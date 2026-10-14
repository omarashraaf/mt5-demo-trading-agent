import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.api_key_health_service import ApiKeyHealthConfig, ApiKeyHealthService


class _Adapter:
    def __init__(self, *, enabled=True, available=True, degraded=False, status="healthy"):
        self.enabled = enabled
        self.available = available
        self.degraded = degraded
        self.status = status

    def healthcheck(self, *, force_refresh: bool = False):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "degraded": self.degraded,
            "status": self.status,
            "reason": "",
        }


def _service(*, email_alerts_enabled: bool = True, adapter=None) -> ApiKeyHealthService:
    return ApiKeyHealthService(
        gemini_status_getter=lambda: {"available": True, "degraded": False, "credits_pct": 80},
        benzinga_adapter=adapter or _Adapter(),
        config=ApiKeyHealthConfig(enabled=email_alerts_enabled),
    )


def _service_with_gemini(gemini_status: dict, *, adapter=None) -> ApiKeyHealthService:
    return ApiKeyHealthService(
        gemini_status_getter=lambda: gemini_status,
        benzinga_adapter=adapter or _Adapter(),
        config=ApiKeyHealthConfig(enabled=False),
    )


class ApiKeyHealthServiceTests(unittest.TestCase):
    def test_benzinga_is_reported_when_configured(self):
        with patch.dict(os.environ, {"BENZINGA_API_KEY": "configured-key"}, clear=False):
            snapshot = _service().get_snapshot(force_refresh=True)

        benzinga = next(item for item in snapshot["items"] if item["id"] == "benzinga")
        self.assertEqual(benzinga["status"], "healthy")
        self.assertTrue(benzinga["available"])
        self.assertTrue(benzinga["configured"])

    def test_benzinga_is_inactive_without_key(self):
        with patch.dict(os.environ, {"BENZINGA_API_KEY": ""}, clear=False):
            snapshot = _service().get_snapshot(force_refresh=True)

        benzinga = next(item for item in snapshot["items"] if item["id"] == "benzinga")
        self.assertEqual(benzinga["status"], "inactive")
        self.assertFalse(benzinga["configured"])

    def test_gemini_and_supabase_items_are_present(self):
        with patch.dict(os.environ, {
            "NVIDIA_API_KEY": "configured-key",
            "SUPABASE_ANON_KEY": "anon",
            "SUPABASE_SERVICE_ROLE_KEY": "service",
        }, clear=False):
            snapshot = _service().get_snapshot(force_refresh=True)

        ids = {item["id"] for item in snapshot["items"]}
        self.assertIn("gemini", ids)
        self.assertIn("benzinga", ids)
        self.assertIn("supabase_anon", ids)
        self.assertIn("supabase_service_role", ids)

    def test_gemini_ready_without_recent_success_is_warning_not_healthy(self):
        with patch.dict(os.environ, {
            "NVIDIA_API_KEY": "configured-key",
            "BENZINGA_API_KEY": "benzinga",
            "SUPABASE_ANON_KEY": "anon",
            "SUPABASE_SERVICE_ROLE_KEY": "service",
        }, clear=False):
            snapshot = _service_with_gemini({
                "available": True,
                "degraded": False,
                "credits_pct": 100,
                "state": "ready",
                "reply": "Gemini API key is configured, but no recent successful Gemini response has verified live availability yet.",
            }).get_snapshot(force_refresh=True)

        gemini = next(item for item in snapshot["items"] if item["id"] == "gemini")
        self.assertEqual(gemini["status"], "warning")
        self.assertEqual(snapshot["overall_status"], "warning")
        self.assertEqual(snapshot["warning_count"], 1)
        self.assertEqual(snapshot["unhealthy_count"], 0)

    def test_status_email_alerts_can_be_disabled_at_runtime(self):
        service = _service(email_alerts_enabled=True)

        service.set_alerts_enabled(False)
        snapshot = service.get_snapshot(force_refresh=True)

        self.assertFalse(service.email_alerts_enabled)
        self.assertFalse(snapshot["email_alerts"]["enabled"])


if __name__ == "__main__":
    unittest.main()
