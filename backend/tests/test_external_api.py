"""`/api/v1` external surface tests (CONTRACT §6, §8, §12).

Scope note: the outbound HTTP layer is a *fake* (`GuardedResponse`), exactly like
`test_execution.py`. These tests prove our surface behaves as documented —
visibility, error codes, ownership, surface labels, SSE framing and the OpenAPI
document. They are **not** evidence that any real provider answers this way;
that needs a live capability test.

Covered:
  * 401 for missing / invalid / revoked / expired tokens with the documented codes
  * an ungranted capability is invisible in the list and 404 on detail (no leak)
  * whoami identity/scopes/limits, capabilities ETag + 304
  * sync invoke shape + the `http` surface on the invocation row
  * unsupported parameter -> 422 `unsupported_parameter`, upstream never called
  * async invoke -> task_id + poll path, `stream` surface for SSE
  * replay by request_id is owner-scoped
  * task read/cancel are owner-scoped and the cancel answer is truthful
  * artifact download is owner-scoped; uploaded user files are never readable
  * rate limit -> 429 with actions
  * `/api/v1/openapi.json` documents only the v1 routes + bearer scheme
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

FAKE_VENDOR_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


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
    """Scripted stand-in for `app.services.network.guarded_request`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responder: Callable[..., Any] | None = None

    def respond(self, responder: Callable[..., Any]) -> None:
        self.responder = responder

    async def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        assert self.responder is not None, "no responder configured"
        result = self.responder(method, url, kwargs)
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
        elapsed_ms=7,
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
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import external
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(external.router)

    @app.middleware("http")
    async def rid(request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(None)
        return await call_next(request)

    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ seeding ---
def _sync_config() -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("text")
    config["model"] = "test-model-1"
    config["auth"] = {"mode": "credential"}
    config["modes"] = {"sync": True, "async": False, "stream": True}
    config["request"]["url"] = "https://api.example.com/v1/chat"
    config["request"]["body_template"] = {"model": "{{ model }}", "input": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "max_words": {"type": "integer"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["output_schema"] = {"type": "object", "properties": {"summary": {"type": "string"}}}
    config["response"] = {
        "kind": "json",
        "extract": {
            "result": "$.choices[0].message.content",
            "usage_in": "$.usage.prompt_tokens",
            "usage_out": "$.usage.completion_tokens",
        },
        "artifact": None,
    }
    config["stream"] = {"enabled": True, "protocol": "sse", "delta_path": "$.delta.text", "done_marker": "[DONE]"}
    config["examples"] = [{"input": {"prompt": "Summarise this."}, "note": "Short summary."}]
    config["pricing"] = {"per_call": 0.0, "input_per_1k": 0.01, "output_per_1k": 0.02, "per_second": 0.0, "currency": "USD"}
    config["limits"] = {"timeout_s": 30, "max_input_bytes": 262144, "max_output_bytes": 8388608, "file_input": False, "max_file_mb": 10}
    return config


def _async_config() -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("video")
    config["auth"] = {"mode": "credential"}
    config["modes"] = {"sync": False, "async": True, "stream": False}
    config["request"]["url"] = "https://api.example.com/v1/render"
    config["request"]["body_template"] = {"prompt": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["output_schema"] = {}
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
            "poll_interval_s": 5,
            "result_extract": {"result": "$.output"},
        },
        # No cancel endpoint on purpose: cancellation must stay truthful.
        "cancel": {"url": "", "method": "POST", "extract": {"status": ""}},
    }
    config["limits"] = {"timeout_s": 30, "max_duration_s": 300}
    return config


def _make_provider() -> str:
    from app.core import db

    now = db.iso()
    provider_id = db.new_id("prov")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": "Acme AI",
            "slug": f"acme-{provider_id[-6:]}",
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
    return provider_id


def _make_credential(provider_id: str) -> str:
    from app.services import credentials

    row = credentials.create_credential(
        provider_id=provider_id,
        name="Acme key",
        auth_type="api_key",
        secrets={"api_key": FAKE_VENDOR_KEY},
    )
    return row["id"]


def _make_capability(
    slug: str,
    config: dict[str, Any],
    *,
    provider_id: str,
    credential_id: str,
    capability_status: str = "published",
    version_status: str = "published",
    needs_live_agent: bool = False,
) -> dict[str, str]:
    """Capability + published version + one explicit route through the credential."""
    from app.core import db

    now = db.iso()
    capability_id = db.new_id("cap")
    version_id = db.new_id("cver")
    db.insert(
        "capabilities",
        {
            "id": capability_id,
            "slug": slug,
            "name": slug.split(".")[-1].title(),
            "type": config.get("modes") and ("video" if config["modes"].get("async") else "text") or "text",
            "description": f"{slug} test capability",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "status": capability_status,
            "current_version_id": version_id,
            "needs_live_agent": 1 if needs_live_agent else 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "capability_versions",
        {
            "id": version_id,
            "capability_id": capability_id,
            "version": 2,
            "status": version_status,
            "config": db.dumps(config),
            "notes": "",
            "checksum": "",
            "created_by": None,
            "created_at": now,
            "published_at": now if version_status == "published" else None,
        },
    )
    db.insert(
        "routes",
        {
            "id": db.new_id("route"),
            "capability_id": capability_id,
            "capability_version_id": version_id,
            "provider_id": provider_id,
            "credential_id": credential_id,
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
    return {"capability_id": capability_id, "version_id": version_id, "slug": slug}


def _make_agent_token(
    *,
    scopes: list[str] | None = None,
    name: str = "ci token",
    status: str = "active",
    expires_at: str | None = None,
    rate_limit_per_min: int | None = None,
    max_concurrency: int | None = None,
    budget_usd: float | None = None,
) -> tuple[str, str, str]:
    from app.core import db
    from app.security import tokens as token_security

    now = db.iso()
    agent_id = db.new_id("agent")
    db.insert(
        "agents",
        {
            "id": agent_id,
            "name": f"agent {agent_id[-6:]}",
            "slug": f"agent-{agent_id[-6:]}",
            "description": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        },
    )
    issued = token_security.issue_agent_token()
    token_id = db.new_id("tok")
    db.insert(
        "agent_tokens",
        {
            "id": token_id,
            "agent_id": agent_id,
            "name": name,
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(scopes if scopes is not None else ["*"]),
            "expires_at": expires_at,
            "rate_limit_per_min": rate_limit_per_min,
            "max_concurrency": max_concurrency,
            "budget_usd": budget_usd,
            "status": status,
            "created_at": now,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    return agent_id, token_id, issued.token


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture()
def world(isolated, upstream):
    """Provider + credential + three capabilities + two agent tokens."""
    provider_id = _make_provider()
    credential_id = _make_credential(provider_id)
    sync = _make_capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    async_cap = _make_capability("video.render", _async_config(), provider_id=provider_id, credential_id=credential_id)
    hidden = _make_capability("image.generate", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent_id, token_id, token = _make_agent_token(scopes=["text.summarize", "video.render"])
    other_agent_id, _, other_token = _make_agent_token(scopes=["text.summarize"], name="other token")
    return {
        "provider_id": provider_id,
        "credential_id": credential_id,
        "sync": sync,
        "async": async_cap,
        "hidden": hidden,
        "agent_id": agent_id,
        "token_id": token_id,
        "token": token,
        "headers": _bearer(token),
        "other_agent_id": other_agent_id,
        "other_token": other_token,
        "other_headers": _bearer(other_token),
        "upstream": upstream,
    }


def _ok_chat(prompt: str = "hello") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": f"summary of {prompt}"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _sse_events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if name:
            events.append((name, json.loads(data) if data else None))
    return events


# ------------------------------------------------------------------- tests ---
def test_missing_token_is_401_with_documented_code(client, world):
    response = client.get("/api/v1/whoami")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"
    assert body["error"]["request_id"].startswith("req_")


def test_invalid_revoked_and_expired_tokens_are_distinguishable(client, world):
    from app.core import db

    invalid = client.get("/api/v1/whoami", headers=_bearer("sr_live_not-a-real-token"))
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_token"

    _revoked_agent_id, _revoked_token_id, revoked_token = _make_agent_token(scopes=["*"], name="revoked", status="revoked")
    revoked = client.get("/api/v1/whoami", headers=_bearer(revoked_token))
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "token_revoked"

    expired_at = db.iso(db.utcnow() - timedelta(minutes=5))
    _, _, expired_token = _make_agent_token(scopes=["*"], name="expired", expires_at=expired_at)
    expired = client.get("/api/v1/whoami", headers=_bearer(expired_token))
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "token_expired"


def test_ungranted_capability_is_hidden_and_404_on_detail(client, world):
    listed = client.get("/api/v1/capabilities", headers=world["headers"])
    assert listed.status_code == 200
    slugs = {item["slug"] for item in listed.json()["capabilities"]}
    assert slugs == {"text.summarize", "video.render"}

    detail = client.get("/api/v1/capabilities/image.generate", headers=world["headers"])
    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "not_found"

    missing = client.get("/api/v1/capabilities/nope.nothing", headers=world["headers"])
    assert missing.status_code == 404
    # An ungranted and a non-existent capability must look identical.
    assert missing.json()["error"]["code"] == detail.json()["error"]["code"]

    granted = client.get("/api/v1/capabilities/text.summarize", headers=world["headers"])
    assert granted.status_code == 200
    body = granted.json()
    assert body["versions"] == {"current": 2, "available": [2]}
    assert body["modes"]["sync"] is True
    assert body["input_schema"]["required"] == ["prompt"]
    assert body["output_schema"]["properties"]["summary"]["type"] == "string"
    assert body["examples"][0]["input"]["prompt"]
    assert body["needs_live_agent"] is False
    assert body["mcp_tool"] == "text__summarize"
    assert body["version_token"]


def test_whoami_reports_identity_scopes_and_limits(client, world):
    response = client.get("/api/v1/whoami", headers=world["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["name"].startswith("agent ")
    assert body["agent"]["id"] == world["agent_id"]
    assert body["scopes"] == ["text.summarize", "video.render"]
    assert body["limits"] == {"rate_limit_per_min": None, "max_concurrency": None, "budget_usd": None}
    assert body["capabilities_count"] == 2
    assert body["server_time"].endswith("Z")
    # No secret material anywhere in the payload.
    blob = json.dumps(body)
    assert FAKE_VENDOR_KEY not in blob
    assert world["token"] not in blob


def test_capabilities_etag_and_304(client, world):
    first = client.get("/api/v1/capabilities", headers=world["headers"])
    assert first.status_code == 200
    etag = first.headers["etag"]
    version_token = first.json()["version_token"]
    assert etag.strip('"') == version_token

    again = client.get("/api/v1/capabilities", headers={**world["headers"], "if-none-match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert not again.content

    refreshed = client.get(
        "/api/v1/capabilities?refresh=1",
        headers={**world["headers"], "if-none-match": etag},
    )
    assert refreshed.status_code == 200
    assert refreshed.headers["cache-control"] == "no-store"
    assert refreshed.json()["version_token"] == version_token


def test_sync_invocation_success_shape_and_surface(client, world):
    from app.core import db

    world["upstream"].respond(lambda *_: json_response(_ok_chat("hello")))
    response = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "text.summarize", "version": 2, "input": {"prompt": "hello", "max_words": 20}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["capability"] == "text.summarize"
    assert body["version"] == 2
    assert body["result"] == "summary of hello"
    assert body["artifacts"] == []
    assert body["usage"]["input_tokens"] == 11
    assert body["cost"]["estimated_usd"] > 0
    assert body["provider"]["name"] == "Acme AI"
    assert body["request_id"].startswith("req_")
    assert body["task_id"] is None
    assert body["deduplicated"] is False

    row = db.query_one("SELECT * FROM invocations WHERE request_id = ?", (body["request_id"],))
    assert row["surface"] == "http"
    assert row["agent_id"] == world["agent_id"]
    assert row["status"] == "succeeded"
    # The credential was used at send time and never echoed back to the agent.
    assert FAKE_VENDOR_KEY not in json.dumps(body)


def test_unsupported_parameter_is_422_and_never_calls_upstream(client, world):
    response = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "text.summarize", "input": {"prompt": "hi", "bogus": 1}},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["details"]["unsupported"] == ["bogus"]
    assert world["upstream"].calls == []


def test_unpublished_capability_cannot_be_called(client, world):
    from app.core import db

    db.execute("UPDATE capabilities SET status = 'draft' WHERE slug = 'text.summarize'")
    hidden = client.get("/api/v1/capabilities/text.summarize", headers=world["headers"])
    assert hidden.status_code == 404

    call = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "text.summarize", "input": {"prompt": "hi"}},
    )
    assert call.status_code == 409
    assert call.json()["error"]["code"] == "capability_not_published"


def test_async_invocation_returns_task_id_and_poll_path(client, world):
    from app.core import db

    world["upstream"].respond(lambda *_: json_response({"id": "job_abc123", "status": "queued"}))
    response = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "video.render", "input": {"prompt": "a sunset"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"]
    assert body["poll"] == f"/api/v1/tasks/{body['task_id']}"

    task = db.query_one("SELECT * FROM tasks WHERE id = ?", (body["task_id"],))
    assert task["agent_id"] == world["agent_id"]
    assert task["upstream_task_id"] == "job_abc123"

    status = client.get(f"/api/v1/tasks/{body['task_id']}", headers=world["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "queued"

    # Another agent must not see or cancel it.
    assert client.get(f"/api/v1/tasks/{body['task_id']}", headers=world["other_headers"]).status_code == 404
    assert client.post(f"/api/v1/tasks/{body['task_id']}/cancel", headers=world["other_headers"]).status_code == 404

    cancel = client.post(
        f"/api/v1/tasks/{body['task_id']}/cancel",
        headers=world["headers"],
        json={"reason": "changed my mind"},
    )
    assert cancel.status_code == 200, cancel.text
    cancel_body = cancel.json()
    # The capability has no cancel endpoint: say so instead of claiming success.
    assert cancel_body["supported"] is False
    assert cancel_body["canceled"] is False
    assert cancel_body["upstream_may_continue"] is True


def test_replay_by_request_id_is_owner_scoped(client, world):
    world["upstream"].respond(lambda *_: json_response(_ok_chat("replay")))
    created = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "text.summarize", "input": {"prompt": "replay"}},
    ).json()
    request_id = created["request_id"]

    replay = client.get(f"/api/v1/invocations/{request_id}", headers=world["headers"])
    assert replay.status_code == 200
    body = replay.json()
    assert body["request_id"] == request_id
    assert body["status"] == "succeeded"
    assert body["replay"] is True
    assert body["capability"] == "text.summarize"

    denied = client.get(f"/api/v1/invocations/{request_id}", headers=world["other_headers"])
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"

    assert client.get("/api/v1/invocations/req_does_not_exist", headers=world["headers"]).status_code == 404


def test_file_download_is_owner_scoped_and_uploads_are_blocked(client, world):
    from app.services import files as file_service

    world["upstream"].respond(lambda *_: json_response(_ok_chat("artifact")))
    created = client.post(
        "/api/v1/invocations",
        headers=world["headers"],
        json={"capability": "text.summarize", "input": {"prompt": "artifact"}},
    ).json()
    from app.core import db

    invocation_id = db.query_scalar("SELECT id FROM invocations WHERE request_id = ?", (created["request_id"],))
    artifact = file_service.save_bytes(
        b"binary-payload",
        filename="out.bin",
        content_type="application/octet-stream",
        purpose="artifact",
        owner_type="invocation",
        owner_id=invocation_id,
        invocation_id=invocation_id,
    )
    upload = file_service.save_bytes(
        b"private-user-upload",
        filename="secret.txt",
        content_type="text/plain",
        purpose="upload",
        owner_type="user",
        owner_id="user_1",
    )

    mine = client.get(f"/api/v1/files/{artifact['id']}", headers=world["headers"])
    assert mine.status_code == 200
    assert mine.content == b"binary-payload"
    assert "attachment" in mine.headers["content-disposition"]

    # Another agent cannot read it, and an uploaded user file is never exposed.
    assert client.get(f"/api/v1/files/{artifact['id']}", headers=world["other_headers"]).status_code == 404
    assert client.get(f"/api/v1/files/{upload['id']}", headers=world["headers"]).status_code == 404
    assert client.get("/api/v1/files/file_missing", headers=world["headers"]).status_code == 404


def test_rate_limit_is_429_with_actions(client, world):
    _, _, limited_token = _make_agent_token(scopes=["text.summarize"], name="limited", rate_limit_per_min=1)
    headers = _bearer(limited_token)
    world["upstream"].respond(lambda *_: json_response(_ok_chat("once")))

    first = client.post(
        "/api/v1/invocations",
        headers=headers,
        json={"capability": "text.summarize", "input": {"prompt": "once"}},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/invocations",
        headers=headers,
        json={"capability": "text.summarize", "input": {"prompt": "twice"}},
    )
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["actions"]


def test_stream_invocation_emits_sse_events(client, world, monkeypatch):
    from app.services import execution

    chunks = [
        'data: {"delta":{"text":"Hel"}}\n\n',
        'data: {"delta":{"text":"lo"}}\n\n',
        "data: [DONE]\n\n",
    ]

    class _FakeStreamResponse:
        status_code = 200

        async def aiter_text(self):
            for chunk in chunks:
                yield chunk

        async def aread(self):
            return b""

    class _FakeStreamContext:
        async def __aenter__(self):
            return _FakeStreamResponse()

        async def __aexit__(self, *exc_info):
            return False

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def stream(self, method, url, **kwargs):
            return _FakeStreamContext()

    # The transport is faked, so the SSRF/DNS pre-flight would only fail on DNS.
    monkeypatch.setattr(execution, "validate_url", lambda url, **kwargs: None)
    monkeypatch.setattr(execution.httpx, "AsyncClient", _FakeAsyncClient)

    with client.stream(
        "POST",
        "/api/v1/invocations",
        headers={**world["headers"], "accept": "text/event-stream"},
        json={"capability": "text.summarize", "input": {"prompt": "stream"}},
    ) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())

    events = _sse_events(text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert "delta" in names
    deltas = [payload["text"] for name, payload in events if name == "delta"]
    assert deltas == ["Hel", "lo"]
    assert "usage" in names, names
    assert names.index("usage") < names.index("done")
    done = dict(events)["done"]
    assert done["text"] == "Hello"

    from app.core import db

    row = db.query_one("SELECT * FROM invocations WHERE request_id = ?", (events[0][1]["request_id"],))
    assert row["surface"] == "stream"
    assert row["mode"] == "stream"


def test_stream_keepalive_comment_when_the_upstream_is_slow(client, world, monkeypatch):
    """A 15s keepalive interval is documented; prove the comment is emitted."""
    import asyncio

    from app.api import external
    from app.services import execution

    class _SlowStreamResponse:
        status_code = 200

        async def aiter_text(self):
            for chunk in ['data: {"delta":{"text":"a"}}\n\n', "data: [DONE]\n\n"]:
                await asyncio.sleep(0.05)
                yield chunk

        async def aread(self):
            return b""

    class _Context:
        async def __aenter__(self):
            return _SlowStreamResponse()

        async def __aexit__(self, *exc_info):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def stream(self, method, url, **kwargs):
            return _Context()

    monkeypatch.setattr(external, "SSE_KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(execution, "validate_url", lambda url, **kwargs: None)
    monkeypatch.setattr(execution.httpx, "AsyncClient", _Client)

    response = client.post(
        "/api/v1/invocations",
        headers={**world["headers"], "accept": "text/event-stream"},
        json={"capability": "text.summarize", "input": {"prompt": "slow"}},
    )
    assert response.status_code == 200
    assert ": keepalive" in response.text
    assert "event: done" in response.text


def test_skill_bundle_is_secret_free_and_carries_markdown(client, world):
    response = client.get("/api/v1/skill", headers=world["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "text/markdown"
    assert body["content"].startswith("---")
    assert body["sha256"] == hashlib.sha256(body["content"].encode("utf-8")).hexdigest()
    assert body["includes_credentials"] is False
    assert body["platform"]["base_url"] == "http://testserver"
    assert body["platform"]["mcp_url"] == "/api/mcp"
    slugs = {item["slug"] for item in body["capabilities"]}
    assert slugs == {"text.summarize", "video.render"}
    blob = json.dumps(body)
    assert FAKE_VENDOR_KEY not in blob
    assert world["token"] not in blob
    assert world["credential_id"] not in blob


def test_openapi_document_covers_only_v1_routes_with_bearer(client):
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.1")
    paths = document["paths"]
    for expected in (
        "/api/v1/whoami",
        "/api/v1/capabilities",
        "/api/v1/capabilities/{slug}",
        "/api/v1/invocations",
        "/api/v1/invocations/{request_id}",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/cancel",
        "/api/v1/files/{file_id}",
        "/api/v1/skill",
    ):
        assert expected in paths, expected
    assert all(path.startswith("/api/v1") for path in paths)
    assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    invoke = paths["/api/v1/invocations"]["post"]
    assert invoke["security"] == [{"bearerAuth": []}]
    assert "text/event-stream" in invoke["responses"]["200"]["content"]
    assert invoke["requestBody"]["content"]["application/json"]["example"]["capability"]
    assert "429" in invoke["responses"]


def test_index_lists_routes_without_auth(client):
    response = client.get("/api/v1")
    assert response.status_code == 200
    body = response.json()
    methods = {(route["method"], route["path"]) for route in body["routes"]}
    assert ("POST", "/api/v1/invocations") in methods
    assert body["mcp"]["url"] == "/api/mcp"
    assert body["auth"].startswith("Authorization: Bearer")
