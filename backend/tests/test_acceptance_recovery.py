"""Acceptance: recovery and durability (CONTRACT §4, §5.6, §5.10, §12).

Scope note: **engine-level** proofs. Everything runs against a throw-away SQLite
file and a fresh keyring; the only simulated things are the two genuine
externals:

  * the provider status probe (`execution.fetch_status`), so a "provider" can be
    scripted to complete a job;
  * the sidecar (`engine_upgrade.new_adapter` + the git/npm/tsc build seam), so
    an engine activation can be made to fail on purpose.

What is *not* simulated: the task state machine, the SQLite schema and its
migrations, the operator CLI (invoked as a real subprocess) and the guarded
outbound HTTP layer used by the capability call.

Covered:
  * a restarted worker reclaims a task whose lease expired, exactly once;
  * `python -m app.cli backup` / `restore --force` round-trip, including refusal
    of a corrupt backup;
  * a failed engine activation keeps the previous release live, a later health
    failure rolls the pointer back, and a published capability keeps answering
    through the whole flow (the agent engine is not on that path);
  * migrations are idempotent and a fresh database gets every table.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

RAW_SECRET = "leakcanary-0f9a8b7c6d5e4f3a"


@pytest.fixture(autouse=True)
def _unload_engine_adapter():
    """Same guard as test_engine_upgrade.py: `test_execution.py` asserts that a
    plain capability call never imports the AragonAgent adapter, so this suite
    must restore the import graph on the way out."""
    yield
    sys.modules.pop("app.services.agent_adapter", None)
    services_package = sys.modules.get("app.services")
    if services_package is not None and hasattr(services_package, "agent_adapter"):
        delattr(services_package, "agent_adapter")


# ---------------------------------------------------------------- fixtures ---
@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.setenv("SR_INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SR_INITIAL_ADMIN_PASSWORD", "adminjr")
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


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responder: Callable[..., Any] | None = None

    def respond(self, responder: Callable[..., Any]) -> None:
        self.responder = responder

    async def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        assert self.responder is not None, "no responder configured"
        result = self.responder(method, url, kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


def json_response(body: Any, *, status_code: int = 200, url: str = "https://api.example.com/v1/chat") -> Any:
    from app.services.network import GuardedResponse

    return GuardedResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
        url=url,
        elapsed_ms=5,
        redirects=[],
    )


@pytest.fixture()
def upstream(isolated, monkeypatch):
    from app.services import execution, network

    fake = FakeUpstream()
    monkeypatch.setattr(network, "guarded_request", fake)
    monkeypatch.setattr(execution, "guarded_request", fake)
    return fake


@pytest.fixture()
def client(isolated):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# --------------------------------------------------------------- task seeding --
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
        "cancel": {"url": "", "method": "POST", "extract": {"status": ""}},
    }
    config["limits"] = {"timeout_s": 30, "max_duration_s": 300}
    return config


def _make_running_task(
    *,
    lease_owner: str | None = "worker:dead-host:4242",
    lease_until: str | None = None,
    poll_after: str | None = None,
) -> dict[str, Any]:
    """A capability + published async version + an in-flight task row."""
    from app.core import db

    now = db.iso()
    capability_id = db.new_id("cap")
    version_id = db.new_id("cver")
    task_id = db.new_id("task")
    db.insert(
        "capabilities",
        {
            "id": capability_id,
            "slug": f"video.render.{task_id[-6:]}",
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
            "capability_id": capability_id,
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
    request_id = db.new_id("req")
    db.insert(
        "tasks",
        {
            "id": task_id,
            "request_id": request_id,
            "kind": "capability_async",
            "capability_id": capability_id,
            "version_id": version_id,
            "provider_id": None,
            "route_id": None,
            "agent_id": None,
            "upstream_task_id": "job_restart",
            "status": "running",
            "progress": 10,
            "progress_note": None,
            "input": db.dumps({"prompt": "a fox"}),
            "result": "{}",
            "error": None,
            "idempotency_key": None,
            "fingerprint": None,
            "attempts": 0,
            "max_attempts": 3,
            "budget_usd": None,
            "cost_actual_usd": None,
            "poll_after": poll_after or db.iso(db.utcnow() - timedelta(seconds=30)),
            "deadline": db.iso(db.utcnow() + timedelta(minutes=10)),
            "lease_owner": lease_owner,
            "lease_until": lease_until or db.iso(db.utcnow() - timedelta(minutes=5)),
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
            "request_id": request_id,
            "caller_type": "agent",
            "agent_id": None,
            "surface": "http",
            "capability_id": capability_id,
            "version_id": version_id,
            "provider_id": None,
            "route_id": None,
            "route_reason": "recovery test",
            "mode": "async",
            "status": "queued",
            "http_status": 202,
            "latency_ms": 10,
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
    return {"task_id": task_id, "capability_id": capability_id, "invocation_id": invocation_id}


@pytest.fixture()
def fake_status(monkeypatch):
    from app.services import execution

    calls: list[dict[str, Any]] = []
    script: dict[str, Any] = {"payload": {"ok": True, "status": "running", "progress": 40}}

    async def _fetch_status(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        item = script["payload"]
        if isinstance(item, BaseException):
            raise item
        return item(kwargs) if callable(item) else item

    monkeypatch.setattr(execution, "fetch_status", _fetch_status)
    return {"calls": calls, "script": script}


# ------------------------------------------------------- 1. service restart ---
def test_restarted_worker_reclaims_the_expired_lease_exactly_once(isolated, fake_status):
    """A worker that died holding a lease must not strand (or duplicate) a task."""
    from app.core import db
    from app.services import tasks as task_service
    from app.services import worker

    rows = _make_running_task()
    fake_status["script"]["payload"] = {
        "ok": True,
        "status": "succeeded",
        "upstream_status": "completed",
        "progress": 100,
        "result": {"output": "the rendered clip"},
    }

    # The old worker is gone; its lease has expired and the claim function hands
    # the row to the restarted worker ...
    claimed = asyncio.run(task_service.claim_due_tasks("worker:restarted:5151", limit=5))
    assert [row["id"] for row in claimed] == [rows["task_id"]]
    assert db.query_scalar("SELECT lease_owner FROM tasks WHERE id = ?", (rows["task_id"],)) == "worker:restarted:5151"
    # ... and to nobody else while that lease is live (no double execution).
    assert asyncio.run(task_service.claim_due_tasks("worker:intruder:6161", limit=5)) == []
    assert fake_status["calls"] == []

    # The restarted worker hands the row back on graceful shutdown, so the next
    # worker runs the full claim -> poll -> finish cycle exactly once.
    asyncio.run(task_service.release_task_lease(rows["task_id"], "worker:restarted:5151"))
    summary = asyncio.run(worker.run_once(owner="worker:restarted:5151", concurrency=2, recover=False))
    assert summary["tasks_processed"] == 1
    assert len(fake_status["calls"]) == 1, "the reclaimed task must be polled exactly once"

    task = db.query_one("SELECT * FROM tasks WHERE id = ?", (rows["task_id"],))
    assert task["status"] == "succeeded"
    assert task["attempts"] == 1, "a reclaimed task must not be executed twice"
    assert task["lease_owner"] is None, "the lease must be released when the poll finishes"
    assert task["finished_at"] is not None

    invocation = db.query_one("SELECT * FROM invocations WHERE id = ?", (rows["invocation_id"],))
    assert invocation["status"] == "succeeded"

    # A second worker pass finds nothing due: no double execution.
    again = asyncio.run(worker.run_once(owner="worker:second:6161", concurrency=2, recover=False))
    assert again["tasks_processed"] == 0
    assert len(fake_status["calls"]) == 1


# ------------------------------------------------------- 2. backup / restore ---
def _run_cli(*args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides or {})
    env.setdefault("SR_ENVIRONMENT", "test")
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def test_backup_and_restore_roundtrip(isolated, tmp_path):
    from app.core import db
    from app.services import credentials

    provider_id = db.new_id("prov")
    now = db.iso()
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": "Before backup",
            "slug": "before-backup",
            "category": "llm",
            "base_url": None,
            "docs_url": None,
            "tags": "[]",
            "environment": "production",
            "notes": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )
    credential = credentials.create_credential(
        provider_id=provider_id, name="Backed-up key", auth_type="api_key", secrets={"api_key": RAW_SECRET}
    )

    out_dir = tmp_path / "backups"
    backup = _run_cli("backup", "--out", str(out_dir))
    assert backup.returncode == 0, backup.stdout + backup.stderr
    payload = json.loads(backup.stdout)
    backup_file = Path(payload["backup"])
    assert backup_file.exists() and backup_file.stat().st_size > 0
    assert Path(payload["manifest"]).exists()
    assert _integrity(backup_file) == "ok"

    # Mutate the live database after the backup was taken.
    db.execute("UPDATE providers SET name = 'Changed after backup' WHERE id = ?", (provider_id,))
    db.execute(
        "INSERT INTO providers (id, name, slug, category, tags, environment, status, created_at, updated_at) "
        "VALUES (?, 'Added after backup', 'added-after-backup', 'llm', '[]', 'production', 'active', ?, ?)",
        (db.new_id("prov"), db.iso(), db.iso()),
    )

    # Restore into a *second* database path (never the live one).
    restored_path = tmp_path / "restored" / "skillrouter.db"
    refused = _run_cli(
        "restore",
        str(backup_file),
        env_overrides={"SR_DATABASE_PATH": str(restored_path)},
    )
    assert refused.returncode != 0, "restore without --force must refuse"
    assert "force" in (refused.stdout + refused.stderr).lower()

    restored = _run_cli(
        "restore",
        str(backup_file),
        "--force",
        env_overrides={"SR_DATABASE_PATH": str(restored_path)},
    )
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert restored_path.exists()
    assert _integrity(restored_path) == "ok"

    restored_connection = sqlite3.connect(f"file:{restored_path}?mode=ro", uri=True)
    try:
        names = {row[0] for row in restored_connection.execute("SELECT name FROM providers")}
        rows = restored_connection.execute(
            "SELECT name FROM providers WHERE id = ?", (provider_id,)
        ).fetchone()
        credential_row = restored_connection.execute(
            "SELECT id FROM credentials WHERE id = ?", (credential["id"],)
        ).fetchone()
    finally:
        restored_connection.close()

    assert rows is not None and rows[0] == "Before backup", "the restored database has the pre-backup value"
    assert "Added after backup" not in names, "rows created after the backup must not be in the restore"
    assert credential_row is not None, "the credential row came back with the backup"

    # The live database is untouched by a restore into another path.
    assert db.query_scalar("SELECT name FROM providers WHERE id = ?", (provider_id,)) == "Changed after backup"


def test_restore_refuses_a_corrupted_backup(isolated, tmp_path):
    corrupted = tmp_path / "corrupted.db"
    corrupted.write_bytes(b"this is not a sqlite database" * 64)

    target = tmp_path / "target" / "skillrouter.db"
    result = _run_cli(
        "restore",
        str(corrupted),
        "--force",
        env_overrides={"SR_DATABASE_PATH": str(target)},
    )

    assert result.returncode != 0, "a corrupt backup must not be restored"
    combined = (result.stdout + result.stderr).lower()
    assert "integrity" in combined or "not look like" in combined or "database" in combined
    assert not target.exists(), "a refused restore must not create or overwrite the target database"


def test_restore_refuses_a_foreign_database(isolated, tmp_path):
    """A structurally valid SQLite file that is not a Skill Router database."""
    foreign = tmp_path / "foreign.db"
    connection = sqlite3.connect(foreign)
    try:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.commit()
    finally:
        connection.close()

    target = tmp_path / "target2" / "skillrouter.db"
    result = _run_cli("restore", str(foreign), "--force", env_overrides={"SR_DATABASE_PATH": str(target)})
    assert result.returncode != 0
    assert not target.exists()


# ------------------------------------------------------- 3. engine upgrade ----
class FakeAdapter:
    """Stand-in for AgentAdapter; only the calls engine_upgrade makes."""

    health: dict[str, Any] = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    releases: dict[str, Any] = {"ok": True}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def check_health(self) -> dict[str, Any]:
        return dict(FakeAdapter.health)

    async def check_releases(self, *, channel: str | None = None) -> dict[str, Any]:
        return dict(FakeAdapter.releases)


@pytest.fixture()
def fake_adapter(monkeypatch):
    from app.services import engine_upgrade

    FakeAdapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    FakeAdapter.releases = {"ok": True}
    monkeypatch.setattr(engine_upgrade, "new_adapter", lambda **kwargs: FakeAdapter(**kwargs))
    return FakeAdapter


@pytest.fixture()
def fast_checks(monkeypatch):
    """Every outbound validation check becomes a scripted verdict."""
    from app.services import engine_upgrade

    async def fake_npm(version: str | None) -> dict[str, Any]:
        return {"name": "npm_available", "status": "passed", "detail": "published"}

    async def fake_build(version: str | None):
        return (
            {"name": "isolated_build", "status": "passed", "detail": "built"},
            {"name": "regression_tests", "status": "passed", "detail": "green"},
        )

    async def fake_sidecar(version: str | None) -> dict[str, Any]:
        return {"name": "sidecar_reachable", "status": "passed", "detail": "reachable"}

    monkeypatch.setattr(engine_upgrade, "_check_npm_available", fake_npm)
    monkeypatch.setattr(engine_upgrade, "_check_sidecar_reports", fake_sidecar)
    monkeypatch.setattr(engine_upgrade, "_build_runner", fake_build)


def _insert_release(*, status: str, version: str, commit_char: str) -> str:
    from app.core import db

    release_id = db.new_id("rel")
    db.insert(
        "engine_releases",
        {
            "id": release_id,
            "channel": "stable",
            "ref": f"v{version}",
            "version": version,
            "commit_sha": commit_char * 40,
            "npm_version": version,
            "status": status,
            "adapter_version": "1.0.0",
            "dependency_fingerprint": "{}",
            "capabilities": "{}",
            "checks": "[]",
            "created_at": db.iso(),
            "activated_at": db.iso() if status == "active" else None,
        },
    )
    if status == "active":
        # A real deployment records the active pointer when a release is activated.
        db.set_setting(
            "engine.active_release",
            {
                "id": release_id,
                "ref": f"v{version}",
                "version": version,
                "commit": commit_char * 40,
                "activated_at": db.iso(),
                "adapter_version": "1.0.0",
            },
        )
    return release_id


def _release_status(release_id: str) -> str:
    from app.core import db

    return db.query_scalar("SELECT status FROM engine_releases WHERE id = ?", (release_id,))


def _active_pointer() -> str | None:
    from app.core import db

    value = db.get_setting("engine.active_release", None)
    return (value or {}).get("id") if isinstance(value, dict) else None


def _seed_published_capability() -> dict[str, str]:
    """A plain published capability + an agent token granted to it."""
    from app.core import db
    from app.services import credentials
    from app.security import tokens as token_security

    now = db.iso()
    provider_id = db.new_id("prov")
    suffix = provider_id[-8:]
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": "Engine-independent AI",
            "slug": f"engine-independent-{suffix}",
            "category": "llm",
            "base_url": "https://api.example.com",
            "docs_url": None,
            "tags": "[]",
            "environment": "production",
            "notes": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )
    credential = credentials.create_credential(
        provider_id=provider_id, name="Engine-independent key", auth_type="api_key", secrets={"api_key": RAW_SECRET}
    )

    from app.services import capability_config

    config = capability_config.default_config("text")
    config["auth"] = {"mode": "credential"}
    config["modes"] = {"sync": True, "async": False, "stream": False}
    config["request"]["url"] = "https://api.example.com/v1/chat"
    config["request"]["body_template"] = {"input": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["response"] = {"kind": "json", "extract": {"result": "$.answer"}, "artifact": None}
    config["retry"] = {"max_attempts": 0, "backoff_ms": [10], "retry_on": []}

    capability_id = db.new_id("cap")
    version_id = db.new_id("cver")
    db.insert(
        "capabilities",
        {
            "id": capability_id,
            "slug": "text.survives_upgrade",
            "name": "Survives upgrade",
            "type": "text",
            "description": "published capability used while the engine is broken",
            "provider_id": provider_id,
            "credential_id": credential["id"],
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
            "capability_id": capability_id,
            "version": 1,
            "status": "published",
            "config": db.dumps(config),
            "notes": "",
            "checksum": "",
            "created_by": None,
            "created_at": now,
            "published_at": now,
        },
    )
    db.insert(
        "routes",
        {
            "id": db.new_id("route"),
            "capability_id": capability_id,
            "capability_version_id": version_id,
            "provider_id": provider_id,
            "credential_id": credential["id"],
            "strategy": "priority",
            "priority": 10,
            "weight": 1,
            "enabled": 1,
            "health_status": "healthy",
            "last_health_at": now,
            "conditions": "{}",
            "created_at": now,
            "updated_at": now,
        },
    )
    agent_id = db.new_id("agent")
    db.insert(
        "agents",
        {
            "id": agent_id,
            "name": "engine test agent",
            "slug": f"engine-test-agent-{agent_id[-8:]}",
            "description": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        },
    )
    issued = token_security.issue_agent_token()
    db.insert(
        "agent_tokens",
        {
            "id": db.new_id("tok"),
            "agent_id": agent_id,
            "name": "engine test token",
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(["text.survives_upgrade"]),
            "expires_at": None,
            "rate_limit_per_min": None,
            "max_concurrency": None,
            "budget_usd": None,
            "status": "active",
            "created_at": now,
            "revoked_at": None,
            "last_used_at": None,
        },
    )

    return {"slug": "text.survives_upgrade", "token": issued.token}


def _call_published(client, upstream, seeded: dict[str, str]) -> dict[str, Any]:
    """Call the plain published capability: the path an agent engine must never break."""
    upstream.respond(lambda *_: json_response({"answer": "still working"}))
    response = client.post(
        "/api/v1/invocations",
        headers={"authorization": f"Bearer {seeded['token']}"},
        json={"capability": seeded["slug"], "input": {"prompt": "ping"}},
    )
    return {"status_code": response.status_code, "body": response.json() if response.content else {}}


def test_failed_engine_activation_rolls_back_and_leaves_capability_calls_working(
    isolated, client, upstream, fake_adapter, fast_checks
):
    """An engine (agent) failure must never reach the plain capability path."""
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    seeded = _seed_published_capability()
    active = _insert_release(status="active", version="0.2.46", commit_char="a")
    candidate = _insert_release(status="detected", version="0.2.50", commit_char="b")
    validated = asyncio.run(engine_upgrade.validate_release(candidate))
    assert validated["status"] == "verified"

    call_during = _call_published(client, upstream, seeded)
    assert call_during["status_code"] == 200, call_during
    assert call_during["body"]["status"] == "succeeded"

    # The candidate's health check fails: activation is refused and the pointer
    # never leaves the previous release.
    fake_adapter.health = {"ok": False, "reachable": False, "error": {"code": "engine_unavailable"}}
    with pytest.raises(ApiError) as excinfo:
        asyncio.run(engine_upgrade.activate_release(candidate))
    assert excinfo.value.code == "engine_activation_refused"
    assert excinfo.value.status_code == 409
    assert excinfo.value.actions, "a refused activation must say what to do"
    assert _release_status(active) == "active"
    assert _release_status(candidate) == "failed"
    assert _active_pointer() == active, "the active pointer must not move on a failed health check"

    call_after_refusal = _call_published(client, upstream, seeded)
    assert call_after_refusal["status_code"] == 200, call_after_refusal
    assert call_after_refusal["body"]["status"] == "succeeded"

    # Now the candidate really gets activated (sidecar reports the new version) ...
    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    second_candidate = _insert_release(status="detected", version="0.2.50", commit_char="c")
    asyncio.run(engine_upgrade.validate_release(second_candidate))
    activated = asyncio.run(engine_upgrade.activate_release(second_candidate))
    assert activated["activated"] is True
    assert _active_pointer() == second_candidate

    # ... and then fails its health check: the rollback restores the previous
    # release and marks the candidate `rolled_back`.
    fake_adapter.health = {"ok": False, "reachable": False, "error": {"code": "engine_unavailable"}}
    rolled = asyncio.run(engine_upgrade.rollback())
    assert rolled["rolled_back"] is True
    assert rolled["from_release_id"] == second_candidate
    assert rolled["release_id"] == active, "the rollback restores the previously active release"
    assert _release_status(second_candidate) == "rolled_back"
    assert _release_status(active) == "active"
    assert _active_pointer() == active

    # The plain capability path still answers, before/during/after the whole flow.
    final_call = _call_published(client, upstream, seeded)
    assert final_call["status_code"] == 200, final_call
    assert final_call["body"]["result"] == "still working"


# --------------------------------------------------------- 4. migrations ------
EXPECTED_TABLES = {
    "schema_migrations",
    "settings",
    "users",
    "sessions",
    "login_attempts",
    "providers",
    "credentials",
    "capabilities",
    "capability_versions",
    "capability_tests",
    "routes",
    "invocations",
    "tasks",
    "jobs",
    "agents",
    "agent_tokens",
    "budget_policies",
    "skills",
    "skill_versions",
    "skill_imports",
    "files",
    "agent_sessions",
    "agent_messages",
    "agent_runs",
    "agent_events",
    "engine_releases",
    "audit_logs",
}


def test_a_fresh_database_gets_every_table(isolated, tmp_path):
    from app.core import db

    fresh = tmp_path / "fresh" / "skillrouter.db"
    applied = db.apply_migrations(fresh)
    assert applied, "a fresh database must apply the migrations"

    tables = _table_names(fresh)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after a fresh migration: {sorted(missing)}"
    assert _integrity(fresh) == "ok"


def test_applying_migrations_twice_is_a_no_op(isolated):
    from app.core import db

    first = db.apply_migrations()
    assert first == [], "the isolated fixture already migrated the database"
    rows_before = db.query_all("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    tables_before = {row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}

    second = db.apply_migrations()
    third = db.apply_migrations()
    assert second == [] and third == []
    rows_after = db.query_all("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    assert rows_after == rows_before, "re-applying migrations must not add or rewrite rows"
    assert {row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before


def test_init_db_is_idempotent_and_seeds_settings_once(isolated):
    from app.core import db

    db.init_db()
    settings_rows = db.query_all("SELECT key, value FROM settings ORDER BY key")
    db.init_db()
    db.init_db()

    assert db.query_all("SELECT key, value FROM settings ORDER BY key") == settings_rows
    assert db.query_scalar("SELECT COUNT(*) FROM settings") == len(settings_rows)
    assert db.query_scalar("SELECT COUNT(*) FROM schema_migrations") == 1
