from __future__ import annotations

import time
from typing import Optional
import asyncio
import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from config import config
from api.routes import cloud_secret_service, apply_runtime_gemini_credentials, hydrate_gemini_credentials_from_cloud
from services.supabase_admin_service import SupabaseAdminService
from storage.db import Database

router = APIRouter()

db: Optional[Database] = None
_db_init_lock = asyncio.Lock()
supabase_admin = SupabaseAdminService(
    url=config.SUPABASE_URL,
    anon_key=config.SUPABASE_ANON_KEY,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
)
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
_admin_users_cache: list[dict] = []
_admin_users_cache_at: float = 0.0
_admin_users_cache_lock = asyncio.Lock()


def set_database(database: Database):
    global db
    db = database


async def _ensure_database() -> Database:
    global db
    if db is not None:
        return db
    async with _db_init_lock:
        if db is None:
            database = Database(config.DB_PATH)
            await database.initialize()
            db = database
    return db


class CreateAdminUserRequest(BaseModel):
    email: str
    password: str
    role: str = "user"


class UpdateRoleRequest(BaseModel):
    user_id: str
    role: str


class RegisterRequest(BaseModel):
    email: str
    password: str


class UpdateAccessRequest(BaseModel):
    user_id: str
    status: str
    notes: str = ""


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class GeminiCloudConfigRequest(BaseModel):
    api_key: str
    model: Optional[str] = None


def _admin_signing_secret() -> str:
    # Use service-role key when available so stateless admin tokens survive serverless invocations.
    base_secret = (
        config.SUPABASE_SERVICE_ROLE_KEY
        or config.ADMIN_BOOTSTRAP_PASSWORD
        or "linktrade-admin-fallback"
    )
    return f"linktrade-admin::{base_secret}"


def _encode_admin_token(*, username: str, expires_at: float) -> str:
    payload = {
        "username": username,
        "iat": int(time.time()),
        "exp": int(expires_at),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(
        _admin_signing_secret().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _decode_admin_token(token: str) -> dict:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid admin token.") from exc
    expected_sig = hmac.new(
        _admin_signing_secret().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        raise HTTPException(status_code=401, detail="Invalid admin token signature.")
    padded_payload = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload_raw = base64.urlsafe_b64decode(padded_payload.encode("ascii"))
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Malformed admin token.") from exc
    if float(payload.get("exp", 0)) <= time.time():
        raise HTTPException(status_code=401, detail="Admin session expired.")
    return payload


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header.")
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid authorization header.")
    token = authorization[len(prefix):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return token


def _get_role(user: dict) -> str:
    app_meta = user.get("app_metadata") or {}
    role = str(app_meta.get("role") or "user").strip().lower()
    return role or "user"


def _user_access_status_from_metadata(user: dict) -> str:
    meta = user.get("user_metadata") or {}
    status = str(meta.get("access_status") or "pending").strip().lower()
    if status not in {"pending", "approved", "rejected"}:
        return "pending"
    return status


async def _current_user(authorization: Optional[str]) -> dict:
    if not supabase_admin.configured:
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    token = _extract_bearer_token(authorization)
    try:
        return supabase_admin.get_user_from_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def _require_admin(authorization: Optional[str]) -> dict:
    # 1) Dedicated admin username/password session
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            payload = _decode_admin_token(token)
            return {
                "id": "local-admin",
                "email": f"{config.ADMIN_BOOTSTRAP_USERNAME}@local",
                "app_metadata": {"role": "admin"},
            }
        except HTTPException:
            pass

    # 2) Supabase admin role token
    user = await _current_user(authorization)
    role = _get_role(user)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    return user


async def _log_activity(
    *,
    user: dict,
    action: str,
    request: Request,
    status_code: int = 200,
    details: Optional[dict] = None,
):
    database = await _ensure_database()
    await database.log_user_activity(
        user_id=str(user.get("id") or ""),
        user_email=str(user.get("email") or ""),
        role=_get_role(user),
        action=action,
        path=request.url.path,
        method=request.method,
        status_code=status_code,
        details=details or {},
    )


async def _fetch_admin_users(*, limit: int = 1000, max_cache_age_seconds: float = 8.0) -> list[dict]:
    """Return Supabase users with short cache + bounded latency.

    Admin panel refresh should stay responsive even when Supabase admin API
    is temporarily slow.
    """
    global _admin_users_cache
    global _admin_users_cache_at

    now = time.time()
    if _admin_users_cache and (now - _admin_users_cache_at) <= max_cache_age_seconds:
        return list(_admin_users_cache)

    async with _admin_users_cache_lock:
        now = time.time()
        if _admin_users_cache and (now - _admin_users_cache_at) <= max_cache_age_seconds:
            return list(_admin_users_cache)
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    supabase_admin.list_users,
                    page=1,
                    per_page=max(1, min(limit, 1000)),
                ),
                timeout=8.0,
            )
            users = payload.get("users", []) if isinstance(payload, dict) else []
            _admin_users_cache = list(users)
            _admin_users_cache_at = time.time()
            return list(_admin_users_cache)
        except Exception:
            if _admin_users_cache:
                return list(_admin_users_cache)
            raise


def _invalidate_admin_users_cache():
    global _admin_users_cache_at
    _admin_users_cache_at = 0.0


@router.get("/auth/me")
async def auth_me(request: Request, authorization: Optional[str] = Header(default=None)):
    user = await _current_user(authorization)
    access_status = _user_access_status_from_metadata(user)
    approved = access_status == "approved"
    # Legacy fallback for previously created users without metadata.
    if access_status == "pending":
        database = await _ensure_database()
        access = await database.get_user_access(user_id=str(user.get("id") or ""))
        if access:
            access_status = str(access.get("status") or "pending")
            approved = access_status == "approved"
    await _log_activity(
        user=user,
        action="auth_me",
        request=request,
        details={"email": user.get("email")},
    )
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "role": _get_role(user),
        "approved": approved,
        "access_status": access_status,
        "user_metadata": user.get("user_metadata") or {},
        "app_metadata": user.get("app_metadata") or {},
    }


@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest):
    username = req.username.strip()
    password = req.password
    if username != config.ADMIN_BOOTSTRAP_USERNAME or password != config.ADMIN_BOOTSTRAP_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")
    expires_at = time.time() + ADMIN_SESSION_TTL_SECONDS
    token = _encode_admin_token(username=username, expires_at=expires_at)
    return {
        "ok": True,
        "token": token,
        "expires_in": ADMIN_SESSION_TTL_SECONDS,
        "username": username,
    }


@router.get("/admin/session")
async def admin_session(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing admin token.")
    token = authorization[7:].strip()
    payload = _decode_admin_token(token)
    return {
        "ok": True,
        "username": payload.get("username", config.ADMIN_BOOTSTRAP_USERNAME),
        "expires_at": payload.get("exp"),
    }


@router.post("/public/register")
async def public_register(req: RegisterRequest):
    if not supabase_admin.configured:
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    database = await _ensure_database()
    email = req.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Valid email is required.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    try:
        created = supabase_admin.create_user(
            email=email,
            password=req.password,
            role="user",
            email_confirm=True,
            user_metadata={
                "access_status": "pending",
                "requested_at": int(time.time()),
            },
        )
        user_id = str(created.get("id") or "")
        if not user_id:
            raise RuntimeError("Supabase did not return created user id.")
        await database.upsert_user_access(
            user_id=user_id,
            email=email,
            status="pending",
            notes="Pending admin approval.",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "Registration submitted. Wait for admin approval before using the dashboard.",
    }


@router.post("/auth/bootstrap-admin")
async def auth_bootstrap_admin():
    database = await _ensure_database()
    if not config.ENABLE_ADMIN_BOOTSTRAP:
        raise HTTPException(status_code=403, detail="Admin bootstrap is disabled.")
    if not supabase_admin.configured:
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    if not config.ADMIN_BOOTSTRAP_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_BOOTSTRAP_PASSWORD is empty.")
    try:
        result = supabase_admin.ensure_bootstrap_admin(
            username=config.ADMIN_BOOTSTRAP_USERNAME,
            password=config.ADMIN_BOOTSTRAP_PASSWORD,
        )
        if result.get("user_id"):
            await database.upsert_user_access(
                user_id=str(result.get("user_id")),
                email=str(result.get("email") or ""),
                status="approved",
                approved_by_user_id="bootstrap",
                notes="Bootstrap admin account.",
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Bootstrap admin failed: {exc}") from exc
    return {
        "ok": True,
        "username": config.ADMIN_BOOTSTRAP_USERNAME,
        "email": result.get("email"),
        "created": bool(result.get("created")),
    }


@router.get("/admin/users")
async def admin_list_users(request: Request, authorization: Optional[str] = Header(default=None), page: int = 1, per_page: int = 200):
    user = await _require_admin(authorization)
    try:
        payload = await asyncio.wait_for(
            asyncio.to_thread(
                supabase_admin.list_users,
                page=max(1, page),
                per_page=max(1, min(per_page, 1000)),
            ),
            timeout=8.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    users = payload.get("users", []) if isinstance(payload, dict) else []
    await _log_activity(
        user=user,
        action="admin_list_users",
        request=request,
        details={"count": len(users)},
    )
    return {"users": users, "count": len(users)}


@router.get("/admin/customers")
async def admin_customers(request: Request, authorization: Optional[str] = Header(default=None), limit: int = 500):
    user = await _require_admin(authorization)
    try:
        users = await _fetch_admin_users(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load customers: {exc}") from exc
    items = []
    for entry in users:
        role = _get_role(entry)
        if role == "admin":
            continue
        status = _user_access_status_from_metadata(entry)
        if status != "approved":
            continue
        created_at = entry.get("created_at")
        ts = time.time()
        if isinstance(created_at, str):
            try:
                ts = time.mktime(time.strptime(created_at.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = time.time()
        items.append(
            {
                "id": len(items) + 1,
                "user_id": str(entry.get("id") or ""),
                "email": str(entry.get("email") or ""),
                "status": status,
                "requested_at": ts,
                "approved_at": ts,
            }
        )
    await _log_activity(
        user=user,
        action="admin_customers",
        request=request,
        details={"count": len(items)},
    )
    return {"items": items, "count": len(items)}


@router.post("/admin/users")
async def admin_create_user(req: CreateAdminUserRequest, request: Request, authorization: Optional[str] = Header(default=None)):
    user = await _require_admin(authorization)
    role = req.role.strip().lower()
    email = req.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Valid email is required.")
    if role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    try:
        created = await asyncio.to_thread(
            supabase_admin.create_user,
            email=email,
            password=req.password,
            role=role,
            email_confirm=True,
        )
        created_user_id = str(created.get("id") or "")
        if created_user_id:
            database = await _ensure_database()
            await database.upsert_user_access(
                user_id=created_user_id,
                email=email,
                status="approved" if role == "admin" else "pending",
                approved_by_user_id=str(user.get("id") or ""),
                notes="Created from admin panel.",
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_admin_users_cache()
    await _log_activity(
        user=user,
        action="admin_create_user",
        request=request,
        details={"created_email": email, "role": role},
    )
    return {"ok": True, "user": created}


@router.post("/admin/users/role")
async def admin_update_role(req: UpdateRoleRequest, request: Request, authorization: Optional[str] = Header(default=None)):
    user = await _require_admin(authorization)
    role = req.role.strip().lower()
    if role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'.")
    try:
        updated = await asyncio.to_thread(
            supabase_admin.update_user_role,
            user_id=req.user_id.strip(),
            role=role,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_admin_users_cache()
    await _log_activity(
        user=user,
        action="admin_update_role",
        request=request,
        details={"target_user_id": req.user_id, "role": role},
    )
    return {"ok": True, "user": updated}


@router.get("/admin/access-requests")
async def admin_access_requests(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    status: Optional[str] = None,
    limit: int = 500,
):
    user = await _require_admin(authorization)
    if status and status not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid status filter.")
    try:
        users = await _fetch_admin_users(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load access requests: {exc}") from exc
    rows = []
    for entry in users:
        role = _get_role(entry)
        if role == "admin":
            continue
        user_status = _user_access_status_from_metadata(entry)
        if status and user_status != status:
            continue
        created_at = entry.get("created_at")
        ts = time.time()
        if isinstance(created_at, str):
            try:
                ts = time.mktime(time.strptime(created_at.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = time.time()
        rows.append(
            {
                "id": len(rows) + 1,
                "user_id": str(entry.get("id") or ""),
                "email": str(entry.get("email") or ""),
                "status": user_status,
                "requested_at": ts,
                "approved_at": ts if user_status == "approved" else None,
                "notes": "",
            }
        )
    await _log_activity(
        user=user,
        action="admin_access_requests",
        request=request,
        details={"status": status or "all", "count": len(rows)},
    )
    return {"items": rows, "count": len(rows)}


@router.post("/admin/access-requests")
async def admin_update_access_request(
    req: UpdateAccessRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = await _require_admin(authorization)
    status = req.status.strip().lower()
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(status_code=400, detail="Status must be approved|rejected|pending.")
    target_user_id = req.user_id.strip()
    # Keep status in Supabase metadata as the source of truth for cloud admin.
    try:
        users = await _fetch_admin_users(limit=1000)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load target user: {exc}") from exc
    target = next((u for u in users if str(u.get("id") or "") == target_user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    user_meta = dict(target.get("user_metadata") or {})
    user_meta["access_status"] = status
    user_meta["access_updated_at"] = int(time.time())
    user_meta["access_notes"] = req.notes.strip()
    try:
        await asyncio.to_thread(
            supabase_admin.update_user_metadata,
            user_id=target_user_id,
            user_metadata=user_meta,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not update access status: {exc}") from exc
    _invalidate_admin_users_cache()
    await _log_activity(
        user=user,
        action="admin_update_access",
        request=request,
        details={"target_user_id": req.user_id, "status": status},
    )
    return {"ok": True}


@router.get("/admin/users/{user_id}")
async def admin_user_detail(user_id: str, request: Request, authorization: Optional[str] = Header(default=None), limit: int = 200):
    user = await _require_admin(authorization)
    try:
        users = await _fetch_admin_users(limit=1000)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load user details: {exc}") from exc
    target = next((u for u in users if str(u.get("id") or "") == user_id.strip()), None)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    created_at = target.get("created_at")
    ts = time.time()
    if isinstance(created_at, str):
        try:
            ts = time.mktime(time.strptime(created_at.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = time.time()
    access = {
        "id": 0,
        "user_id": str(target.get("id") or ""),
        "email": str(target.get("email") or ""),
        "status": _user_access_status_from_metadata(target),
        "requested_at": ts,
        "approved_at": ts if _user_access_status_from_metadata(target) == "approved" else None,
        "notes": "",
    }
    database = await _ensure_database()
    all_activity = await database.get_user_activity(limit=max(200, limit * 5))
    user_activity = [
        row for row in all_activity
        if str(row.get("user_id") or "") == access.get("user_id")
        or str(row.get("user_email") or "").lower() == str(access.get("email") or "").lower()
    ][:limit]
    await _log_activity(
        user=user,
        action="admin_user_detail",
        request=request,
        details={"target_user_id": user_id, "activity_count": len(user_activity)},
    )
    return {
        "user": access,
        "activity": user_activity,
        "activity_count": len(user_activity),
    }


@router.get("/admin/activity")
async def admin_activity(request: Request, authorization: Optional[str] = Header(default=None), limit: int = 200):
    user = await _require_admin(authorization)
    database = await _ensure_database()
    logs = await database.get_user_activity(limit=limit)
    await _log_activity(
        user=user,
        action="admin_view_activity",
        request=request,
        details={"limit": limit, "returned": len(logs)},
    )
    return {"activity": logs, "count": len(logs)}


@router.get("/cloud/status")
async def cloud_status(authorization: Optional[str] = Header(default=None)):
    user = await _current_user(authorization)
    role = _get_role(user)
    return {
        "auth_provider": "supabase",
        "supabase_configured": supabase_admin.configured,
        "cloud_sync_enabled": bool(config.CLOUD_SYNC_ENABLED),
        "cloud_log_table": config.CLOUD_LOG_TABLE,
        "user_role": role,
        "hybrid_mode": True,
        "runtime": "local_app + cloud_auth_and_logs",
    }


@router.get("/admin/cloud/gemini")
async def admin_get_cloud_gemini(authorization: Optional[str] = Header(default=None)):
    await _require_admin(authorization)
    if not cloud_secret_service.configured:
        raise HTTPException(status_code=503, detail="Cloud secret service is not configured.")
    try:
        row = await asyncio.to_thread(cloud_secret_service.get_secret, "GEMINI_API_KEY")
        model_row = await asyncio.to_thread(cloud_secret_service.get_secret, "GEMINI_MODEL")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch cloud Gemini config: {exc}") from exc
    key = str((row or {}).get("value") or "")
    masked = f"{key[:4]}...{key[-4:]}" if len(key) >= 8 else ("*" * len(key))
    return {
        "configured": bool(key),
        "api_key_masked": masked,
        "model": str((model_row or {}).get("value") or ""),
        "table": config.CLOUD_SECRETS_TABLE,
    }


@router.post("/admin/cloud/gemini")
async def admin_set_cloud_gemini(req: GeminiCloudConfigRequest, authorization: Optional[str] = Header(default=None)):
    await _require_admin(authorization)
    if not cloud_secret_service.configured:
        raise HTTPException(status_code=503, detail="Cloud secret service is not configured.")
    api_key = str(req.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required.")
    model = str(req.model or "").strip()
    try:
        await asyncio.to_thread(cloud_secret_service.upsert_secret, key="GEMINI_API_KEY", value=api_key)
        if model:
            await asyncio.to_thread(cloud_secret_service.upsert_secret, key="GEMINI_MODEL", value=model)
        applied = await hydrate_gemini_credentials_from_cloud()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save cloud Gemini config: {exc}") from exc
    if not applied.get("loaded"):
        applied = apply_runtime_gemini_credentials(api_key=api_key, model_name=(model or None), source="admin_fallback")
    return {
        "saved": True,
        "source": applied.get("source"),
        "model": applied.get("model"),
        "gemini_available": bool(applied.get("gemini_available")),
        "event_classifier_available": bool(applied.get("event_classifier_available")),
        "strategy_advisor_available": bool(applied.get("strategy_advisor_available")),
    }
