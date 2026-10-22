"""Recovery of agent runs stranded by a process restart.

A run executes inside the API process, so killing the process would otherwise
leave the row `running` forever and the agent panel spinning. These tests pin the
startup reconciliation that prevents it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from app.core import config, db

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    config.reset_settings_cache()
    config.get_settings().ensure_dirs()
    db.init_db()
    yield
    config.reset_settings_cache()


def _make_session_and_run(status: str) -> tuple[str, str]:
    from app.core import db

    now = db.iso()
    session_id = db.new_id("ses")
    run_id = db.new_id("run")
    db.insert(
        "agent_sessions",
        {"id": session_id, "title": "recovery", "status": "running", "created_at": now, "updated_at": now},
    )
    db.insert(
        "agent_runs",
        {
            "id": run_id,
            "session_id": session_id,
            "status": status,
            "prompt": "do something expensive",
            "created_at": now,
            "updated_at": now,
        },
    )
    return session_id, run_id


def test_orphaned_runs_are_failed_with_an_honest_reason(isolated):
    from app.core import db
    from app.services import agent_runtime

    _, running_run = _make_session_and_run("running")
    queued_session, queued_run = _make_session_and_run("queued")

    outcome = agent_runtime.reconcile_orphaned_runs()
    assert outcome["reconciled"] == 2
    assert set(outcome["run_ids"]) == {running_run, queued_run}

    for run_id in (running_run, queued_run):
        row = db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        assert row["status"] == "failed"
        assert row["stop_reason"] == "process_restart"
        assert row["finished_at"]
        assert "may have completed" in row["error"], "the message must admit upstream side effects"

    session = db.query_one("SELECT status FROM agent_sessions WHERE id = ?", (queued_session,))
    assert session["status"] == "idle"

    audit_row = db.query_one(
        "SELECT * FROM audit_logs WHERE action = 'agent.run_reconciled' AND object_id = ?", (running_run,)
    )
    assert audit_row is not None


def test_reconciliation_is_idempotent_and_leaves_finished_runs_alone(isolated):
    from app.core import db
    from app.services import agent_runtime

    _, run_id = _make_session_and_run("running")
    first = agent_runtime.reconcile_orphaned_runs()
    second = agent_runtime.reconcile_orphaned_runs()
    assert first["reconciled"] == 1
    assert second["reconciled"] == 0, "a second boot must not touch already-resolved runs"

    db.execute(
        "INSERT INTO agent_runs (id, session_id, status, prompt, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("run_finished", db.query_scalar("SELECT session_id FROM agent_runs WHERE id = ?", (run_id,)), "succeeded", "x", db.iso(), db.iso()),
    )
    agent_runtime.reconcile_orphaned_runs()
    row = db.query_one("SELECT status FROM agent_runs WHERE id = 'run_finished'")
    assert row["status"] == "succeeded"


def test_execution_still_does_not_import_the_agent_adapter(isolated):
    """Published capabilities must survive an agent/engine failure."""
    import importlib

    os.environ.pop("SR_ARAGON_SIDECAR_URL", None)
    importlib.reload(importlib.import_module("app.services.execution"))
    assert "app.services.agent_adapter" not in sys.modules
