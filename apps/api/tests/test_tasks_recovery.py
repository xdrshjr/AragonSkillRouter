"""租约过期、重启恢复、取消语义（§15.3）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from skillrouter_api.db import db_write
from skillrouter_api.models import Task, TaskQueue
from skillrouter_api.services import tasks as task_service
from skillrouter_api.worker import leases
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


def _iso(delta_seconds: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=delta_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_lease_claim_and_renew() -> None:
    queue_id = task_service.enqueue("retention_sweep", {"probe": True}, priority=1)
    claimed = leases.claim("w-lease", ["retention_sweep"])
    assert any(row.id == queue_id for row in claimed)
    row_id = next(row.id for row in claimed if row.id == queue_id)
    assert leases.renew(row_id, "w-lease") is True
    assert leases.renew(row_id, "w-other") is False
    leases.release(row_id, worker_id="w-lease", ok=True)
    with db_write() as s:
        assert s.get(TaskQueue, row_id).status == "done"


def test_expired_lease_is_reaped() -> None:
    task_service.enqueue("retention_sweep", {"probe": True})
    leases.claim("w-reap", ["retention_sweep"])
    with db_write() as s:
        row = s.scalar(select(TaskQueue).where(TaskQueue.payload["probe"].as_boolean().is_(True)))
        row.status = "leased"
        row.lease_owner = "w-dead"
        row.lease_expires_at = _iso(-120)
        target = row.id
    reaped = leases.reap()
    assert reaped >= 1
    with db_write() as s:
        assert s.get(TaskQueue, target).status == "queued"


def test_failed_handler_requeues_until_max_attempts(monkeypatch) -> None:
    task_service.enqueue("file_parse", {"file_id": None}, max_attempts=2)
    for _ in range(2):
        claimed = leases.claim("w-retry", ["file_parse"])
        for row in claimed:
            leases.release(row.id, worker_id="w-retry", ok=False, error="boom", retry_delay_s=120)
    with db_write() as s:
        rows = list(s.scalars(select(TaskQueue).where(TaskQueue.kind == "file_parse")))
    assert all(row.status in ("queued", "dead") for row in rows)


def test_cancel_local_only_when_upstream_unsupported(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async"])
    # 去掉 cancel 配置 → 只能本地取消
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    for item in definition["routes"]:
        item["async"]["query"].pop("cancel", None)
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "x"}},
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish",
        headers=admin["headers"],
        json={"version": "1.0.0"},
    )
    token = make_token()
    task_id = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "cancel"}, "options": {"mode": "async"}},
    ).json()["task_id"]
    # 新语义（§A.3.6-1）：`upstream_task_id` 为空时是「提交前取消」（202 pending_submit），
    # 要走到 local_only 分支必须先模拟「已提交上游但上游不支持取消」。
    with db_write() as s:
        row = s.get(Task, task_id)
        row.status = "running"
        row.upstream_task_id = "up-local-1"
        row.cancel_capability = "none"
    r = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=token["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["cancel_scope"] == "local_only"
    assert "远端" in body["note"] or "上游任务可能仍在执行" in body["note"]
    assert body["upstream_task_id"] == "up-local-1", "local_only 必须保留 upstream_task_id 供人工追踪"
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()
    assert detail["status"] == "canceled"
    assert "远端可能仍在执行" in detail["status_detail"]
    assert detail["upstream_task_id"] == "up-local-1"


def test_cancel_remote_when_supported(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async"])
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["async"]["query"]["cancel"] = {
        "method": "POST",
        "url_template": "{base_url}/v1/tasks/{task_id}/cancel",
    }
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    task_id = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "cancel"}, "options": {"mode": "async"}},
    ).json()["task_id"]
    # 模拟已提交上游
    with db_write() as s:
        row = s.get(Task, task_id)
        row.status = "running"
        row.upstream_task_id = "up-task-1"
        row.cancel_capability = "remote"
    r = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=token["headers"])
    assert r.json()["cancel_scope"] == "remote"
    assert r.json()["status"] == "canceling"


def test_status_machine_rejects_illegal_transition() -> None:
    from skillrouter_api.errors import ApiError
    from skillrouter_api.models import Task

    row = Task(
        id="tsk_x",
        request_id="req_x",
        capability_key="k",
        mode="async",
        status="succeeded",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    try:
        task_service.transition(row, "running")
    except ApiError as exc:
        assert exc.code == "INVALID_STATE_TRANSITION"
    else:  # pragma: no cover
        raise AssertionError("应当拒绝 succeeded → running")


def test_recover_on_startup_requeues(monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    task_id = task_service.create(
        request_id="req_recover",
        capability_key="text.generate.unknown",
        capability_version_id=None,
        mode="async",
        input_snapshot={"input": {"prompt": "x"}},
        route_id=None,
        token_id=None,
    )["task_id"]
    # 任务刚创建就已经有一条 queued 队列项 → 重启不得再补一条（否则会重复提交上游）
    result = task_service.recover_on_startup()
    assert result["resumed_tasks"] == 0
    assert result["requeued_leases"] >= 0
    with db_write() as s:
        row = s.get(Task, task_id)
        assert row.status in ("queued", "running")
        queued = [
            item
            for item in s.scalars(select(TaskQueue))
            if (item.payload or {}).get("task_id") == task_id and item.status in ("queued", "leased")
        ]
        assert len(queued) == 1


def test_recover_on_startup_requeues_lost_queue_row() -> None:
    """队列项丢失（进程崩溃在入队之后/租约被清理）时，恢复补一条且只补一条。"""
    task_id = task_service.create(
        request_id="req_recover_lost",
        capability_key="text.generate.unknown",
        capability_version_id=None,
        mode="async",
        input_snapshot={"input": {"prompt": "x"}},
        route_id=None,
        token_id=None,
    )["task_id"]
    with db_write() as s:
        for item in list(s.scalars(select(TaskQueue))):
            if (item.payload or {}).get("task_id") == task_id:
                item.status = "failed"
    result = task_service.recover_on_startup()
    assert result["resumed_tasks"] >= 1
    with db_write() as s:
        queued = [
            item
            for item in s.scalars(select(TaskQueue))
            if (item.payload or {}).get("task_id") == task_id and item.status in ("queued", "leased")
        ]
        assert len(queued) == 1


def test_task_not_found(client, admin) -> None:
    r = client.get("/api/v1/tasks/tsk_missing", headers={"Authorization": "Bearer srk_bad_x"})
    assert r.status_code == 401
    r = client.get("/api/admin/tasks/tsk_missing", headers=admin["headers"])
    assert r.status_code == 404
