from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import requests

logger = logging.getLogger(__name__)


class CloudBrainService:
    """Poll cloud brain commands and apply them locally.

    The cloud can only influence high-level runtime controls/policy.
    All market analysis, risk validation, and order execution remain local.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        supabase_url: str,
        service_role_key: str,
        table: str = "brain_commands",
        timeout_seconds: float = 8.0,
        poll_seconds: int = 8,
        apply_command: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
    ):
        self.enabled = bool(enabled)
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.service_role_key = (service_role_key or "").strip()
        self.table = (table or "brain_commands").strip()
        self.timeout_seconds = max(2.0, float(timeout_seconds or 8.0))
        self.poll_seconds = max(3, int(poll_seconds or 8))
        self.apply_command = apply_command

        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_command_id: str = ""
        self._last_sync_at: float = 0.0
        self._last_error: str | None = None
        self._applied_count: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.supabase_url and self.service_role_key and self.table and self.apply_command)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "configured": bool(self.configured),
            "table": self.table,
            "poll_seconds": self.poll_seconds,
            "last_command_id": self._last_command_id or None,
            "last_sync_at": self._last_sync_at or None,
            "last_error": self._last_error,
            "applied_count": int(self._applied_count),
            "running": bool(self._task and not self._task.done()),
        }

    def start(self):
        if not self.configured:
            logger.info("Cloud brain disabled or not configured.")
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._runner(), name="cloud-brain-poller")
        logger.info("Cloud brain poller started (table=%s interval=%ss).", self.table, self.poll_seconds)

    async def stop(self):
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def sync_once(self) -> dict[str, Any]:
        if not self.configured:
            return {"applied": False, "reason": "cloud_brain_not_configured"}
        try:
            result = await asyncio.to_thread(self._fetch_and_apply_latest_sync)
            self._last_sync_at = time.time()
            self._last_error = None
            return result
        except Exception as exc:
            self._last_sync_at = time.time()
            self._last_error = str(exc)
            return {"applied": False, "error": str(exc)}

    async def _runner(self):
        while not self._stop_event.is_set():
            result = await self.sync_once()
            if result.get("error"):
                logger.debug("Cloud brain sync skipped: %s", result["error"])
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def _fetch_and_apply_latest_sync(self) -> dict[str, Any]:
        # Flexible read: keep schema-tolerant by accepting either command_json or payload.
        url = (
            f"{self.supabase_url}/rest/v1/{self.table}"
            "?select=id,created_at,active,target,command_json,payload"
            "&order=created_at.desc&limit=1"
        )
        response = requests.get(url, headers=self._headers(), timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud brain fetch failed: HTTP {response.status_code}")
        rows = response.json() or []
        if not rows:
            return {"applied": False, "reason": "no_commands"}
        row = rows[0] or {}
        if str(row.get("active", True)).lower() in {"false", "0", "no"}:
            return {"applied": False, "reason": "inactive_command", "id": row.get("id")}
        command_id = str(row.get("id") or "")
        if command_id and command_id == self._last_command_id:
            return {"applied": False, "reason": "already_applied", "id": command_id}

        payload = row.get("command_json")
        if payload is None:
            payload = row.get("payload")
        if not isinstance(payload, dict):
            return {"applied": False, "reason": "invalid_payload", "id": command_id}

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            apply_result = loop.run_until_complete(self.apply_command(payload)) if self.apply_command else {}
        finally:
            try:
                loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)

        self._last_command_id = command_id
        self._applied_count += 1
        return {
            "applied": True,
            "id": command_id or None,
            "created_at": row.get("created_at"),
            "apply_result": apply_result or {},
        }

