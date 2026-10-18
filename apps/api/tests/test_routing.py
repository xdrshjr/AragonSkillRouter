"""等价性、优先级、健康切换、preferred_provider、route 级决策（v2）。"""

from __future__ import annotations

from tests.fixtures.builders import capability_payload, route
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


def _two_route_payload(provider_id: str, credential_id: str, key: str) -> dict:
    definition = capability_payload(provider_id, credential_id, key=key)
    definition["routes"] = [
        route(provider_id, credential_id, "rt_primary", 0),
        route(provider_id, credential_id, "rt_backup", 10, url_suffix="/v2/generate"),
    ]
    return definition


def _mk(client, admin, provider, credential, key: str) -> dict:
    definition = _two_route_payload(provider["id"], credential["id"], key)
    r = client.post(
        "/api/admin/capabilities",
        headers=admin["headers"],
        json={"key": key, "name": definition["name"], "kind": "text.generate", "definition": definition},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _publish(client, admin, capability_id: str) -> None:
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "发布前测试"}},
    )
    assert r.json()["status"] == "succeeded", r.text
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/publish",
        headers=admin["headers"],
        json={"version": "1.0.0"},
    )
    assert r.status_code == 200, r.text


def test_primary_route_selected_by_priority(client, admin, provider, credential, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _mk(client, admin, provider, credential, "text.generate.routepk")
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "选主 route"}},
    )
    assert r.json()["status"] == "succeeded"
    detail = client.get(f"/api/admin/calls/{r.json()['request_id']}", headers=admin["headers"]).json()
    assert detail["route_decision"]["chosen_route_id"] == "rt_primary"
    assert "priority" in detail["route_decision"]["reason"]


def test_failover_on_retryable_error(client, admin, provider, credential, make_token, monkeypatch) -> None:
    """主 route 429 且未产生输出 → 切到备 route，决策记录 dropped 原因（v2）。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _mk(client, admin, provider, credential, "text.generate.failover")
    _publish(client, admin, cap["id"])
    token = make_token()
    # 主 route 429（retry.max_attempts=1 → 一次即判定可切换）
    upstream.push(429, {"error": "rate limited"})
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "触发切换"}},
    )
    body = r.json()
    assert body["status"] == "succeeded", body
    assert body["output"] == "备用提供商回复"
    detail = client.get(f"/api/admin/calls/{body['request_id']}", headers=admin["headers"]).json()
    assert detail["route_decision"]["chosen_route_id"] in ("rt_backup", "rt_primary")
    urls = [c["url"] for c in upstream.calls]
    assert any("v1/generate" in u for u in urls) and any("v2/generate" in u for u in urls)


def test_failover_disabled_keeps_failure(client, admin, provider, credential, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _mk(client, admin, provider, credential, "text.generate.nofailover")
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["failover"] = {"enabled": False, "max_switches": 0}
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    _publish(client, admin, cap["id"])
    token = make_token()
    upstream.push(429, {"error": "rate"})
    upstream.push(429, {"error": "rate"})
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "不切换"}},
    )
    assert r.json()["status"] == "failed"
    assert not any("v2/generate" in c["url"] for c in upstream.calls)


def test_disabled_route_is_dropped(client, admin, provider, credential, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = _mk(client, admin, provider, credential, "text.generate.disabledroute")
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["enabled"] = False
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "只有备 route"}},
    )
    assert r.json()["status"] == "succeeded"
    assert r.json()["output"] == "备用提供商回复"
    detail = client.get(f"/api/admin/calls/{r.json()['request_id']}", headers=admin["headers"]).json()
    dropped = detail["route_decision"]["dropped"]
    assert any(d["route_id"] == "rt_primary" for d in dropped)


def test_no_enabled_route_is_actionable_error(client, admin, provider, credential, make_token) -> None:
    cap = _mk(client, admin, provider, credential, "text.generate.noroute")
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    for item in definition["routes"]:
        item["enabled"] = False
    client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    # 用测试接口验证「无可用路由」的可操作错误（发布会被校验拦下）
    r = client.post(
        f"/api/admin/capabilities/{cap['id']}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "x"}},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CAPABILITY_NOT_CALLABLE"
    assert r.json()["error"]["details"]["dropped"]


def test_compatibility_requires_explicit_group() -> None:
    from skillrouter_api.router.compatibility import equivalent

    base = {
        "kind": "text.generate",
        "input_schema": {"required": ["prompt"], "properties": {"prompt": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "modes": ["sync"],
        "compatibility_group": None,
    }
    ok, reasons = equivalent(base, dict(base))
    assert ok is False
    assert any("compatibility_group" in r for r in reasons)
    grouped = {**base, "compatibility_group": "text.chat.v1"}
    ok, reasons = equivalent(grouped, dict(grouped))
    assert ok is True, reasons


def test_compatibility_rejects_different_kind() -> None:
    from skillrouter_api.router.compatibility import equivalent

    a = {
        "kind": "text.generate",
        "input_schema": {"required": ["prompt"], "properties": {"prompt": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "modes": ["sync"],
        "compatibility_group": "g1",
    }
    b = {**a, "kind": "image.generate"}
    ok, reasons = equivalent(a, b)
    assert ok is False
    assert any("kind" in r for r in reasons)


def test_route_crud_endpoints(client, admin, provider, credential) -> None:
    cap = _mk(client, admin, provider, credential, "text.generate.routecrud")
    r = client.get(f"/api/admin/capabilities/{cap['id']}/routes", headers=admin["headers"])
    assert r.status_code == 200
    routes = r.json()["items"]
    assert len(routes) == 2
    assert routes[0]["is_primary"] is True
    r = client.post(
        f"/api/admin/capabilities/{cap['id']}/routes",
        headers=admin["headers"],
        json={
            "route_id": "rt_third",
            "provider_id": provider["id"],
            "credential_id": credential["id"],
            "priority": 20,
            "request": {"url_template": "{base_url}/v3/generate", "method": "POST"},
            "param_map": {"prompt": {"from": "input.prompt"}},
        },
    )
    assert r.status_code == 201, r.text
    r = client.delete(f"/api/admin/capabilities/{cap['id']}/routes/rt_third", headers=admin["headers"])
    assert r.status_code == 200
    names = [i["route_id"] for i in client.get(f"/api/admin/capabilities/{cap['id']}/routes", headers=admin["headers"]).json()["items"]]
    assert "rt_third" not in names


def test_impact_endpoint(client, admin, provider, credential, make_capability, make_token) -> None:
    cap = make_capability()
    token = make_token(scopes=[cap["key"]])
    r = client.get(f"/api/admin/capabilities/{cap['id']}/impact", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert any(t["id"] == token["token_id"] for t in body["tokens"])
    assert body["route_credentials"][0]["status"] == "active"
