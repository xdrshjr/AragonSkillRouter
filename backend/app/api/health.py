"""Health and readiness. No auth: these are for probes and the settings page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import db
from app.core.config import get_settings
from app.services import bootstrap

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "skill-router", "version": "1.0.0"}


@router.get("/ready")
def ready() -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True
    try:
        db.query_scalar("SELECT 1")
        checks["database"] = {"status": "ok", "path": str(get_settings().db_path)}
    except Exception as exc:  # pragma: no cover - operational signal
        ok = False
        checks["database"] = {"status": "error", "message": str(exc)}
    try:
        status = bootstrap.get_status()
        checks["bootstrap"] = {
            "status": "ok" if status.initialized else "pending",
            "must_change_password": status.must_change_password,
            "admin": status.admin_username,
        }
        if not status.initialized:
            ok = False
    except Exception as exc:  # pragma: no cover
        ok = False
        checks["bootstrap"] = {"status": "error", "message": str(exc)}
    try:
        from app.services.worker import queue_depth

        checks["queue"] = {"status": "ok", **queue_depth()}
    except Exception as exc:  # pragma: no cover
        checks["queue"] = {"status": "unknown", "message": str(exc)}
    body = {"status": "ready" if ok else "degraded", "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=body)
