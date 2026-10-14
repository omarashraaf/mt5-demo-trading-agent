from __future__ import annotations

import asyncio
import logging
import math
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
        self.timeout_seconds = self._bounded_timeout(timeout_seconds, default=6.0, minimum=1.0)
        self.last_error: str | None = None
        self.disabled_reason: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.url and not self.disabled_reason)

    async def decide(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            reason = "cloud_decision_not_configured"
            if self.disabled_reason:
                reason = f"cloud_decision_disabled: {self.disabled_reason}"
            return {"used": False, "reason": reason}
        try:
            result = await asyncio.to_thread(self._decide_sync, payload, timeout_seconds)
            self.last_error = None
            self.disabled_reason = None
            return {"used": True, **result}
        except Exception as exc:
            error_text = str(exc)
            if error_text == "HTTP 404":
                # A 404 means the configured route does not exist, so keep local
                # trading logic active and stop repeatedly treating this as a live fault.
                self.enabled = False
                self.disabled_reason = "configured endpoint returned HTTP 404"
                self.last_error = None
                logger.warning("Cloud decision disabled after permanent endpoint failure: %s", error_text)
                return {"used": False, "reason": f"cloud_decision_disabled: {self.disabled_reason}"}
            self.last_error = error_text
            logger.debug("Cloud decision failed: %s", exc)
            return {"used": False, "reason": f"cloud_decision_failed: {exc}"}

    def _decide_sync(self, payload: dict[str, Any], timeout_seconds: float | None = None) -> dict[str, Any]:
        request_timeout = self.timeout_seconds if timeout_seconds is None else self._bounded_timeout(
            timeout_seconds,
            default=self.timeout_seconds,
            minimum=0.5,
        )
        response = requests.post(
            self.url,
            json=payload or {},
            timeout=request_timeout,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("invalid cloud response")
        return body

    def _bounded_timeout(self, value: Any, *, default: float, minimum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = float(default)
        if not math.isfinite(parsed):
            parsed = float(default)
        return max(float(minimum), parsed)
