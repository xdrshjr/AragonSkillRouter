"""Acceptance: security behaviour end to end (CONTRACT §2.1, §4, §6).

Scope note: these are **engine-level** proofs. The real routers, the real
executor, the real credential store and the real SQLite schema are used; only
the two genuinely external things are replaced:

  * the outbound HTTP layer (`app.services.network.guarded_request`), so we can
    script a provider's 401/429/timeout/echo behaviour deterministically;
  * nothing else — timeouts, retries, failover, dedupe, cancellation and
    redaction are all *our* code.

They are not evidence that any particular provider behaves this way. A live
capability test is still required for that.

Covered:
  * a wrong secret -> `upstream_auth_failed`, invocation `failed`, credential
    `last_verify_status = invalid`;
  * a disabled credential is refused with `credential_inactive` and no HTTP call;
  * scope enforcement (`capability_not_granted`), invisibility of ungranted
    capabilities, and immediate revocation (`token_revoked`);
  * idempotency: one upstream call for a repeated key, one task for a concurrent
    duplicate of an async call;
  * a read timeout -> `uncertain` / `upstream_timeout_uncertain`, never retried,
    never failed over, with a "check before retrying" action;
  * token rate limits -> 429 `rate_limited` with `retry_after_seconds`, and an
    upstream 429 -> `rate_limited` without marking the credential invalid;
  * cancel truthfulness: no cancel endpoint keeps `running`; an accepting
    provider goes `cancel_requested` -> `cancelled` only when it says so;
  * the raw secret and the ciphertext envelope appear in no response, no audit
    row, no preview, no export and no other database column.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

NEW_PASSWORD = "Acceptance-Passw0rd!9"
#: Deliberately NOT shaped like a known vendor key, so only the exact-value
#: scrubbing layer in app.security.redact can keep it out of a response.
RAW_SECRET = "leakcanary-0f9a8b7c6d5e4f3a"
RAW_SECRET_2 = "secondcanary-11223344556677889900"


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
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def admin(client):
    """Signed in, password gate cleared, CSRF header attached."""
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
def _provider(name: str = "Acme AI") -> str:
    from app.core import db

    now = db.iso()
    provider_id = db.new_id("prov")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": name,
            "slug": f"acme-{provider_id[-8:]}",
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


def _credential(provider_id: str, *, secret: str = RAW_SECRET, status: str = "active") -> str:
    from app.services import credentials

    row = credentials.create_credential(
        provider_id=provider_id,
        name=f"Acme key {secret[:6]}",
        auth_type="api_key",
        secrets={"api_key": secret},
        status=status,
    )
    return row["id"]


def _sync_config(url: str = "https://api.example.com/v1/chat") -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("text")
    config["model"] = "acceptance-model"
    config["auth"] = {"mode": "credential"}
    config["modes"] = {"sync": True, "async": False, "stream": False}
    config["request"]["url"] = url
    config["request"]["body_template"] = {"model": "{{ model }}", "input": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["output_schema"] = {}
    config["response"] = {"kind": "json", "extract": {"result": "$.echo"}, "artifact": None}
    config["examples"] = [{"input": {"prompt": "hello"}}]
    # No retries on purpose: these tests count HTTP calls.
    config["retry"] = {"max_attempts": 0, "backoff_ms": [10], "retry_on": []}
    config["limits"] = {"timeout_s": 20, "max_input_bytes": 262144, "max_output_bytes": 8388608}
    return config


def _async_config(*, cancel_url: str, url: str = "https://api.example.com/v1/render") -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("video")
    config["auth"] = {"mode": "credential"}
    config["modes"] = {"sync": False, "async": True, "stream": False}
    config["request"]["url"] = url
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
            "url": url + "/{task_id}",
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
        "cancel": {"url": cancel_url, "method": "POST", "extract": {"status": "$.status"}},
    }
    config["retry"] = {"max_attempts": 0, "backoff_ms": [10], "retry_on": []}
    config["limits"] = {"timeout_s": 20, "max_duration_s": 300}
    return config


def _capability(
    slug: str,
    config: dict[str, Any],
    *,
    provider_id: str,
    credential_id: str,
    extra_routes: list[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Published capability + version + one explicit route (optionally more)."""
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
            "type": "video" if (config.get("modes") or {}).get("async") else "text",
            "description": f"{slug} acceptance capability",
            "provider_id": provider_id,
            "credential_id": credential_id,
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

    def add_route(prov: str, cred: str, priority: int) -> str:
        route_id = db.new_id("route")
        db.insert(
            "routes",
            {
                "id": route_id,
                "capability_id": capability_id,
                "capability_version_id": version_id,
                "provider_id": prov,
                "credential_id": cred,
                "strategy": "priority",
                "priority": priority,
                "weight": 1,
                "enabled": 1,
                "health_status": "healthy",
                "last_health_at": now,
                "conditions": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )
        return route_id

    route_id = add_route(provider_id, credential_id, 10)
    for index, (prov, cred) in enumerate(extra_routes or [], start=1):
        add_route(prov, cred, 10 + index)
    return {"capability_id": capability_id, "version_id": version_id, "route_id": route_id, "slug": slug}


def _agent_token(
    *,
    scopes: list[str] | None = None,
    rate_limit_per_min: int | None = None,
) -> dict[str, Any]:
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
            "name": "acceptance token",
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(scopes if scopes is not None else ["*"]),
            "expires_at": None,
            "rate_limit_per_min": rate_limit_per_min,
            "max_concurrency": None,
            "budget_usd": None,
            "status": "active",
            "created_at": now,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    return {
        "agent_id": agent_id,
        "token_id": token_id,
        "token": issued.token,
        "headers": {"authorization": f"Bearer {issued.token}"},
    }


def _invoke(client, headers: dict[str, str], capability: str, payload: dict[str, Any], **options: Any):
    return client.post(
        "/api/v1/invocations",
        headers=headers,
        json={"capability": capability, "input": payload, "options": options},
    )


def _echo_headers(method: str, url: str, kwargs: dict[str, Any]) -> Any:
    """Provider stand-in that echoes everything it received (worst case for leaks)."""
    return json_response(
        {
            "echo": {"sent": {key: value for key, value in (kwargs.get("headers") or {}).items()}},
            "body": kwargs.get("json_body"),
        }
    )


# -------------------------------------------------------------- credentials --
def test_wrong_secret_maps_to_upstream_auth_failed_and_marks_credential_invalid(client, upstream):
    from app.core import db

    provider_id = _provider()
    credential_id = _credential(provider_id, secret=RAW_SECRET)
    capability = _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent = _agent_token(scopes=["text.summarize"])

    upstream.respond(lambda *_: json_response({"error": {"code": "invalid_api_key"}}, status_code=401))

    response = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"})

    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "upstream_auth_failed"
    assert error["actions"], "the caller must be told to verify the credential"
    assert error["request_id"].startswith("req_")

    # The credential really was sent (send-time injection) ...
    assert upstream.calls, "no upstream call was attempted"
    sent_headers = upstream.calls[0]["kwargs"]["headers"]
    assert RAW_SECRET in json.dumps(sent_headers), "the credential was never injected"

    # ... the invocation is recorded as failed with the same code ...
    invocation = db.query_one(
        "SELECT * FROM invocations WHERE capability_id = ? ORDER BY created_at DESC LIMIT 1",
        (capability["capability_id"],),
    )
    assert invocation["status"] == "failed"
    assert invocation["error_code"] == "upstream_auth_failed"

    # ... and the credential is marked invalid for the UI to surface.
    credential = db.query_one("SELECT * FROM credentials WHERE id = ?", (credential_id,))
    assert credential["last_verify_status"] == "invalid"
    assert credential["last_verify_message"]


def test_disabled_credential_is_refused_before_any_http_call(client, upstream):
    from app.core.errors import ApiError
    from app.services import credentials

    provider_id = _provider()
    credential_id = _credential(provider_id, status="disabled")
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent = _agent_token(scopes=["text.summarize"])

    upstream.respond(lambda *_: json_response({"echo": {"sent": {}}}))

    # Service boundary: the cleartext map is never even produced.
    with pytest.raises(ApiError) as excinfo:
        credentials.get_secret_map(credential_id)
    assert excinfo.value.code == "credential_inactive"
    assert excinfo.value.status_code == 409

    response = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"})

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "credential_inactive"
    assert error["actions"], "the caller must be told how to re-enable the credential"
    assert upstream.calls == [], "a disabled credential must never reach the provider"


# ------------------------------------------------------------------ scoping --
def test_scoped_token_cannot_call_or_see_an_ungranted_capability(client, upstream):
    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    _capability("image.generate", _sync_config(), provider_id=provider_id, credential_id=credential_id)

    agent = _agent_token(scopes=["text.summarize"])
    upstream.respond(lambda *_: json_response({"echo": {"sent": {}}}))

    allowed = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"})
    assert allowed.status_code == 200, allowed.text
    assert len(upstream.calls) == 1

    denied = _invoke(client, agent["headers"], "image.generate", {"prompt": "hello"})
    assert denied.status_code == 403
    error = denied.json()["error"]
    assert error["code"] == "capability_not_granted"
    assert error["actions"]
    assert len(upstream.calls) == 1, "a refused capability must not reach the provider"

    listed = client.get("/api/v1/capabilities", headers=agent["headers"])
    assert listed.status_code == 200
    assert {item["slug"] for item in listed.json()["capabilities"]} == {"text.summarize"}
    assert "image.generate" not in listed.text

    hidden = client.get("/api/v1/capabilities/image.generate", headers=agent["headers"])
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"


def test_revoked_token_is_refused_immediately_without_a_cache(admin, upstream):
    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)

    agent_row = admin.post("/api/admin/agents", json={"name": "Revocation agent"})
    assert agent_row.status_code == 201, agent_row.text
    agent_id = agent_row.json()["id"]
    issued = admin.post(
        f"/api/admin/agents/{agent_id}/tokens",
        json={"name": "revoke me", "scopes": ["text.summarize"]},
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]
    token_id = issued.json()["token_record"]["id"]
    headers = {"authorization": f"Bearer {token}"}

    upstream.respond(lambda *_: json_response({"echo": {"sent": {}}}))
    assert _invoke(admin, headers, "text.summarize", {"prompt": "hello"}).status_code == 200

    revoked = admin.post(f"/api/admin/tokens/{token_id}/revoke", json={})
    assert revoked.status_code in (200, 204), revoked.text

    # Same client, same second: no in-process cache may keep the token alive.
    after = _invoke(admin, headers, "text.summarize", {"prompt": "hello"})
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "token_revoked"
    assert admin.get("/api/v1/whoami", headers=headers).status_code == 401


# --------------------------------------------------------------- idempotency --
def test_duplicate_idempotency_key_makes_a_single_upstream_call(client, upstream):
    import json as _json

    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent = _agent_token(scopes=["text.summarize"])
    upstream.respond(_echo_headers)

    first = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"}, idempotency_key="dup-1")
    second = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"}, idempotency_key="dup-1")

    assert first.status_code == second.status_code == 200, first.text
    assert len(upstream.calls) == 1, "the second submit must not reach the provider"
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert second.json()["request_id"] == first.json()["request_id"]
    assert second.json()["invocation_id"] == first.json()["invocation_id"]
    assert second.json()["result"] == first.json()["result"]
    # A different key is a different call.
    third = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"}, idempotency_key="dup-2")
    assert third.status_code == 200
    assert len(upstream.calls) == 2
    assert third.json()["request_id"] != first.json()["request_id"]
    assert _json.dumps(third.json())  # response is JSON-serialisable


def test_concurrent_duplicate_async_submit_creates_one_task(isolated, upstream):
    """Two simultaneous submissions of one idempotency key must submit once.

    The fake provider yields to the event loop before answering, which is exactly
    the window in which a second, still-in-flight duplicate arrives.
    """
    from app.core import db
    from app.deps import AgentPrincipal
    from app.services import execution

    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability(
        "video.render",
        _async_config(cancel_url="https://api.example.com/v1/render/{task_id}/cancel"),
        provider_id=provider_id,
        credential_id=credential_id,
    )

    async def slow_submit(method: str, url: str, kwargs: dict[str, Any]) -> Any:
        await asyncio.sleep(0)  # let the other coroutine reach the dedupe check
        return json_response({"id": "job_concurrent"})

    upstream.respond(slow_submit)
    principal = AgentPrincipal(
        agent_id="",
        agent_name="engine-acceptance",
        token_id="",
        scopes=["*"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )
    options = execution.InvokeOptions(mode="async", idempotency_key="concurrent-duplicate")

    async def both():
        return await asyncio.gather(
            execution.invoke(principal, "video.render", {"prompt": "a fox"}, options),
            execution.invoke(principal, "video.render", {"prompt": "a fox"}, options),
        )

    first, second = asyncio.run(both())

    assert len(upstream.calls) == 1, "the same idempotency key submitted the job twice"
    assert db.query_scalar("SELECT COUNT(*) FROM tasks") == 1, "a duplicate submit created a second task"
    assert first.task_id and first.request_id == second.request_id
    assert second.deduplicated is True
    assert first.deduplicated is False
    # The duplicate arrived while the first submit was still in flight, so it
    # answers "running" without the upstream job id yet — the documented way to
    # pick the task up is the recorded invocation for that request id.
    task_id_in_record = db.query_scalar(
        "SELECT task_id FROM invocations WHERE request_id = ?", (first.request_id,)
    )
    assert task_id_in_record == first.task_id


# ------------------------------------------------------------------ timeouts --
def test_read_timeout_is_uncertain_and_never_retried_or_failed_over(client, upstream):
    from app.core import db

    provider_id = _provider()
    credential_id = _credential(provider_id, secret=RAW_SECRET)
    fallback_provider = _provider("Backup AI")
    fallback_credential = _credential(fallback_provider, secret=RAW_SECRET_2)
    capability = _capability(
        "text.summarize",
        _sync_config(),
        provider_id=provider_id,
        credential_id=credential_id,
        extra_routes=[(fallback_provider, fallback_credential)],
    )
    agent = _agent_token(scopes=["text.summarize"])
    upstream.respond(lambda *_: httpx.ReadTimeout("provider never answered"))

    response = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "uncertain"
    assert body["error"]["code"] == "upstream_timeout_uncertain"
    assert "before retrying" in body["error"]["action"], body["error"]
    assert body["result"] is None
    assert len(upstream.calls) == 1, "an unknown outcome must never be retried or failed over"

    invocation = db.query_one(
        "SELECT * FROM invocations WHERE capability_id = ? ORDER BY created_at DESC LIMIT 1",
        (capability["capability_id"],),
    )
    assert invocation["status"] == "uncertain"
    assert invocation["error_code"] == "upstream_timeout_uncertain"
    assert int(invocation["retries"] or 0) == 0


# --------------------------------------------------------------- rate limits --
def test_token_rate_limit_answers_429_with_a_retry_after(client, upstream):
    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent = _agent_token(scopes=["text.summarize"], rate_limit_per_min=1)
    upstream.respond(lambda *_: json_response({"echo": {"sent": {}}}))

    first = _invoke(client, agent["headers"], "text.summarize", {"prompt": "one"})
    assert first.status_code == 200, first.text

    second = _invoke(client, agent["headers"], "text.summarize", {"prompt": "two"})
    assert second.status_code == 429, second.text
    error = second.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["limit"] == 1
    assert error["details"]["retry_after_seconds"] > 0, error
    assert error["actions"]
    assert len(upstream.calls) == 1, "a throttled call must not reach the provider"


def test_upstream_rate_limit_maps_to_rate_limited_and_keeps_the_credential_valid(client, upstream):
    from app.core import db

    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    agent = _agent_token(scopes=["text.summarize"])
    upstream.respond(
        lambda *_: json_response({"error": {"code": "rate_limit_exceeded"}}, status_code=429)
    )

    response = _invoke(client, agent["headers"], "text.summarize", {"prompt": "hello"})

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "rate_limited"
    # A provider throttle says nothing about the credential's validity.
    credential = db.query_one("SELECT * FROM credentials WHERE id = ?", (credential_id,))
    assert credential["last_verify_status"] != "invalid"
    assert len(upstream.calls) == 1


# ------------------------------------------------------------------- cancel ---
def test_cancel_without_a_provider_endpoint_keeps_the_task_running(admin, client, upstream):
    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability(
        "video.render",
        _async_config(cancel_url=""),  # the provider exposes no cancel endpoint
        provider_id=provider_id,
        credential_id=credential_id,
    )
    agent = _agent_token(scopes=["video.render"])

    def responder(method: str, url: str, kwargs: dict[str, Any]) -> Any:
        if method == "GET":
            return json_response({"status": "processing", "progress": 20})
        return json_response({"id": "job_nocancel"})

    upstream.respond(responder)

    submitted = _invoke(client, agent["headers"], "video.render", {"prompt": "a fox"})
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task_id"]
    assert task_id

    # The worker's first status probe moves the task to `running`.
    polled = admin.post(f"/api/admin/tasks/{task_id}/reconcile", json={})
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "running"

    calls_before = len(upstream.calls)
    canceled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=agent["headers"], json={})
    assert canceled.status_code == 200, canceled.text
    outcome = canceled.json()

    assert outcome["supported"] is False
    assert outcome["canceled"] is False
    assert outcome["upstream_may_continue"] is True
    assert outcome["status"] == "running", "we must never claim a cancellation the provider never made"
    assert len(upstream.calls) == calls_before, "there is no endpoint to call"

    status = client.get(f"/api/v1/tasks/{task_id}", headers=agent["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "running"
    assert "may still complete" in (status.json()["progress_note"] or "").lower()


def test_cancel_accepted_stays_cancel_requested_until_the_provider_confirms(admin, client, upstream):
    provider_id = _provider()
    credential_id = _credential(provider_id)
    _capability(
        "video.render",
        _async_config(cancel_url="https://api.example.com/v1/render/{task_id}/cancel"),
        provider_id=provider_id,
        credential_id=credential_id,
    )
    agent = _agent_token(scopes=["video.render"])
    state = {"upstream": "processing", "cancels": 0}

    def responder(method: str, url: str, kwargs: dict[str, Any]) -> Any:
        if url.endswith("/cancel"):
            state["cancels"] += 1
            return json_response({"status": "canceling"})
        if method == "GET":
            return json_response({"status": state["upstream"], "progress": 40})
        return json_response({"id": "job_withcancel"})

    upstream.respond(responder)

    submitted = _invoke(client, agent["headers"], "video.render", {"prompt": "a fox"})
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task_id"]

    accepted = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=agent["headers"], json={})
    assert accepted.status_code == 200, accepted.text
    outcome = accepted.json()

    assert state["cancels"] == 1, "the provider's cancel endpoint must be called"
    assert outcome["supported"] is True
    assert outcome["canceled"] is False, "the provider has not confirmed anything yet"
    assert outcome["status"] == "cancel_requested"
    assert outcome["upstream_may_continue"] is True

    # The provider now reports the job as cancelled; only then is it cancelled.
    state["upstream"] = "canceled"
    reconciled = admin.post(f"/api/admin/tasks/{task_id}/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "cancelled"

    task = client.get(f"/api/v1/tasks/{task_id}", headers=agent["headers"]).json()
    assert task["status"] == "cancelled"
    assert task["finished_at"]


# ------------------------------------------------------------------- secrets --
def _db_text_values() -> list[tuple[str, str, str]]:
    """(table, column, value) for every TEXT-ish value in the database."""
    from app.core.config import get_settings

    found: list[tuple[str, str, str]] = []
    connection = sqlite3.connect(get_settings().db_path)
    try:
        connection.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                if table == "credentials" and column == "secret_ciphertext":
                    continue  # the one and only place the envelope may live
                for row in connection.execute(f'SELECT "{column}" AS v FROM "{table}"'):
                    value = row["v"]
                    if isinstance(value, str) and value:
                        found.append((table, column, value))
    finally:
        connection.close()
    return found


def _admin_get_routes(app, ids: dict[str, str]) -> list[tuple[str, str]]:
    """Ready-to-call admin GET URLs, with unmappable templates skipped."""
    from fastapi.routing import APIRoute

    urls: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in (route.methods or set()):
            continue
        path = str(route.path)
        if not path.startswith("/api/admin"):
            continue
        resolved = path
        usable = True
        for name in [part.split("}")[0] for part in path.split("{")[1:]]:
            value = ids.get(name)
            if not value:
                usable = False
                break
            resolved = resolved.replace("{" + name + "}", value)
        if usable:
            urls.append((path, resolved))
    return urls


def test_raw_secret_and_ciphertext_never_leave_the_credential_store(admin, client, upstream):
    """The secret exists in exactly one place: `credentials.secret_ciphertext`."""
    from app.core import db

    provider = admin.post(
        "/api/admin/providers",
        json={"name": "Leak Canary", "category": "llm", "base_url": "https://api.example.com", "environment": "development"},
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["id"]

    created = admin.post(
        "/api/admin/credentials",
        json={
            "provider_id": provider_id,
            "name": "Canary key",
            "auth_type": "api_key",
            "secret": {"api_key": RAW_SECRET},
        },
    )
    assert created.status_code == 201, created.text
    credential_id = created.json()["id"]

    capability = _capability(
        "text.echo",
        _sync_config(),
        provider_id=provider_id,
        credential_id=credential_id,
    )
    agent = _agent_token(scopes=["text.echo"])

    # The provider echoes the credentials it received: if redaction is broken the
    # secret lands in the result, the preview, the audit row or an export.
    upstream.respond(_echo_headers)
    invoked = _invoke(client, agent["headers"], "text.echo", {"prompt": "leak please"})
    assert invoked.status_code == 200, invoked.text
    assert RAW_SECRET not in invoked.text, "the secret was returned in the invocation response"

    # A published SKILL that references the capability and the credential.
    skill = admin.post("/api/admin/skills", json={"name": "Canary skill", "description": "leak probe", "category": "writing"})
    assert skill.status_code == 201, skill.text
    skill_id = skill.json()["id"]
    manifest = {
        "name": "Canary skill",
        "description": "leak probe",
        "instructions": "Call text.echo.",
        "plan": [{"id": "step1", "kind": "capability", "capability": "text.echo", "input": {"prompt": "{{ input.topic }}"}, "save_as": "out"}],
        "dependencies": [{"capability": "text.echo", "version": 1, "required": True}],
        "permissions": {"network": ["api.example.com"], "files": [], "credentials": [credential_id]},
        "examples": [{"input": {"topic": "x"}}],
    }
    version = admin.post(f"/api/admin/skills/{skill_id}/versions", json={"manifest": manifest, "version": "2"})
    assert version.status_code == 201, version.text
    published = admin.post(f"/api/admin/skills/{skill_id}/versions/{version.json()['id']}/publish")
    assert published.status_code == 200, published.text

    # The envelope that must never appear anywhere else.
    row = db.query_one("SELECT secret_ciphertext FROM credentials WHERE id = ?", (credential_id,))
    envelope = row["secret_ciphertext"]
    assert envelope and RAW_SECRET not in envelope, "ciphertext must not contain the plaintext"
    assert db.query_scalar("SELECT COUNT(*) FROM credentials WHERE secret_ciphertext = ?", (envelope,)) == 1

    surfaces: dict[str, str] = {}

    # 1. every stored text column of every table
    leaks_in_db = [
        f"{table}.{column}={value[:80]}"
        for table, column, value in _db_text_values()
        if RAW_SECRET in value or envelope in value
    ]
    assert not leaks_in_db, "the secret or its envelope leaked into another column:\n" + "\n".join(leaks_in_db)

    # 2. the audit log, invocation records and task rows through their APIs
    for label, url in (
        ("audit", "/api/admin/audit?limit=200"),
        ("invocations", "/api/admin/invocations?limit=50"),
        ("tasks", "/api/admin/tasks?limit=50"),
        ("credentials", "/api/admin/credentials?limit=50"),
        ("logs", "/api/admin/logs"),
        ("usage", "/api/admin/usage"),
        ("overview", "/api/admin/overview"),
    ):
        response = admin.get(url)
        assert response.status_code == 200, f"{label}: {response.status_code} {response.text[:200]}"
        surfaces[label] = response.text

    invocation_id = invoked.json()["invocation_id"]
    invocation_detail = admin.get(f"/api/admin/invocations/{invocation_id}")
    assert invocation_detail.status_code == 200, invocation_detail.text
    surfaces["invocation_detail"] = invocation_detail.text

    # 3. exported SKILL (both formats) and the external bundle
    for fmt in ("skill", "json"):
        exported = admin.get(f"/api/admin/skills/{skill_id}/export?format={fmt}")
        assert exported.status_code == 200, exported.text
        surfaces[f"skill_export_{fmt}"] = exported.text

    bundle = client.get("/api/v1/skill", headers=agent["headers"])
    assert bundle.status_code == 200, bundle.text
    surfaces["external_skill_bundle"] = bundle.text
    assert "includes_credentials" in bundle.json()

    # 4. every admin GET endpoint that can be addressed with our ids
    from app.main import create_app

    app = create_app()
    ids = {
        "capability_id": capability["capability_id"],
        "version_id": capability["version_id"],
        "credential_id": credential_id,
        "provider_id": provider_id,
        "skill_id": skill_id,
        "route_id": capability["route_id"],
        "invocation_id": invocation_id,
        "task_id": "",
    }
    scanned = 0
    for template, url in _admin_get_routes(app, ids):
        response = admin.get(url)
        assert response.status_code < 500, f"{template} failed: {response.status_code} {response.text[:200]}"
        surfaces[f"GET {template}"] = response.text
        scanned += 1
    assert scanned >= 15, f"only {scanned} admin GET endpoints were scanned"

    offenders = [name for name, text in surfaces.items() if RAW_SECRET in text or envelope in text]
    assert not offenders, "the secret or its ciphertext envelope leaked into: " + ", ".join(sorted(offenders))

    # Positive control: the value really is stored (encrypted) and usable.
    assert db.query_one("SELECT hint FROM credentials WHERE id = ?", (credential_id,))["hint"]
    from app.services import credentials as credential_service

    secrets = credential_service.get_secret_map(credential_id)
    assert secrets.values["api_key"] == RAW_SECRET
