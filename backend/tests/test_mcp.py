"""MCP adapter tests `/api/mcp` (CONTRACT §7).

Scope note: as in `test_execution.py`, the outbound provider call is a *fake*
(`GuardedResponse`). These tests prove the adapter's protocol behaviour —
version negotiation, tool naming/schemas, JSON-RPC error shapes, session
headers, transport auth and that every `tools/call` goes through
`execution.invoke` (surface="mcp"). They are not a claim about real providers.

Deliberately self-contained (its own seeding helpers) so this file can be run on
its own: `pytest backend/tests/test_mcp.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

FAKE_VENDOR_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"
NEWEST_PROTOCOL = "2025-06-18"
OLDER_PROTOCOL = "2024-11-05"


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


def json_response(body: Any, *, status_code: int = 200) -> Any:
    from app.services.network import GuardedResponse

    return GuardedResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
        url="https://api.example.com/v1/chat",
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
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import mcp
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(mcp.router)

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
    config["modes"] = {"sync": True, "async": False, "stream": False}
    config["request"]["url"] = "https://api.example.com/v1/chat"
    config["request"]["body_template"] = {"input": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "max_words": {"type": "integer"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["output_schema"] = {"type": "object", "properties": {"summary": {"type": "string"}}}
    config["response"] = {
        "kind": "json",
        "extract": {"result": "$.choices[0].message.content"},
        "artifact": None,
    }
    config["limits"] = {"timeout_s": 30}
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
    config["response"] = {"kind": "json", "extract": {"result": "$.output"}, "artifact": None}
    config["async"] = {
        "enabled": True,
        "submit_extract": {"task_id": "$.id"},
        "status": {
            "url": "https://api.example.com/v1/render/{task_id}",
            "method": "GET",
            "extract": {"status": "$.status", "progress": "$.progress"},
            "map": {
                "queued": ["queued"],
                "running": ["running"],
                "succeeded": ["succeeded"],
                "failed": ["failed"],
                "cancelled": ["cancelled"],
            },
            "poll_interval_s": 5,
            "result_extract": {"result": "$.output"},
        },
        "cancel": {"url": "", "method": "POST", "extract": {"status": ""}},
    }
    config["limits"] = {"timeout_s": 30, "max_duration_s": 300}
    return config


def _seed_capability(slug: str, config: dict[str, Any], *, provider_id: str, credential_id: str) -> str:
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
            "type": "text" if config["modes"].get("sync") else "video",
            "description": f"{slug} capability",
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
    return capability_id


@pytest.fixture()
def world(isolated, upstream):
    from app.core import db
    from app.security import tokens as token_security
    from app.services import credentials

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
    credential_id = credentials.create_credential(
        provider_id=provider_id,
        name="Acme key",
        auth_type="api_key",
        secrets={"api_key": FAKE_VENDOR_KEY},
    )["id"]

    _seed_capability("text.summarize", _sync_config(), provider_id=provider_id, credential_id=credential_id)
    _seed_capability("video.render", _async_config(), provider_id=provider_id, credential_id=credential_id)
    _seed_capability("image.generate", _sync_config(), provider_id=provider_id, credential_id=credential_id)

    agent_id = db.new_id("agent")
    db.insert(
        "agents",
        {
            "id": agent_id,
            "name": "mcp-agent",
            "slug": f"mcp-agent-{agent_id[-6:]}",
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
            "name": "mcp token",
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(["text.summarize", "video.render"]),
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
    return {
        "agent_id": agent_id,
        "token": issued.token,
        "headers": {"authorization": f"Bearer {issued.token}"},
        "upstream": upstream,
    }


# ------------------------------------------------------------------- helpers --
def _rpc(client, payload: Any, *, headers: dict[str, str] | None = None, **kwargs: Any):
    return client.post("/api/mcp", json=payload, headers=headers or {}, **kwargs)


def _ok_chat(prompt: str = "hello") -> dict[str, Any]:
    return {"choices": [{"message": {"content": f"summary of {prompt}"}}]}


def _tool_names(client, world) -> list[str]:
    response = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=world["headers"])
    assert response.status_code == 200, response.text
    return [tool["name"] for tool in response.json()["result"]["tools"]]


# -------------------------------------------------------------------- tests ---
def test_initialize_negotiates_supported_protocol_versions(client, world):
    for requested in (NEWEST_PROTOCOL, OLDER_PROTOCOL):
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": requested,
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.1"},
                },
            },
            headers=world["headers"],
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["protocolVersion"] == requested
        assert result["capabilities"]["tools"] == {"listChanged": False}
        assert result["capabilities"]["resources"] == {}
        assert result["capabilities"]["prompts"] == {}
        assert result["serverInfo"]["name"] == "skill-router"
        assert result["serverInfo"]["version"]


def test_initialize_falls_back_to_the_newest_protocol(client, world):
    response = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        headers=world["headers"],
    )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == NEWEST_PROTOCOL


def test_transport_auth_failure_is_plain_http_401_not_jsonrpc(client, world):
    missing = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert missing.status_code == 401
    body = missing.json()
    assert "jsonrpc" not in body
    assert body["error"]["code"] == "unauthenticated"

    invalid = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"authorization": "Bearer sr_live_not-a-real-token"},
    )
    assert invalid.status_code == 401
    assert "jsonrpc" not in invalid.json()
    assert invalid.json()["error"]["code"] == "invalid_token"


def test_ping_notification_and_session_round_trip(client, world):
    init = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": NEWEST_PROTOCOL}},
        headers=world["headers"],
    )
    session_id = init.headers.get("mcp-session-id")
    assert session_id, "initialize must mint a Mcp-Session-Id"

    acknowledged = _rpc(
        client,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**world["headers"], "mcp-session-id": session_id},
    )
    assert acknowledged.status_code == 204
    assert acknowledged.headers.get("mcp-session-id") == session_id

    ping = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers={**world["headers"], "mcp-session-id": session_id},
    )
    assert ping.status_code == 200
    assert ping.json() == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert ping.headers.get("mcp-session-id") == session_id

    deleted = client.delete("/api/mcp", headers={**world["headers"], "mcp-session-id": session_id})
    assert deleted.status_code == 204


def test_get_is_405_with_allow_header(client):
    response = client.get("/api/mcp")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST, DELETE"
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_tools_list_uses_double_underscore_names_and_schemas(client, world):
    response = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=world["headers"])
    assert response.status_code == 200
    result = response.json()["result"]
    names = {tool["name"] for tool in result["tools"]}
    # Dots become "__" and the ungranted capability is absent.
    assert names == {"text__summarize", "video__render"}
    assert result["nextCursor"] is None

    summarize = next(tool for tool in result["tools"] if tool["name"] == "text__summarize")
    schema = summarize["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["prompt"]
    assert schema["additionalProperties"] is False
    assert "text" in summarize["description"]
    assert "Modes:" in summarize["description"]

    render = next(tool for tool in result["tools"] if tool["name"] == "video__render")
    assert "task id" in render["description"]

    assert slug_back("text__summarize") == "text.summarize"

    # Cursor pagination is accepted (and a bogus cursor is a -32602, not a crash).
    paged = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": "1"}},
        headers=world["headers"],
    )
    assert paged.status_code == 200
    assert len(paged.json()["result"]["tools"]) == 1
    assert paged.json()["result"]["nextCursor"] is None

    bad_cursor = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"cursor": "not-a-cursor"}},
        headers=world["headers"],
    )
    assert bad_cursor.json()["error"]["code"] == -32602


def slug_back(name: str) -> str:
    from app.api.mcp import slug_from_tool, tool_name

    assert tool_name("text.summarize") == name
    return slug_from_tool(name)


def test_tools_call_success_goes_through_execution(client, world):
    from app.core import db

    world["upstream"].respond(lambda *_: json_response(_ok_chat("via-mcp")))
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "text__summarize", "arguments": {"prompt": "via-mcp"}},
        },
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["status"] == "succeeded"
    assert structured["result"] == "summary of via-mcp"
    assert structured["capability"] == "text.summarize"
    assert structured["request_id"].startswith("req_")
    # content[0].text is the JSON result the client renders.
    assert json.loads(result["content"][0]["text"]) == "summary of via-mcp"

    row = db.query_one("SELECT * FROM invocations WHERE request_id = ?", (structured["request_id"],))
    assert row["surface"] == "mcp"
    assert row["agent_id"] == world["agent_id"]
    assert row["caller_type"] == "agent"


def test_tools_call_async_returns_task_id(client, world):
    world["upstream"].respond(lambda *_: json_response({"id": "job_mcp_1", "status": "queued"}))
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "video__render", "arguments": {"prompt": "a sunset"}},
        },
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["task_id"]
    assert structured["poll"] == f"/api/v1/tasks/{structured['task_id']}"
    text = json.loads(result["content"][0]["text"])
    assert text["task_id"] == structured["task_id"]
    assert "asynchronous" in text["note"]


def test_unknown_tool_is_minus_32602_with_tool_not_found(client, world):
    response = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope__nothing", "arguments": {}}},
        headers=world["headers"],
    )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["code"] == "tool_not_found"
    assert world["upstream"].calls == []


def test_ungranted_tool_is_rejected(client, world):
    assert "image__generate" not in _tool_names(client, world)
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "image__generate", "arguments": {"prompt": "a cat"}},
        },
        headers=world["headers"],
    )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["code"] == "capability_not_granted"
    assert world["upstream"].calls == []


def test_tool_execution_error_is_iserror_with_platform_code(client, world):
    world["upstream"].respond(lambda *_: json_response({"error": {"code": "bad key"}}, status_code=401))
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "text__summarize", "arguments": {"prompt": "boom"}},
        },
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is True
    structured = result["structuredContent"]
    assert structured["error"]["code"] == "upstream_auth_failed"
    assert structured["request_id"].startswith("req_")
    assert json.loads(result["content"][0]["text"])["error"]["code"] == "upstream_auth_failed"


def test_invalid_arguments_are_reported_as_tool_errors(client, world):
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "text__summarize", "arguments": {"prompt": "x", "bogus": True}},
        },
        headers=world["headers"],
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "unsupported_parameter"
    assert world["upstream"].calls == []


def test_unknown_method_and_malformed_json(client, world):
    unknown = _rpc(client, {"jsonrpc": "2.0", "id": 10, "method": "resources/read"}, headers=world["headers"])
    assert unknown.status_code == 200
    assert unknown.json()["error"]["code"] == -32601

    broken = client.post(
        "/api/mcp",
        content=b"{not json",
        headers={**world["headers"], "content-type": "application/json"},
    )
    assert broken.status_code == 200
    assert broken.json()["error"]["code"] == -32700
    assert broken.json()["id"] is None


def test_batch_request_and_sse_encoding(client, world):
    batch = [
        {"jsonrpc": "2.0", "id": 11, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 12, "method": "prompts/list"},
    ]
    response = _rpc(client, batch, headers=world["headers"])
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert [item["id"] for item in body] == [11, 12]
    assert body[1]["result"] == {"prompts": [], "nextCursor": None}

    sse = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 13, "method": "resources/list"},
        headers={**world["headers"], "accept": "text/event-stream"},
    )
    assert sse.status_code == 200
    assert sse.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in sse.text.splitlines() if line.startswith("data:")]
    payload = json.loads(lines[0][5:].strip())
    assert payload["id"] == 13
    assert payload["result"]["resources"] == []
