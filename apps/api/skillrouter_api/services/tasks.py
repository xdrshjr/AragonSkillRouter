"""任务：创建/查询/取消/恢复/状态机（§7.8 / §4.4(d)）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import Task, TaskQueue
from ..schemas.common import paginate

log = get_logger("skillrouter.run")

ALLOWED = {
    "queued": {"running", "canceled", "failed"},
    "running": {"succeeded", "failed", "canceling", "canceled", "status_unknown"},
    "status_unknown": {"running", "failed", "succeeded"},
    "canceling": {"canceled", "status_unknown", "failed"},
    "canceled": set(),
    "succeeded": set(),
    "failed": set(),
}


def enqueue(kind: str, payload: dict, *, priority: int = 0, delay_s: int = 0, max_attempts: int = 3) -> str:
    queue_id = uuid_pk("tq")
    available = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_s)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_write() as s:
        s.add(
            TaskQueue(
                id=queue_id,
                kind=kind,
                payload=payload,
                priority=priority,
                status="queued",
                available_at=available,
                attempts=0,
                max_attempts=max_attempts,
                created_at=now_iso(),
            )
        )
    return queue_id


def transition(task: Task, target: str) -> None:
    if target == task.status:
        return
    if target not in ALLOWED.get(task.status, set()):
        raise ApiError(
            Code.INVALID_STATE_TRANSITION,
            message=f"任务状态不能从 {task.status} 迁移到 {target}",
            details={"from": task.status, "to": target},
        )
    task.status = target
    task.updated_at = now_iso()
    if target in ("succeeded", "failed", "canceled"):
        task.finished_at = now_iso()
        if target == "succeeded":
            task.progress_percent = 100


def create(
    *,
    request_id: str,
    capability_key: str,
    capability_version_id: str | None,
    mode: str,
    input_snapshot: dict,
    route_id: str | None,
    token_id: str | None,
    agent_session_id: str | None = None,
    max_attempts: int = 3,
) -> dict:
    task_id = uuid_pk("tsk")
    with db_write() as s:
        s.add(
            Task(
                id=task_id,
                request_id=request_id,
                capability_key=capability_key,
                capability_version_id=capability_version_id,
                mode=mode,
                status="queued",
                progress_percent=0,
                progress_stage="queued",
                input_snapshot=input_snapshot,
                route_id=route_id,
                token_id=token_id,
                agent_session_id=agent_session_id,
                max_attempts=max_attempts,
                resume_policy="check",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    enqueue(
        "capability_invoke_async",
        {"task_id": task_id, "request_id": request_id},
        priority=5,
        max_attempts=max_attempts,
    )
    return {"task_id": task_id, "request_id": request_id, "status": "queued"}


def get(task_id: str) -> dict:
    with db_session() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        return _detail(row)


def get_owned(task_id: str, token_id: str) -> dict:
    """外部 Agent 读取任务：不属于该令牌时按「不存在」处理，避免跨令牌探测（§11）。"""
    with db_session() as s:
        row = s.get(Task, task_id)
        if row is None or (row.token_id and row.token_id != token_id):
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        return _detail(row)


def _detail(row: Task) -> dict:
    return {
        "task_id": row.id,
        "request_id": row.request_id,
        "capability_key": row.capability_key,
        "capability_version_id": row.capability_version_id,
        "mode": row.mode,
        "status": row.status,
        "progress": {"percent": row.progress_percent, "stage": row.progress_stage},
        "status_detail": _status_detail(row),
        "upstream_task_id": row.upstream_task_id,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "failure_class": row.failure_class,
        "cancel_capability": row.cancel_capability,
        "error": (
            {"code": row.error_code, "message": row.error_detail_redacted}
            if row.error_code
            else None
        ),
        "result": row.result or {},
        "output_files": row.output_files or [],
        "resume_policy": row.resume_policy,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "finished_at": row.finished_at,
    }


def _status_detail(row: Task) -> str:
    mapping = {
        "queued": "平台已受理，尚未提交上游",
        "running": "正在等待上游完成" if row.upstream_task_id else "正在提交上游",
        "canceling": "已请求上游取消，等待确认",
        "canceled": "已取消（若上游不支持取消，远端可能仍在执行）",
        "succeeded": "已完成",
        "failed": "已失败",
        "status_unknown": "无法判定上游是否受理，请先对账查询",
    }
    return mapping.get(row.status, row.status)


def list_tasks(
    *,
    status: str | None = None,
    capability: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    with db_session() as s:
        stmt = select(Task).order_by(desc(Task.created_at))
        if status == "active":
            stmt = stmt.where(Task.status.in_(("queued", "running", "canceling")))
        elif status == "unknown":
            stmt = stmt.where(Task.status == "status_unknown")
        elif status:
            stmt = stmt.where(Task.status == status)
        if capability:
            stmt = stmt.where(Task.capability_key == capability)
        rows = list(s.scalars(stmt.limit(1000)))
    items = [_detail(r) for r in rows]
    if q:
        needle = q.lower()
        items = [
            i
            for i in items
            if needle in i["task_id"].lower()
            or needle in i["capability_key"].lower()
            or needle in (i["upstream_task_id"] or "").lower()
        ]
    return paginate(items, page, page_size)


def update_progress(task_id: str, *, percent: int | None = None, stage: str | None = None) -> None:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row:
            if percent is not None:
                row.progress_percent = max(0, min(100, int(percent)))
            if stage:
                row.progress_stage = stage
            row.updated_at = now_iso()


def mark_running(task_id: str, *, upstream_task_id: str | None = None) -> None:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        transition(row, "running")
        row.attempts = int(row.attempts or 0) + 1
        if upstream_task_id:
            row.upstream_task_id = upstream_task_id
            row.cancel_capability = "remote"
        row.progress_stage = "upstream_polling" if upstream_task_id else "submitting"


def claim_submission(task_id: str) -> bool:
    """提交上游前的 compare-and-set：只有 queued 的任务可以被认领提交。

    重启恢复、重复入队或多 worker 竞争时，同一任务只能有一个执行者真正调用上游，
    否则会给用户重复下单/重复计费（§7.8 / §9.3 幂等）。
    """
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None or row.status != "queued":
            return False
        transition(row, "running")
        row.attempts = int(row.attempts or 0) + 1
        row.progress_stage = "submitting"
        row.updated_at = now_iso()
        return True


def runtime_context(task_id: str) -> dict:
    """worker 专用执行上下文：冻结的能力版本 / 路由与输入快照（不对外暴露）。"""
    with db_session() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        return {
            "task_id": row.id,
            "request_id": row.request_id,
            "capability_key": row.capability_key,
            "capability_version_id": row.capability_version_id,
            "route_id": row.route_id,
            "token_id": row.token_id,
            "mode": row.mode,
            "status": row.status,
            "upstream_task_id": row.upstream_task_id,
            "progress_stage": row.progress_stage,
            "input_snapshot": dict(row.input_snapshot or {}),
        }


def finish(task_id: str, *, status: str, result: dict | None = None, error: dict | None = None) -> None:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            return
        transition(row, status)
        if result is not None:
            row.result = result
        if error is not None:
            row.error_code = error.get("code")
            row.error_detail_redacted = redact(str(error.get("message") or ""), max_len=1024)
            if status == "failed":
                row.failure_class = "UPSTREAM" if str(error.get("code", "")).startswith("UPSTREAM") else "INTERNAL"


def cancel(task_id: str, *, scope: str = "remote") -> dict:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        if row.status in ("succeeded", "failed", "canceled"):
            return {"ok": False, "status": row.status, "cancel_scope": "none"}
        if row.cancel_capability != "remote":
            transition(row, "canceled")
            return {
                "ok": True,
                "status": "canceled",
                "cancel_scope": "local_only",
                "note": "已停止平台侧等待；上游任务可能仍在执行并产生费用",
            }
        transition(row, "canceling")
        return {"ok": True, "status": "canceling", "cancel_scope": scope}


def mark_canceled(task_id: str, *, upstream_ok: bool) -> dict:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        if upstream_ok:
            transition(row, "canceled")
            return {"status": "canceled", "cancel_scope": "remote"}
        transition(row, "canceled")
        return {
            "status": "canceled",
            "cancel_scope": "local_only",
            "note": "上游未确认取消，远端任务可能仍在执行",
        }


def reconcile(task_id: str) -> dict:
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "任务不存在")
        if row.status != "status_unknown":
            return {"ok": False, "status": row.status, "note": "只有 status_unknown 需要人工对账"}
        row.status = "running"
        row.resume_policy = "check"
        row.updated_at = now_iso()
    enqueue("capability_invoke_async", {"task_id": task_id, "reconcile": True}, priority=8)
    return {"ok": True, "status": "running", "note": "已排队对上游进行查询（不会重复提交）"}


def recover_on_startup() -> dict:
    """worker 启动：复位过期租约 + 恢复未完成任务轮询。

    只给「队列里没有在途条目」的任务补一条恢复条目；否则重启会把同一个
    异步任务排两次，两个执行者各提交一次上游 = 重复下单（§7.8）。
    """
    now = now_iso()
    reset = 0
    with db_write() as s:
        for item in s.scalars(select(TaskQueue).where(TaskQueue.status == "leased")):
            if not item.lease_expires_at or item.lease_expires_at < now:
                item.status = "queued"
                item.lease_owner = None
                item.lease_expires_at = None
                item.attempts = int(item.attempts or 0)
                reset += 1
        pending = list(
            s.scalars(
                select(Task).where(Task.status.in_(("queued", "running", "canceling", "status_unknown")))
            )
        )
        in_flight = {
            (item.payload or {}).get("task_id")
            for item in s.scalars(
                select(TaskQueue).where(TaskQueue.status.in_(("queued", "leased")))
            )
        }
    resumed = 0
    for task in pending:
        if task.id in in_flight:
            continue
        queued = enqueue(
            "capability_invoke_async",
            {"task_id": task.id, "request_id": task.request_id, "resume": True},
            priority=3,
        )
        resumed += 1
        log.info("task_resumed", extra={"object": task.id, "detail": queued})
    return {"requeued_leases": reset, "resumed_tasks": resumed}


def queue_depth() -> int:
    with db_session() as s:
        return len(list(s.scalars(select(TaskQueue).where(TaskQueue.status.in_(("queued", "leased"))))))


def recent_failures(hours: int = 24) -> list[dict]:
    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_session() as s:
        rows = list(
            s.scalars(
                select(Task).where(Task.status == "failed", Task.updated_at >= since).order_by(desc(Task.updated_at)).limit(20)
            )
        )
        return [_detail(r) for r in rows]
