"""Skill Router — FastAPI application.

Boot order: load settings → migrate SQLite → create the keyring + admin (once) →
serve. Nothing in the request path depends on in-process state that a restart
would lose.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import db
from app.core.config import get_settings
from app.core.errors import error_response, install_error_handlers
from app.core.request_context import get_request_id, set_request_id
from app.security import sessions as session_store
from app.services import bootstrap

log = logging.getLogger("skillrouter")

API_VERSION = "1.0.0"

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/health",
    "/api/v1/",       # external API authenticates with a bearer token
    "/api/mcp",       # MCP clients authenticate with a bearer token
    "/api/agent/stream",  # SSE (GET), kept for clarity
)


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    bootstrap.ensure_bootstrap()
    # A run executes in this process; a restart strands the row otherwise.
    try:
        from app.services.agent_runtime import reconcile_orphaned_runs

        outcome = reconcile_orphaned_runs()
        if outcome.get("reconciled"):
            log.warning("reconciled %s interrupted agent run(s) at startup", outcome["reconciled"])
    except Exception:  # noqa: BLE001 - never block startup on this
        log.exception("agent run reconciliation failed at startup")
    from app.services.execution import shutdown_executor

    log.info("Skill Router ready (env=%s, db=%s)", get_settings().environment, get_settings().db_path)
    try:
        yield
    finally:
        shutdown_executor()
        log.info("Skill Router stopped")


# `installer_meta` lives in app.services.installer now: the connect-command router
# needs it too, and importing it from here would be a circular import. Re-exported
# so existing callers (and /api/meta) keep working.
from app.services.installer import installer_meta  # noqa: E402,F401


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Skill Router",
        version=API_VERSION,
        description=(
            "Unified credential management, AI capability routing and SKILL distribution. "
            "Administrator UI/API lives under /api; external agents use /api/v1 and /api/mcp."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    install_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-csrf-token", "authorization", "x-request-id", "x-api-key", "last-event-id", "mcp-session-id", "mcp-protocol-version"],
        expose_headers=["x-request-id", "x-response-time-ms", "etag", "mcp-session-id"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        rid = set_request_id(request.headers.get("x-request-id"))
        if request.method in MUTATING_METHODS and request.url.path.startswith("/api"):
            if not request.url.path.startswith(CSRF_EXEMPT_PREFIXES):
                origin = request.headers.get("origin")
                if origin and origin not in settings.allowed_origins:
                    # Defect fixed: this hand-built body omitted `details`, so the
                    # middleware answered with a different envelope than every other
                    # error (CONTRACT §2.1). Use the one shared helper instead.
                    return error_response(
                        status_code=403,
                        code="origin_rejected",
                        message="Requests from this origin are not allowed.",
                        actions=["Use the configured Skill Router URL."],
                    )
                token = request.cookies.get(settings.session_cookie_name)
                if token:
                    session = session_store.get_session(token)
                    header = request.headers.get("x-csrf-token")
                    if session and not session_store.verify_csrf(session, header):
                        # Same defect as above: the CSRF rejection must use the
                        # uniform envelope (code/message/details/request_id/actions).
                        return error_response(
                            status_code=403,
                            code="csrf_failed",
                            message="The page token is missing or expired. Reload and retry.",
                            actions=["Reload the page.", "Repeat the action."],
                        )
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = rid
        response.headers["x-response-time-ms"] = str(duration_ms)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["x-frame-options"] = "DENY"
        if request.url.path.startswith("/api") and duration_ms > 500:
            log.info("slow request %s %s %sms", request.method, request.url.path, duration_ms)
        return response

    from app.api import (
        admin_agent,
        admin_capabilities,
        admin_connect,
        admin_credentials,
        admin_files,
        admin_overview,
        admin_providers,
        admin_routing,
        admin_settings,
        admin_skills,
        admin_tokens,
        admin_tasks,
        admin_usage,
        auth,
        external,
        health,
        mcp,
        stream,
    )

    for module in (
        auth,
        health,
        admin_overview,
        admin_providers,
        admin_credentials,
        admin_capabilities,
        admin_routing,
        admin_tasks,
        admin_skills,
        admin_tokens,
        admin_usage,
        admin_files,
        admin_settings,
        admin_agent,
        admin_connect,
        external,
        mcp,
        stream,
    ):
        app.include_router(module.router)
        if getattr(module, "public_router", None) is not None:
            app.include_router(module.public_router)

    @app.get("/api/meta", tags=["meta"])
    def meta() -> dict[str, Any]:
        """Non-secret build/deployment facts the UI shows in Settings."""
        from app.services.network import outbound_policy_summary

        status = bootstrap.get_status()
        return {
            "app": "skill-router",
            "api_version": API_VERSION,
            "api_base": "/api/v1",
            "environment": settings.environment,
            "public_base_url": settings.public_base_url,
            "initialized": status.initialized,
            "engine": {
                "upstream": settings.aragon_repo_url,
                "npm": settings.aragon_npm_package,
                "cli_npm": settings.aragon_cli_package,
                "aligned_core": settings.aragon_aligned_core_version,
                "aligned_cli": settings.aragon_aligned_cli_version,
                "aligned_commit": settings.aragon_aligned_commit,
                "channel": settings.aragon_upgrade_channel,
                "tracked_branch": settings.aragon_track_branch,
                "sidecar_url": settings.aragon_sidecar_url,
            },
            "features": {
                "mcp": True,
                "streaming_sse": True,
                "external_api_v1": True,
                "async_tasks": True,
            },
            "network_policy": outbound_policy_summary(),
            "installer": installer_meta(),
            "request_id": get_request_id(),
        }

    return app


app = create_app()
