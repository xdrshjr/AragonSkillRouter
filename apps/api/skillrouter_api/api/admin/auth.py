"""认证路由：bootstrap / login / logout / me / change-password（§9.2 / §4.4(a)）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import __version__
from ...db import db_write, now_iso
from ...db.seed import get_state
from ...deps import mark_initialized, require_admin, require_initialized
from ...errors import ApiError, Code
from ...logging import get_logger
from ...models import AdminUser, SystemState
from ...schemas.auth import BootstrapResponse, ChangePasswordRequest, LoginRequest
from ...security import passwords
from ...security.csrf import require_csrf
from ...security.ratelimit import check_login_allowed, clear_login_window, record_attempt
from ...security.sessions import (
    COOKIE_NAME,
    CSRF_COOKIE,
    SessionContext,
    issue_session,
    resolve_session,
    revoke_other_sessions,
    revoke_session,
)
from ...services import audit
from ...settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("skillrouter.access")


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _set_cookies(response: Response, token: str, csrf: str) -> None:
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/bootstrap", response_model=None)
async def bootstrap(request: Request) -> dict:
    """匿名可访问：是否需要初始化、是否强制改密。"""
    from ...db import db_session

    with db_session() as s:
        initialized = (get_state(s, "admin_initialized", "0") == "1")
        user = s.query(AdminUser).first()
    ctx = resolve_session(request.cookies.get(COOKIE_NAME))
    return BootstrapResponse(
        initialized=initialized,
        must_change_password=bool(user and user.must_change_password) if initialized else False,
        version=__version__,
        default_credentials=bool(initialized and user and user.must_change_password),
    ).model_dump() | {
        "authenticated": ctx is not None,
        "password_change_required": bool(ctx and ctx.must_change_password),
    }


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    require_initialized()
    ip = _client_ip(request)
    check_login_allowed(payload.username, ip)
    with db_write() as s:
        user = s.query(AdminUser).filter(AdminUser.username == payload.username).one_or_none()
        valid = bool(user and not user.disabled and passwords.verify_password(payload.password, user.password_hash))
    if not valid:
        record_attempt(payload.username, ip, success=False)
        raise ApiError(
            Code.AUTH_INVALID_CREDENTIALS,
            hint="检查用户名与密码；连续失败将触发 5 分钟锁定",
        )
    record_attempt(payload.username, ip, success=True)
    clear_login_window(payload.username)
    assert user is not None
    if passwords.needs_rehash(user.password_hash):
        with db_write() as s:
            row = s.get(AdminUser, user.id)
            if row:
                row.password_hash = passwords.hash_password(payload.password)
                row.last_login_at = now_iso()
    else:
        with db_write() as s:
            row = s.get(AdminUser, user.id)
            if row:
                row.last_login_at = now_iso()
    scope = "password_change" if user.must_change_password else "normal"
    issued = issue_session(
        user,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        scope=scope,
    )
    _set_cookies(response, issued.token, issued.csrf_token)
    log.info("admin_login", extra={"actor": user.username, "detail": {"scope": scope}})
    return {
        "admin": {
            "id": user.id,
            "username": user.username,
            "must_change_password": bool(user.must_change_password),
        },
        "csrf_token": issued.csrf_token,
        "expires_at": issued.expires_at,
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    ctx = resolve_session(token)
    if ctx is not None:
        revoke_session(ctx.session_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, ctx: SessionContext = Depends(require_admin)) -> dict:
    from ...agent.client import get_adapter

    engine: dict
    try:
        health = await get_adapter().health()
        engine = {
            "status": health.get("engine", {}).get("status", "available"),
            "version": health.get("engine", {}).get("version"),
            "active_turns": health.get("active_turns", 0),
        }
    except ApiError:
        engine = {"status": "unavailable", "version": None, "active_turns": 0}
    return {
        "admin": {
            "id": ctx.admin_id,
            "username": ctx.username,
            "must_change_password": ctx.must_change_password,
        },
        "session": {"expires_at": ctx.expires_at, "scope": ctx.scope},
        "engine": engine,
        "env": settings.env,
        "version": __version__,
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    ctx: SessionContext = Depends(require_admin),
) -> dict:
    require_csrf(ctx, request.headers.get("X-CSRF-Token"), request.method)
    issues = passwords.validate_strength(payload.new_password)
    if issues:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="新密码不符合要求",
            details={"fields": issues},
        )
    with db_write() as s:
        user = s.get(AdminUser, ctx.admin_id)
        if user is None:
            raise ApiError(Code.NOT_FOUND, "账号不存在")
        if not passwords.verify_password(payload.current_password, user.password_hash):
            raise ApiError(Code.AUTH_INVALID_CREDENTIALS, "当前密码不正确")
        if payload.new_password == payload.current_password:
            raise ApiError(Code.VALIDATION_FAILED, "新密码不能与当前密码相同")
        user.password_hash = passwords.hash_password(payload.new_password)
        user.must_change_password = 0
        user.password_changed_at = now_iso()
    revoked = revoke_other_sessions(ctx.admin_id, keep_session_id=ctx.session_id)
    issued = issue_session(
        type("U", (), {"id": ctx.admin_id, "username": ctx.username})(),  # type: ignore[arg-type]
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        scope="normal",
    )
    revoke_session(ctx.session_id)
    _set_cookies(response, issued.token, issued.csrf_token)
    audit.write(
        "admin.password_changed",
        object_type="admin_user",
        object_id=ctx.admin_id,
        summary={"revoked_sessions": revoked},
        actor_id=ctx.admin_id,
    )
    with db_write() as s:
        row = s.get(SystemState, "password_rotated_at")
        if row is None:
            s.add(SystemState(key="password_rotated_at", value=now_iso(), updated_at=now_iso()))
        else:
            row.value = now_iso()
            row.updated_at = now_iso()
    mark_initialized()
    return {
        "ok": True,
        "csrf_token": issued.csrf_token,
        "admin": {"id": ctx.admin_id, "username": ctx.username, "must_change_password": False},
    }
