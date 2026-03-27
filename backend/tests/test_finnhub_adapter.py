import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.finnhub_adapter import FinnhubAdapter, FinnhubAdapterError


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FinnhubAdapterTests(unittest.TestCase):
    def test_healthcheck_degrades_without_key(self):
        adapter = FinnhubAdapter(enabled=True, api_key="")
        result = adapter.healthcheck()
        self.assertTrue(result["enabled"])
        self.assertTrue(result["degraded"])
        self.assertFalse(result["available"])

    def test_market_news_request_is_authenticated(self):
        session = _Session([_Response(payload=[{"id": 1, "headline": "test"}])])
        adapter = FinnhubAdapter(
            enabled=True,
            api_key="demo-key",
            session=session,
        )
        items = adapter.get_market_news()
        self.assertEqual(len(items), 1)
        self.assertEqual(session.calls[0]["params"]["token"], "demo-key")

    def test_http_errors_are_normalized(self):
        session = _Session([_Response(status_code=429, text="rate limit")])
        adapter = FinnhubAdapter(
            enabled=True,
            api_key="demo-key",
            session=session,
            max_retries=0,
        )
        with self.assertRaises(FinnhubAdapterError):
            adapter.get_market_news()
