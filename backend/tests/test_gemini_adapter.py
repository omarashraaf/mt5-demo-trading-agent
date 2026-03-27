import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.interface import TradeSignal
from services.gemini_adapter import GeminiAdapter
from domain.models import AgentSignal


class _OkAgent:
    available = True
    unavailable_reason = ""

    def evaluate(self, _input_data):
        return TradeSignal(action="BUY", confidence=0.7, reason="Aligned")


class _FailAgent:
    available = True
    unavailable_reason = ""

    def assess(self, _input_data, technical_signal=None):
        raise RuntimeError("bad response")


class _UnavailableAgent:
    available = False
    unavailable_reason = "missing dependency"

    def evaluate(self, _input_data):
        raise AssertionError("should not be called")


class _MalformedAgent:
    available = True
    unavailable_reason = ""

    def assess(self, _input_data, technical_signal=None):
        raise ValueError("Malformed Gemini JSON")


class GeminiAdapterTests(unittest.TestCase):
    def test_confirmed_signal_boosts_confidence(self):
        adapter = GeminiAdapter(_OkAgent(), timeout_seconds=1, max_retries=0)
        primary = AgentSignal(agent_name="SmartAgent", action="BUY", confidence=0.6, reason="Trend")
        result = asyncio.run(adapter.confirm(object(), primary))

        self.assertTrue(result.used)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.confidence_delta, 0.05)

    def test_failure_degrades_cleanly(self):
        adapter = GeminiAdapter(_FailAgent(), timeout_seconds=1, max_retries=0)
        primary = AgentSignal(agent_name="SmartAgent", action="BUY", confidence=0.6, reason="Trend")
        result = asyncio.run(adapter.confirm(object(), primary))

        self.assertTrue(result.degraded)
        self.assertIn("Proceeding with deterministic", result.reason)
        self.assertIsNotNone(adapter.last_error)

    def test_unavailable_agent_short_circuits(self):
        adapter = GeminiAdapter(_UnavailableAgent(), timeout_seconds=1, max_retries=0)
        primary = AgentSignal(agent_name="SmartAgent", action="BUY", confidence=0.6, reason="Trend")
        result = asyncio.run(adapter.confirm(object(), primary))

        self.assertFalse(result.used)
        self.assertFalse(result.available)
        self.assertEqual(result.summary_reason, "missing dependency")

    def test_malformed_output_falls_back_cleanly(self):
        adapter = GeminiAdapter(_MalformedAgent(), timeout_seconds=1, max_retries=0)
        primary = AgentSignal(agent_name="SmartAgent", action="BUY", confidence=0.6, reason="Trend")
        result = asyncio.run(adapter.confirm(object(), primary))

        self.assertTrue(result.degraded)
        self.assertIn("deterministic technical logic", result.summary_reason)
