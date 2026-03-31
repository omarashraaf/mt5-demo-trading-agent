from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class SupabaseAdminService:
    def __init__(
        self,
        *,
        url: str,
        anon_key: str,
        service_role_key: str,
        timeout_seconds: float = 10.0,
    ):
        self.url = (url or "").rstrip("/")
        self.anon_key = anon_key or ""
        self.service_role_key = service_role_key or ""
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_role_key)

    def get_user_from_token(self, access_token: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Supabase is not configured.")
        headers = {
            "apikey": self.anon_key or self.service_role_key,
            "Authorization": f"Bearer {access_token}",
        }
        response = requests.get(
            f"{self.url}/auth/v1/user",
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError("Invalid or expired auth session.")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError("Could not resolve authenticated user.")
        return payload

    def list_users(self, *, page: int = 1, per_page: int = 200) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Supabase is not configured.")
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }
        response = requests.get(
            f"{self.url}/auth/v1/admin/users",
            headers=headers,
            params={"page": page, "per_page": per_page},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._extract_error(response))
        return response.json()

    def create_user(
        self,
        *,
        email: str,
        password: str,
        role: str = "user",
        email_confirm: bool = True,
        user_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Supabase is not configured.")
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "email": email.strip().lower(),
            "password": password,
            "email_confirm": bool(email_confirm),
            "app_metadata": {"role": role},
        }
        if user_metadata:
            payload["user_metadata"] = user_metadata
        response = requests.post(
            f"{self.url}/auth/v1/admin/users",
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._extract_error(response))
        return response.json()

    def update_user_role(self, *, user_id: str, role: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Supabase is not configured.")
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        response = requests.put(
            f"{self.url}/auth/v1/admin/users/{user_id}",
            headers=headers,
            json={"app_metadata": {"role": role}},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._extract_error(response))
        return response.json()

    def ensure_bootstrap_admin(self, *, username: str, password: str) -> dict[str, Any]:
        email = f"{username.strip().lower()}@linktrade.local"
        users = self.list_users(page=1, per_page=1000).get("users", [])
        existing = next((u for u in users if str(u.get("email", "")).lower() == email), None)
        if existing:
            user_id = str(existing.get("id") or "")
            if user_id:
                self.update_user_role(user_id=user_id, role="admin")
            return {"created": False, "email": email, "user_id": user_id}
        created = self.create_user(
            email=email,
            password=password,
            role="admin",
            email_confirm=True,
            user_metadata={"username": username.strip()},
        )
        return {"created": True, "email": email, "user_id": created.get("id")}

    def _extract_error(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("msg") or payload.get("message") or payload.get("error_description") or payload.get("error") or f"Supabase HTTP {response.status_code}")
        except Exception:
            pass
        return f"Supabase HTTP {response.status_code}"
