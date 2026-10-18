"""MCP initialize / tools.list / tools.call 与错误（§9.7）。"""

from __future__ import annotations

from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

VERSION = "2025-06-18"


def _rpc(client, headers, method, params=None, request_id=1, session=None):  # noqa: ANN001
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    extra = dict(headers)
    if session:
        extra["Mcp-Session-Id"] = session
    return client.post("/api/v1/mcp", headers=extra, json=body)


def test_initialize_negotiates_version(client, make_token) -> None:
    token = make_token()
    r = _rpc(client, token["headers"], "initialize", {"protocolVersion": VERSION})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == VERSION
    assert result["serverInfo"]["name"] == "skill-router"
    assert r.headers.get("Mcp-Session-Id")


def test_initialize_falls_back_for_unknown_version(client, make_token) -> None:
    token = make_token()
    r = _rpc(client, token["headers"], "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == VERSION


def test_ping(client, make_token) -> None:
    token = make_token()
    r = _rpc(client, token["headers"], "ping")
    assert r.status_code == 200
    assert r.json()["result"] == {}


def test_tools_list_names_are_protocol_safe(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.mcpdemo")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    init = _rpc(client, token["headers"], "initialize", {"protocolVersion": VERSION})
    session = init.headers["Mcp-Session-Id"]
    r = _rpc(client, token["headers"], "tools/list", session=session)
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "capabilities_list" in names
    assert "capability__text_generate_mcpdemo" in names
    import re

    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", n) for n in names), names


def test_tools_call_matches_http_result(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(key="text.generate.mcpcall")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    init = _rpc(client, token["headers"], "initialize", {"protocolVersion": VERSION})
    session = init.headers["Mcp-Session-Id"]
    r = _rpc(
        client,
        token["headers"],
        "tools/call",
        {"name": "capability__text_generate_mcpcall", "arguments": {"input": {"prompt": "mcp"}}},
        session=session,
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["structuredContent"]["output"] == "假的回复"
    assert not result.get("isError")
    # 与 HTTP 结果一致
    http = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "mcp"}},
    ).json()
    assert http["output"] == result["structuredContent"]["output"]


def test_tools_call_unknown_tool(client, make_token) -> None:
    token = make_token()
    init = _rpc(client, token["headers"], "initialize", {"protocolVersion": VERSION})
    session = init.headers["Mcp-Session-Id"]
    r = _rpc(client, token["headers"], "tools/call", {"name": "nope", "arguments": {}}, session=session)
    body = r.json()
    assert body["error"]["code"] == -32602


def test_tools_call_error_is_reported_as_iserror(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.mcperr")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    init = _rpc(client, token["headers"], "initialize", {"protocolVersion": VERSION})
    session = init.headers["Mcp-Session-Id"]
    r = _rpc(
        client,
        token["headers"],
        "tools/call",
        {"name": "capability__text_generate_mcperr", "arguments": {"input": {"undeclared": 1}}},
        session=session,
    )
    result = r.json()["result"]
    assert result["isError"] is True
    assert "INPUT_SCHEMA_INVALID" in result["content"][0]["text"]


def test_unknown_method(client, make_token) -> None:
    token = make_token()
    r = _rpc(client, token["headers"], "resources/list")
    assert r.json()["error"]["code"] == -32601


def test_notification_accepted(client, make_token) -> None:
    token = make_token()
    r = client.post(
        "/api/v1/mcp",
        headers=token["headers"],
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert r.status_code == 202


def test_mcp_requires_token(client) -> None:
    r = client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
