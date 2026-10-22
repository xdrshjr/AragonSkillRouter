"""Request dependencies: browser session auth, CSRF, and external agent tokens."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, forbidden, unauthorized
from app.security import sessions as session_store
from app.security.tokens import hash_agent_token


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def session_token_from(request: Request) -> str | None:
    settings = get_settings()
    return request.cookies.get(settings.session_cookie_name)


def current_session(request: Request) -> session_store.SessionRecord | None:
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached
    record = session_store.get_session(session_token_from(request))
    request.state.session = record
    return record


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    role: str
    must_change_password: bool
    session: session_store.SessionRecord

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _load_user(user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT id, username, role, must_change_password, disabled, created_at, password_changed_at FROM users WHERE id = ?",
        (user_id,),
    )


def require_user(request: Request) -> AuthUser:
    session = current_session(request)
    if session is None:
        raise unauthorized()
    user = _load_user(session.user_id)
    if not user or user["disabled"]:
        session_store.delete_session(session_token_from(request))
        raise unauthorized("This session is no longer valid. Sign in again.")
    auth = AuthUser(
        id=user["id"],
        username=user["username"],
        role=user["role"],
        must_change_password=bool(user["must_change_password"]),
        session=session,
    )
    if auth.must_change_password and not _is_password_change_allowed(request.url.path):
        raise ApiError(
            "password_change_required",
            "Change the initial admin password before using Skill Router.",
            status_code=403,
            actions=["Open Account and set a new password."],
        )
    return auth


def _is_password_change_allowed(path: str) -> bool:
    allowed = (
        "/api/auth/session",
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/bootstrap",
        "/api/health",
    )
    return path.startswith(allowed)


def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if not user.is_admin:
        raise forbidden("admin_required", "This action requires an administrator.")
    return user


def require_csrf(request: Request, user: AuthUser = Depends(require_user)) -> AuthUser:
    """Double-submit check for mutating admin calls.

    The middleware already rejects mutations without a valid header; this
    dependency keeps the guarantee for handlers mounted outside /api.
    """
    header = request.headers.get("x-csrf-token")
    if not session_store.verify_csrf(user.session, header):
        raise forbidden(
            "csrf_failed",
            "Your page token expired. Reload the page and try again.",
            actions=["Reload the page.", "Repeat the action."],
        )
    return user


# ------------------------------------------------------------ agent tokens --
@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    agent_name: str
    token_id: str
    scopes: list[str]
    budget_usd: float | None
    rate_limit_per_min: int | None
    max_concurrency: int | None

    def can_use(self, capability_slug: str) -> bool:
        if not self.scopes or "*" in self.scopes:
            return True
        return capability_slug in self.scopes or f"{capability_slug}:*" in self.scopes


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return None


def agent_principal(request: Request) -> AgentPrincipal | None:
    token = _token_from_request(request)
    if not token:
        return None
    row = db.query_one(
        """SELECT t.id, t.agent_id, t.scopes, t.status, t.expires_at, t.budget_usd,
                  t.rate_limit_per_min, t.max_concurrency, a.name AS agent_name, a.status AS agent_status
             FROM agent_tokens t JOIN agents a ON a.id = t.agent_id
            WHERE t.token_hash = ?""",
        (hash_agent_token(token),),
    )
    if not row:
        raise ApiError("invalid_token", "This access token is not recognised.", status_code=401)
    if row["status"] != "active" or row["agent_status"] != "active":
        raise ApiError(
            "token_revoked",
            "This access token has been revoked.",
            status_code=401,
            actions=["Ask the administrator for a new token."],
        )
    expires = db.parse_iso(row["expires_at"])
    if expires and expires <= db.utcnow():
        raise ApiError("token_expired", "This access token has expired.", status_code=401)
    db.execute("UPDATE agent_tokens SET last_used_at = ? WHERE id = ?", (db.iso(), row["id"]))
    return AgentPrincipal(
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        token_id=row["id"],
        scopes=db.loads(row["scopes"], []),
        budget_usd=row["budget_usd"],
        rate_limit_per_min=row["rate_limit_per_min"],
        max_concurrency=row["max_concurrency"],
    )


def require_agent(request: Request) -> AgentPrincipal:
    principal = agent_principal(request)
    if principal is None:
        cookie_session = current_session(request)
        if cookie_session is not None:
            user = _load_user(cookie_session.user_id)
            if user and not user["disabled"]:
                # An authenticated admin using the same route from the UI.
                return AgentPrincipal(
                    agent_id="",
                    agent_name=user["username"],
                    token_id="",
                    scopes=["*"],
                    budget_usd=None,
                    rate_limit_per_min=None,
                    max_concurrency=None,
                )
        raise unauthorized("Provide an agent access token in the Authorization header.")
    return principal


def request_id_header(request: Request) -> str:
    from app.core.request_context import get_request_id

    return get_request_id()


def hash_for_log(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
