import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from config import load_app_config
from mt5.execution import OrderRequest, OrderResult
from services.execution_service import ExecutionService
from storage.db import Database


class _FakeSupabase:
    configured = True

    def __init__(self, user):
        self.user = user

    def get_user_from_token(self, token):
        if token == "bad":
            raise RuntimeError("Invalid token")
        return self.user


class _NoopExecution:
    def __init__(self):
        self.called = False

    def place_order(self, _order_request):
        self.called = True
        return OrderResult(success=True, retcode=0, retcode_desc="sent", ticket=123)


class _MemoryCloudSecrets:
    configured = True

    def __init__(self):
        self.rows = {}

    def get_secret(self, key):
        value = self.rows.get(key)
        if value is None:
            return None
        return {"key": key, "value": value, "updated_at": 1.0}

    def upsert_secret(self, *, key, value):
        self.rows[key] = value
        return {"key": key, "value": value, "updated_at": 1.0}


class P0SecuritySafetyTests(unittest.TestCase):
    def test_auth_guard_rejects_missing_auth_for_sensitive_api(self):
        from api.auth_guard import require_api_access

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            try:
                with self.assertRaises(HTTPException) as cm:
                    asyncio.run(
                        require_api_access(
                            path="/api/status",
                            authorization=None,
                            auth_required=True,
                            database=db,
                            supabase=_FakeSupabase({"id": "approved", "email": "ok@example.com"}),
                        )
                    )
                self.assertEqual(cm.exception.status_code, 401)
            finally:
                asyncio.run(db.close())

    def test_auth_guard_rejects_authenticated_unapproved_user(self):
        from api.auth_guard import require_api_access

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            try:
                with self.assertRaises(HTTPException) as cm:
                    asyncio.run(
                        require_api_access(
                            path="/api/signals/generate",
                            authorization="Bearer good",
                            auth_required=True,
                            database=db,
                            supabase=_FakeSupabase(
                                {
                                    "id": "pending-user",
                                    "email": "pending@example.com",
                                    "user_metadata": {"access_status": "pending"},
                                    "app_metadata": {"role": "user"},
                                }
                            ),
                        )
                    )
                self.assertEqual(cm.exception.status_code, 403)
            finally:
                asyncio.run(db.close())

    def test_auth_guard_allows_approved_user_and_admin_login_public(self):
        from api.auth_guard import is_public_api_path, require_api_access

        self.assertTrue(is_public_api_path("/api/admin/login"))
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            try:
                user = asyncio.run(
                    require_api_access(
                        path="/api/signals/generate",
                        authorization="Bearer good",
                        auth_required=True,
                        database=db,
                        supabase=_FakeSupabase(
                            {
                                "id": "approved-user",
                                "email": "ok@example.com",
                                "user_metadata": {"access_status": "approved"},
                                "app_metadata": {"role": "user"},
                            }
                        ),
                    )
                )
                self.assertEqual(user["id"], "approved-user")
            finally:
                asyncio.run(db.close())

    def test_production_config_rejects_unsafe_defaults(self):
        with self.assertRaises(Exception):
            load_app_config(
                {
                    "APP_MODE": "production",
                    "AUTH_REQUIRED": "false",
                    "ADMIN_BOOTSTRAP_USERNAME": "admin",
                    "ADMIN_BOOTSTRAP_PASSWORD": "admin",
                }
            )

    def test_production_cloud_secret_mode_requires_encryption_key(self):
        with self.assertRaises(Exception):
            load_app_config(
                {
                    "APP_MODE": "production",
                    "AUTH_REQUIRED": "true",
                    "SUPABASE_URL": "https://example.supabase.co",
                    "SUPABASE_ANON_KEY": "anon",
                    "SUPABASE_SERVICE_ROLE_KEY": "service",
                    "ENABLE_ADMIN_BOOTSTRAP": "false",
                    "ADMIN_BOOTSTRAP_USERNAME": "owner",
                    "ADMIN_BOOTSTRAP_PASSWORD": "not-default",
                    "SECRET_STORAGE_MODE": "cloud",
                    "SECRET_ENCRYPTION_KEY": "",
                }
            )

    def test_secret_store_encrypts_cloud_secrets_and_decrypts_on_read(self):
        from cryptography.fernet import Fernet
        from services.secret_store import SecretStore

        cloud = _MemoryCloudSecrets()
        store = SecretStore(mode="cloud", encryption_key=Fernet.generate_key().decode("ascii"), cloud_secret_service=cloud)

        store.store_cloud_secret("GEMINI_API_KEY", "gemini-secret-value")
        raw_row = cloud.get_secret("GEMINI_API_KEY")
        self.assertNotEqual(raw_row["value"], "gemini-secret-value")
        self.assertTrue(raw_row["value"].startswith("fernet:"))
        self.assertEqual(store.get_cloud_secret("GEMINI_API_KEY"), "gemini-secret-value")

    def test_secret_store_migrates_legacy_cloud_plaintext_when_key_available(self):
        from cryptography.fernet import Fernet
        from services.secret_store import SecretStore

        cloud = _MemoryCloudSecrets()
        cloud.upsert_secret(key="GEMINI_API_KEY", value="legacy-plain")
        store = SecretStore(mode="cloud", encryption_key=Fernet.generate_key().decode("ascii"), cloud_secret_service=cloud)

        self.assertEqual(store.get_cloud_secret("GEMINI_API_KEY", migrate_plaintext=True), "legacy-plain")
        self.assertTrue(cloud.get_secret("GEMINI_API_KEY")["value"].startswith("fernet:"))

    def test_runtime_admin_helper_rejects_non_admin_when_auth_required(self):
        from api import routes

        original_auth_required = routes.config.AUTH_REQUIRED
        original_supabase_auth = routes.supabase_auth
        try:
            routes.config.AUTH_REQUIRED = True
            routes.supabase_auth = _FakeSupabase(
                {
                    "id": "approved-user",
                    "email": "user@example.com",
                    "app_metadata": {"role": "user"},
                    "user_metadata": {"access_status": "approved"},
                }
            )
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes._require_runtime_admin("Bearer user-token"))
            self.assertEqual(cm.exception.status_code, 403)

            routes.supabase_auth = _FakeSupabase(
                {
                    "id": "admin-user",
                    "email": "admin@example.com",
                    "app_metadata": {"role": "admin"},
                }
            )
            admin = asyncio.run(routes._require_runtime_admin("Bearer admin-token"))
            self.assertEqual(admin["role"], "admin")
        finally:
            routes.config.AUTH_REQUIRED = original_auth_required
            routes.supabase_auth = original_supabase_auth

    def test_runtime_permission_helper_rejects_missing_auth_when_auth_required(self):
        from api import routes

        original_auth_required = routes.config.AUTH_REQUIRED
        try:
            routes.config.AUTH_REQUIRED = True
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes._require_user_permission(None, "can_execute_demo"))
            self.assertEqual(cm.exception.status_code, 401)
        finally:
            routes.config.AUTH_REQUIRED = original_auth_required

    def test_runtime_permission_helper_blocks_user_without_live_execution_permission(self):
        from api import routes

        original_auth_required = routes.config.AUTH_REQUIRED
        original_db = routes.db
        original_resolve = routes._resolve_authenticated_user
        try:
            routes.config.AUTH_REQUIRED = True
            routes.db = None

            async def fake_user(_authorization):
                return {
                    "id": "approved-user",
                    "email": "ok@example.com",
                    "role": "user",
                    "app_metadata": {"permissions": {"can_execute_live": False}},
                    "user_metadata": {},
                }

            routes._resolve_authenticated_user = fake_user
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes._require_user_permission("Bearer good", "can_execute_live"))
            self.assertEqual(cm.exception.status_code, 403)
            self.assertIn("can_execute_live", str(cm.exception.detail))
        finally:
            routes.config.AUTH_REQUIRED = original_auth_required
            routes.db = original_db
            routes._resolve_authenticated_user = original_resolve

    def test_user_access_permissions_json_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            asyncio.run(db.initialize())
            try:
                asyncio.run(
                    db.upsert_user_access(
                        user_id="user-1",
                        email="u@example.com",
                        status="approved",
                        permissions={"can_execute_demo": True, "can_execute_live": False},
                    )
                )
                row = asyncio.run(db.get_user_access(user_id="user-1"))
                self.assertEqual(row["permissions_json"]["can_execute_demo"], True)
                self.assertEqual(row["permissions_json"]["can_execute_live"], False)
            finally:
                asyncio.run(db.close())

    def test_saved_credentials_plaintext_passwords_are_cleared_on_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = Database(str(db_path))
            asyncio.run(db.initialize())
            try:
                asyncio.run(
                    db._db.execute(
                        """INSERT INTO saved_credentials
                        (account, server, password, secret_ref, secret_backend, created_at, last_used)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (123, "Demo", "plain-secret", "mt5://123@Demo", "keyring", 1.0, 1.0),
                    )
                )
                asyncio.run(db._db.commit())
                asyncio.run(db._migrate_schema())
                row = asyncio.run(db.get_saved_credential(123))
                self.assertEqual(row.get("password"), "")
            finally:
                asyncio.run(db.close())

    def test_kill_switch_blocks_execution_service_before_order_send(self):
        from services.kill_switch_service import KillSwitchState

        engine = _NoopExecution()
        service = ExecutionService(
            execution_engine=engine,
            risk_service=SimpleNamespace(risk_engine=SimpleNamespace(settings=SimpleNamespace(min_reward_risk_ratio=1.2))),
            signal_pipeline=None,
        )
        service.kill_switch_state_getter = lambda: KillSwitchState(
            enabled=True,
            reason="operator stop",
            set_by="admin",
            set_at=123.0,
        )
        result = service.place_order_if_approved(
            OrderRequest(symbol="US500", action="BUY", volume=0.01, stop_loss=99.0, take_profit=101.0),
            SimpleNamespace(approved=True, entry_price=100.0, required_reward_risk=1.2, spread_cost=0.0),
        )

        self.assertFalse(result.success)
        self.assertFalse(engine.called)
        self.assertIn("kill switch", result.retcode_desc.lower())


if __name__ == "__main__":
    unittest.main()
