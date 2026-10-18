"""Schema / 映射 / 限制 / 错误映射校验 + 多 route、modes、file_io、examples（v2）。"""

from __future__ import annotations

from tests.fixtures.builders import capability_payload


def _validate(client, admin, capability_id):
    r = client.post(f"/api/admin/capabilities/{capability_id}/validate", headers=admin["headers"])
    assert r.status_code == 200
    return r.json()


def _codes(result: dict, level: str = "error") -> list[str]:
    return [i["code"] for i in result["issues"] if i["level"] == level]


def _paths(result: dict, level: str = "error") -> list[str]:
    return [i["path"] for i in result["issues"] if i["level"] == level]


def test_valid_definition_passes(client, admin, make_capability) -> None:
    cap = make_capability()
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is True, result


def test_key_format_enforced(client, admin, make_capability) -> None:
    cap = make_capability()
    detail = client.get(f"/api/admin/capabilities/{cap['id']}").json()
    definition = detail["definition"]
    definition["key"] = "Bad Key"
    r = client.patch(
        f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition}
    )
    assert r.status_code == 200
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is False
    assert "key" in _paths(result)


def test_additional_properties_must_be_false(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["input_schema"]["additionalProperties"] = True
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is False
    assert "INPUT_SCHEMA_INVALID" in _codes(result)


def test_param_map_unknown_input_field(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["param_map"]["ghost"] = {"from": "input.not_declared"}
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "PARAM_MAP_UNRESOLVED" in _codes(result)


def test_param_map_unknown_namespace(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["param_map"]["bad"] = {"from": "secrets.value"}
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "PARAM_MAP_UNRESOLVED" in _codes(result)


def test_modes_stream_requires_route_support(client, admin, make_capability) -> None:
    cap = make_capability(modes=["sync", "stream"])
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0].pop("stream")
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "UNSUPPORTED_MODE" in _codes(result)


def test_modes_async_requires_query_config(client, admin, make_capability) -> None:
    cap = make_capability(modes=["sync", "async"])
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["async"] = {"submit": {"task_id_path": "$.id"}}
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "ASYNC_QUERY_MISSING" in _codes(result)


def test_invalid_mode_value(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["modes"] = ["sync", "magic"]
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "UNSUPPORTED_MODE" in _codes(result)


def test_no_routes_is_error(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"] = []
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "routes" in _paths(result)


def test_examples_required(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["examples"] = []
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "examples" in _paths(result)


def test_example_must_match_schema(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["examples"] = [{"title": "坏示例", "input": {"unexpected": 1}}]
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert any("examples[0]" in p for p in _paths(result))


def test_url_template_scheme_restricted(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["request"]["url_template"] = "file:///etc/passwd"
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is False


def test_multipart_requires_file_input(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["request"]["encoding"] = "multipart"
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is False


def test_error_rule_requires_status_or_json(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["errors"] = [{"match": {}, "code": "UPSTREAM_ERROR"}]
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is False


def test_missing_429_is_warning_not_error(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["errors"] = [
        {"match": {"status": 401}, "code": "UPSTREAM_AUTH", "retryable": False}
    ]
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is True
    assert result["warn_count"] >= 1


def test_compatibility_group_must_exist(client, admin, make_capability) -> None:
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["compatibility_group"] = "ghost.group"
    client.patch(f"/api/admin/capabilities/{cap['id']}", headers=admin["headers"], json={"definition": definition})
    result = _validate(client, admin, cap["id"])
    assert "compatibility_group" in _paths(result)


def test_confirm_compatibility_creates_group(client, admin, make_capability) -> None:
    cap = make_capability()
    r = client.post(
        f"/api/admin/capabilities/{cap['id']}/compatibility",
        headers=admin["headers"],
        json={"group": f"text.chat.{cap['key'].split('.')[-1]}", "confirm": True},
    )
    assert r.status_code == 200
    result = _validate(client, admin, cap["id"])
    assert not any("compatibility_group" in p for p in _paths(result))


def test_inactive_credential_is_warning_when_saving(client, admin, provider, credential, make_capability) -> None:
    cap = make_capability()
    client.post(f"/api/admin/credentials/{credential['id']}/disable", headers=admin["headers"])
    result = _validate(client, admin, cap["id"])
    assert result["ok"] is True  # 保存草稿只是 warn
    assert "CREDENTIAL_DISABLED" in _codes(result, "warn")


def test_publish_rejected_with_inactive_credential(client, admin, provider, credential, make_capability) -> None:
    cap = make_capability()
    client.post(f"/api/admin/credentials/{credential['id']}/disable", headers=admin["headers"])
    r = client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    assert r.status_code in (400, 409), r.text
    issues = r.json()["error"]["details"].get("issues") or []
    assert any(i["code"] == "CREDENTIAL_DISABLED" for i in issues), r.text
    client.post(f"/api/admin/credentials/{credential['id']}/enable", headers=admin["headers"])


def test_duplicate_route_id_rejected(client, admin, provider, credential, make_capability) -> None:
    payload = capability_payload(provider["id"], credential["id"], key="text.generate.duproute")
    payload["routes"] = payload["routes"] * 2
    r = client.post(
        "/api/admin/capabilities",
        headers=admin["headers"],
        json={"key": "text.generate.duproute", "name": "重复 route", "kind": "text.generate", "definition": payload},
    )
    assert r.status_code == 201
    result = _validate(client, admin, r.json()["id"])
    assert result["ok"] is False
