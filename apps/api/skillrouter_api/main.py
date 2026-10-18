"""`create_app()`：中间件、路由挂载、异常处理、健康检查（§7.1 / §7.14）。"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from . import ADAPTER_VERSION, __version__
from .db import db_session, schema_version
from .db.seed import get_state
from .deps import maintenance_block
from .errors import error_response, install_error_handlers
from .logging import get_logger, set_request_id, setup_logging
from .settings import settings

log = get_logger("skillrouter.access")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
}


def _log_request(
    request: Request,
    *,
    rid: str,
    latency: int,
    event: str = "request",
    level: int = logging.INFO,
    code: str | None = None,
    exc_info: bool = False,
) -> None:
    """统一的请求日志：**只记 `url.path`**（§A.3.3）。

    查询串里可能带 `?token=` 或签名 URL 的 `sig`，一旦进日志就等于把凭据写进
    磁盘；这里连 `str(request.url)` 都不拼，避免未来有人顺手加回来。
    401/403 与 5xx 也一律走本函数，保证「错误日志」与「访问日志」是同一套字段。
    """
    extra: dict[str, object] = {"rid": rid, "path": request.url.path, "latency_ms": latency}
    if code is not None:
        extra["code"] = code
    log.log(level, event, extra=extra, exc_info=exc_info)


def _finalize(response: Response, rid: str) -> Response:
    response.headers["X-Request-Id"] = rid
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _mcp_maintenance_response(blocked) -> JSONResponse:  # noqa: ANN001
    """MCP 通道的维护模式错误体（JSON-RPC `-32000`，§A.5.1）。"""
    return JSONResponse(
        status_code=blocked.status,
        content={
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000,
                "message": blocked.message,
                "data": {"code": blocked.code, "hint": blocked.hint, "retryable": True},
            },
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        set_request_id(rid)
        started = time.perf_counter()
        blocked = maintenance_block(request)
        if blocked is not None:
            _log_request(
                request,
                rid=rid,
                latency=int((time.perf_counter() - started) * 1000),
                event="maintenance_blocked",
                level=logging.WARNING,
            )
            if request.url.path.endswith("/mcp"):
                return _finalize(_mcp_maintenance_response(blocked), rid)
            return _finalize(error_response(blocked, rid), rid)
        try:
            response = await call_next(request)
        except Exception:
            latency = int((time.perf_counter() - started) * 1000)
            _log_request(
                request,
                rid=rid,
                latency=latency,
                event="request_failed",
                level=logging.ERROR,
                exc_info=True,
            )
            raise
        latency = int((time.perf_counter() - started) * 1000)
        _finalize(response, rid)
        status = response.status_code
        if status >= 500:
            _log_request(
                request,
                rid=rid,
                latency=latency,
                event="request_error",
                level=logging.ERROR,
                code=str(status),
            )
        elif status in (401, 403):
            _log_request(
                request,
                rid=rid,
                latency=latency,
                event="request_rejected",
                level=logging.WARNING,
                code=str(status),
            )
        elif latency > 500 and request.url.path.startswith("/api/admin"):
            _log_request(request, rid=rid, latency=latency, event="slow_request")
        return response


def create_app() -> FastAPI:
    settings.ensure_dirs()
    setup_logging(settings.logs_dir)

    app = FastAPI(
        title="Skill Router",
        version=__version__,
        description=(
            "面向智能体的能力中枢：统一管理 AI 凭证、结构化能力、SKILL 分发与统一调用。\n\n"
            "管理端 `/api/admin/*` 使用 Cookie + X-CSRF-Token；对外 `/api/v1/*` 使用 `Authorization: Bearer srk_...`。"
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )
    install_error_handlers(app)

    from .api.admin import router as admin_router
    from .api.internal import router as internal_router
    from .api.v1 import router as v1_router

    app.include_router(internal_router)
    app.include_router(admin_router)
    app.include_router(v1_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        db_ok = True
        error = None
        try:
            with db_session() as s:
                from .db.seed import get_state as _gs

                initialized = _gs(s, "admin_initialized", "0") == "1"
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            initialized = False
            error = str(exc)
        try:
            version = schema_version()
        except Exception:  # noqa: BLE001
            version = -1
        from .services.maintenance import is_maintenance

        return {
            "ok": db_ok,
            "service": "skillrouter-api",
            "version": __version__,
            "adapter_version": ADAPTER_VERSION,
            "env": settings.env,
            "db": {"ok": db_ok, "schema_version": version, "error": error},
            "initialized": initialized,
            "engine_pin": settings.engine_pin,
            "maintenance": is_maintenance(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    async def metrics() -> str:
        return _metrics()

    @app.get("/api/health", tags=["ops"])
    async def api_health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - 由 bootstrap 负责建库
        log.info(
            "api_started",
            extra={"detail": {"version": __version__, "env": settings.env, "pin": settings.engine_pin}},
        )

    return app


def _metrics() -> str:
    from sqlalchemy import func, select

    from .models import CallLog, TaskQueue

    lines: list[str] = []
    try:
        with db_session() as s:
            rows = s.execute(
                select(CallLog.status, CallLog.capability_key, func.count()).group_by(
                    CallLog.status, CallLog.capability_key
                )
            ).all()
            depth = s.scalar(
                select(func.count()).select_from(TaskQueue).where(TaskQueue.status == "queued")
            )
        for status, capability, count in rows:
            lines.append(
                f'sr_calls_total{{status="{status}",capability="{capability}"}} {count}'
            )
        lines.append(f"sr_queue_depth {depth or 0}")
        lines.append(f'sr_engine_version_info{{version="{settings.gateway_pin_version}"}} 1')
    except Exception as exc:  # noqa: BLE001
        lines.append(f"# metrics_error {type(exc).__name__}")
    return "\n".join(lines) + "\n"


app = create_app()

__all__ = ["app", "create_app", "get_state", "JSONResponse"]
