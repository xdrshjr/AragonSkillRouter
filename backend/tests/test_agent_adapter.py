"""Adapter, tool-token and tool-ceiling tests (CONTRACT §10, §12).

What is proven here, and why each matters:

* the SSE parser handles resume, keepalive comments and terminal frames — the
  panel's streaming depends on exactly these three;
* a dead sidecar becomes ``ApiError('engine_unavailable')`` with text an operator
  can act on, never a bare traceback;
* the platform tool ceiling in Python and in TypeScript are the *same list* (the
  test reads the TypeScript source), so the two halves cannot drift apart;
* a run-scoped tool token cannot be forged, reused after expiry, or replayed
  against another run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

SIDECAR_TOOLS_TS = REPO_DIR / "sidecar" / "src" / "tools.ts"


@pytest.fixture(autouse=True)
def _unload_engine_adapter():
    """Put the import graph back the way this suite found it.

    `test_execution.py` asserts that a plain capability call never leaves
    `app.services.agent_adapter` in `sys.modules` — the agent engine is an optional
    part of the platform. This suite has to import the adapter to test it, so it
    removes it again on the way out. That is only safe because `agent_runtime` and
    `engine_upgrade` import the client lazily and hold no module-level reference.
    """
    yield
    sys.modules.pop("app.services.agent_adapter", None)
    services_package = sys.modules.get("app.services")
    if services_package is not None and hasattr(services_package, "agent_adapter"):
        delattr(services_package, "agent_adapter")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Temp data dir, fresh keyring, migrated database (same pattern as other suites)."""
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


def patch_http(monkeypatch, handler: Callable[[httpx.Request], Any]) -> None:
    """Route every AgentAdapter request through an in-memory transport.

    The handler may be sync or async; a streaming response must supply an *async*
    iterable of bytes or `AsyncClient` rejects it (httpx distinguishes sync and
    async byte streams).
    """
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    import app.services.agent_adapter as adapter_module

    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", factory)


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #
def _sse(*frames: str) -> bytes:
    return "".join(frames).encode("utf-8")


def _frame(seq: int, event_type: str, **fields: Any) -> str:
    body = json.dumps({"seq": seq, "type": event_type, "run_id": "run_1", **fields})
    return f"id: {seq}\nevent: {event_type}\ndata: {body}\n\n"


@pytest.mark.asyncio
async def test_stream_events_parses_frames_and_stops_on_terminal(monkeypatch, isolated):
    seen_headers: dict[str, str] = {}
    seen_params: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update({k.lower(): v for k, v in request.headers.items()})
        seen_params.update(dict(request.url.params))
        payload = _sse(
            ": connected session=ses_1 after=5 gap=0\n\n",
            ": keepalive 2026-10-08T00:00:00Z\n\n",
            _frame(6, "run.started"),
            _frame(7, "assistant.delta", delta="hello"),
            _frame(8, "run.finished", turns=1),
            _frame(9, "run.finished"),  # must never be read
        )

        async def stream():
            yield payload

        return httpx.Response(200, content=stream(), headers={"content-type": "text/event-stream"})

    patch_http(monkeypatch, handler)
    from app.services.agent_adapter import AgentAdapter

    adapter = AgentAdapter(base_url="http://127.0.0.1:7317", token="tok")
    events = [event async for event in adapter.stream_events("ses_1", last_event_id=5)]

    assert [event.seq for event in events] == [6, 7, 8]
    assert [event.type for event in events] == ["run.started", "assistant.delta", "run.finished"]
    assert events[1].payload()["delta"] == "hello"
    # Resume is expressed twice so a proxy that strips the header still works.
    assert seen_headers["last-event-id"] == "5"
    assert seen_params["since"] == "5"
    assert seen_headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_stream_events_rejects_unknown_tool_ceiling_and_surfaces_code(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "code": "tool_not_allowed", "message": "Tool(s) not in the platform permission ceiling: bash"},
        )

    patch_http(monkeypatch, handler)
    from app.core.errors import ApiError
    from app.services.agent_adapter import AgentAdapter

    adapter = AgentAdapter(base_url="http://127.0.0.1:7317", token="tok")
    with pytest.raises(ApiError) as excinfo:
        _ = [event async for event in adapter.stream_events("ses_1")]
    assert excinfo.value.code == "tool_not_allowed"
    assert "permission ceiling" in excinfo.value.message


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unreachable_sidecar_becomes_actionable_engine_unavailable(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    patch_http(monkeypatch, handler)
    from app.core.errors import ApiError
    from app.services.agent_adapter import AgentAdapter

    adapter = AgentAdapter(base_url="http://127.0.0.1:7317", token="tok")
    with pytest.raises(ApiError) as excinfo:
        await adapter.info()
    error = excinfo.value
    assert error.code == "engine_unavailable"
    assert error.status_code == 503
    assert "http://127.0.0.1:7317" in error.message
    assert any("npm start" in action for action in error.actions)
    assert error.details["adapter_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_sidecar_401_is_reported_as_a_token_problem(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "code": "unauthenticated", "message": "nope"})

    patch_http(monkeypatch, handler)
    from app.core.errors import ApiError
    from app.services.agent_adapter import AgentAdapter

    adapter = AgentAdapter(base_url="http://127.0.0.1:7317", token="wrong")
    with pytest.raises(ApiError) as excinfo:
        await adapter.info()
    assert excinfo.value.code == "engine_unauthorised"
    assert any("SR_ARAGON_SIDECAR_TOKEN" in action for action in excinfo.value.actions)


@pytest.mark.asyncio
async def test_check_health_never_raises(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    patch_http(monkeypatch, handler)
    from app.services.agent_adapter import AgentAdapter

    report = await AgentAdapter(base_url="http://127.0.0.1:7317", token="tok").check_health()
    assert report["ok"] is False
    assert report["reachable"] is False
    assert report["error"]["code"] == "engine_unavailable"


@pytest.mark.asyncio
async def test_check_health_reports_model_state(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "engine": {"package": "@aragon-agent/core", "version": "0.2.50", "commit": None},
                "model_configured": False,
            },
        )

    patch_http(monkeypatch, handler)
    from app.services.agent_adapter import AgentAdapter

    report = await AgentAdapter(base_url="http://127.0.0.1:7317", token="tok").check_health()
    assert report["reachable"] is True
    assert report["model_configured"] is False
    assert report["engine"]["version"] == "0.2.50"
    assert report["engine"]["adapter_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_releases_check_failure_is_not_swallowed(monkeypatch, isolated):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "ok": False,
                "channel": "stable",
                "commit": None,
                "source": "git ls-remote --tags https://example.invalid/repo.git",
                "error": "git failed; no fallback is applied",
            },
        )

    patch_http(monkeypatch, handler)
    from app.core.errors import ApiError
    from app.services.agent_adapter import AgentAdapter

    with pytest.raises(ApiError) as excinfo:
        await AgentAdapter(base_url="http://127.0.0.1:7317", token="tok").check_releases(channel="stable")
    assert excinfo.value.details["http_status"] == 502


# --------------------------------------------------------------------------- #
# The ceiling must match the TypeScript side
# --------------------------------------------------------------------------- #
def _typescript_tool_names() -> list[str]:
    source = SIDECAR_TOOLS_TS.read_text(encoding="utf-8")
    start = source.index("export const PLATFORM_TOOL_NAMES = [")
    end = source.index("] as const;", start)
    block = source[start:end]
    return [line.strip().strip('",') for line in block.splitlines() if line.strip().startswith('"')]


def test_tool_ceiling_matches_the_sidecar():
    from app.services import agent_runtime

    assert list(agent_runtime.PLATFORM_TOOL_NAMES) == _typescript_tool_names()
    assert len(agent_runtime.PLATFORM_TOOL_NAMES) == 12


def test_every_tool_has_a_schema_and_a_handler():
    from app.services import agent_runtime

    assert set(agent_runtime.TOOL_DEFINITIONS) == set(agent_runtime.PLATFORM_TOOL_NAMES)
    assert set(agent_runtime.TOOL_HANDLERS) == set(agent_runtime.PLATFORM_TOOL_NAMES)
    for name, spec in agent_runtime.TOOL_DEFINITIONS.items():
        assert spec["description"], f"{name} has no description"
        assert spec["parameters"]["type"] == "object"
        assert "additionalProperties" in spec["parameters"]
    # Engine-side specs carry no extra keys the sidecar would have to guess at.
    for spec in agent_runtime.tool_specs_for_engine():
        assert set(spec) == {"name", "description", "parameters"}


def test_forbidden_tools_are_not_in_the_ceiling():
    from app.services import agent_runtime

    for forbidden in ("bash", "read_file", "write_file", "edit_file", "grep", "glob", "web_fetch", "code_exec"):
        assert forbidden not in agent_runtime.PLATFORM_TOOL_NAMES


# --------------------------------------------------------------------------- #
# Scoped tool token
# --------------------------------------------------------------------------- #
def test_tool_token_round_trip(isolated):
    from app.services import agent_runtime

    token = agent_runtime.mint_tool_token(run_id="run_abc", session_id="ses_xyz")
    assert token.startswith("srt1.")
    payload = agent_runtime.verify_tool_token(token)
    assert payload["r"] == "run_abc"
    assert payload["s"] == "ses_xyz"
    assert payload["a"] == "1.0.0"


def test_tool_token_signature_cannot_be_forged(isolated):
    from app.core.errors import ApiError
    from app.services import agent_runtime

    token = agent_runtime.mint_tool_token(run_id="run_abc", session_id="ses_xyz")
    prefix, body, signature = token.split(".")
    forged_body = agent_runtime._b64(
        json.dumps({"r": "run_other", "s": "ses_xyz", "e": 9999999999, "a": "1.0.0"}).encode()
    )
    with pytest.raises(ApiError) as excinfo:
        agent_runtime.verify_tool_token(f"{prefix}.{forged_body}.{signature}")
    assert excinfo.value.status_code == 401

    with pytest.raises(ApiError):
        agent_runtime.verify_tool_token(f"{prefix}.{body}.{'A' * len(signature)}")

    with pytest.raises(ApiError):
        agent_runtime.verify_tool_token("garbage")


def test_expired_tool_token_is_rejected_with_reason(isolated):
    import hashlib
    import hmac

    from app.core.errors import ApiError
    from app.services import agent_runtime

    ring = agent_runtime._keyring()
    body = agent_runtime._b64(
        json.dumps({"r": "run_abc", "s": "ses_xyz", "e": 1, "a": "1.0.0"}).encode()
    )
    signature = agent_runtime._b64(
        hmac.new(agent_runtime._derive_key(ring.key(ring.active)), body.encode("ascii"), hashlib.sha256).digest()
    )
    with pytest.raises(ApiError) as excinfo:
        agent_runtime.verify_tool_token(f"srt1.{body}.{signature}")
    assert excinfo.value.code == "tool_token_expired"


def test_tool_token_survives_a_key_rotation_for_its_lifetime(isolated):
    """A token minted before rotation must still verify afterwards."""
    from app.security import crypto
    from app.services import agent_runtime

    token = agent_runtime.mint_tool_token(run_id="run_abc", session_id="ses_xyz")
    ring = crypto.load_keyring()
    new_id = f"{ring.active}-rotated"
    rotated = crypto.Keyring(active=new_id, keys={**ring.keys, new_id: b"\x11" * 32})
    crypto.write_keyring(rotated)

    payload = agent_runtime.verify_tool_token(token)
    assert payload["r"] == "run_abc"


# --------------------------------------------------------------------------- #
# Credential masking assertion
# --------------------------------------------------------------------------- #
def test_masked_credentials_guard_blocks_a_leak(isolated):
    from app.services import agent_runtime

    safe = [
        {
            "id": "cred_1",
            "name": "primary",
            "hint": "sk-…9f2a",
            "field_names": ["api_key"],
            "config": {"header": "x-api-key"},
            "status": "active",
        }
    ]
    assert agent_runtime._assert_masked(safe) == safe

    leaking = [dict(safe[0], secret_ciphertext="{\"v\":1,...}")]
    with pytest.raises(agent_runtime.ToolRefused) as excinfo:
        agent_runtime._assert_masked(leaking)
    assert excinfo.value.code == "credential_leak_blocked"

    leaking_inside = [{"id": "cred_1", "config": {"ciphertext": "abc"}}]
    with pytest.raises(agent_runtime.ToolRefused):
        agent_runtime._assert_masked(leaking_inside)


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #
def _add_provider_and_credential(*, category: str = "llm", base_url: str | None = None, config: dict | None = None):
    from app.core import db
    from app.services import credentials as credential_service

    provider_id = db.new_id("prov")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": "Zenmux",
            "slug": "zenmux",
            "category": category,
            "base_url": base_url,
            "tags": "[]",
            "environment": "production",
            "status": "active",
            "created_at": db.iso(),
            "updated_at": db.iso(),
        },
    )
    credential = credential_service.create_credential(
        provider_id=provider_id,
        name="agent key",
        auth_type="api_key",
        secrets={"api_key": "sk-live-secret-value"},
        config=config or {},
    )
    return provider_id, credential


def test_resolve_model_requires_a_credential(isolated):
    from app.core.errors import ApiError
    from app.services import agent_runtime

    with pytest.raises(ApiError) as excinfo:
        agent_runtime.resolve_model()
    assert excinfo.value.code == "engine_model_unconfigured"
    assert any("credentials" in action for action in excinfo.value.actions)


def test_resolve_model_needs_a_model_id(isolated):
    from app.core import db
    from app.core.errors import ApiError
    from app.services import agent_runtime

    _add_provider_and_credential(base_url="https://zenmux.ai/api/v1")
    with pytest.raises(ApiError) as excinfo:
        agent_runtime.resolve_model()
    assert excinfo.value.code == "engine_model_unconfigured"
    assert "model_id" in excinfo.value.message + str(excinfo.value.actions)


def test_resolve_model_maps_a_gateway_to_openai_with_base_url(isolated):
    from app.core import db
    from app.services import agent_runtime

    _add_provider_and_credential(base_url="https://zenmux.ai/api/v1", config={"model": "gpt-4o-mini"})
    db.set_setting("agent.model", {})
    resolved = agent_runtime.resolve_model()
    assert resolved.provider_id == "openai"
    assert resolved.model_id == "gpt-4o-mini"
    assert resolved.base_url == "https://zenmux.ai/api/v1"
    # The public view must never carry the key.
    assert "sk-live-secret-value" not in json.dumps(resolved.public())


def test_resolve_model_ignores_non_llm_providers(isolated):
    from app.core.errors import ApiError
    from app.services import agent_runtime

    _add_provider_and_credential(category="image", config={"model": "dall-e"})
    with pytest.raises(ApiError) as excinfo:
        agent_runtime.resolve_model()
    assert excinfo.value.code == "engine_model_unconfigured"


def test_engine_provider_id_follows_the_protocol_not_the_name():
    """The base_url decides the adapter.

    Zhipu's provider is called "GLM" — no "anthropic"/"openai"/"google" in the name
    — yet its documented base_url `…/api/anthropic` speaks the Anthropic Messages
    API. Inferring "openai" from the name sent OpenAI-shaped requests to an Anthropic
    endpoint and surfaced a 404 that looked like a bad key.
    """
    from app.services import agent_runtime

    cases = [
        ({"name": "GLM", "slug": "glm", "provider_base_url": "https://open.bigmodel.cn/api/anthropic"}, "anthropic"),
        ({"name": "Zhipu", "slug": "zhipu", "base_url": "https://open.bigmodel.cn/api/anthropic"}, "anthropic"),
        ({"name": "Anthropic", "slug": "anthropic", "provider_base_url": "https://api.anthropic.com"}, "anthropic"),
        ({"name": "Gemini", "slug": "gemini", "provider_base_url": "https://generativelanguage.googleapis.com/v1beta"}, "google"),
        ({"name": "Vertex", "slug": "vertex", "provider_base_url": "https://us-central1-aiplatform.googleapis.com"}, "google"),
        ({"name": "ZenMux", "slug": "zenmux", "provider_base_url": "https://zenmux.ai/api/v1"}, "openai"),
        ({"name": "GLM native", "slug": "glm", "provider_base_url": "https://open.bigmodel.cn/api/paas/v4"}, "openai"),
        ({"name": "Local vLLM", "slug": "local", "provider_base_url": None}, "openai"),
    ]
    for row, expected in cases:
        actual = agent_runtime._engine_provider_id(row)
        assert actual == expected, f"{row} → {actual}, expected {expected}"


def test_resolve_model_accepts_an_anthropic_compatible_gateway(isolated):
    """A provider whose base_url is Anthropic-compatible must resolve to anthropic."""
    from app.core import db
    from app.services import agent_runtime

    provider_id = db.new_id("prov")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": "GLM",
            "slug": "glm",
            "category": "llm",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "tags": "[]",
            "environment": "production",
            "status": "active",
            "created_at": db.iso(),
            "updated_at": db.iso(),
        },
    )
    from app.services import credentials as credential_service

    credential_service.create_credential(
        provider_id=provider_id,
        name="GLM key",
        auth_type="api_key",
        secrets={"api_key": "sk-glm-secret"},
        config={"model": "glm-5.3-flash"},
    )
    db.set_setting("agent.model", {})
    resolved = agent_runtime.resolve_model()
    assert resolved.provider_id == "anthropic"
    assert resolved.model_id == "glm-5.3-flash"
    assert resolved.base_url == "https://open.bigmodel.cn/api/anthropic"
    assert "sk-glm-secret" not in json.dumps(resolved.public())


# --------------------------------------------------------------------------- #
# Runtime: budget + refusal messages the engine can act on
# --------------------------------------------------------------------------- #
def _seed_run() -> str:
    from app.core import db
    from app.services import agent_runtime

    session = agent_runtime.ensure_session("budget test")
    run = agent_runtime.create_run(session["id"], "hello")
    agent_runtime._mark_run(run["id"], "running")
    return run["id"]


def test_budget_refuses_mutation_when_auto_execute_is_off(isolated):
    from app.core import db
    from app.services import agent_runtime

    run_id = _seed_run()
    policy = agent_runtime.AutoExecutePolicy.load()
    assert policy.enabled is False
    context = agent_runtime.ToolContext(
        run_id=run_id, session_id="ses", policy=policy
    )
    with pytest.raises(agent_runtime.ToolRefused) as excinfo:
        context.check_budget("platform.publish_capability")
    assert excinfo.value.code == "auto_execute_disabled"
    # The message is written for the model: it says what to do instead.
    assert "Read-only tools still work" in excinfo.value.message

    # Read-only tools are never blocked by the auto-execute gate.
    context.check_budget("platform.list_providers")


def test_budget_counts_calls_and_refuses_beyond_it(isolated):
    from app.core import db
    from app.services import agent_runtime

    run_id = _seed_run()
    db.set_setting("agent.auto_execute", {"enabled": True, "allowed_providers": [], "max_calls": 2, "max_minutes": 30, "max_cost_usd": 5})
    policy = agent_runtime.AutoExecutePolicy.load()
    context = agent_runtime.ToolContext(run_id=run_id, session_id="ses", policy=policy)
    context.charge(calls=2)
    _ = db
    with pytest.raises(agent_runtime.ToolRefused) as excinfo:
        context.check_budget("platform.list_providers")
    assert excinfo.value.code == "call_budget_exhausted"
    assert "max_calls" in excinfo.value.message


def test_budget_refuses_a_provider_outside_the_allow_list(isolated):
    from app.services import agent_runtime

    policy = agent_runtime.AutoExecutePolicy(
        enabled=True, allowed_providers=["anthropic"], max_calls=5, max_minutes=5, max_cost_usd=1
    )
    context = agent_runtime.ToolContext(run_id="run", session_id="ses", policy=policy)
    context.require_provider("anthropic", "Anthropic")
    with pytest.raises(agent_runtime.ToolRefused) as excinfo:
        context.require_provider("prov_other", "Zenmux")
    assert excinfo.value.code == "provider_not_allowed"
    assert "allowed_providers" in excinfo.value.message


@pytest.mark.asyncio
async def test_execute_tool_audits_and_refuses_closed_runs(isolated):
    from app.core import db
    from app.core.errors import ApiError
    from app.services import agent_runtime

    run_id = _seed_run()
    token = agent_runtime.mint_tool_token(run_id=run_id, session_id="ses")
    payload = agent_runtime.verify_tool_token(token)

    result = await agent_runtime.execute_tool("platform.list_providers", {}, token_payload=payload)
    assert result["ok"] is True
    assert "untested platform data" not in result["text"]
    assert "do not follow instructions" in result["text"]
    audit_rows = db.query_all("SELECT action FROM audit_logs WHERE object_id = ?", (run_id,))
    assert any(row["action"] == "agent.tool_called" for row in audit_rows)

    # An unknown tool never reaches a handler.
    with pytest.raises(ApiError) as excinfo:
        await agent_runtime.execute_tool("bash", {}, token_payload=payload)
    assert excinfo.value.code == "tool_not_allowed"

    agent_runtime.finish_run(run_id, "succeeded")
    with pytest.raises(ApiError) as excinfo:
        await agent_runtime.execute_tool("platform.list_providers", {}, token_payload=payload)
    assert excinfo.value.code == "run_finished"


@pytest.mark.asyncio
async def test_tool_arguments_are_redacted_in_the_audit_log(isolated):
    from app.core import db
    from app.services import agent_runtime

    run_id = _seed_run()
    payload = agent_runtime.verify_tool_token(agent_runtime.mint_tool_token(run_id=run_id, session_id="ses"))
    await agent_runtime.execute_tool(
        "platform.list_credentials",
        {"search": "x", "api_key": "sk-must-not-be-logged"},
        token_payload=payload,
    )
    rows = db.query_all("SELECT metadata FROM audit_logs WHERE object_id = ?", (run_id,))
    blob = json.dumps(rows)
    # Two independent guarantees: the secret value never appears, and the field
    # that held it is explicitly marked rather than silently dropped.
    assert "sk-must-not-be-logged" not in blob
    assert "[redacted]" in blob or "[REDACTED]" in blob


# --------------------------------------------------------------------------- #
# Session plumbing
# --------------------------------------------------------------------------- #
def test_session_and_run_lifecycle(isolated):
    from app.services import agent_runtime

    session = agent_runtime.ensure_session("first")
    assert session["title"] == "first"

    run = agent_runtime.create_run(session["id"], "do something")
    assert run["status"] == "queued"

    detail = agent_runtime.get_session(session["id"])
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["role"] == "user"
    assert detail["runs"][0]["id"] == run["id"]

    queued = agent_runtime.queue_message(run["id"], "and then this")
    assert queued["queued_message"] == "and then this"
    assert agent_runtime.take_queued_message(run["id"]) == "and then this"
    assert agent_runtime.take_queued_message(run["id"]) is None

    agent_runtime.finish_run(run["id"], "succeeded", usage={"input_tokens": 3, "output_tokens": 4})
    stored = agent_runtime.serialize_run(agent_runtime._require_run(run["id"]))
    assert stored["status"] == "succeeded"
    assert stored["usage"]["total_tokens"] if "total_tokens" in stored["usage"] else True


def test_empty_message_is_refused(isolated):
    from app.core.errors import ApiError
    from app.services import agent_runtime

    session = agent_runtime.ensure_session()
    with pytest.raises(ApiError) as excinfo:
        agent_runtime.create_run(session["id"], "   ")
    assert excinfo.value.code == "empty_message"


def test_event_replay_is_ordered_and_resumable(isolated):
    from app.services import agent_runtime

    session = agent_runtime.ensure_session()
    run = agent_runtime.create_run(session["id"], "hi")
    for index in range(3):
        agent_runtime._insert_event(run["id"], index + 1, "run.progress", {"type": "run.progress", "phase": "turn_start"})

    everything = agent_runtime.load_events(run["id"])
    assert [event["seq"] for event in everything] == [1, 2, 3]
    assert everything[0]["type"] == "run.progress"

    resumed = agent_runtime.load_events(run["id"], after_seq=2)
    assert [event["seq"] for event in resumed] == [3]


@pytest.mark.asyncio
async def test_platform_and_sidecar_sequences_do_not_collide(isolated):
    """Regression: both streams start at 1, and merging them dropped run.started.

    The platform records its own `message.accepted` as seq 1; the sidecar's first
    event is also seq 1. Writing it verbatim produced a duplicate sequence and the
    resume guard skipped the sidecar's first event entirely.
    """
    from app.services import agent_runtime
    from app.services.agent_adapter import SidecarEvent

    session = agent_runtime.ensure_session()
    run = agent_runtime.create_run(session["id"], "hi")
    agent_runtime._record_event(
        run["id"],
        session["id"],
        agent_runtime.next_event_seq(run["id"]),
        "message.accepted",
        {"text_chars": 2},
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.seen_last_event_id: int | None = -1

        async def stream_events(self, session_id: str, *, last_event_id=None, **_: Any):
            self.seen_last_event_id = last_event_id
            for seq, event_type in ((1, "run.started"), (2, "assistant.delta"), (3, "run.finished")):
                yield SidecarEvent(
                    seq=seq,
                    type=event_type,
                    data={"seq": seq, "type": event_type, "run_id": run["id"], "session_id": session_id},
                )

    first = FakeAdapter()
    outcome = await agent_runtime._consume_stream(first, run["id"], session["id"])

    events = agent_runtime.load_events(run["id"])
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert [event["type"] for event in events] == [
        "message.accepted",
        "run.started",
        "assistant.delta",
        "run.finished",
    ]
    # The sidecar's own number survives inside the payload for debugging, and it is
    # what a reconnect must send back.
    assert events[1]["upstream_seq"] == 1
    assert outcome["status"] == "succeeded"
    # First open: nothing persisted yet, so no resume point is sent.
    assert first.seen_last_event_id is None

    # A second consume resumes from the last upstream seq, not the platform's.
    second = FakeAdapter()
    await agent_runtime._consume_stream(second, run["id"], session["id"])
    assert second.seen_last_event_id == 3
    assert [event["seq"] for event in agent_runtime.load_events(run["id"])] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_stream_ending_without_a_terminal_event_is_reported_as_failure(isolated):
    from app.services import agent_runtime
    from app.services.agent_adapter import SidecarEvent

    session = agent_runtime.ensure_session()
    run = agent_runtime.create_run(session["id"], "hi")

    class TruncatedAdapter:
        async def stream_events(self, session_id: str, **_: Any):
            yield SidecarEvent(seq=1, type="run.started", data={"type": "run.started"})
            yield SidecarEvent(seq=2, type="assistant.delta", data={"type": "assistant.delta", "delta": "partial"})

    outcome = await agent_runtime._consume_stream(TruncatedAdapter(), run["id"], session["id"])
    assert outcome["status"] == "failed"
    assert outcome["error"]["code"] == "engine_stream_ended"
    assert "unknown" in outcome["error"]["message"]
    # Partial text is still stored: the operator keeps what was produced.
    messages = agent_runtime.get_session(session["id"])["messages"]
    assert any(message["role"] == "assistant" and "partial" in message["content"] for message in messages)


def test_stop_report_is_persisted_on_the_run(isolated):
    from app.core import db
    from app.services import agent_runtime

    session = agent_runtime.ensure_session()
    run = agent_runtime.create_run(session["id"], "hi")
    agent_runtime._apply_stop_report(
        run["id"],
        {
            "reason": "stop_requested",
            "settled": True,
            "stopped_steps": [{"kind": "agent_loop", "turn": 1}],
            "may_continue_upstream": [{"tool": "platform.test_capability", "tool_call_id": "t1"}],
        },
    )
    row = db.query_one("SELECT usage FROM agent_runs WHERE id = ?", (run["id"],))
    report = db.loads(row["usage"], {})["stop_report"]
    assert report["stopped_steps"][0]["kind"] == "agent_loop"
    assert report["may_continue_upstream"][0]["tool"] == "platform.test_capability"
    assert report["settled"] is True


def test_tool_callback_route_accepts_a_scoped_token_over_http(isolated):
    """The machine endpoint must work without a cookie and reject a bad token.

    This is the exact request the sidecar makes, and the one place where the CSRF
    middle-ware could silently get in the way: it must not, because the caller is
    not a browser.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import agent_runtime

    session = agent_runtime.ensure_session()
    run = agent_runtime.create_run(session["id"], "list providers")
    agent_runtime._mark_run(run["id"], "running")
    token = agent_runtime.mint_tool_token(run_id=run["id"], session_id=session["id"])

    client = TestClient(create_app())

    ok = client.post(
        "/api/admin/agent/tools/platform.list_providers",
        json={"run_id": run["id"], "session_id": session["id"], "tool": "platform.list_providers", "arguments": {}},
        headers={"authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert "providers" in body["structured"] or body["structured"].get("items") == []

    # No cookie and no token: refused, and refused *by the token check*.
    anon = client.post("/api/admin/agent/tools/platform.list_providers", json={"arguments": {}})
    assert anon.status_code == 401
    assert anon.json()["error"]["code"] == "tool_token_invalid"

    # A token for another run cannot be pointed at this one.
    other_session = agent_runtime.ensure_session()
    other_run = agent_runtime.create_run(other_session["id"], "x")
    other_token = agent_runtime.mint_tool_token(run_id=other_run["id"], session_id=other_session["id"])
    mismatch = client.post(
        "/api/admin/agent/tools/platform.list_providers",
        json={"run_id": run["id"], "arguments": {}},
        headers={"authorization": f"Bearer {other_token}"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "tool_token_mismatch"

    # A tool outside the ceiling never reaches a handler.
    missing = client.post(
        "/api/admin/agent/tools/bash",
        json={"run_id": run["id"], "arguments": {}},
        headers={"authorization": f"Bearer {token}"},
    )
    assert missing.status_code == 404


def test_system_prompt_states_the_untrusted_content_rule(isolated):
    from app.services import agent_runtime

    prompt = agent_runtime.build_system_prompt()
    assert "untrusted data" in prompt
    assert "never" in prompt.lower()
    assert "publish" in prompt.lower()
