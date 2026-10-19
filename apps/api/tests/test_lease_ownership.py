"""租约所有权：越权释放被拒 + 续租丢失取消 handler（§A.3.6 / T-14、T-15）。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from skillrouter_api.db import db_session, db_write
from skillrouter_api.models import TaskQueue
from skillrouter_api.services import tasks as task_service
from skillrouter_api.worker import leases
from skillrouter_api.worker import runner as runner_module
from skillrouter_api.worker.runner import Worker


def _iso(delta_seconds: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lease(queue_id: str, owner: str, *, attempts: int = 1) -> None:
    """把队列项直接置为「已被 owner 租用」——避免依赖队列里其他测试残留条目的优先级。"""
    with db_write() as s:
        row = s.get(TaskQueue, queue_id)
        row.status = "leased"
        row.lease_owner = owner
        row.lease_expires_at = _iso(60)
        row.attempts = attempts


def _run(coro):  # noqa: ANN001, ANN202
    """独立事件循环执行协程，并把循环留在「当前」位置（其它用例仍会用 get_event_loop）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_stale_worker_release_is_rejected() -> None:
    """T-14：租约被抢后原 worker 释放 → False，队列行的 owner/status 不变。"""
    queue_id = task_service.enqueue("retention_sweep", {"probe": "lease-owner-a"})
    _lease(queue_id, "w-a")

    # 自己持有的租约可以正常释放（返回 True）
    other = task_service.enqueue("retention_sweep", {"probe": "lease-owner-self"})
    _lease(other, "w-self")
    assert leases.release(other, worker_id="w-self", ok=True) is True
    with db_session() as s:
        assert s.get(TaskQueue, other).status == "done"

    # 租约过期后被 w-b 重新领取
    _lease(queue_id, "w-b")

    assert leases.release(queue_id, worker_id="w-a", ok=True) is False
    with db_session() as s:
        row = s.get(TaskQueue, queue_id)
        assert row.status == "leased", "越权释放不得改动队列行状态"
        assert row.lease_owner == "w-b", "越权释放不得抢走新 owner"
        assert row.lease_expires_at


def test_lease_lost_cancels_handler(monkeypatch) -> None:
    """T-15：`renew` 失败 → handler 收到取消（事件置位）→ 队列项回到 `queued`。"""
    queue_id = task_service.enqueue("retention_sweep", {"probe": "lease-lost"})
    _lease(queue_id, "w-lost")

    cancelled = threading.Event()

    async def handler(payload: dict) -> dict:  # noqa: ANN001
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"terminal": True}

    monkeypatch.setattr(runner_module, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(leases, "renew", lambda *args, **kwargs: False)

    worker = Worker(worker_id="w-lost")
    worker.register("retention_sweep", handler)

    async def run() -> None:
        with db_session() as s:
            item = s.get(TaskQueue, queue_id)
            s.expunge(item)
        await worker._execute(item)

    _run(run())

    assert cancelled.is_set(), "续租失败后 handler 必须收到取消信号"
    with db_session() as s:
        row = s.get(TaskQueue, queue_id)
    assert row.status == "queued", f"租约丢失后队列项应回到 queued，实际 {row.status}"
    assert row.lease_owner is None
    assert "LEASE_LOST" in (row.last_error or "")
