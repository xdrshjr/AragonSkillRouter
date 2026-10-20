"""LLM 配置端点的接口契约（T-20…T-23、T-36，spec §5.1–§5.5 / §7.1）。

两类结果必须分清（这是本轮体验的核心）：

- 鉴权失败 / 上游错误这类「正常的坏结果」→ `200 + ok=false + error_code + hint`；
- 请求本身不成立 / 凭证不能用 → 4xx/5xx（走统一错误体）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from skillrouter_api.db import db_session
from skillrouter_api.models import LlmConnectionProbe
from skillrouter_api.services.llm import probe
from tests.fixtures.fake_upstream import FakeLlmUpstream, patch_httpx

SECRET = "sk-api-secret-0123456789"


@pytest.fixture(autouse=True)
def _clean_state():
    probe.reset_state()
    yield
    probe.reset_state()


def _rows() -> list[LlmConnectionProbe]:
    with db_session() as s:
        return list(s.scalars(select(LlmConnectionProbe).order_by(LlmConnectionProbe.at)))


# ── §5.1 协议注册表 ─────────────────────────────────────────
def test_protocols_requires_login(client) -> None:
    """T-20：未登录 401。"""
    r = client.get("/api/admin/llm/protocols")
    assert r.status_code == 401
    assert r.json()["error"]["code"] in ("UNAUTHENTICATED", "SESSION_EXPIRED")


def test_protocols_lists_four_wire_specs(client, admin) -> None:
    r = client.get("/api/admin/llm/protocols", headers=admin["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [item["key"] for item in body["items"]] == [
        "openai",
        "anthropic",
        "google",
        "custom",
    ]
    assert body["wires"] == ["openai", "anthropic", "google"]
    anthropic = next(item for item in body["items"] if item["key"] == "anthropic")
    assert anthropic["models_path"] == "/v1/models"
    assert anthropic["default_auth_style"] == "api_key"
    custom = next(item for item in body["items"] if item["key"] == "custom")
    assert custom["models_path"] is None and custom["supports_models_list"] is True


# ── T-20 / T-21 / T-22：请求校验 ─────────────────────────────
def test_test_connection_rejects_missing_csrf(client, admin) -> None:
    r = client.post(
        "/api/admin/llm/test-connection",
        json={"protocol": "openai", "secret": SECRET, "mode": "auth"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_INVALID"


def test_secret_and_credential_are_exclusive(client, admin) -> None:
    """T-21：两者同时给 → 400 `VALIDATION_FAILED`。"""
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "secret": SECRET,
            "credential_id": "crd_whatever",
            "mode": "auth",
        },
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "二选一" in error["message"] or any(
        "二选一" in str(item) for item in error.get("details", {}).get("fields", [])
    )


def test_unknown_protocol_is_llm_protocol_unsupported(client, admin) -> None:
    """T-22：`protocol=zzz` → 400 `LLM_PROTOCOL_UNSUPPORTED`。"""
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={"protocol": "zzz", "secret": SECRET, "base_url": "http://127.0.0.1:9/v1"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "LLM_PROTOCOL_UNSUPPORTED"


def test_custom_without_base_url_is_rejected(client, admin) -> None:
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={"protocol": "custom", "secret": SECRET, "mode": "auth"},
    )
    assert r.status_code == 400
    assert "base_url" in json.dumps(r.json(), ensure_ascii=False)


def test_metadata_address_is_blocked(client, admin) -> None:
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "secret": SECRET,
            "base_url": "http://169.254.169.254/latest",
            "mode": "auth",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SSRF_BLOCKED"


# ── 成功的 auth 探测（真实请求，假上游）──────────────────────
def test_test_connection_reports_auth_stage_without_billing(client, admin, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o", "gpt-4o-mini"])
    patch_httpx(monkeypatch, upstream)
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "secret": SECRET,
            "base_url": "http://127.0.0.1:9/v1",
            "mode": "auth",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "auth"
    assert [stage["stage"] for stage in body["stages"]] == ["auth"]
    assert body["stages"][0]["model_count"] == 2
    assert body["probe_id"].startswith("llmp_")
    assert upstream.completion_calls == [], "默认 mode 不得发出计费请求"


def test_failure_is_200_with_error_code_and_hint(client, admin, monkeypatch) -> None:
    """鉴权失败是「正常的坏结果」：200 + ok=false + error_code + hint，不是 500。"""
    upstream = FakeLlmUpstream(protocol="google", reject_auth=True)
    patch_httpx(monkeypatch, upstream)
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "google",
            "secret": SECRET,
            "base_url": "http://127.0.0.1:9",
            "mode": "auth",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "UPSTREAM_AUTH"
    assert body["hint"] and "API key" in body["hint"]


# ── T-23：密钥不外泄 ────────────────────────────────────────
def test_secret_never_appears_in_response_or_probe_row(client, admin, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai")
    patch_httpx(monkeypatch, upstream)
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "secret": SECRET,
            "base_url": "http://127.0.0.1:9/v1",
            "mode": "auth",
        },
    )
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    probe_id = r.json()["probe_id"]
    row = next(item for item in _rows() if item.id == probe_id)
    dumped = json.dumps(
        {"stages": row.stages, "detail": row.detail_redacted, "base_url": row.base_url},
        ensure_ascii=False,
    )
    assert SECRET not in dumped
    assert row.credential_id is None


# ── §5.3 取模型清单 ────────────────────────────────────────
def test_models_endpoint_returns_catalog(client, admin, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    r = client.post(
        "/api/admin/llm/models",
        headers=admin["headers"],
        json={
            "protocol": "anthropic",
            "secret": SECRET,
            "base_url": "http://127.0.0.1:9",
            "refresh": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "upstream"
    assert body["count"] == 1
    assert body["items"][0]["id"] == "claude-sonnet-4-5"
    assert body["truncated"] is False
    assert body["probe_id"].startswith("llmp_")


# ── §5.4 凭证下的取模型 ────────────────────────────────────
def test_credential_models_uses_saved_credential(
    client, admin, provider, credential, monkeypatch
) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o"])
    patch_httpx(monkeypatch, upstream)
    r = client.get(
        f"/api/admin/credentials/{credential['id']}/models",
        headers=admin["headers"],
        params={"protocol": "openai", "refresh": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "upstream"
    assert [item["id"] for item in body["items"]] == ["gpt-4o"]


def test_credential_models_with_unknown_protocol(client, admin, credential) -> None:
    r = client.get(
        f"/api/admin/credentials/{credential['id']}/models",
        headers=admin["headers"],
        params={"protocol": "zzz"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "LLM_PROTOCOL_UNSUPPORTED"


# ── T-36：凭证不可用 ───────────────────────────────────────
def test_disabled_credential_returns_409(client, admin, credential) -> None:
    disable = client.post(
        f"/api/admin/credentials/{credential['id']}/disable", headers=admin["headers"]
    )
    assert disable.status_code == 200, disable.text
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={"protocol": "openai", "credential_id": credential["id"], "mode": "auth"},
    )
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "CREDENTIAL_DISABLED"
    assert error["message"] and error["hint"]


def test_missing_credential_returns_404(client, admin) -> None:
    r = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={"protocol": "openai", "credential_id": "crd_does_not_exist", "mode": "auth"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ── §5.5 探测历史 ─────────────────────────────────────────
def test_probes_list_carries_full_stages(client, admin, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai")
    patch_httpx(monkeypatch, upstream)
    created = client.post(
        "/api/admin/llm/test-connection",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "secret": SECRET,
            "base_url": "http://127.0.0.1:9/v1",
            "mode": "auth",
        },
    )
    assert created.status_code == 200, created.text
    probe_id = created.json()["probe_id"]

    r = client.get("/api/admin/llm/probes", headers=admin["headers"], params={"page_size": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    item = next(entry for entry in body["items"] if entry["id"] == probe_id)
    # 列表项已带完整 stages → 不需要详情端点（R-14）
    assert item["stages"] and item["stages"][0]["stage"] == "auth"
    assert item["mode"] == "auth"
    assert "detail_redacted" in item


def test_throttle_returns_429(client, admin, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai")
    patch_httpx(monkeypatch, upstream)
    payload = {
        "protocol": "openai",
        "secret": SECRET,
        "base_url": "http://127.0.0.1:9/v1",
        "mode": "auth",
    }
    statuses = []
    for _ in range(12):
        r = client.post("/api/admin/llm/test-connection", headers=admin["headers"], json=payload)
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert r.json()["error"]["code"] == "LLM_PROBE_THROTTLED"
            assert r.json()["error"]["details"]["retry_after_s"] >= 1
            break
    assert 429 in statuses, f"11 次以上应触发限流，实际状态码：{statuses}"
