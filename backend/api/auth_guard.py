from __future__ import annotations

from typing import Any

from fastapi import HTTPException


PUBLIC_API_PATHS = {
    "/api/admin/login",
    "/api/public/register",
    "/api/auth/bootstrap-admin",
}

PUBLIC_API_PREFIXES = (
    "/api/docs",
    "/api/openapi",
)

AUTH_ONLY_API_PATHS = {
    "/api/auth/me",
}


def is_public_api_path(path: str) -> bool:
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in PUBLIC_API_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization[len(prefix):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return token


def user_role(user: dict[str, Any]) -> str:
    app_meta = user.get("app_metadata") or {}
    role = str(app_meta.get("role") or "user").strip().lower()
    return role or "user"


def metadata_access_status(user: dict[str, Any]) -> str:
    meta = user.get("user_metadata") or {}
    status = str(meta.get("access_status") or "pending").strip().lower()
    return status if status in {"pending", "approved", "rejected"} else "pending"


async def resolved_access_status(user: dict[str, Any], database) -> str:
    status = metadata_access_status(user)
    if status == "approved":
        return status
    if database is None:
        return status
    try:
        access = await database.get_user_access(user_id=str(user.get("id") or ""))
    except Exception:
        access = None
    if access:
        db_status = str(access.get("status") or "pending").strip().lower()
        if db_status in {"pending", "approved", "rejected"}:
            return db_status
    return status


async def require_api_access(
    *,
    path: str,
    authorization: str | None,
    auth_required: bool,
    database,
    supabase,
) -> dict[str, Any] | None:
    if not auth_required:
        return None

    normalized = str(path or "").rstrip("/") or "/"
    if is_public_api_path(normalized):
        return None

    # Admin routes use their own admin-token/Supabase-role dependency. Do not
    # force normal approved-user auth onto local admin sessions.
    if normalized.startswith("/api/admin/"):
        return None

    token = _extract_bearer_token(authorization)
    if not getattr(supabase, "configured", False):
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    try:
        user = supabase.get_user_from_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc) or "Invalid authentication token.") from exc

    if normalized in AUTH_ONLY_API_PATHS:
        return user

    if user_role(user) == "admin":
        return user

    status = await resolved_access_status(user, database)
    if status != "approved":
        raise HTTPException(status_code=403, detail="User is not approved for LinkTrade app access.")
    return user
