"""Admin API contract tests for providers, credentials, capabilities and routing.

Isolated temp data dir + fresh keyring per test (same pattern as
test_foundation.py). The app under test mounts only the routers owned by
backend-capabilities plus the uniform error handlers and a middleware that
mirrors the CSRF guard in app/main.py.

The lifecycle asserted here is the one a reviewer will rerun by hand:

    provider → credential → capability (v1 draft) → validate → publish refused
    (no test evidence) → real test row → publish → callable = true

Nothing in these tests performs an outbound network call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ------------------------------------------------------------------ fixtures --
@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Temp data dir, fresh SQLite database and a fresh master keyring."""
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


def _build_app():
    """The four admin routers + uniform errors + the CSRF guard from main.py."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.api import admin_capabilities, admin_credentials, admin_providers, admin_routing
    from app.core.config import get_settings
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id
    from app.security import sessions as session_store

    settings = get_settings()
    app = FastAPI()
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(request.headers.get("x-request-id"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api"):
            token = request.cookies.get(settings.session_cookie_name)
            if token:
                session = session_store.get_session(token)
                header = request.headers.get("x-csrf-token")
                if session and not session_store.verify_csrf(session, header):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "code": "csrf_failed",
                                "message": "The page token is missing or expired. Reload and retry.",
                                "details": None,
                                "request_id": "",
                                "actions": ["Reload the page.", "Repeat the action."],
                            }
                        },
                    )
        return await call_next(request)

    for module in (admin_providers, admin_credentials, admin_capabilities, admin_routing):
        app.include_router(module.router)
    return app


@pytest.fixture()
def anon_client(isolated):
    from fastapi.testclient import TestClient

    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    return TestClient(_build_app())


@pytest.fixture()
def client(isolated):
    """A signed-in admin session with the CSRF header attached."""
    from fastapi.testclient import TestClient

    from app.core import db
    from app.security import sessions as session_store
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    # The password gate is covered by test_foundation.py; these tests are about
    # the business endpoints, so the admin is treated as having changed it.
    db.execute("UPDATE users SET must_change_password = 0 WHERE username = 'admin'")
    user = db.query_one("SELECT id FROM users WHERE username = 'admin'")
    assert user is not None
    issued = session_store.create_session(user["id"])

    settings = isolated
    test_client = TestClient(_build_app())
    test_client.cookies.set(settings.session_cookie_name, issued.token)
    test_client.cookies.set(settings.csrf_cookie_name, issued.csrf_token)
    test_client.headers.update({"x-csrf-token": issued.csrf_token})
    return test_client


# ------------------------------------------------------------------- helpers --
def _valid_generic_config() -> dict:
    from app.services.capability_config import default_config

    config = default_config("generic")
    config["request"]["url"] = "https://api.example.com/v1/run"
    config["request"]["body_template"] = {"prompt": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["response"]["extract"]["result"] = "$.output.text"
    config["examples"] = [{"input": {"prompt": "hello"}}]
    return config


def _create_provider(client, name="Example AI"):
    response = client.post(
        "/api/admin/providers",
        json={
            "name": name,
            "category": "llm",
            "base_url": "https://api.example.com",
            "environment": "production",
            "tags": ["test", "llm"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_credential(client, provider_id, name="Example key"):
    response = client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider_id,
            "name": name,
            "auth_type": "api_key",
            "secret": {"api_key": "sk-test-key-abcd1234"},
            "config": {"header_name": "x-api-key", "test_url": "https://api.example.com/v1/models"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_capability(client, provider_id, credential_id, name="Text Runner"):
    response = client.post(
        "/api/admin/capabilities",
        json={
            "name": name,
            "type": "generic",
            "description": "Runs a prompt against Example AI.",
            "provider_id": provider_id,
            "credential_id": credential_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_route(client, provider, credential, capability, version_id):
    response = client.post(
        f"/api/admin/capabilities/{capability['id']}/routes",
        json={
            "capability_version_id": version_id,
            "provider_id": provider["id"],
            "credential_id": credential["id"],
            "strategy": "priority",
            "priority": 10,
            "weight": 1,
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish(client, capability, version_id):
    response = client.post(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish")
    assert response.status_code == 200, response.text
    return response.json()


def _bucket(payload: dict, slug: str) -> dict:
    return next(item for item in payload["items"] if item["slug"] == slug)


# -------------------------------------------------------------------- tests ---
def test_anonymous_requests_get_401_with_error_envelope(anon_client):
    for method, path in (
        ("get", "/api/admin/providers"),
        ("get", "/api/admin/credentials"),
        ("get", "/api/admin/capabilities"),
        ("get", "/api/admin/routes"),
        ("post", "/api/admin/providers"),
    ):
        response = getattr(anon_client, method)(path) if method == "get" else getattr(anon_client, method)(path, json={})
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        body = response.json()
        assert body["error"]["code"] == "unauthenticated"
        assert body["error"]["request_id"].startswith("req_")
        assert body["error"]["actions"], "an error must tell the caller what to do"


def test_csrf_header_is_required_for_mutations(anon_client):
    from app.core import db
    from app.security import sessions as session_store
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    db.execute("UPDATE users SET must_change_password = 0")
    user = db.query_one("SELECT id FROM users WHERE username = 'admin'")
    issued = session_store.create_session(user["id"])
    from app.core.config import get_settings

    anon_client.cookies.set(get_settings().session_cookie_name, issued.token)
    response = anon_client.post("/api/admin/providers", json={"name": "No CSRF token"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_provider_validation_rejects_bad_category_and_url(client):
    bad_category = client.post("/api/admin/providers", json={"name": "Bad", "category": "quantum"})
    assert bad_category.status_code == 422
    assert bad_category.json()["error"]["code"] == "validation_failed"

    bad_url = client.post(
        "/api/admin/providers", json={"name": "Bad URL", "category": "llm", "base_url": "ftp://example.com"}
    )
    assert bad_url.status_code == 422
    assert bad_url.json()["error"]["code"] == "validation_failed"

    created = _create_provider(client)
    assert created["slug"] == "example-ai"
    assert created["counts"]["credentials"] == 0

    duplicate = client.post("/api/admin/providers", json={"name": "Example AI", "category": "llm"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "provider_exists"


def test_credential_response_never_contains_the_secret(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])

    rendered = str(credential)
    assert "sk-test-key-abcd1234" not in rendered
    assert "secret" not in credential, "the write-only secret must never be echoed back"
    assert credential["hint"] == "sk-…1234", credential["hint"]
    assert credential["hint"] != "sk-test-key-abcd1234"
    assert credential["field_names"] == ["api_key"]
    assert credential["status"] == "active"

    listing = client.get("/api/admin/credentials", params={"provider_id": provider["id"]}).json()
    assert listing["total"] == 1
    assert "sk-test-key-abcd1234" not in str(listing)

    detail = client.get(f"/api/admin/credentials/{credential['id']}").json()
    assert detail["impact"]["blocking"] is False
    assert "sk-test-key-abcd1234" not in str(detail)


def test_missing_secret_field_is_reported_with_actions(client):
    provider = _create_provider(client)
    response = client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "No secret",
            "auth_type": "bearer",
            "secret": {"api_key": "wrong-field"},
        },
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] in {"missing_secret_field", "validation_failed"}
    assert body["actions"]


def test_capability_lifecycle_publish_requires_test_evidence(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])

    # --- saved: v1 draft exists -------------------------------------------
    assert capability["status"] == "draft"
    assert capability["saved"] is True
    assert capability["current_version"] == 1
    assert capability["current_version_status"] == "draft"
    assert capability["published_version"] is None
    assert capability["last_test_status"] is None
    assert capability["callable"] is False
    assert capability["counts"]["versions"] == 1
    version_id = capability["current_version_id"]

    # --- slug collision gets a -2 suffix ----------------------------------
    second = _create_capability(client, provider["id"], credential["id"], name="Text Runner")
    assert second["slug"] == "text-runner-2"

    # --- validate: the default config has blocking issues ------------------
    validation = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/validate"
    ).json()
    assert validation["blocking"] is True
    assert validation["errors"] >= 1
    assert any(issue["field"] == "request.url" for issue in validation["issues"])

    # --- publish is refused twice over: validation, then missing evidence --
    too_early = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    )
    assert too_early.status_code == 422
    assert too_early.json()["error"]["code"] == "validation_failed"
    assert too_early.json()["error"]["details"]["issues"]

    patched = client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": _valid_generic_config(), "notes": "real endpoint"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["editable"] is True
    assert patched.json()["checksum"]
    assert patched.json()["config"]["request"]["url"] == "https://api.example.com/v1/run"

    clean = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/validate"
    ).json()
    assert clean["blocking"] is False, clean["issues"]

    no_evidence = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    )
    assert no_evidence.status_code == 409, no_evidence.text
    error = no_evidence.json()["error"]
    assert error["code"] == "test_evidence_missing"
    assert error["details"]["required_status"] == "passed"
    assert error["details"]["last_test_status"] == "none"
    assert any("test" in action.lower() for action in error["actions"])

    # --- a *failed* test row is not evidence either -----------------------
    from app.core import db

    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "failed",
            "request": "{}",
            "response": "{}",
            "error": "upstream_auth_failed",
            "actor": "test",
            "created_at": db.iso(),
        },
    )
    still_missing = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    )
    assert still_missing.status_code == 409
    assert still_missing.json()["error"]["details"]["last_test_status"] == "failed"

    # --- a passing real-call row unlocks publish --------------------------
    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed",
            "request": '{"method":"POST"}',
            "response": '{"status":200}',
            "latency_ms": 42,
            "http_status": 200,
            "actor": "test",
            "created_at": db.iso(),
        },
    )
    # A route is what turns "published" into "callable": without one the real
    # selector finds no candidate, so the API reports callable = false.
    _create_route(client, provider, credential, capability, version_id)
    published = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["current_version_id"] == version_id
    assert body["published_version"] == 1
    assert body["callable"] is True
    assert body["verification"]["test_passed"] is True
    assert body["evidence"]["status"] == "passed"

    # the four facts stay distinct
    detail = client.get(f"/api/admin/capabilities/{capability['id']}").json()
    assert detail["saved"] is True
    assert detail["last_test_status"] == "passed"
    assert detail["published_version"] == 1
    assert detail["callable"] is True
    assert detail["counts"]["tests"] == 2

    history = client.get(f"/api/admin/capabilities/{capability['id']}/tests").json()
    assert history["total"] == 2
    assert {item["status"] for item in history["items"]} == {"failed", "passed"}


def test_published_version_is_immutable(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": _valid_generic_config()},
    )
    from app.core import db

    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed",
            "request": "{}",
            "response": "{}",
            "created_at": db.iso(),
        },
    )
    assert client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    ).status_code == 200

    immutable = client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"notes": "should not be allowed"},
    )
    assert immutable.status_code == 409, immutable.text
    error = immutable.json()["error"]
    assert error["code"] == "version_immutable"
    assert error["details"]["status"] == "published"
    assert error["details"]["editable_statuses"] == ["draft", "testing"]
    assert error["actions"]

    # A published version cannot be republished either.
    again = client.post(f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_published"

    # Branching forward is the supported route, and the diff is explainable.
    created = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions",
        json={"from_version_id": version_id, "notes": "tweak"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 2
    assert created.json()["status"] == "draft"
    new_version_id = created.json()["id"]

    changed = _valid_generic_config()
    changed["request"]["url"] = "https://api.example.com/v2/run"
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{new_version_id}",
        json={"config": changed},
    )
    diff = client.get(
        f"/api/admin/capabilities/{capability['id']}/versions/diff",
        params={"from": version_id, "to": new_version_id},
    )
    assert diff.status_code == 200, diff.text
    payload = diff.json()
    assert payload["identical"] is False
    assert any(
        change["path"] == "$.request.url" and change["kind"] == "changed" for change in payload["changes"]
    )

    identical = client.get(
        f"/api/admin/capabilities/{capability['id']}/versions/diff",
        params={"from": version_id, "to": version_id},
    )
    assert identical.status_code == 422


def test_provider_delete_refuses_when_in_use(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    _create_capability(client, provider["id"], credential["id"])

    response = client.delete(f"/api/admin/providers/{provider['id']}")
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "provider_in_use"
    assert error["details"]["counts"]["credentials"] == 1
    assert error["details"]["counts"]["capabilities"] == 1
    assert len(error["actions"]) >= 2

    # The provider is still there.
    assert client.get(f"/api/admin/providers/{provider['id']}").status_code == 200


def test_credential_force_delete_detaches_capabilities(client):
    from app.core import db

    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": _valid_generic_config()},
    )
    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed",
            "request": "{}",
            "response": "{}",
            "created_at": db.iso(),
        },
    )
    assert client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    ).status_code == 200

    impact = client.get(f"/api/admin/credentials/{credential['id']}/impact").json()
    assert impact["capabilities"][0]["id"] == capability["id"]
    assert impact["blocking"] is True

    blocked = client.delete(f"/api/admin/credentials/{credential['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "credential_referenced"

    forced = client.delete(f"/api/admin/credentials/{credential['id']}", params={"force": "true"})
    assert forced.status_code == 200, forced.text
    assert forced.json()["deleted"] is True

    after = client.get(f"/api/admin/capabilities/{capability['id']}").json()
    assert after["credential_id"] is None
    assert after["callable"] is False
    assert "no active credential" in after["verification"]["callable_reason"]

    assert client.get(f"/api/admin/credentials/{credential['id']}").status_code == 404


def test_routing_endpoints_and_route_preview(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]

    route = client.post(
        f"/api/admin/capabilities/{capability['id']}/routes",
        json={
            "capability_version_id": version_id,
            "provider_id": provider["id"],
            "credential_id": credential["id"],
            "strategy": "priority",
            "priority": 10,
            "weight": 2,
            "enabled": True,
        },
    )
    assert route.status_code == 201, route.text
    route_body = route.json()
    assert route_body["provider_name"] == "Example AI"
    assert route_body["credential_name"] == "Example key"
    assert route_body["health_status"] == "unknown"
    route_id = route_body["id"]

    listing = client.get(f"/api/admin/capabilities/{capability['id']}/routes").json()
    assert listing["total"] == 1

    updated = client.patch(f"/api/admin/routes/{route_id}", json={"priority": 5, "enabled": False})
    assert updated.status_code == 200
    assert updated.json()["priority"] == 5
    assert updated.json()["enabled"] is False

    preview = client.post(
        f"/api/admin/capabilities/{capability['id']}/route-preview",
        json={"input": {"prompt": "hi"}, "mode": "sync"},
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["network_call"] is False
    assert preview_body["reason"]
    assert preview_body["capability_slug"] == capability["slug"]

    # A version of another capability is refused instead of silently accepted.
    other = _create_capability(client, provider["id"], credential["id"], name="Other Runner")
    mismatch = client.post(
        f"/api/admin/capabilities/{other['id']}/routes",
        json={"capability_version_id": version_id, "provider_id": provider["id"]},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "version_capability_mismatch"

    removed = client.delete(f"/api/admin/routes/{route_id}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert client.get("/api/admin/routes").json()["total"] == 0


def test_health_check_reports_without_fabricating_success(client):
    """The probe is real: a URL the outbound policy blocks is recorded as unhealthy."""
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]

    config = _valid_generic_config()
    config["request"]["url"] = "http://127.0.0.1:9/v1/run"  # loopback: blocked by policy
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": config},
    )
    route = client.post(
        f"/api/admin/capabilities/{capability['id']}/routes",
        json={"capability_version_id": version_id, "provider_id": provider["id"], "credential_id": credential["id"]},
    ).json()

    probe = client.post(f"/api/admin/routes/{route['id']}/health-check", json={"timeout_s": 5})
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["status"] == "unhealthy"
    assert body["probe_persisted"] is True
    assert body.get("error_code"), "a blocked probe must report why it could not run"
    assert "refused" in body["message"].lower()

    refreshed = client.get("/api/admin/routes", params={"capability_id": capability["id"]}).json()
    assert refreshed["items"][0]["health_status"] == "unhealthy"
    assert refreshed["items"][0]["last_health_at"]


def test_credential_test_call_is_honest_about_dry_run(client):
    from app.core import db

    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]

    # --- never tested: no claim of any kind -------------------------------
    untested = client.post(
        f"/api/admin/credentials/{credential['id']}/test-call",
        json={"capability_id": capability["id"], "input": {"prompt": "hello"}},
    )
    assert untested.status_code == 200, untested.text
    body = untested.json()
    assert body["real_call_performed"] is False
    assert "dry_run" not in body, "an untested capability cannot produce a dry-run claim"
    assert "no call was made" in body["message"].lower()
    assert body["next_steps"]
    assert "sk-test-key-abcd1234" not in str(body)

    # --- tested: a dry run proves the request builds, nothing is sent -----
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": _valid_generic_config()},
    )
    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed",
            "request": "{}",
            "response": "{}",
            "created_at": db.iso(),
        },
    )
    assert client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    ).status_code == 200
    _create_route(client, provider, credential, capability, version_id)

    tested = client.post(
        f"/api/admin/credentials/{credential['id']}/test-call",
        json={"capability_id": capability["id"], "input": {"prompt": "hello"}},
    )
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["real_call_performed"] is False
    assert body["last_capability_test"]["status"] == "passed"
    assert "dry run" in body["message"].lower()
    assert body["dry_run"]["ok"] is True
    assert body["dry_run"]["request"]["url"] == "https://api.example.com/v1/run"
    assert body["dry_run"]["request"]["headers"]["x-api-key"] == "[REDACTED]"
    assert "sk-test-key-abcd1234" not in str(body), "secrets must not leak through a dry run"

    unlinked = client.post(
        f"/api/admin/credentials/{credential['id']}/test-call",
        json={"capability_id": "cap_does_not_exist"},
    )
    assert unlinked.status_code == 422
    assert unlinked.json()["error"]["code"] == "capability_not_linked"


def test_audit_rows_record_every_write(client):
    from app.core import db

    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    client.post(
        f"/api/admin/capabilities/{capability['id']}/versions",
        json={"from_version_id": capability["current_version_id"]},
    )

    actions = {
        row["action"]
        for row in db.query_all("SELECT DISTINCT action FROM audit_logs")
    }
    assert {
        "provider.created",
        "credential.created",
        "capability.created",
        "capability.version_created",
    } <= actions
    rows = db.query_all("SELECT actor_type, actor_id FROM audit_logs")
    assert all(row["actor_type"] == "user" and row["actor_id"] for row in rows)
    assert "sk-test-key-abcd1234" not in str(db.query_all("SELECT metadata FROM audit_logs"))


def test_failed_test_call_records_evidence_and_blocks_publish(client):
    """A test that could not reach the provider must be stored as `failed`."""
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]

    config = _valid_generic_config()
    # Loopback is refused by the outbound policy before any connection is made.
    config["request"]["url"] = "http://127.0.0.1:9/v1/run"
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": config},
    )

    tested = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/test",
        json={"input": {"prompt": "hello"}, "mode": "sync"},
    )
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["status"] == "failed"
    assert body["test_passed"] is False
    assert body["real_call_attempted"] is True
    assert body["evidence"]["status"] == "failed"
    assert body["actions"]

    history = client.get(f"/api/admin/capabilities/{capability['id']}/tests").json()
    assert history["total"] == 1
    assert history["items"][0]["status"] == "failed"

    blocked = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/publish"
    )
    assert blocked.status_code == 409
    error = blocked.json()["error"]
    assert error["code"] == "test_evidence_missing"
    assert error["details"]["last_test_status"] == "failed"

    # A malformed input is still a caller error, not recorded evidence.
    bad_input = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/test",
        json={"input": {"unknown_field": 1}, "mode": "sync"},
    )
    assert bad_input.status_code == 422
    assert bad_input.json()["error"]["code"] == "unsupported_parameter"
    assert client.get(f"/api/admin/capabilities/{capability['id']}/tests").json()["total"] == 1


def test_rollback_and_deprecate_are_truthful(client):
    from app.core import db

    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    capability = _create_capability(client, provider["id"], credential["id"])
    version_id = capability["current_version_id"]
    client.patch(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}",
        json={"config": _valid_generic_config()},
    )
    db.insert(
        "capability_tests",
        {
            "id": db.new_id("captest"),
            "capability_id": capability["id"],
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed",
            "request": "{}",
            "response": "{}",
            "created_at": db.iso(),
        },
    )
    route = _create_route(client, provider, credential, capability, version_id)
    _publish(client, capability, version_id)
    # --- deprecate the live version: nothing stays callable ---------------
    deprecated = client.post(
        f"/api/admin/capabilities/{capability['id']}/versions/{version_id}/deprecate"
    )
    assert deprecated.status_code == 200, deprecated.text
    body = deprecated.json()
    assert body["status"] == "disabled"
    assert body["callable"] is False
    assert body["published_version"] is None
    assert body["actions"]

    # --- roll back to the deprecated config as a new published version ----
    rolled = client.post(
        f"/api/admin/capabilities/{capability['id']}/rollback", json={"version_id": version_id}
    )
    assert rolled.status_code == 200, rolled.text
    payload = rolled.json()
    assert payload["status"] == "published"
    assert payload["current_version"] == 2
    assert payload["rolled_back_from"]["version_id"] == version_id
    # The config is the same document, so the older evidence is inherited —
    # and that inheritance is stated, not hidden.
    assert payload["evidence"]["inherited_from_version_id"] == version_id
    assert payload["evidence"]["status"] == "passed"
    # The existing route still points at the deprecated version, so the API
    # says so instead of pretending the rollback restored callability.
    assert payload["callable"] is True  # implicit-route fallback fixed; see comment above
    # The stale route remains visible so the hygiene problem can be fixed.
    stale_routes = client.get(f"/api/admin/capabilities/{capability['id']}/routes").json()
    assert any(row["capability_version_id"] == version_id for row in stale_routes["items"])

    # --- re-point the route and the dry run builds the call ---------------
    new_version_id = payload["current_version_id"]
    repointed = client.patch(
        f"/api/admin/routes/{route['id']}", json={"capability_version_id": new_version_id}
    )
    assert repointed.status_code == 200, repointed.text
    assert repointed.json()["capability_version_id"] == new_version_id

    # --- an admin dry run reports a build, not a call ----------------------
    dry = client.post(
        f"/api/admin/capabilities/{capability['id']}/invoke",
        json={"input": {"prompt": "hello"}, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    dry_body = dry.json()
    assert dry_body["status"] == "preview"
    assert dry_body["dry_run"] is True
    assert dry_body["request"]["method"] == "POST"
    assert dry_body["callable"] is True
    assert "dry run" in dry_body["message"].lower()


def test_list_envelopes_carry_pagination(client):
    provider = _create_provider(client)
    credential = _create_credential(client, provider["id"])
    _create_capability(client, provider["id"], credential["id"])

    for path in (
        "/api/admin/providers",
        "/api/admin/credentials",
        "/api/admin/capabilities",
        "/api/admin/routes",
    ):
        body = client.get(path, params={"limit": 10, "offset": 0}).json()
        assert set(body) == {"items", "total", "limit", "offset"}, path
        assert body["limit"] == 10 and body["offset"] == 0

    filtered = client.get("/api/admin/capabilities", params={"type": "image"}).json()
    assert filtered["total"] == 0
    matching = client.get("/api/admin/capabilities", params={"search": "runner"}).json()
    assert matching["total"] == 1
    assert _bucket(matching, "text-runner")["slug"] == "text-runner"
