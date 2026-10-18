"""租约：acquire / renew / release / reap（§7.8）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from ..db import db_write, now_iso
from ..models import TaskQueue

LEASE_SECONDS = 60


def _iso(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def claim(worker_id: str, kinds: list[str], limit: int = 1) -> list[TaskQueue]:
    """单条 UPDATE ... WHERE id=(SELECT ...) RETURNING id 领取（SQLite 3.35+）。

    必须是「条件更新」而不是「先 SELECT 再改」：api 与 worker 是两个进程，
    `db_write()` 的锁只在进程内有效，SELECT-then-mutate 会让两个进程同时
    领到同一条队列项（§7.8）。
    """
    claimed: list[TaskQueue] = []
    now = now_iso()
    expires = _iso(LEASE_SECONDS)
    for _ in range(limit):
        with db_write() as s:
            pick = (
                select(TaskQueue.id)
                .where(
                    TaskQueue.status == "queued",
                    TaskQueue.available_at <= now,
                )
                .order_by(TaskQueue.priority.desc(), TaskQueue.available_at)
                .limit(1)
            )
            if kinds:
                pick = pick.where(TaskQueue.kind.in_(kinds))
            stmt = (
                update(TaskQueue)
                .where(TaskQueue.id == pick.scalar_subquery(), TaskQueue.status == "queued")
                .values(
                    status="leased",
                    lease_owner=worker_id,
                    lease_expires_at=expires,
                    attempts=func.coalesce(TaskQueue.attempts, 0) + 1,
                )
                .returning(TaskQueue.id)
            )
            queue_id = s.scalar(stmt)
            if queue_id is None:
                break
            row = s.get(TaskQueue, queue_id)
            if row is None:  # pragma: no cover - 领到后立即被删除
                break
            s.expunge(row)
            claimed.append(row)
    return claimed


def renew(queue_id: str, worker_id: str) -> bool:
    with db_write() as s:
        row = s.get(TaskQueue, queue_id)
        if row is None or row.status != "leased" or row.lease_owner != worker_id:
            return False
        row.lease_expires_at = _iso(LEASE_SECONDS)
        return True


def release(queue_id: str, *, ok: bool, error: str | None = None, retry_delay_s: int = 0) -> None:
    with db_write() as s:
        row = s.get(TaskQueue, queue_id)
        if row is None:
            return
        if ok:
            row.status = "done"
            row.lease_owner = None
            row.lease_expires_at = None
        else:
            row.last_error = (error or "")[:2000]
            if int(row.attempts or 0) >= int(row.max_attempts or 3):
                row.status = "dead"
                row.lease_owner = None
                row.lease_expires_at = None
            else:
                row.status = "queued"
                row.lease_owner = None
                row.lease_expires_at = None
                row.available_at = _iso(retry_delay_s or 5)


def reap() -> int:
    """把租约过期的队列项复位为 queued（worker 崩溃恢复）。"""
    now = now_iso()
    count = 0
    with db_write() as s:
        for row in s.scalars(select(TaskQueue).where(TaskQueue.status == "leased")):
            if not row.lease_expires_at or row.lease_expires_at < now:
                if int(row.attempts or 0) >= int(row.max_attempts or 3):
                    row.status = "dead"
                else:
                    row.status = "queued"
                row.lease_owner = None
                row.lease_expires_at = None
                count += 1
    return count


def depth() -> dict:
    from ..db import db_session

    with db_session() as s:
        rows = list(s.scalars(select(TaskQueue)))
    out = {"queued": 0, "leased": 0, "done": 0, "dead": 0}
    for row in rows:
        out[row.status] = out.get(row.status, 0) + 1
    return out
