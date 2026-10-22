"""Authentication routes: sign in, sign out, session probe, password change."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, forbidden, unauthorized
from app.deps import client_ip, current_session, require_user, AuthUser
from app.security import passwords, sessions as session_store, throttle
from app.services import audit, bootstrap

router = APIRouter(prefix="/api/auth", tags=["auth"])
public_router = APIRouter(prefix="/api/bootstrap", tags=["bootstrap"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def _user_payload(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "must_change_password": bool(user["must_change_password"]),
        "created_at": user["created_at"],
        "password_changed_at": user.get("password_changed_at"),
    }


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    settings = get_settings()
    max_age = settings.session_ttl_minutes * 60
    secure = settings.secure_cookies
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf,
        max_age=max_age,
        httponly=False,  # the SPA must read it to echo it back in a header
        secure=secure,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain,
    )


def _clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    username = payload.username.strip()
    ip = client_ip(request)
    state = throttle.check_login_allowed(username, ip)
    if state.blocked:
        raise ApiError(
            "too_many_attempts",
            state.message,
            status_code=429,
            details={"retry_after_seconds": state.retry_after_seconds},
            actions=["Wait for the lockout window to pass.", "Or reset the password from the server CLI."],
        )

    user = db.query_one(
        "SELECT id, username, role, password_hash, must_change_password, disabled, created_at, password_changed_at "
        "FROM users WHERE username = ?",
        (username,),
    )
    ok = bool(user) and not user["disabled"] and passwords.verify_password(user["password_hash"], payload.password)
    if not ok:
        throttle.record_attempt(username, ip, success=False)
        remaining = max(0, get_settings().login_max_attempts - (state.failures + 1))
        raise unauthorized(
            "Incorrect username or password."
            + (f" {remaining} attempt(s) left before a temporary lock." if remaining else "")
        )

    throttle.record_attempt(username, ip, success=True)
    if passwords.needs_rehash(user["password_hash"]):
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (passwords.hash_password(payload.password), db.iso(), user["id"]),
        )

    issued = session_store.create_session(
        user["id"], ip=ip, user_agent=request.headers.get("user-agent", "")[:300]
    )
    _set_session_cookies(response, issued.token, issued.csrf_token)
    db.execute("UPDATE users SET updated_at = ? WHERE id = ?", (db.iso(), user["id"]))
    audit.record(
        actor_type="user",
        actor_id=user["id"],
        action="auth.login",
        object_type="user",
        object_id=user["id"],
        summary=f"{user['username']} signed in",
        ip=ip,
    )
    return {
        "user": _user_payload(user),
        "csrf_token": issued.csrf_token,
        "must_change_password": bool(user["must_change_password"]),
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    session = current_session(request)
    if session:
        session_store.delete_session(request.cookies.get(get_settings().session_cookie_name))
        audit.record(
            actor_type="user",
            actor_id=session.user_id,
            action="auth.logout",
            object_type="session",
            object_id=session.id[:12],
            summary="signed out",
            ip=client_ip(request),
        )
    _clear_session_cookies(response)
    return {"ok": True}


@router.get("/session")
def read_session(user: AuthUser = Depends(require_user)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user.id,))
    assert row is not None
    status = bootstrap.get_status()
    return {
        "user": _user_payload(row),
        "csrf_token": user.session.csrf_token,
        "must_change_password": user.must_change_password,
        "onboarding": status.steps,
        "initialized": status.initialized,
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    row = db.query_one("SELECT password_hash FROM users WHERE id = ?", (user.id,))
    if not row or not passwords.verify_password(row["password_hash"], payload.current_password):
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="auth.password_change_failed",
            object_type="user",
            object_id=user.id,
            summary="wrong current password",
            ip=client_ip(request),
        )
        raise forbidden("invalid_current_password", "The current password is not correct.")
    if payload.current_password == payload.new_password:
        raise ApiError("password_unchanged", "Choose a password different from the current one.")
    try:
        passwords.check_password_policy(payload.new_password, username=user.username)
    except passwords.PasswordPolicyError as exc:
        raise ApiError("weak_password", str(exc)) from exc

    now = db.iso()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?, updated_at = ? WHERE id = ?",
        (passwords.hash_password(payload.new_password), now, now, user.id),
    )
    # Every other session is invalidated; this one keeps working and gets a
    # fresh CSRF token so the SPA can continue without a reload.
    session_store.delete_user_sessions(user.id, keep_session_id=user.session.id)
    new_csrf = session_store.rotate_csrf(user.session.id)
    settings = get_settings()
    response.set_cookie(
        settings.csrf_cookie_name,
        new_csrf,
        max_age=settings.session_ttl_minutes * 60,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    bootstrap.mark_password_changed()
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="auth.password_changed",
        object_type="user",
        object_id=user.id,
        summary="password changed; other sessions revoked",
        ip=client_ip(request),
    )
    return {"ok": True, "csrf_token": new_csrf, "must_change_password": False}


@public_router.get("/status")
def bootstrap_status() -> dict[str, Any]:
    """Public, non-sensitive: lets the sign-in page explain the first-run flow."""
    status = bootstrap.get_status()
    return {
        "initialized": status.initialized,
        "admin_username": status.admin_username,
        "must_change_password": status.must_change_password,
        "created_at": status.created_at,
        "steps": status.steps,
        "agent_configured": status.agent_configured,
        "published_capabilities": status.published_capabilities,
        "active_agent_tokens": status.active_agent_tokens,
        "version": db.get_setting("bootstrap.schema_version", "1"),
    }
