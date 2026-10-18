"""取消竞态与 `canceling` 不变式（§A.3.6 / T-12、T-13、T-44）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from skillrouter_api.db import db_session, db_write
from skillrouter_api.models import Task, TaskQueue
from skillrouter_api.services import tasks as task_service
from skillrouter_api.worker.runner import Worker
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


def _iso(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _async_capability(client, admin, make_capability, *, cancel: bool) -> dict:
    """发布一个带 async 模式的能力；`cancel=True` 时上游支持远程追取消。"""
    cap = make_capability(modes=["sync", "async"])
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    if cancel:
        definition["routes"][0]["async"]["query"]["cancel"] = {
            "method": "POST",
            "url_template": "{base_url}/v1/tasks/{task_id}/cancel",
        }
    else:
        definition["routes"][0]["async"]["query"].pop("cancel", None)
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "x"}},
    )
    r = client.post(
        f"/api/admin/capabilities/{cap['id']}/publish",
        headers=admin["headers"],
        json={"version": "1.0.0"},
    )
    assert r.status_code in (200, 201), r.text
    return cap


def _invoke_async(client, cap: dict, token: dict) -> str:
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "异步"}, "options": {"mode": "async"}},
    )
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


async def _drain_until(client, token: dict, task_id: str, worker: Worker, limit: int = 60) -> dict:
    """跑到该任务进入终态（或队列空）为止，返回任务详情。"""
    for _ in range(limit):
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()
        if detail["status"] in ("succeeded", "failed", "canceled"):
            return detail
        claimed = await worker.run_once(["capability_invoke_async"])
        if claimed == 0:
            break
    return client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()


def _run(coro):  # noqa: ANN001, ANN202
    """独立事件循环执行协程，并把循环留在「当前」位置（其它用例仍会用 get_event_loop）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_cancel_during_submit_reaches_upstream(
    client, admin, make_capability, make_token, monkeypatch
) -> None:
    """T-12：提交途中取消 → 终态 canceled、upstream_task_id 非空、上游 cancel 被调用 1 次。"""
    from skillrouter_api.executors import async_poll

    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _async_capability(client, admin, make_capability, cancel=True)
    token = make_token()
    task_id = _invoke_async(client, cap, token)

    real_submit = async_poll.submit

    async def submit_then_cancel(**kwargs):  # noqa: ANN003, ANN202
        # 用户在「提交已发出、响应未回」的窗口里点了取消
        result = task_service.cancel(task_id)
        assert result["cancel_scope"] == "pending_submit", result
        return await real_submit(**kwargs)

    monkeypatch.setattr(async_poll, "submit", submit_then_cancel)

    worker = Worker(worker_id="pytest-cancel-race")
    worker.register_all()
    detail = _run(_drain_until(client, token, task_id, worker))

    assert detail["status"] == "canceled", (detail["status"], detail.get("error"))
    assert detail["upstream_task_id"] == "up-task-1", "追取消必须拿到上游任务标识"
    assert detail["cancel_requested_at"], "canceling 路径必须留取消意图时间戳"
    assert upstream.count("/cancel") == 1, [c["url"] for c in upstream.calls]
    error_code = (detail.get("error") or {}).get("code") or ""
    assert "INVALID_STATE_TRANSITION" not in error_code, detail.get("error")
    # 取消竞态不该把队列项判成失败重排队
    with db_session() as s:
        rows = [
            item
            for item in s.scalars(select(TaskQueue))
            if (item.payload or {}).get("task_id") == task_id
        ]
    assert rows and all(item.status in ("done",) for item in rows), [r.status for r in rows]


def test_canceling_state_follows_invariant(client, admin, make_capability, make_token, monkeypatch) -> None:
    """T-13：全表扫描 `canceling ⇒ cancel_requested_at`；两类悬挂行都被 `recover_on_startup` 收敛。"""
    patch_httpx(monkeypatch, FakeUpstream())
    cap = _async_capability(client, admin, make_capability, cancel=True)
    token = make_token()

    # 1) 提交前取消（queued → canceling）
    submitted_early = _invoke_async(client, cap, token)
    r = client.post(f"/api/v1/tasks/{submitted_early}/cancel", headers=token["headers"])
    assert r.status_code == 202, r.text
    # 2) 已提交上游后取消（running → canceling）
    submitted_late = _invoke_async(client, cap, token)
    with db_write() as s:
        row = s.get(Task, submitted_late)
        row.status = "running"
        row.upstream_task_id = "up-late-1"
        row.cancel_capability = "remote"
    r = client.post(f"/api/v1/tasks/{submitted_late}/cancel", headers=token["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["cancel_scope"] == "remote"

    # 不变式 I2：任何 canceling 行都必须有 cancel_requested_at
    with db_session() as s:
        bad = [
            row.id
            for row in s.scalars(select(Task))
            if row.status == "canceling" and not row.cancel_requested_at
        ]
        statuses = {row.status for row in s.scalars(select(Task))}
    assert bad == [], f"canceling 行缺少 cancel_requested_at：{bad}"
    assert {"canceling", "queued"} & statuses

    # 3) 两类悬挂行：超过 SKILLROUTER_CANCEL_RECONCILE_MINUTES（默认 30）仍停在 canceling
    hang_no_upstream = _invoke_async(client, cap, token)
    hang_with_upstream = _invoke_async(client, cap, token)
    stale = _iso(-3600)
    with db_write() as s:
        first = s.get(Task, hang_no_upstream)
        first.status = "canceling"
        first.cancel_requested_at = stale
        first.upstream_task_id = None
        first.cancel_capability = "none"
        second = s.get(Task, hang_with_upstream)
        second.status = "canceling"
        second.cancel_requested_at = stale
        second.upstream_task_id = "up-hang-1"
        second.cancel_capability = "remote"
        for item in list(s.scalars(select(TaskQueue))):
            if (item.payload or {}).get("task_id") in (hang_no_upstream, hang_with_upstream):
                item.status = "done"

    first_pass = task_service.recover_on_startup()
    assert first_pass["cancel_abandoned"] >= 1
    assert first_pass["cancel_requeued"] >= 1
    with db_write() as s:
        no_upstream = s.get(Task, hang_no_upstream)
        assert no_upstream.status == "canceled"
        assert no_upstream.resume_policy == "abandon"
        with_upstream = s.get(Task, hang_with_upstream)
        assert with_upstream.status == "canceling", "第 1 次只重新入队一次 cancel_only"
        cancel_items = [
            item
            for item in s.scalars(select(TaskQueue))
            if (item.payload or {}).get("cancel_only") and (item.payload or {}).get("task_id") == hang_with_upstream
        ]
        assert len(cancel_items) == 1, [i.payload for i in cancel_items]

    task_service.recover_on_startup()
    with db_write() as s:
        with_upstream = s.get(Task, hang_with_upstream)
        assert with_upstream.status == "canceled", "第 2 次必须直接收敛，不能永远停在 canceling"
        assert "远端可能仍在执行" in (with_upstream.error_detail_redacted or "")
        # 收尾：本用例造的两条 canceling 行收敛掉，避免污染后续用例的 recover 计数
        for task_id in (submitted_early, submitted_late):
            row = s.get(Task, task_id)
            if row.status == "canceling":
                task_service.transition(row, "canceled")


def test_queued_cancel_is_allowed(client, admin, make_capability, make_token, monkeypatch) -> None:
    """T-44：`queued` 取消 → `canceling`（不抛 INVALID_STATE_TRANSITION），最终 `canceled`。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _async_capability(client, admin, make_capability, cancel=True)
    token = make_token()
    task_id = _invoke_async(client, cap, token)
    submits_before = upstream.count("/v1/jobs")
    assert submits_before == 0

    r = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=token["headers"])
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "canceling"
    assert body["cancel_scope"] == "pending_submit"
    assert body["cancel_requested_at"]

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()
    assert detail["status"] == "canceling"
    assert detail["cancel_scope"] == "pending_submit"
    assert (detail.get("error") or {}).get("code") != "INVALID_STATE_TRANSITION"

    worker = Worker(worker_id="pytest-queued-cancel")
    worker.register_all()
    detail = _run(_drain_until(client, token, task_id, worker))
    assert detail["status"] == "canceled"
    assert upstream.count("/v1/jobs") == submits_before, "从未提交上游的任务不该产生提交调用"
    with db_session() as s:
        assert s.get(Task, task_id).upstream_task_id is None
