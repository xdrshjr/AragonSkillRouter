"""`create_app()`：中间件、路由挂载、异常处理、健康检查（§7.1 / §7.14）。"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import ADAPTER_VERSION, __version__
from .db import db_session, schema_version
from .db.seed import get_state
from .errors import install_error_handlers
from .logging import get_logger, set_request_id, setup_logging
from .settings import settings

log = get_logger("skillrouter.access")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        set_request_id(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            latency = int((time.perf_counter() - started) * 1000)
            log.exception("request_failed", extra={"rid": rid, "path": request.url.path, "latency_ms": latency})
            raise
        latency = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-Id"] = rid
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if latency > 500 and request.url.path.startswith("/api/admin"):
            log.info("slow_request", extra={"rid": rid, "path": request.url.path, "latency_ms": latency})
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
        return {
            "ok": db_ok,
            "service": "skillrouter-api",
            "version": __version__,
            "adapter_version": ADAPTER_VERSION,
            "env": settings.env,
            "db": {"ok": db_ok, "schema_version": version, "error": error},
            "initialized": initialized,
            "engine_pin": settings.engine_pin,
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
