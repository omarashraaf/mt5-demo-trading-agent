from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import keyring
except Exception:  # pragma: no cover - depends on optional dependency
    keyring = None


@dataclass(frozen=True)
class StoredCredentialSecret:
    secret_ref: str
    backend: str


class CredentialVault:
    def __init__(self, service_name: str = "mt5-demo-trading-agent"):
        self.service_name = service_name

    @property
    def available(self) -> bool:
        return keyring is not None

    def build_secret_ref(self, account: int, server: str) -> str:
        normalized = server.replace(" ", "_")
        return f"mt5://{account}@{normalized}"

    def save_password(self, account: int, server: str, password: str) -> StoredCredentialSecret:
        if not self.available:
            raise RuntimeError("Secure credential storage is unavailable on this machine.")
        secret_ref = self.build_secret_ref(account, server)
        keyring.set_password(self.service_name, secret_ref, password)
        return StoredCredentialSecret(secret_ref=secret_ref, backend="keyring")

    def get_password(self, secret_ref: str, backend: str) -> Optional[str]:
        if backend != "keyring":
            return None
        if not self.available:
            return None
        return keyring.get_password(self.service_name, secret_ref)

    def delete_password(self, secret_ref: str, backend: str):
        if backend != "keyring" or not self.available:
            return
        try:
            keyring.delete_password(self.service_name, secret_ref)
        except Exception as exc:  # pragma: no cover - backend dependent
            logger.warning("Failed to delete secure credential %s: %s", secret_ref, exc)

