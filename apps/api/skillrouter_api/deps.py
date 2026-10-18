"""依赖注入与闸门（§7.3 / §4.4(a)）。

闸门顺序固定：require_initialized → require_admin → require_password_changed → require_csrf
→ 业务依赖；`/api/v1/*` 走 require_initialized → require_token。

维护模式（§A.3.13）的拦截点放在 `main.py` 的中间件里，由本模块的
`maintenance_block()` 做判定：`require_admin` 由每个写路由自己声明，逐个插入检查
既会漏也会改到 `api/admin/*` 的每个文件；中间件是唯一能一次覆盖
「全部 `/api/admin` 写方法 + `/api/v1` 写接口 + MCP」的位置。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import db_session, get_session_factory
from .errors import ApiError, Code
from .logging import get_logger
from .models import SystemState
from .security.csrf import require_csrf
from .security.sessions import COOKIE_NAME, SessionContext, resolve_session
from .security.tokens import TokenContext, verify_token
from .settings import settings

log = get_logger("skillrouter.run")

_INIT_CACHE: dict[str, str | bool] = {"value": False}

#: 维护模式下被拦截的方法（读方法一律放行）
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 维护模式下仍然放行的写接口：只有会话本体的登录/登出（不写业务数据），
#: 否则管理员在恢复窗口里既登不进也退不出控制台。
MAINTENANCE_EXEMPT_PATHS = frozenset({"/api/admin/auth/login", "/api/admin/auth/logout"})

#: 维护模式覆盖的前缀；MCP 走 `/api/v1/mcp`（含在 `/api/v1` 里），此处额外列出根级别名
_MAINTENANCE_PREFIXES = ("/api/admin", "/api/v1")
_MAINTENANCE_EXTRA_PATHS = frozenset({"/mcp"})


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


def _raw_token(request: Request) -> str | None:
    """只认 `Authorization: Bearer`；`?token=` 仅在显式开启兼容开关时接受（§A.3.3）。"""
    header = request.headers.get("Authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else None
    if not raw and settings.allow_query_token:
        raw = request.query_params.get("token")
        if raw:
            log.warning("query_token_used", extra={"object": request.url.path})
    return raw


_TOKEN_HINT = "平台令牌只能通过 Authorization 头传递；查询串通道默认关闭"


def require_token(request: Request) -> TokenContext:
    require_initialized()
    raw = _raw_token(request)
    if not raw:
        raise ApiError(
            Code.TOKEN_INVALID,
            message="缺少 Authorization: Bearer <token>",
            hint=_TOKEN_HINT,
        )
    ctx = verify_token(raw)
    request.state.token = ctx
    return ctx


def optional_token(request: Request) -> TokenContext | None:
    """签名 URL 用：有令牌就照旧校验（非法 401），没有令牌返回 `None`（§A.3.10）。"""
    has_header = bool(request.headers.get("Authorization", "").strip())
    has_query = bool(settings.allow_query_token and request.query_params.get("token"))
    if not has_header and not has_query:
        return None
    raw = _raw_token(request)
    if not raw:
        raise ApiError(
            Code.TOKEN_INVALID,
            message="Authorization 头格式非法",
            hint="使用 Authorization: Bearer <token>",
        )
    ctx = verify_token(raw)
    request.state.token = ctx
    return ctx


def maintenance_block(request: Request) -> ApiError | None:
    """维护模式判定：写方法 → `MAINTENANCE`（503），读方法放行（§A.3.13）。

    返回 `ApiError` 而不是直接抛，是为了让中间件能以统一错误体返回
    （中间件在 `ExceptionMiddleware` 之外，抛异常会变成纯 500）。
    """
    if request.method.upper() not in WRITE_METHODS:
        return None
    path = request.url.path
    if path in MAINTENANCE_EXEMPT_PATHS:
        return None
    if not (path.startswith(_MAINTENANCE_PREFIXES) or path in _MAINTENANCE_EXTRA_PATHS):
        return None
    from .services.maintenance import is_maintenance  # 延迟导入：避免影响启动期导入顺序

    if not is_maintenance():
        return None
    return ApiError(
        Code.MAINTENANCE,
        message="维护中：正在恢复备份，写操作已暂停",
        hint="恢复完成后重试；读接口仍可用（GET /healthz 的 maintenance 字段）",
        details={"path": path},
    )


def enforce_maintenance(request: Request) -> None:
    """可复用闸门（路由内也可调用）：命中维护模式直接抛 503 `MAINTENANCE`。"""
    blocked = maintenance_block(request)
    if blocked is not None:
        raise blocked


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
