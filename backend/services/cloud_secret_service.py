from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class CloudSecretService:
    """Read/write app secrets from a Supabase REST table."""

    def __init__(
        self,
        *,
        enabled: bool,
        supabase_url: str,
        service_role_key: str,
        table: str = "app_secrets",
        timeout_seconds: float = 8.0,
    ):
        self.enabled = bool(enabled)
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.service_role_key = service_role_key or ""
        self.table = (table or "app_secrets").strip()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.supabase_url and self.service_role_key and self.table)

    def get_secret(self, key: str) -> dict[str, Any] | None:
        if not self.configured:
            return None
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None

        response = requests.get(
            f"{self.supabase_url}/rest/v1/{self.table}",
            headers=self._headers(content_type=False),
            params={
                "key": f"eq.{normalized_key}",
                "select": "key,value,updated_at",
                "limit": 1,
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud secret fetch failed: HTTP {response.status_code}")

        payload = response.json()
        if isinstance(payload, list) and payload:
            row = payload[0]
            if isinstance(row, dict):
                return row
        return None

    def upsert_secret(self, *, key: str, value: str) -> dict[str, Any] | None:
        if not self.configured:
            raise RuntimeError("Cloud secret service is not configured.")

        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise RuntimeError("Secret key is required.")

        response = requests.post(
            f"{self.supabase_url}/rest/v1/{self.table}",
            headers=self._headers(content_type=True, prefer="resolution=merge-duplicates,return=representation"),
            params={"on_conflict": "key"},
            json={"key": normalized_key, "value": str(value or "")},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud secret upsert failed: HTTP {response.status_code}")

        payload = response.json()
        if isinstance(payload, list) and payload:
            row = payload[0]
            if isinstance(row, dict):
                return row
        return None

    def _headers(self, *, content_type: bool, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        return headers
