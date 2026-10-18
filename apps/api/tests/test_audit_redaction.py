"""审计脱敏、日志无密钥、错误码一致性（C8）。"""

from __future__ import annotations

from skillrouter_api.errors import REGISTRY, Code
from skillrouter_api.logging import mask_secret, redact


def test_redact_patterns() -> None:
    payload = {
        "authorization": "Bearer abcdefghijklmnop",
        "api_key": "sk-live-1234567890abcdef",
        "note": "用户手机 13800138000 邮箱 a@b.com 令牌 srk_ab12_secretvalue",
        "nested": {"password": "p", "token": "t"},
        "blob": "A" * 200,
    }
    cleaned = redact(payload)
    text = str(cleaned)
    assert "sk-live-1234567890abcdef" not in text
    assert "srk_ab12_secretvalue" not in text
    assert "13800138000" not in text
    assert "a@b.com" not in text
    assert cleaned["authorization"] == "***"
    assert cleaned["api_key"] == "***"
    assert cleaned["nested"]["password"] == "***"
    assert len(cleaned["blob"]) < 200


def test_mask_secret_variants() -> None:
    assert mask_secret("sk-1234567890abcdef") == "sk-***cdef"
    assert mask_secret("") == "***"
    assert mask_secret("short") == "***rt"
    assert "1234567890" not in mask_secret("sk-1234567890abcdef")


def test_audit_records_are_redacted(client, admin, provider) -> None:
    secret = "sk-audit-secret-0987654321"
    client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "审计脱敏", "kind": "llm", "auth_type": "bearer", "secret": secret},
    )
    r = client.get("/api/admin/settings/audit", headers=admin["headers"])
    assert r.status_code == 200
    assert secret not in r.text
    actions = {item["action"] for item in r.json()["items"]}
    assert "credential.created" in actions


def test_audit_covers_required_actions(client, admin, provider, make_token) -> None:
    token = make_token()
    client.delete(f"/api/admin/access/tokens/{token['token_id']}", headers=admin["headers"])
    actions = {
        item["action"]
        for item in client.get("/api/admin/settings/audit", headers=admin["headers"]).json()["items"]
    }
    assert {"token.created", "token.revoked"} <= actions


def test_audit_filters(client, admin) -> None:
    r = client.get(
        "/api/admin/settings/audit", headers=admin["headers"], params={"action": "credential"}
    )
    assert r.status_code == 200
    assert all(item["action"].startswith("credential") for item in r.json()["items"])
    r = client.get("/api/admin/settings/audit/actions", headers=admin["headers"])
    assert "credential.created" in r.json()["items"]


def test_error_registry_is_complete() -> None:
    """每个 Code 常量都必须在 REGISTRY 里登记（前端只按 code 分支）。"""
    declared = {
        value
        for name, value in vars(Code).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    missing = declared - set(REGISTRY)
    assert not missing, f"未登记的错误码：{missing}"


def test_error_bodies_expose_code_hint_request_id(client) -> None:
    r = client.get("/api/v1/whoami", headers={"Authorization": "Bearer srk_bad_x"})
    error = r.json()["error"]
    assert error["code"] == "TOKEN_INVALID"
    assert error["hint"]
    assert error["request_id"]
    assert error["retryable"] is False


def test_call_log_previews_redacted(client, admin, make_capability, make_token, monkeypatch) -> None:
    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.redact")
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "记录脱敏"}},
    )
    detail = client.get(f"/api/admin/calls/{r.json()['request_id']}", headers=admin["headers"])
    assert "sk-pytest-secret" not in detail.text
    assert detail.json()["payload_policy"] == "redacted_preview"


def test_logs_directory_written(client, admin) -> None:
    from skillrouter_api.settings import settings

    client.get("/api/admin/overview", headers=admin["headers"])
    assert settings.logs_dir.exists()


def test_maintenance_and_db_busy_codes_registered() -> None:
    for code in ("DB_BUSY", "MAINTENANCE", "NOT_INITIALIZED", "INVALID_STATE_TRANSITION", "PROVIDER_IN_USE"):
        assert code in REGISTRY


def test_every_registered_code_can_be_raised() -> None:
    """REGISTRY 里每一条都必须是 4 元组，且能真正构造出 ApiError（回归：NOT_INITIALIZED 少一位 → IndexError）。"""
    from skillrouter_api.errors import ApiError

    for code, entry in REGISTRY.items():
        assert len(entry) == 4, f"{code} 的注册项应为 (status, message, retryable, hint)"
        exc = ApiError(code)
        assert exc.status == entry[0]
        assert exc.message == entry[1]
        assert exc.retryable is entry[2]
        assert exc.hint == entry[3]
