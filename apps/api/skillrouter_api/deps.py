"""依赖注入与闸门（§7.3 / §4.4(a)）。

闸门顺序固定：require_initialized → require_admin → require_password_changed → require_csrf
→ 业务依赖；`/api/v1/*` 走 require_initialized → require_token。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import db_session, get_session_factory
from .errors import ApiError, Code
from .models import SystemState
from .security.csrf import require_csrf
from .security.sessions import COOKIE_NAME, SessionContext, resolve_session
from .security.tokens import TokenContext, verify_token

_INIT_CACHE: dict[str, str | bool] = {"value": False}


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def is_initialized() -> bool:
    if _INIT_CACHE["value"] is True:
        return True
    with db_session() as s:
        row = s.get(SystemState, "admin_initialized")
        ok = bool(row and row.value == "1")
    if ok:
        _INIT_CACHE["value"] = True
    return ok


def mark_initialized() -> None:
    _INIT_CACHE["value"] = True


def require_initialized() -> None:
    if not is_initialized():
        raise ApiError(Code.NOT_INITIALIZED)


def _session_from_request(request: Request) -> SessionContext:
    token = request.cookies.get(COOKIE_NAME)
    ctx = resolve_session(token)
    if ctx is None:
        raise ApiError(Code.SESSION_EXPIRED if token else Code.UNAUTHENTICATED)
    return ctx


def require_admin(request: Request) -> SessionContext:
    require_initialized()
    ctx = _session_from_request(request)
    require_csrf(ctx, request.headers.get("X-CSRF-Token"), request.method)
    request.state.admin = ctx
    return ctx


def require_password_changed(ctx: SessionContext = Depends(require_admin)) -> SessionContext:
    if ctx.must_change_password:
        raise ApiError(Code.PASSWORD_CHANGE_REQUIRED)
    return ctx


def require_token(request: Request) -> TokenContext:
    require_initialized()
    header = request.headers.get("Authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else None
    if not raw:
        raw = request.query_params.get("token")
    ctx = verify_token(raw)
    request.state.token = ctx
    return ctx


def _split(header: str | None) -> list[str]:
    return [x.strip() for x in (header or "").split(",") if x.strip()]


async def page_params(request: Request) -> dict:
    params = request.query_params
    try:
        page = int(params.get("page", 1))
        page_size = int(params.get("page_size", 20))
    except ValueError:
        raise ApiError(Code.VALIDATION_FAILED, "page/page_size 必须为整数") from None
    return {
        "page": max(1, page),
        "page_size": min(max(1, page_size), 100),
        "sort": params.get("sort"),
        "q": params.get("q"),
    }
