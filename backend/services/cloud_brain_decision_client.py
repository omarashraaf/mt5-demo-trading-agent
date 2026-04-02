from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class CloudBrainDecisionClient:
    """Calls cloud decision API with local analyzed factors."""

    def __init__(
        self,
        *,
        enabled: bool,
        url: str,
        timeout_seconds: float = 6.0,
    ):
        self.enabled = bool(enabled)
        self.url = (url or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds or 6.0))
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.url)

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            return {"used": False, "reason": "cloud_decision_not_configured"}
        try:
            result = await asyncio.to_thread(self._decide_sync, payload)
            self.last_error = None
            return {"used": True, **result}
        except Exception as exc:
            self.last_error = str(exc)
            logger.debug("Cloud decision failed: %s", exc)
            return {"used": False, "reason": f"cloud_decision_failed: {exc}"}

    def _decide_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            self.url,
            json=payload or {},
            timeout=self.timeout_seconds,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("invalid cloud response")
        return body

