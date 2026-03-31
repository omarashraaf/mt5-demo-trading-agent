from __future__ import annotations

import secrets
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from config import config
from services.supabase_admin_service import SupabaseAdminService
from storage.db import Database

router = APIRouter()

db: Optional[Database] = None
supabase_admin = SupabaseAdminService(
    url=config.SUPABASE_URL,
    anon_key=config.SUPABASE_ANON_KEY,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
)
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
admin_sessions: dict[str, dict] = {}


def set_database(database: Database):
    global db
    db = database


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
        session = admin_sessions.get(token)
        now = time.time()
        if session and float(session.get("expires_at", 0.0)) > now:
            return {
                "id": "local-admin",
                "email": f"{config.ADMIN_BOOTSTRAP_USERNAME}@local",
                "app_metadata": {"role": "admin"},
            }
        if session and float(session.get("expires_at", 0.0)) <= now:
            admin_sessions.pop(token, None)

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
    if db is None:
        return
    await db.log_user_activity(
        user_id=str(user.get("id") or ""),
        user_email=str(user.get("email") or ""),
        role=_get_role(user),
        action=action,
        path=request.url.path,
        method=request.method,
        status_code=status_code,
        details=details or {},
    )


@router.get("/auth/me")
async def auth_me(request: Request, authorization: Optional[str] = Header(default=None)):
    user = await _current_user(authorization)
    access_status = "pending"
    approved = False
    if db is not None:
        access = await db.get_user_access(user_id=str(user.get("id") or ""))
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
    token = secrets.token_urlsafe(32)
    admin_sessions[token] = {
        "username": username,
        "issued_at": time.time(),
        "expires_at": time.time() + ADMIN_SESSION_TTL_SECONDS,
    }
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
    session = admin_sessions.get(token)
    now = time.time()
    if not session or float(session.get("expires_at", 0.0)) <= now:
        admin_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Admin session expired.")
    return {
        "ok": True,
        "username": session.get("username", config.ADMIN_BOOTSTRAP_USERNAME),
        "expires_at": session.get("expires_at"),
    }


@router.post("/public/register")
async def public_register(req: RegisterRequest):
    if not supabase_admin.configured:
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
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
        )
        user_id = str(created.get("id") or "")
        if not user_id:
            raise RuntimeError("Supabase did not return created user id.")
        await db.upsert_user_access(
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
        if db is not None and result.get("user_id"):
            await db.upsert_user_access(
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
        payload = supabase_admin.list_users(page=max(1, page), per_page=max(1, min(per_page, 1000)))
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
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    items = await db.list_user_access_requests(status="approved", limit=limit)
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
        created = supabase_admin.create_user(
            email=email,
            password=req.password,
            role=role,
            email_confirm=True,
        )
        created_user_id = str(created.get("id") or "")
        if created_user_id and db is not None:
            await db.upsert_user_access(
                user_id=created_user_id,
                email=email,
                status="approved" if role == "admin" else "pending",
                approved_by_user_id=str(user.get("id") or ""),
                notes="Created from admin panel.",
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        updated = supabase_admin.update_user_role(user_id=req.user_id.strip(), role=role)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    if status and status not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid status filter.")
    rows = await db.list_user_access_requests(status=status, limit=limit)
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
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    status = req.status.strip().lower()
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(status_code=400, detail="Status must be approved|rejected|pending.")
    await db.set_user_access_status(
        user_id=req.user_id.strip(),
        status=status,
        approved_by_user_id=str(user.get("id") or ""),
        notes=req.notes.strip(),
    )
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
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    access = await db.get_user_access(user_id=user_id.strip())
    if not access:
        raise HTTPException(status_code=404, detail="User not found")
    all_activity = await db.get_user_activity(limit=max(200, limit * 5))
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
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    logs = await db.get_user_activity(limit=limit)
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
