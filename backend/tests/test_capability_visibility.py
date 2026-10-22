"""The empty capability list must be actionable, and drafts must be diagnosable.

Two states were indistinguishable and cost a user real time:

  * a token asking `/api/v1/capabilities` got `[]` with no explanation — it could not tell
    "the platform has nothing published" from "nothing in my scope", so the agent guessed
    and told the user to go authorise something that was never publishable;
  * the platform's own Overview said only "nothing is published", without pointing at the
    draft that the user had just added and which was missing a URL and a result path.

These tests pin the wording and the diagnosis. They are engine-level: no HTTP, only the
functions the endpoints call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from app.core import config, db
    from app.security import crypto
    from app.services import bootstrap

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    config.reset_settings_cache()
    settings = config.get_settings()
    settings.ensure_dirs()
    db.init_db()
    crypto.create_keyring(settings.keyring_path, overwrite=True)
    bootstrap.ensure_bootstrap()
    yield db
    config.reset_settings_cache()


def _capability(db, *, suffix: str, publish: bool) -> tuple[str, str]:
    """A capability with either an empty draft config or a complete published one."""
    from app.services.capability_config import default_config, validate_config

    now = db.iso()
    cap_id = db.new_id("cap")
    version_id = db.new_id("capver")
    config = default_config("generic")
    if publish:
        config["request"]["url"] = "https://postman-echo.com/post"
        config["request"]["body_template"] = {"prompt": "{{ input.prompt }}"}
        config["input_schema"] = {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
            "additionalProperties": False,
        }
        config["response"]["extract"]["result"] = "$.json.prompt"
        config["examples"] = [{"input": {"prompt": "hi"}}]
        assert not [i for i in validate_config(config, capability_type="generic") if i["severity"] == "error"]

    db.insert(
        "capabilities",
        {
            "id": cap_id,
            "slug": f"probe-{suffix}",
            "name": f"probe {suffix}",
            "type": "generic",
            "description": "",
            "status": "published" if publish else "draft",
            "current_version_id": version_id,
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
            "status": "published" if publish else "draft",
            "config": db.dumps(config),
            "checksum": "",
            "created_at": now,
            "published_at": now if publish else None,
        },
    )
    if publish:
        db.insert(
            "capability_tests",
            {
                "id": db.new_id("captest"),
                "capability_id": cap_id,
                "version_id": version_id,
                "kind": "sample_call",
                "status": "passed",
                "request": "{}",
                "response": "{}",
                "created_at": now,
            },
        )
    return cap_id, version_id


def test_empty_capability_list_carries_an_actionable_hint(isolated, monkeypatch):
    """`[]` alone is not an answer; the caller needs the two possible causes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import external
    from app.core import db
    from app.core.errors import install_error_handlers
    from app.deps import AgentPrincipal, require_agent

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(external.router)
    app.dependency_overrides[require_agent] = lambda: AgentPrincipal(
        agent_id="agent_x", agent_name="probe", token_id="tok_x", scopes=["*"],
        budget_usd=None, rate_limit_per_min=None, max_concurrency=None,
    )
    client = TestClient(app)

    empty = client.get("/api/v1/capabilities")
    assert empty.status_code == 200
    body = empty.json()
    assert body["count"] == 0
    assert "empty_hint" in body, "an empty list must explain itself"
    hint = body["empty_hint"]
    assert "published" in hint and "scope" in hint
    assert "refresh=1" in json.dumps(body), "the caller is told how to bypass the ETag"

    # And it disappears as soon as something is actually available.
    _capability(db, suffix="one", publish=True)
    listed = client.get("/api/v1/capabilities")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert "empty_hint" not in listed.json()


def test_unpublished_capability_diagnosis_names_the_missing_fields(isolated):
    """The Overview must say *why* an added capability is invisible."""
    from app.api.admin_connect import _diagnose_capability
    from app.core import db

    cap_id, _ = _capability(db, suffix="empty", publish=False)
    row = db.query_one("SELECT id, slug, name, type, status, current_version_id FROM capabilities WHERE id = ?", (cap_id,))
    diagnosis = _diagnose_capability(row)

    assert diagnosis["status"] == "draft"
    assert any("still draft" in blocker for blocker in diagnosis["blockers"])
    assert any("incomplete" in blocker for blocker in diagnosis["blockers"])
    assert any("no passing test" in blocker for blocker in diagnosis["blockers"])
    fields = {error["field"] for error in diagnosis["validation_errors"]}
    assert "request.url" in fields, "the diagnosis must name the field to fill in"
    assert "response.extract.result" in fields
    assert diagnosis["next_step"].startswith("Fill the fields")


def test_diagnosis_of_a_complete_but_untested_capability_points_at_test(isolated):
    """With a valid config the only missing evidence is the test, and it says so."""
    from app.api.admin_connect import _diagnose_capability
    from app.core import db

    cap_id, version_id = _capability(db, suffix="ready", publish=True)
    # Simulate "configured correctly, never tested": drop the test and un-publish it.
    db.execute("DELETE FROM capability_tests WHERE version_id = ?", (version_id,))
    db.execute("UPDATE capability_versions SET status = 'draft', published_at = NULL WHERE id = ?", (version_id,))
    db.execute("UPDATE capabilities SET status = 'draft' WHERE id = ?", (cap_id,))

    row = db.query_one("SELECT id, slug, name, type, status, current_version_id FROM capabilities WHERE id = ?", (cap_id,))
    diagnosis = _diagnose_capability(row)
    assert not diagnosis["validation_errors"]
    assert any("no passing test" in blocker for blocker in diagnosis["blockers"])
    assert diagnosis["next_step"].startswith("Run Test")
