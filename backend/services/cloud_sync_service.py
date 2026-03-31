from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class CloudSyncService:
    """Push selected local runtime logs to Supabase REST table.

    Best-effort only: failures never block local trading flow.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        supabase_url: str,
        service_role_key: str,
        table: str = "runtime_logs",
        timeout_seconds: float = 8.0,
    ):
        self.enabled = bool(enabled)
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.service_role_key = service_role_key or ""
        self.table = (table or "runtime_logs").strip()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.supabase_url and self.service_role_key and self.table)

    async def emit(self, event_type: str, payload: dict[str, Any]):
        if not self.configured:
            return
        try:
            await asyncio.to_thread(self._emit_sync, event_type, payload)
        except Exception as exc:
            logger.debug("Cloud log sync skipped: %s", exc)

    def _emit_sync(self, event_type: str, payload: dict[str, Any]):
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        event_payload = {
            "event_type": event_type,
            "timestamp_utc": time.time(),
            "payload": payload or {},
        }
        response = requests.post(
            f"{self.supabase_url}/rest/v1/{self.table}",
            headers=headers,
            json=event_payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            # Non-fatal: cloud table may not exist yet.
            raise RuntimeError(f"Supabase log sync failed: HTTP {response.status_code}")
