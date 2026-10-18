"""内部接口（Python ↔ 网关）：鉴权、凭证代理、工具代理（§9.5 / §12.3）。"""

from __future__ import annotations

from skillrouter_api.settings import settings

HEADERS = {"X-Internal-Token": settings.agent_gateway_secret}


def test_internal_requires_token(client) -> None:
    assert client.post("/internal/tools/sr_capabilities_list", json={"arguments": {}}).status_code == 401
    r = client.post(
        "/internal/tools/sr_capabilities_list",
        headers={"X-Internal-Token": "wrong"},
        json={"arguments": {}},
    )
    assert r.status_code == 401


def test_internal_capabilities_catalog(client) -> None:
    r = client.get("/internal/capabilities", headers=HEADERS)
    assert r.status_code == 200
    assert "items" in r.json()


def test_internal_credential_materialize(client, admin, provider) -> None:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "代理凭证", "kind": "llm", "auth_type": "bearer", "secret": "sk-agent-proxy-123456"},
    )
    assert r.status_code == 201, r.text
    credential_id = r.json()["id"]
    resp = client.post(
        "/internal/credentials/materialize",
        headers=HEADERS,
        json={"credential_ref": credential_id, "purpose": "agent", "ttl_s": 60},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["secret"] == "sk-agent-proxy-123456"
    assert body["expires_in_s"] == 60
    assert body["provider_id"] == provider["id"]


def test_materialize_requires_active_credential(client, admin, provider) -> None:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "停用凭证", "kind": "llm", "auth_type": "bearer", "secret": "sk-disabled-0001"},
    )
    credential_id = r.json()["id"]
    client.post(f"/api/admin/credentials/{credential_id}/disable", headers=admin["headers"])
    resp = client.post(
        "/internal/credentials/materialize",
        headers=HEADERS,
        json={"credential_ref": credential_id},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CREDENTIAL_DISABLED"


def test_credential_materialize_rejects_ttl_over_60(client) -> None:
    r = client.post(
        "/internal/credentials/materialize",
        headers=HEADERS,
        json={"credential_ref": "crd_x", "ttl_s": 3600},
    )
    assert r.status_code == 400


def test_internal_audit_writes_entry(client, admin) -> None:
    r = client.post(
        "/internal/audit",
        headers=HEADERS,
        json={"action": "agent.notice", "object_type": "agent", "summary": {"x": 1}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_tool_proxy_rejects_unregistered_tool(client) -> None:
    r = client.post(
        "/internal/tools/bash",
        headers=HEADERS,
        json={"arguments": {"cmd": "rm -rf /"}},
    )
    assert r.status_code == 404
    assert "白名单" in r.json()["error"]["hint"]


def test_tool_proxy_capabilities_list(client) -> None:
    r = client.post(
        "/internal/tools/sr_capabilities_list", headers=HEADERS, json={"arguments": {}}
    )
    assert r.status_code == 200
    assert "items" in r.json()


def test_tool_proxy_records_finding(client) -> None:
    r = client.post(
        "/internal/tools/sr_record_finding",
        headers=HEADERS,
        json={"arguments": {"kind": "auth", "confidence": "inferred", "text": "推测使用 Bearer"}},
    )
    assert r.status_code == 200
    assert r.json()["finding_id"].startswith("fnd_")


def test_tool_proxy_request_approval(client, admin) -> None:
    r = client.post(
        "/internal/tools/sr_request_approval",
        headers=HEADERS,
        json={
            "arguments": {
                "action": "delete_capability",
                "target": "cap_1",
                "impact": {"skills": 2},
            }
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "pending"
    approvals = client.get("/api/admin/agent/approvals", headers=admin["headers"])
    assert approvals.status_code == 200
    assert any(a["id"] == body["approval_id"] for a in approvals.json()["items"])


def test_tool_proxy_propose_and_test_draft(client, admin, provider, credential, monkeypatch) -> None:
    """端到端：Agent 提草案 → 结构校验 → 真实（假上游）调用测试。"""
    from tests.fixtures.builders import capability_payload
    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    definition = capability_payload(provider["id"], credential["id"], key="text.generate.agentdraft")
    resp = client.post(
        "/internal/tools/sr_propose_capability_draft",
        headers=HEADERS,
        json={"arguments": {**definition, "evidence": [{"source": "docs"}]}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_id"]
    assert not [i for i in body["issues"] if i["level"] == "error"], body["issues"]

    test_resp = client.post(
        "/internal/tools/sr_test_capability",
        headers=HEADERS,
        json={"arguments": {"draft_id": body["draft_id"], "input": {"prompt": "hello"}}},
    )
    assert test_resp.status_code == 200, test_resp.text
    result = test_resp.json()
    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["test_run_id"]


def test_tool_proxy_validate_skill(client, admin) -> None:
    created = client.post(
        "/api/admin/skills",
        headers=admin["headers"],
        json={
            "name": "proxy-skill",
            "title": "代理校验",
            "description": "d",
            "instructions": "i",
            "execution_plan": {"steps": [{"id": "s1", "type": "transform"}]},
        },
    ).json()
    r = client.post(
        "/internal/tools/sr_validate_skill",
        headers=HEADERS,
        json={"arguments": {"skill_id": created["id"]}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
