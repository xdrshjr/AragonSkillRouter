"""`/api/v1` 契约与 OpenAPI 一致性（§9.2.1 / C8）。"""

from __future__ import annotations

import json

V1_ENDPOINTS = [
    ("get", "/api/v1/health", False),
    ("get", "/api/v1/whoami", True),
    ("get", "/api/v1/capabilities", True),
    ("get", "/api/v1/capabilities/{key}", True),
    ("post", "/api/v1/capabilities/{key}/invoke", True),
    ("get", "/api/v1/tasks/{task_id}", True),
    ("post", "/api/v1/tasks/{task_id}/cancel", True),
    ("get", "/api/v1/files/{file_id}", True),
    ("post", "/api/v1/files", True),
    ("get", "/api/v1/skills", True),
    ("get", "/api/v1/skills/{name}", True),
    ("get", "/api/v1/skills/{name}/bundle", True),
    ("post", "/api/v1/mcp", True),
    ("get", "/api/v1/mcp", True),
    ("post", "/api/v1/callbacks/{provider_slug}", False),
]

ADMIN_ENDPOINTS = [
    "/api/admin/auth/bootstrap",
    "/api/admin/auth/login",
    "/api/admin/auth/logout",
    "/api/admin/auth/me",
    "/api/admin/auth/change-password",
    "/api/admin/overview",
    "/api/admin/events",
    "/api/admin/providers",
    "/api/admin/providers/{provider_id}",
    "/api/admin/providers/{provider_id}/credentials",
    "/api/admin/credentials/{credential_id}",
    "/api/admin/credentials/{credential_id}/verify",
    "/api/admin/credentials/{credential_id}/verifications",
    "/api/admin/credentials/{credential_id}/disable",
    "/api/admin/credentials/{credential_id}/enable",
    "/api/admin/credentials/{credential_id}/replace",
    "/api/admin/credentials/{credential_id}/rotate",
    "/api/admin/credentials/{credential_id}/impact",
    "/api/admin/capabilities",
    "/api/admin/capabilities/{capability_id}",
    "/api/admin/capabilities/{capability_id}/validate",
    "/api/admin/capabilities/{capability_id}/test",
    "/api/admin/capabilities/{capability_id}/publish",
    "/api/admin/capabilities/{capability_id}/deprecate",
    "/api/admin/capabilities/{capability_id}/versions",
    "/api/admin/capabilities/{capability_id}/diff",
    "/api/admin/capabilities/{capability_id}/rollback",
    "/api/admin/capabilities/{capability_id}/compatibility",
    "/api/admin/capabilities/{capability_id}/impact",
    "/api/admin/capabilities/{capability_id}/routes",
    "/api/admin/skills",
    "/api/admin/skills/{skill_id}",
    "/api/admin/skills/import/preview",
    "/api/admin/skills/import/commit",
    "/api/admin/skills/{skill_id}/test",
    "/api/admin/skills/{skill_id}/publish",
    "/api/admin/skills/{skill_id}/export",
    "/api/admin/skills/{skill_id}/enable",
    "/api/admin/skills/{skill_id}/disable",
    "/api/admin/skills/{skill_id}/dependencies",
    "/api/admin/skills/{skill_id}/preview",
    "/api/admin/access/agents",
    "/api/admin/access/agents/{agent_id}",
    "/api/admin/access/agents/{agent_id}/tokens",
    "/api/admin/access/tokens",
    "/api/admin/access/tokens/{token_id}",
    "/api/admin/calls",
    "/api/admin/calls/{request_id}",
    "/api/admin/tasks",
    "/api/admin/tasks/{task_id}",
    "/api/admin/tasks/{task_id}/cancel",
    "/api/admin/tasks/{task_id}/reconcile",
    "/api/admin/agent/sessions",
    "/api/admin/agent/sessions/{session_id}",
    "/api/admin/agent/sessions/{session_id}/messages",
    "/api/admin/agent/sessions/{session_id}/stream",
    "/api/admin/agent/sessions/{session_id}/stop",
    "/api/admin/agent/sessions/{session_id}/retry",
    "/api/admin/agent/flows",
    "/api/admin/agent/flows/{flow_id}",
    "/api/admin/agent/flows/{flow_id}/steps/{step}/run",
    "/api/admin/agent/flows/{flow_id}/publish",
    "/api/admin/agent/approvals",
    "/api/admin/agent/approvals/{approval_id}/decide",
    "/api/admin/agent/config",
    "/api/admin/agent/connectivity",
    "/api/admin/engine/status",
    "/api/admin/engine/candidates",
    "/api/admin/engine/check",
    "/api/admin/engine/upgrade",
    "/api/admin/engine/runs",
    "/api/admin/engine/runs/{run_id}",
    "/api/admin/engine/rollback",
    "/api/admin/engine/capabilities",
    "/api/admin/settings",
    "/api/admin/settings/rotate-master-key",
    "/api/admin/settings/audit",
    "/api/admin/settings/backup",
]


def test_openapi_generated(client) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Skill Router"
    assert "/api/v1/capabilities/{key}/invoke" in spec["paths"]


def test_all_v1_endpoints_documented(client) -> None:
    spec = client.get("/openapi.json").json()
    missing = []
    for method, path, _auth in V1_ENDPOINTS:
        if path not in spec["paths"] or method not in spec["paths"][path]:
            missing.append(f"{method.upper()} {path}")
    assert not missing, missing


def test_all_admin_endpoints_present(client) -> None:
    spec = client.get("/openapi.json").json()
    missing = [p for p in ADMIN_ENDPOINTS if p not in spec["paths"]]
    assert not missing, missing


def test_health_contract(client) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] in ("ok", "initializing")
    assert "version" in body
    assert "engine" in body
    assert "time" in body


def test_invoke_error_contract(client, make_token) -> None:
    token = make_token()
    r = client.post(
        "/api/v1/capabilities/text.generate.none/invoke",
        headers=token["headers"],
        json={"input": {}},
    )
    assert r.status_code == 404
    error = r.json()["error"]
    for key in ("code", "message", "hint", "retryable", "details", "request_id"):
        assert key in error


def test_capability_detail_hides_upstream_urls(client, admin, make_capability, make_token, monkeypatch) -> None:
    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.detail")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    r = client.get(f"/api/v1/capabilities/{cap['key']}", headers=token["headers"])
    assert r.status_code == 200
    body = r.json()
    assert "input_schema" in body and "output_schema" in body
    assert "routes_summary" in body
    text = json.dumps(body)
    assert "fake-upstream.test" not in text
    assert "credential" not in text.lower()


def test_etag_and_cache_control(client, make_token) -> None:
    token = make_token()
    r = client.get("/api/v1/capabilities", headers=token["headers"])
    assert r.headers["ETag"].startswith('W/"')
    assert "private" in r.headers["Cache-Control"]


def test_pagination_shape(client, admin) -> None:
    r = client.get("/api/admin/providers", headers=admin["headers"], params={"page": 1, "page_size": 5})
    body = r.json()
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["page"] == 1 and body["page_size"] == 5
