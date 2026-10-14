from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

FERNET_PREFIX = "fernet:"


@dataclass(frozen=True)
class StoredSecret:
    key: str
    backend: str
    encrypted: bool


def redact_secret(value: str | None, *, visible: int = 4) -> str:
    cleaned = str(value or "")
    if not cleaned:
        return ""
    if len(cleaned) <= visible * 2:
        return "*" * len(cleaned)
    return f"{cleaned[:visible]}...{cleaned[-visible:]}"


class SecretStore:
    """Unified secret access for local and cloud secrets.

    Keyring remains the preferred Windows/local backend. Cloud secrets are
    encrypted client-side when an encryption key is configured; legacy plaintext
    rows can be read only to migrate them into encrypted form.
    """

    def __init__(
        self,
        *,
        mode: str = "keyring",
        encryption_key: str = "",
        credential_vault: Any = None,
        cloud_secret_service: Any = None,
        allow_insecure_dev: bool = False,
    ):
        self.mode = str(mode or "keyring").strip().lower()
        self.credential_vault = credential_vault
        self.cloud_secret_service = cloud_secret_service
        self.allow_insecure_dev = bool(allow_insecure_dev)
        self._fernet: Optional[Fernet] = None
        key = str(encryption_key or "").strip()
        if key:
            self._fernet = Fernet(key.encode("utf-8"))

    @property
    def encryption_available(self) -> bool:
        return self._fernet is not None

    @property
    def secure_storage_available(self) -> bool:
        if self.mode == "keyring":
            return bool(getattr(self.credential_vault, "available", False))
        if self.mode in {"cloud", "encrypted"}:
            return self.encryption_available
        return self.mode == "insecure_dev" and self.allow_insecure_dev

    def encrypt_secret(self, value: str) -> str:
        if self._fernet is None:
            if self.allow_insecure_dev and self.mode == "insecure_dev":
                logger.warning("Using insecure development secret storage mode.")
                return str(value or "")
            raise RuntimeError("Secret encryption key is not configured.")
        token = self._fernet.encrypt(str(value or "").encode("utf-8")).decode("ascii")
        return f"{FERNET_PREFIX}{token}"

    def decrypt_secret(self, value: str) -> str:
        raw = str(value or "")
        if not raw:
            return ""
        if raw.startswith(FERNET_PREFIX):
            if self._fernet is None:
                raise RuntimeError("Secret encryption key is required to decrypt this secret.")
            token = raw[len(FERNET_PREFIX) :]
            try:
                return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except InvalidToken as exc:
                raise RuntimeError("Encrypted secret is invalid or was encrypted with a different key.") from exc
        return raw

    def store_cloud_secret(self, key: str, value: str) -> StoredSecret:
        if self.cloud_secret_service is None or not getattr(self.cloud_secret_service, "configured", False):
            raise RuntimeError("Cloud secret service is not configured.")
        encrypted = True
        payload = self.encrypt_secret(value)
        self.cloud_secret_service.upsert_secret(key=key, value=payload)
        return StoredSecret(key=key, backend="cloud", encrypted=encrypted)

    def get_cloud_secret(self, key: str, *, migrate_plaintext: bool = False) -> str:
        if self.cloud_secret_service is None or not getattr(self.cloud_secret_service, "configured", False):
            return ""
        row = self.cloud_secret_service.get_secret(key)
        raw = str((row or {}).get("value") or "")
        if not raw:
            return ""
        plaintext = self.decrypt_secret(raw)
        if migrate_plaintext and raw and not raw.startswith(FERNET_PREFIX) and self.encryption_available:
            self.store_cloud_secret(key, plaintext)
        return plaintext
