"""Durable task lifecycle, lease and cancel-truthfulness tests (CONTRACT §12).

Scope note: every upstream interaction here is a *fake* — `execution.fetch_status`
and `execution.request_cancel` are monkeypatched. These are engine-level tests of
our own state machine (leases, retries, deadlines, truthful cancellation). They
are not evidence that any real provider behaves this way; that needs a live
capability test.

Covered: lease recovery after a worker restart, claim exclusion for a live lease,
cancel truthfulness (unsupported vs accepted), transient-error retry that ends in
`uncertain` only at the deadline, and a single `worker.run_once` pass.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.delenv("SR_MASTER_KEYS", raising=False)
    monkeypatch.delenv("SR_MASTER_KEY_ACTIVE", raising=False)
    config.reset_settings_cache()
    settings = config.get_settings()
    settings.ensure_dirs()
    from app.core import db

    db.init_db()
    from app.security import crypto

    crypto.create_keyring(settings.keyring_path, overwrite=True)
    yield settings
    config.reset_settings_cache()


# ------------------------------------------------------------------ fixtures -
def _async_config() -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("video")
    config["auth"] = {"mode": "none"}
    config["modes"] = {"sync": False, "async": True, "stream": False}
    config["request"]["url"] = "https://api.example.com/v1/render"
    config["request"]["body_template"] = {"prompt": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["response"] = {"kind": "json", "extract": {"result": "$.output"}, "artifact": None}
    config["async"] = {
        "enabled": True,
        "submit_extract": {"task_id": "$.id"},
        "status": {
            "url": "https://api.example.com/v1/render/{task_id}",
            "method": "GET",
            "extract": {"status": "$.status", "progress": "$.progress"},
            "map": {
                "queued": ["queued", "pending"],
                "running": ["running", "processing"],
                "succeeded": ["succeeded", "completed"],
                "failed": ["failed", "error"],
                "cancelled": ["cancelled", "canceled"],
            },
            "poll_interval_s": 1,
            "result_extract": {"result": "$.output"},
        },
        "cancel": {
            "url": "https://api.example.com/v1/render/{task_id}/cancel",
            "method": "POST",
            "extract": {"status": "$.status"},
        },
    }
    config["limits"] = {"timeout_s": 30, "max_duration_s": 300}
    return config


def _make_task(
    *,
    status: str = "running",
    poll_after: str | None = None,
    deadline: str | None = None,
    lease_owner: str | None = None,
    lease_until: str | None = None,
    attempts: int = 0,
    max_attempts: int = 3,
    slug: str = "video.render",
) -> dict[str, Any]:
    """Insert a capability, its async version and a task row."""
    from app.core import db

    now = db.iso()
    cap_id = db.new_id("cap")
    version_id = db.new_id("cver")
    task_id = db.new_id("task")
    db.insert(
        "capabilities",
        {
            "id": cap_id,
            "slug": f"{slug}.{task_id[-6:]}",
            "name": "Render",
            "type": "video",
            "description": "",
            "provider_id": None,
            "credential_id": None,
            "status": "published",
            "current_version_id": version_id,
            "needs_live_agent": 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "capability_versions",
        {
            "id": version_id,
            "capability_id": cap_id,
            "version": 1,
            "status": "published",
            "config": db.dumps(_async_config()),
            "notes": "",
            "checksum": "",
            "created_by": None,
            "created_at": now,
            "published_at": now,
        },
    )
    db.insert(
        "tasks",
        {
            "id": task_id,
            "request_id": db.new_id("req"),
            "kind": "capability_async",
            "capability_id": cap_id,
            "version_id": version_id,
            "provider_id": None,
            "route_id": None,
            "agent_id": None,
            "upstream_task_id": db.new_id("up"),
            "status": status,
            "progress": 0,
            "progress_note": None,
            "input": db.dumps({"prompt": "a red fox"}),
            "result": "{}",
            "error": None,
            "idempotency_key": None,
            "fingerprint": None,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "budget_usd": None,
            "cost_actual_usd": None,
            "poll_after": poll_after or db.iso(db.utcnow() - timedelta(seconds=5)),
            "deadline": deadline or db.iso(db.utcnow() + timedelta(minutes=10)),
            "lease_owner": lease_owner,
            "lease_until": lease_until,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        },
    )
    invocation_id = db.new_id("inv")
    db.insert(
        "invocations",
        {
            "id": invocation_id,
            "request_id": db.query_scalar("SELECT request_id FROM tasks WHERE id = ?", (task_id,)),
            "caller_type": "agent",
            "agent_id": None,
            "surface": "http",
            "capability_id": cap_id,
            "version_id": version_id,
            "provider_id": None,
            "route_id": None,
            "route_reason": "test",
            "mode": "async",
            "status": "queued",
            "http_status": 202,
            "latency_ms": 12,
            "retries": 0,
            "task_id": task_id,
            "idempotency_key": None,
            "request_digest": None,
            "request_preview": "{}",
            "response_preview": "{}",
            "usage": "{}",
            "cost_estimate_usd": None,
            "cost_actual_usd": None,
            "error_code": None,
            "error_message": None,
            "created_at": now,
            "finished_at": None,
        },
    )
    return {"task_id": task_id, "capability_id": cap_id, "version_id": version_id, "invocation_id": invocation_id}


@pytest.fixture()
def fake_status(monkeypatch):
    """Replace the executor's upstream status probe with a scripted fake."""
    from app.services import execution

    calls: list[dict[str, Any]] = []
    script: dict[str, Any] = {"payload": {"ok": True, "status": "running", "progress": 40}}

    async def _fetch_status(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        item = script["payload"]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(kwargs)
        return item

    monkeypatch.setattr(execution, "fetch_status", _fetch_status)
    return {"calls": calls, "script": script}


@pytest.fixture()
def fake_cancel(monkeypatch):
    from app.services import execution

    calls: list[dict[str, Any]] = []
    script: dict[str, Any] = {"payload": {"supported": False, "status": "unsupported", "message": "no cancel endpoint"}}

    async def _request_cancel(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return script["payload"]

    monkeypatch.setattr(execution, "request_cancel", _request_cancel)
    return {"calls": calls, "script": script}


def _task(task_id: str) -> dict[str, Any]:
    from app.core import db

    return db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def _invocation(invocation_id: str) -> dict[str, Any]:
    from app.core import db

    return db.query_one("SELECT * FROM invocations WHERE id = ?", (invocation_id,))


# -------------------------------------------------- lease / restart recovery -
def test_restart_reclaims_a_task_whose_lease_expired(isolated, fake_status):
    """A worker that died mid-poll must not strand its task."""
    from app.services import tasks as task_service

    rows = _make_task(
        status="running",
        poll_after="2020-01-01T00:00:00.000000Z",
        lease_owner="worker:old-host:4242",
        lease_until="2020-01-01T00:00:00.000000Z",
    )
    recovery = asyncio.run(task_service.recover_stale_tasks())
    assert recovery["requeued"] >= 1

    claimed = asyncio.run(task_service.claim_due_tasks("worker:new-host:5151", limit=5))
    assert [row["id"] for row in claimed] == [rows["task_id"]]
    assert _task(rows["task_id"])["lease_owner"] == "worker:new-host:5151"

    # Second claim while the lease is live: nothing is handed out twice.
    assert asyncio.run(task_service.claim_due_tasks("worker:other:6161", limit=5)) == []


def test_queue_claim_excludes_a_live_leased_row(isolated):
    from app.core import db
    from app.services import tasks as task_service

    free = _make_task(status="running", poll_after=db.iso(db.utcnow() - timedelta(seconds=30)))
    leased = _make_task(
        status="running",
        poll_after=db.iso(db.utcnow() - timedelta(seconds=30)),
        lease_owner="worker:busy:1",
        lease_until=db.iso(db.utcnow() + timedelta(minutes=5)),
    )

    claimed = asyncio.run(task_service.claim_due_tasks("worker:fresh:2", limit=10))

    ids = [row["id"] for row in claimed]
    assert free["task_id"] in ids
    assert leased["task_id"] not in ids, "a row with a live lease belongs to another worker"


def test_job_queue_leases_jobs_and_expired_leases_are_reclaimed(isolated):
    from app.core import db
    from app.services import tasks as task_service

    job = asyncio.run(task_service.enqueue_job("task.poll", {"task_id": "task_x"}, priority=10))
    claimed = asyncio.run(task_service.claim_jobs("worker:a", limit=5))
    assert [row["id"] for row in claimed] == [job["id"]]
    assert claimed[0]["payload"] == {"task_id": "task_x"}

    # A second worker cannot take it while the lease is valid.
    assert asyncio.run(task_service.claim_jobs("worker:b", limit=5)) == []

    # The owning worker crashes: the expired lease is reclaimed on the next claim.
    db.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (db.iso(db.utcnow() - timedelta(minutes=1)), job["id"]))
    reclaimed = asyncio.run(task_service.claim_jobs("worker:c", limit=5))
    assert [row["id"] for row in reclaimed] == [job["id"]]
    assert reclaimed[0]["lease_owner"] == "worker:c"


# ------------------------------------------------------ cancel truthfulness --
def test_cancel_without_provider_support_keeps_the_task_running(isolated, fake_cancel, fake_status):
    """No cancel endpoint: we stop waiting, the upstream job may still run."""
    from app.services import tasks as task_service

    rows = _make_task(status="running")
    outcome = asyncio.run(task_service.cancel_task(rows["task_id"], {"type": "user", "id": "usr_1"}))

    assert outcome["supported"] is False
    assert outcome["canceled"] is False
    assert outcome["upstream_may_continue"] is True
    assert outcome["status"] == "running", "we must never claim the provider cancelled"
    assert fake_cancel["calls"], "the site must ask the provider, not assume"
    assert fake_status["calls"] == [], "no status probe should mask the unsupported cancel"

    stored = _task(rows["task_id"])
    assert stored["status"] == "running"
    assert stored["finished_at"] is None
    note = (stored["progress_note"] or "").lower()
    assert "no cancel endpoint" in note and "may still complete" in note


def test_cancel_accepted_and_confirmed_sets_cancelled(isolated, fake_cancel, fake_status):
    from app.services import tasks as task_service

    fake_cancel["script"]["payload"] = {
        "supported": True,
        "status": "accepted",
        "http_status": 200,
        "message": "The provider accepted the cancellation request.",
    }
    fake_status["script"]["payload"] = {"ok": True, "status": "cancelled", "upstream_status": "canceled", "progress": 100}

    rows = _make_task(status="running")
    outcome = asyncio.run(task_service.cancel_task(rows["task_id"]))

    assert outcome["supported"] is True
    assert outcome["canceled"] is True
    assert outcome["status"] == "cancelled"
    assert outcome["upstream_may_continue"] is False
    assert _task(rows["task_id"])["status"] == "cancelled"
    assert _invocation(rows["invocation_id"])["status"] == "cancelled"


def test_cancel_accepted_but_provider_still_running_stays_unconfirmed(isolated, fake_cancel, fake_status):
    from app.services import tasks as task_service

    fake_cancel["script"]["payload"] = {"supported": True, "status": "accepted", "http_status": 200, "message": "ok"}
    fake_status["script"]["payload"] = {"ok": True, "status": "running", "upstream_status": "processing", "progress": 55}

    rows = _make_task(status="running")
    outcome = asyncio.run(task_service.cancel_task(rows["task_id"]))

    assert outcome["canceled"] is False
    assert outcome["upstream_may_continue"] is True
    assert _task(rows["task_id"])["status"] == "cancel_requested"
    assert _task(rows["task_id"])["status"] != "cancelled"


# ------------------------------------------------- transient error handling --
def test_transient_status_error_retries_with_backoff_until_the_deadline(isolated, fake_status):
    """A network blip is never a task failure; only the deadline ends the wait."""
    from app.services import tasks as task_service

    rows = _make_task(status="running", max_attempts=5)
    fake_status["script"]["payload"] = httpx.ReadTimeout("provider did not answer")

    first = asyncio.run(task_service.poll_task(rows["task_id"]))
    assert first["status"] == "running", "a transient network error must not fail the task"
    stored = _task(rows["task_id"])
    assert stored["attempts"] == 1
    assert "Retrying" in (stored["progress_note"] or "")
    assert stored["poll_after"] > stored["updated_at"]
    assert stored["finished_at"] is None

    second = asyncio.run(task_service.poll_task(rows["task_id"]))
    assert second["status"] == "running"
    assert _task(rows["task_id"])["attempts"] == 2

    # Deadline passes while the outcome is still unknown -> uncertain, not failed.
    from app.core import db

    db.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (db.iso(db.utcnow() - timedelta(minutes=1)), rows["task_id"]))
    final = asyncio.run(task_service.poll_task(rows["task_id"]))

    assert final["status"] == "uncertain"
    assert final["error"]["code"] == "task_timeout"
    assert _task(rows["task_id"])["finished_at"] is not None
    assert _invocation(rows["invocation_id"])["status"] == "uncertain"


def test_exhausted_transient_attempts_end_uncertain_not_failed(isolated, fake_status):
    from app.services import tasks as task_service

    rows = _make_task(status="running", max_attempts=2)
    fake_status["script"]["payload"] = httpx.ConnectError("connect failed")

    asyncio.run(task_service.poll_task(rows["task_id"]))
    assert _task(rows["task_id"])["status"] == "running"
    outcome = asyncio.run(task_service.poll_task(rows["task_id"]))

    assert outcome["status"] == "uncertain"
    assert outcome["error"]["code"] == "task_poll_failed"
    assert _invocation(rows["invocation_id"])["status"] == "uncertain"


def test_provider_still_running_past_the_deadline_becomes_uncertain(isolated, fake_status):
    """A live poll that outlives the deadline must not keep a task running forever."""
    from datetime import timedelta

    from app.core import db
    from app.services import tasks as task_service

    rows = _make_task(status="running", deadline=db.iso(db.utcnow() - timedelta(seconds=1)))
    fake_status["script"]["payload"] = {"ok": True, "status": "running", "upstream_status": "processing", "progress": 60}

    outcome = asyncio.run(task_service.poll_task(rows["task_id"]))

    assert outcome["status"] == "uncertain"
    assert outcome["error"]["code"] == "task_timeout"
    assert outcome["progress"] == 60
    assert _invocation(rows["invocation_id"])["status"] == "uncertain"


def test_success_records_result_and_finishes_the_invocation(isolated, fake_status):
    from app.services import tasks as task_service

    rows = _make_task(status="running")
    fake_status["script"]["payload"] = {
        "ok": True,
        "status": "succeeded",
        "upstream_status": "completed",
        "progress": 100,
        "result": {"output": "the rendered clip"},
    }

    outcome = asyncio.run(task_service.poll_task(rows["task_id"]))

    assert outcome["status"] == "succeeded"
    assert outcome["progress"] == 100
    assert outcome["result"] == {"output": "the rendered clip"}
    assert outcome["result_document"]["upstream_status"] == "completed"
    assert outcome["finished_at"] is not None
    invocation = _invocation(rows["invocation_id"])
    assert invocation["status"] == "succeeded"
    assert invocation["finished_at"] is not None
    assert "rendered clip" in (invocation["response_preview"] or "")


def test_permanent_status_config_error_fails_the_task(isolated, fake_status, monkeypatch):
    """A broken async.status config is a real failure, not a retry loop."""
    from app.core.errors import unprocessable
    from app.services import tasks as task_service

    rows = _make_task(status="running")

    async def _bad(**kwargs: Any) -> dict[str, Any]:
        raise unprocessable("missing_status_url", "This capability has no async status URL.")

    from app.services import execution

    monkeypatch.setattr(execution, "fetch_status", _bad)

    outcome = asyncio.run(task_service.poll_task(rows["task_id"]))

    assert outcome["status"] == "failed"
    assert outcome["error"]["code"] == "missing_status_url"


# ---------------------------------------------------------------- the worker -
def test_worker_run_once_processes_a_due_task_exactly_once(isolated, fake_status):
    from app.core import db
    from app.services import worker

    rows = _make_task(status="running", poll_after=db.iso(db.utcnow() - timedelta(seconds=10)))
    fake_status["script"]["payload"] = {"ok": True, "status": "running", "upstream_status": "processing", "progress": 30}

    summary = asyncio.run(worker.run_once(owner="worker:test:1", concurrency=2, recover=False))

    assert summary["tasks_processed"] == 1
    assert len(fake_status["calls"]) == 1
    stored = _task(rows["task_id"])
    assert stored["attempts"] == 1
    assert stored["lease_owner"] is None, "the lease must be released after the poll"
    assert stored["poll_after"] > db.iso(), "the next poll is scheduled in the future"

    # Immediately running again must not re-poll: poll_after is still ahead.
    again = asyncio.run(worker.run_once(owner="worker:test:1", concurrency=2, recover=False))
    assert again["tasks_processed"] == 0
    assert len(fake_status["calls"]) == 1


def test_worker_marks_unknown_job_kinds_failed_without_retrying(isolated):
    from app.core import db
    from app.services import tasks as task_service
    from app.services import worker

    job = asyncio.run(task_service.enqueue_job("totally.unknown", {"x": 1}))
    summary = asyncio.run(worker.run_once(owner="worker:test:2", concurrency=1, recover=False))

    assert summary["jobs_processed"] == 0
    assert summary["errors"], "the unknown kind must surface as an error"
    stored = db.query_one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
    assert stored["status"] == "failed"
    assert "Unknown job kind" in (stored["last_error"] or "")
    assert stored["run_at"] <= db.iso(), "a failed job must not be rescheduled"


def test_worker_processes_a_task_poll_job(isolated, fake_status):
    from app.core import db
    from app.services import tasks as task_service
    from app.services import worker

    rows = _make_task(
        status="running",
        poll_after=db.iso(db.utcnow() + timedelta(hours=1)),  # not due by itself
    )
    asyncio.run(task_service.enqueue_job("task.poll", {"task_id": rows["task_id"]}, priority=1))
    fake_status["script"]["payload"] = {"ok": True, "status": "running", "upstream_status": "processing", "progress": 20}

    summary = asyncio.run(worker.run_once(owner="worker:test:3", concurrency=2, recover=False))

    assert summary["jobs_processed"] == 1
    assert len(fake_status["calls"]) == 1
    job = db.query_one("SELECT status FROM jobs")
    assert job["status"] == "succeeded"


# ------------------------------------------------------------- queue depth ---
def test_queue_depth_counts_active_tasks_and_jobs(isolated):
    from app.services import tasks as task_service

    _make_task(status="running")
    _make_task(status="queued")
    _make_task(status="succeeded")
    asyncio.run(task_service.enqueue_job("task.poll", {"task_id": "x"}))
    asyncio.run(task_service.claim_jobs("worker:depth", limit=1))

    depth = asyncio.run(task_service.queue_depth())
    assert depth == {"tasks_active": 2, "jobs_queued": 0, "jobs_running": 1}


# ------------------------------------------------------- public serialisation -
def test_task_serialisation_hides_leases_and_includes_timeline(isolated, fake_status):
    from app.services import files as file_service
    from app.services import tasks as task_service

    rows = _make_task(status="running")
    file_service.save_bytes(
        b"%PDF-1.4 test",
        filename="render.pdf",
        content_type="application/pdf",
        purpose="artifact",
        task_id=rows["task_id"],
        invocation_id=rows["invocation_id"],
    )
    fake_status["script"]["payload"] = {"ok": True, "status": "succeeded", "upstream_status": "completed", "progress": 100, "result": {"output": "done"}}
    asyncio.run(task_service.poll_task(rows["task_id"]))

    detail = asyncio.run(task_service.get_task(rows["task_id"]))

    assert detail["status"] == "succeeded"
    assert detail["capability"]["slug"].startswith("video.render")
    assert "lease_owner" not in detail and "lease_until" not in detail
    assert [artifact["filename"] for artifact in detail["artifacts"]] == ["render.pdf"]
    assert "storage_path" not in detail["artifacts"][0]
    assert detail["events"], "the detail payload carries the timeline"
    assert detail["events"][0]["type"] == "task.created"
    assert any(event["type"] == "task.succeeded" for event in detail["events"])

    events = asyncio.run(task_service.task_events(rows["task_id"]))
    assert events == detail["events"]

    items, total = asyncio.run(task_service.list_tasks(status="succeeded"))
    assert total == 1 and items[0]["id"] == rows["task_id"]
