"""升级状态机与审计（§12.4 状态机）。"""

from __future__ import annotations

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger
from ..models import EngineCandidate, EngineUpgradeRun
from ..settings import settings

log = get_logger("skillrouter.run")

RUN_STEPS = ["detected", "built", "verified", "deployed", "healthy"]


def current_run() -> dict | None:
    with db_session() as s:
        row = s.scalar(select(EngineUpgradeRun).where(EngineUpgradeRun.result.is_(None)))
        if row is None:
            return None
        return _brief(row)


def _brief(row: EngineUpgradeRun) -> dict:
    return {
        "id": row.id,
        "from_version": row.from_version,
        "to_version": row.to_version,
        "mode": row.mode,
        "policy": row.policy,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "result": row.result,
        # 候选结论与「运行怎么结束」是两列，不得互相顶替（§A.6.2 I5）
        "verify_status": row.verify_status,
        "blocked_reasons": list(row.blocked_reasons or []),
        "steps": row.steps or [],
        "error": row.error,
        "backup_file_id": row.backup_file_id,
        "rollback_of": row.rollback_of,
    }


def create_run(*, to_version: str, mode: str, policy: str, rollback_of: str | None = None) -> str:
    if current_run() is not None:
        raise ApiError(
            Code.UPGRADE_IN_PROGRESS,
            message="已有升级正在执行，请等待其结束",
        )
    with db_session() as s:
        from ..db.seed import get_state

        from_pin = get_state(s, "engine_pin", settings.engine_pin)
    run_id = uuid_pk("egr")
    with db_write() as s:
        s.add(
            EngineUpgradeRun(
                id=run_id,
                from_version=from_pin.split("@", 1)[0],
                to_version=to_version,
                mode=mode,
                policy=policy,
                started_at=now_iso(),
                steps=[{"name": "detected", "status": "pending", "detail": f"目标 {to_version}", "duration_ms": 0}],
                rollback_of=rollback_of,
            )
        )
    from ..services import audit

    audit.write(
        "engine.upgrade_started",
        object_type="engine_upgrade_run",
        object_id=run_id,
        summary={"to": to_version, "mode": mode, "policy": policy},
    )
    return run_id


def mark_step(run_id: str, name: str, status: str, detail: str = "", duration_ms: int = 0) -> None:
    with db_write() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            return
        steps = list(row.steps or [])
        for step in steps:
            if step.get("name") == name:
                step.update({"status": status, "detail": detail[:4000], "duration_ms": duration_ms})
                break
        else:
            steps.append({"name": name, "status": status, "detail": detail[:4000], "duration_ms": duration_ms})
        row.steps = steps


def finish_run(run_id: str, *, result: str, error: str | None = None) -> None:
    with db_write() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            return
        row.result = result
        row.error = error
        row.finished_at = now_iso()


def list_runs(*, page: int = 1, page_size: int = 20) -> dict:
    from ..schemas.common import paginate

    with db_session() as s:
        rows = list(s.scalars(select(EngineUpgradeRun).order_by(EngineUpgradeRun.started_at.desc()).limit(100)))
    return paginate([_brief(r) for r in rows], page, page_size)


def get_run(run_id: str) -> dict:
    with db_session() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "升级记录不存在")
        return _brief(row)


def set_candidate_status(version: str, status: str, report: dict) -> None:
    with db_write() as s:
        row = s.scalar(select(EngineCandidate).where(EngineCandidate.version == version))
        if row:
            row.verification_status = status
            row.verification_report = report


def set_verify_status(run_id: str, status: str, blocked_reasons: list[str] | None = None) -> None:
    """写入候选结论（`passed | blocked | failed`）与阻断原因列表。

    只写 `verify_status`/`blocked_reasons`，**不动** `result`
    （`result` 由 `finish_run`/`switch` 按「这次运行怎么结束」写）。
    """
    with db_write() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            return
        row.verify_status = status
        row.blocked_reasons = list(blocked_reasons or [])


def last_candidate_status(version: str) -> str | None:
    """该版本候选行上一次的验证结论（`POST /engine/upgrade` 判断 `acknowledge_blocked`）。"""
    with db_session() as s:
        row = s.scalar(
            select(EngineCandidate)
            .where(EngineCandidate.version == version)
            .order_by(EngineCandidate.detected_at.desc())
        )
    return row.verification_status if row else None
