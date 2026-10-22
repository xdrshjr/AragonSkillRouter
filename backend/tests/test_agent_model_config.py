"""Agent model configuration: the API surface the Settings form depends on.

Two defects are pinned here:

1. `resolve_model()` falls back to `credential.config.model`, but the credential
   create/update schema used `extra="forbid"` without a `model` field, so that
   documented fallback could never be configured — the UI form had no way to save it.
2. There was no way to ask a provider which model ids it serves, so naming a model
   was guesswork (and a wrong guess surfaced as "no model configured" or a 404).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Isolated DB + a signed-in admin (session cookie and CSRF header)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import admin_credentials, admin_providers, auth
    from app.core import config, db
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id
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

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(auth.public_router)
    app.include_router(admin_providers.router)
    app.include_router(admin_credentials.router)

    @app.middleware("http")
    async def rid(request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(None)
        return await call_next(request)

    http = TestClient(app)
    login = http.post("/api/auth/login", json={"username": "admin", "password": "adminjr"})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]
    http.headers.update({"x-csrf-token": csrf})
    # Clear the first-run gate so business routes answer.
    changed = http.post(
        "/api/auth/change-password",
        json={"current_password": "adminjr", "new_password": "Model-Config!Pass9"},
    )
    assert changed.status_code == 200, changed.text
    http.headers.update({"x-csrf-token": changed.json()["csrf_token"]})
    http.csrf = changed.json()["csrf_token"]  # type: ignore[attr-defined]
    return http


def _provider(client, **overrides) -> dict[str, Any]:
    body = {
        "name": "GLM",
        "category": "llm",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "environment": "production",
        "tags": ["llm"],
        "status": "active",
    }
    body.update(overrides)
    response = client.post("/api/admin/providers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_credential_accepts_a_model_id(client):
    """`config.model` must be storable: resolve_model() reads it as a fallback."""
    provider = _provider(client)
    response = client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "GLM key",
            "auth_type": "api_key",
            "secret": {"api_key": "sk-test-value"},
            "config": {"model": "glm-5.3-flash", "test_url": "https://open.bigmodel.cn/api/anthropic/v1/models"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["config"]["model"] == "glm-5.3-flash"
    assert "sk-test-value" not in json.dumps(body)
    # And the resolver now sees a model without any agent.model setting.
    from app.core import db
    from app.services import agent_runtime

    db.set_setting("agent.model", {})
    resolved = agent_runtime.resolve_model()
    assert resolved.model_id == "glm-5.3-flash"
    assert resolved.provider_id == "anthropic"


def test_credential_rejects_unknown_config_keys(client):
    """extra="forbid" still guards against typos that would be silently ignored."""
    provider = _provider(client)
    response = client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "typo",
            "auth_type": "api_key",
            "secret": {"api_key": "sk-test-value"},
            "config": {"modle": "glm-5.3-flash"},
        },
    )
    assert response.status_code == 422, response.text


def test_models_endpoint_reads_the_anthropic_path_first(client, monkeypatch):
    """An Anthropic-compatible base_url serves /v1/models; the probe tries it first."""
    from app.services import network

    provider = _provider(client)
    client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "GLM key",
            "auth_type": "api_key",
            "secret": {"api_key": "sk-test-value"},
            "config": {},
        },
    )

    calls: list[str] = []

    async def fake_request(method, url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(url)
        if url.endswith("/api/anthropic/v1/models"):
            body = {"data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}, {"display_name": "GLM-4.7", "id": "glm-4.7"}]}
            return network.GuardedResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
                url=url,
                elapsed_ms=12,
            )
        return network.GuardedResponse(
            status_code=404,
            headers={"content-type": "application/json"},
            content=b'{"error":"not found"}',
            url=url,
            elapsed_ms=8,
        )

    monkeypatch.setattr(network, "guarded_request", fake_request)

    response = client.post(f"/api/admin/providers/{provider['id']}/models")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == ["glm-5.3", "glm-5.3-flash", "glm-4.7"]
    assert body["count"] == 3
    assert body["source_url"].endswith("/api/anthropic/v1/models")
    assert calls and calls[0].endswith("/api/anthropic/v1/models")


def test_models_endpoint_falls_back_to_the_openai_path(client, monkeypatch):
    from app.services import network

    provider = _provider(client, name="ZenMux", base_url="https://zenmux.ai/api/v1")
    client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "ZenMux key",
            "auth_type": "bearer",
            "secret": {"token": "sk-test-value"},
            "config": {},
        },
    )

    async def fake_request(method, url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if url.endswith("/api/v1/models"):
            body = {"object": "list", "data": [{"id": "google/gemini-2.5-flash"}, {"id": "openai/gpt-5"}]}
            return network.GuardedResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
                url=url,
                elapsed_ms=9,
            )
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(network, "guarded_request", fake_request)
    response = client.post(f"/api/admin/providers/{provider['id']}/models")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == ["google/gemini-2.5-flash", "openai/gpt-5"]


def test_models_endpoint_reports_why_it_failed(client, monkeypatch):
    from app.services import network

    provider = _provider(client)
    client.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider["id"],
            "name": "bad key",
            "auth_type": "api_key",
            "secret": {"api_key": "sk-test-value"},
            "config": {},
        },
    )

    async def fake_request(method, url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        return network.GuardedResponse(
            status_code=401,
            headers={"content-type": "application/json"},
            content=b'{"error":"bad key"}',
            url=url,
            elapsed_ms=5,
        )

    monkeypatch.setattr(network, "guarded_request", fake_request)
    response = client.post(f"/api/admin/providers/{provider['id']}/models")
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "models_unavailable"
    assert body["actions"], "the failure must tell the user what to do instead"
    assert len(body["details"]["attempts"]) == 2


def test_models_endpoint_needs_a_base_url_and_a_credential(client):
    bare = _provider(client, name="No URL", base_url=None)
    response = client.post(f"/api/admin/providers/{bare['id']}/models")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_has_no_base_url"

    with_url = _provider(client, name="No credential", base_url="https://example.invalid/api/v1")
    response = client.post(f"/api/admin/providers/{with_url['id']}/models")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_has_no_credential"


def test_model_id_extraction_handles_every_provider_shape():
    from app.api.admin_providers import _model_ids

    assert _model_ids({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}) == ["gpt-4o", "gpt-4o-mini"]
    assert _model_ids({"data": [{"id": "glm-5.3", "display_name": "GLM-5.3"}]}) == ["glm-5.3"]
    assert _model_ids({"models": [{"name": "models/gemini-2.5-flash"}]}) == ["gemini-2.5-flash"]
    assert _model_ids({"items": ["a", "b", "a"]}) == ["a", "b"]
    assert _model_ids({"unexpected": True}) == []
    assert _model_ids(None) == []
