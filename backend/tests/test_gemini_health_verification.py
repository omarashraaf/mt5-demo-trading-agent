import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import routes


class _FakeGeminiAgent:
    available = True
    model_name = "gemini-test"
    active_model_name = "gemini-test"
    unavailable_reason = ""

    def __init__(self):
        self._runtime_last_error = ""
        self._runtime_last_error_at = 0.0
        self._runtime_last_success_at = 0.0
        self._quota_block_until = 0.0

    @property
    def runtime_last_error(self):
        return self._runtime_last_error

    @property
    def runtime_last_error_at(self):
        return self._runtime_last_error_at

    @property
    def runtime_last_success_at(self):
        return self._runtime_last_success_at

    def mark_runtime_error(self, error: str):
        self._runtime_last_error = str(error or "")
        self._runtime_last_error_at = time.time()

    def mark_runtime_success(self):
        self._runtime_last_error = ""
        self._runtime_last_error_at = 0.0
        self._runtime_last_success_at = time.time()


class GeminiHealthVerificationTests(unittest.TestCase):
    def test_live_verification_marks_recent_success(self):
        fake_agent = _FakeGeminiAgent()
        fake_google = ModuleType("google")
        fake_genai = ModuleType("google.genai")
        fake_genai.types = SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs)

        async def fake_generate(**_kwargs):
            fake_agent.mark_runtime_success()
            return SimpleNamespace(text="LINKTRADE_GEMINI_OK")

        with patch.object(routes, "gemini_agent", fake_agent), \
            patch.object(routes, "gemini_adapter", SimpleNamespace(last_error="", last_failure_at=0.0)), \
            patch.object(routes, "_generate_gemini_content_with_fallback", fake_generate), \
            patch.dict(sys.modules, {"google": fake_google, "google.genai": fake_genai}):
            before = routes._current_gemini_status()
            result = asyncio.run(routes.verify_gemini_live_availability())
            after = routes._current_gemini_status()

        self.assertEqual(before["state"], "ready")
        self.assertIn("no recent successful", before["reply"])
        self.assertTrue(result["ok"])
        self.assertEqual(after["state"], "available")
        self.assertEqual(after["reply"], "Yes. Gemini is available and healthy right now.")


if __name__ == "__main__":
    unittest.main()
