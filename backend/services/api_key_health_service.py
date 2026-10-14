from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable


logger = logging.getLogger(__name__)

API_KEY_HEALTH_SETTINGS_STATE_KEY = "api_key_health_settings_v1"


@dataclass
class ApiKeyHealthConfig:
    enabled: bool = True
    check_interval_seconds: int = 60
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sender_email: str = ""
    receiver_email: str = ""
    alert_cooldown_seconds: int = 900


class ApiKeyHealthService:
    def __init__(
        self,
        *,
        gemini_status_getter: Callable[[], dict[str, Any]],
        benzinga_adapter: Any,
        config: ApiKeyHealthConfig,
    ):
        self._gemini_status_getter = gemini_status_getter
        self._benzinga_adapter = benzinga_adapter
        self._cfg = config
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_snapshot: dict[str, Any] | None = None
        self._last_alert_key: str = ""
        self._last_alert_at: float = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_alert_at(self) -> float:
        return self._last_alert_at

    @property
    def email_alerts_enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def set_alerts_enabled(self, enabled: bool) -> None:
        self._cfg.enabled = bool(enabled)
        if not enabled:
            self._last_alert_key = ""

    def _configured_for_email(self) -> bool:
        return bool(
            self._cfg.smtp_user.strip()
            and self._cfg.smtp_password.strip()
            and self._cfg.sender_email.strip()
            and self._cfg.receiver_email.strip()
        )

    def _item_status(
        self,
        *,
        enabled: bool,
        configured: bool,
        available: bool,
        degraded: bool,
        credits_pct: int | None = None,
    ) -> str:
        if not enabled:
            return "disabled"
        if not configured:
            return "inactive"
        if not available:
            return "offline"
        if degraded:
            return "degraded"
        if credits_pct is not None and credits_pct <= 10:
            return "low_credits"
        return "healthy"

    def _build_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        items: list[dict[str, Any]] = []

        gemini = dict(self._gemini_status_getter() or {})
        gemini_available = bool(gemini.get("available"))
        gemini_degraded = bool(gemini.get("degraded"))
        gemini_credits = int(gemini.get("credits_pct", 0) or 0)
        gemini_state = str(gemini.get("state", "unknown"))
        gemini_reason = str(gemini.get("last_error") or gemini.get("reply") or "")
        gemini_configured = bool(
            os.getenv("NVIDIA_API_KEY", "").strip()
            or os.getenv("GEMINI_API_KEY", "").strip()
            or gemini_available
        )
        gemini_status = self._item_status(
            enabled=True,
            configured=gemini_configured,
            available=gemini_available,
            degraded=gemini_degraded,
            credits_pct=gemini_credits,
        )
        if gemini_status == "healthy" and gemini_state == "ready" and "no recent successful" in gemini_reason.lower():
            gemini_status = "warning"
        items.append(
            {
                "id": "gemini",
                "name": "Gemini",
                "configured": gemini_configured,
                "enabled": True,
                "available": gemini_available,
                "degraded": gemini_degraded,
                "state": gemini_state,
                "credits_pct": gemini_credits,
                "status": gemini_status,
                "reason": gemini_reason,
                "checked_at": now,
            }
        )

        try:
            benzinga = dict(self._benzinga_adapter.healthcheck(force_refresh=force_refresh) or {})
        except Exception as exc:
            benzinga = {
                "enabled": True,
                "available": False,
                "degraded": True,
                "reason": str(exc),
                "status": "degraded",
            }
        benzinga_enabled = bool(benzinga.get("enabled"))
        benzinga_available = bool(benzinga.get("available"))
        benzinga_degraded = bool(benzinga.get("degraded"))
        items.append(
            {
                "id": "benzinga",
                "name": "Benzinga",
                "configured": bool(os.getenv("BENZINGA_API_KEY", "").strip()),
                "enabled": benzinga_enabled,
                "available": benzinga_available,
                "degraded": benzinga_degraded,
                "state": str(benzinga.get("status") or ("degraded" if benzinga_degraded else "healthy")),
                "credits_pct": None,
                "status": self._item_status(
                    enabled=benzinga_enabled,
                    configured=bool(os.getenv("BENZINGA_API_KEY", "").strip()),
                    available=benzinga_available,
                    degraded=benzinga_degraded,
                ),
                "reason": str(benzinga.get("reason") or ""),
                "checked_at": now,
            }
        )

        supabase_anon = str(os.getenv("SUPABASE_ANON_KEY", "")).strip()
        items.append(
            {
                "id": "supabase_anon",
                "name": "Supabase Anon",
                "configured": bool(supabase_anon),
                "enabled": True,
                "available": bool(supabase_anon),
                "degraded": not bool(supabase_anon),
                "state": "configured" if supabase_anon else "missing",
                "credits_pct": None,
                "status": self._item_status(
                    enabled=True,
                    configured=bool(supabase_anon),
                    available=bool(supabase_anon),
                    degraded=not bool(supabase_anon),
                ),
                "reason": "" if supabase_anon else "SUPABASE_ANON_KEY missing.",
                "checked_at": now,
            }
        )

        supabase_service = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
        items.append(
            {
                "id": "supabase_service_role",
                "name": "Supabase Service Role",
                "configured": bool(supabase_service),
                "enabled": True,
                "available": bool(supabase_service),
                "degraded": not bool(supabase_service),
                "state": "configured" if supabase_service else "missing",
                "credits_pct": None,
                "status": self._item_status(
                    enabled=True,
                    configured=bool(supabase_service),
                    available=bool(supabase_service),
                    degraded=not bool(supabase_service),
                ),
                "reason": "" if supabase_service else "SUPABASE_SERVICE_ROLE_KEY missing.",
                "checked_at": now,
            }
        )

        finnhub_enabled = str(os.getenv("ENABLE_FINNHUB", "")).strip().lower() in {"1", "true", "yes", "on"}
        finnhub_key = str(os.getenv("FINNHUB_API_KEY", "")).strip()
        if finnhub_enabled:
            items.append(
                {
                    "id": "finnhub",
                    "name": "Finnhub",
                    "configured": bool(finnhub_key),
                    "enabled": True,
                    "available": bool(finnhub_key),
                    "degraded": False,
                    "state": "configured" if finnhub_key else "missing_key",
                    "credits_pct": None,
                    "status": self._item_status(
                        enabled=True,
                        configured=bool(finnhub_key),
                        available=bool(finnhub_key),
                        degraded=False,
                    ),
                    "reason": "" if finnhub_key else "FINNHUB_API_KEY missing while ENABLE_FINNHUB=true.",
                    "checked_at": now,
                }
            )

        unhealthy = [
            item for item in items
            if item["status"] in {"inactive", "offline", "degraded", "low_credits"}
        ]
        warnings = [
            item for item in items
            if item["status"] == "warning"
        ]
        overall = "degraded" if unhealthy else ("warning" if warnings else "healthy")

        return {
            "checked_at": now,
            "overall_status": overall,
            "items": items,
            "unhealthy_count": len(unhealthy),
            "warning_count": len(warnings),
            "email_alerts": {
                "enabled": bool(self._cfg.enabled),
                "configured": self._configured_for_email(),
                "last_alert_at": self._last_alert_at or None,
            },
        }

    def get_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if force_refresh or self._last_snapshot is None:
            self._last_snapshot = self._build_snapshot(force_refresh=force_refresh)
        return dict(self._last_snapshot)

    async def run_check(self, *, force_refresh: bool = True, send_alerts: bool = True) -> dict[str, Any]:
        snapshot = self._build_snapshot(force_refresh=force_refresh)
        self._last_snapshot = snapshot
        if send_alerts and self._cfg.enabled:
            await self._maybe_send_alert(snapshot)
        return dict(snapshot)

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="api-key-health-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = self._build_snapshot(force_refresh=True)
                self._last_snapshot = snapshot
                if self._cfg.enabled:
                    await self._maybe_send_alert(snapshot)
            except Exception as exc:
                logger.warning("API key health monitor iteration failed: %s", exc)
            wait_seconds = max(15, int(self._cfg.check_interval_seconds or 60))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue

    async def _maybe_send_alert(self, snapshot: dict[str, Any]) -> None:
        unhealthy = [
            item for item in (snapshot.get("items") or [])
            if str(item.get("status") or "").lower() in {"inactive", "offline", "degraded", "low_credits"}
        ]
        if not unhealthy:
            self._last_alert_key = ""
            return
        if not self._configured_for_email():
            logger.warning("API health alerts enabled but SMTP/email config is incomplete.")
            return

        fingerprint_parts = []
        for item in unhealthy:
            fingerprint_parts.append(f"{item.get('id')}:{item.get('status')}:{item.get('reason')}")
        fingerprint = "|".join(sorted(fingerprint_parts))
        now = time.time()
        if fingerprint == self._last_alert_key and (now - self._last_alert_at) < max(60, int(self._cfg.alert_cooldown_seconds or 900)):
            return

        subject = f"[LinkTrade] API Key Health Alert ({len(unhealthy)} issue{'s' if len(unhealthy) != 1 else ''})"
        lines = [
            "LinkTrade detected API health issues:",
            "",
        ]
        for item in unhealthy:
            lines.append(
                f"- {item.get('name')}: status={item.get('status')} state={item.get('state')} reason={item.get('reason') or 'n/a'}"
            )
        lines.append("")
        lines.append(f"Checked at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(snapshot.get('checked_at') or now)))}")
        body = "\n".join(lines)

        await asyncio.to_thread(self._send_email, subject, body)
        self._last_alert_key = fingerprint
        self._last_alert_at = now

    def _send_email(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._cfg.sender_email
        msg["To"] = self._cfg.receiver_email
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(self._cfg.smtp_host, int(self._cfg.smtp_port), timeout=20) as server:
            server.starttls()
            server.login(self._cfg.smtp_user, self._cfg.smtp_password)
            server.send_message(msg)
