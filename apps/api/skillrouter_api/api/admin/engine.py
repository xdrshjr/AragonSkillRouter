"""引擎路由（§9.2 /engine/*）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ...agent.client import get_adapter
from ...db import db_session
from ...db.seed import get_state
from ...deps import require_password_changed
from ...errors import ApiError, Code
from ...models import EngineCapabilityStatus, EngineUpgradeRun
from ...schemas.engine import RollbackEngineRequest, UpgradeRequest
from ...security.sessions import SessionContext
from ...services import tasks
from ...settings import settings
from ...upgrade import detect, rollback, state

router = APIRouter(prefix="/engine", tags=["engine"])


@router.get("/status")
async def status(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    adapter = get_adapter()
    engine: dict
    tools: list[str] = []
    try:
        health = await adapter.health()
        engine = health.get("engine") or {}
        tools = health.get("tools") or []
        engine["reachable"] = True
    except ApiError as exc:
        engine = {"reachable": False, "status": "unavailable", "error": exc.message}
    with db_session() as s:
        caps = list(
            s.scalars(select(EngineCapabilityStatus).order_by(EngineCapabilityStatus.ordinal))
        )
        running = s.scalar(select(EngineUpgradeRun).where(EngineUpgradeRun.result.is_(None)))
        last_checked = get_state(s, "engine_last_check", None)
    return {
        **detect.settings_note(),
        "engine": engine,
        "engine_policy": settings.engine_policy,
        "last_checked_at": last_checked,
        "tools": [
            {"name": t} if isinstance(t, str) else t for t in tools
        ][:30],
        "adapter_version": "1",
        "pinned_package": "@aragon-agent/core",
        "capabilities": [
            {
                "key": c.capability_key,
                "state": c.state,
                "detail": c.detail,
                "since_version": c.since_version,
                "ordinal": c.ordinal,
            }
            for c in caps
        ],
        "upgrade_in_progress": bool(running),
        "checks_allowed": settings.egress_policy == "allow",
    }


@router.get("/candidates")
async def candidates(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": detect.list_candidates()}


@router.post("/check")
async def check(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    result = detect.check(triggered_by=ctx.admin_id)
    detect.mark_checked()
    return result


@router.post("/upgrade", status_code=202)
async def upgrade(payload: UpgradeRequest, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    if payload.mode not in ("verify_only", "deploy"):
        raise ApiError(Code.VALIDATION_FAILED, "mode 必须是 verify_only 或 deploy")
    if state.current_run() is not None:
        raise ApiError(Code.UPGRADE_IN_PROGRESS)
    run_id = state.create_run(to_version=payload.version, mode=payload.mode, policy=settings.engine_policy)
    tasks.enqueue(
        "engine_upgrade",
        {"run_id": run_id, "version": payload.version, "mode": payload.mode},
        priority=9,
        max_attempts=1,
    )
    return {"run_id": run_id, "status": "queued", "mode": payload.mode}


@router.get("/runs")
async def runs(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return state.list_runs()


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return state.get_run(run_id)


@router.post("/rollback")
async def do_rollback(
    payload: RollbackEngineRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    if payload.to_version:
        return await rollback.rollback_to(payload.to_version, reason="管理员手动回滚", actor=ctx.admin_id)
    return await rollback.rollback_to_previous(reason="管理员手动回滚", actor=ctx.admin_id)


@router.get("/deployments")
async def deployments(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": rollback.history()}


@router.get("/capabilities")
async def engine_capabilities(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    with db_session() as s:
        caps = list(
            s.scalars(select(EngineCapabilityStatus).order_by(EngineCapabilityStatus.ordinal))
        )
    grouped: dict[str, list[dict]] = {"available": [], "needs_adapter": [], "permission_limited": [], "unsupported": []}
    for cap in caps:
        grouped.setdefault(cap.state, []).append(
            {"key": cap.capability_key, "detail": cap.detail, "since_version": cap.since_version}
        )
    return {
        "groups": grouped,
        "note": "不得因为引擎版本升级就宣称全部新能力可用；状态由升级验证结果重算",
    }
