"""凭证有效性探测：成功 / 401 / 429 / 超时，结果落库（§15.3 / v2 R14）。"""

from __future__ import annotations

import os

import httpx

from tests.fixtures.fake_upstream import FakeLlmUpstream, FakeUpstream, patch_httpx


def _create(client, admin, provider, *, secret="sk-probe-value-1234") -> str:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={
            "name": "探测凭证",
            "kind": "llm",
            "auth_type": "bearer",
            "secret": secret,
            "meta": {"probe_path": "/probe"},
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_verify_success(client, admin, provider, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    credential_id = _create(client, admin, provider)
    upstream.push(200, {"ok": True})
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status_code"] == 200
    detail = client.get(f"/api/admin/credentials/{credential_id}")
    assert detail.json()["last_verified_at"] is not None
    assert detail.json()["last_verify_ok"] is True


def test_verify_hits_probe_path(client, admin, provider, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    credential_id = _create(client, admin, provider)
    upstream.push(200, {"ok": True})
    client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert upstream.calls[-1]["url"].endswith("/probe")


def test_verify_401_marks_expired(client, admin, provider, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    credential_id = _create(client, admin, provider)
    upstream.push(401, {"error": "unauthorized"})
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error_code"] == "UPSTREAM_AUTH"
    detail = client.get(f"/api/admin/credentials/{credential_id}")
    assert detail.json()["last_verify_ok"] is False
    assert "sk-probe-value-1234" not in detail.text


def test_verify_429_records_rate_limit(client, admin, provider, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    credential_id = _create(client, admin, provider)
    upstream.push(429, {"error": "rate"})
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.json()["error_code"] == "UPSTREAM_RATE_LIMIT"


def test_verify_timeout(client, admin, provider, monkeypatch) -> None:
    original = httpx.AsyncClient

    def boom(request):  # noqa: ANN001, ANN202
        raise httpx.ConnectTimeout("timeout", request=request)

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = httpx.MockTransport(boom)
        return original(*args, **kwargs)

    credential_id = _create(client, admin, provider)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error_code"] == "UPSTREAM_TIMEOUT"


def test_verification_history_endpoint(client, admin, provider, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    credential_id = _create(client, admin, provider)
    upstream.push(200, {"ok": True})
    client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    r = client.get(f"/api/admin/credentials/{credential_id}/verifications", headers=admin["headers"])
    assert r.status_code == 200
    assert "items" in r.json()
    assert r.json()["items"][0]["ok"] is True


def test_impact_lists_route_references(client, admin, provider, credential, make_capability) -> None:
    cap = make_capability()
    r = client.get(f"/api/admin/credentials/{credential['id']}/impact", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["ref_count"] >= 1
    assert any(c["id"] == cap["id"] for c in body["capabilities"])
    assert body["blocking"] is True


def test_delete_referenced_credential_rejected(client, admin, provider, credential, make_capability) -> None:
    make_capability()
    r = client.delete(f"/api/admin/credentials/{credential['id']}", headers=admin["headers"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CREDENTIAL_IN_USE"
    assert r.json()["error"]["details"]["capabilities"]


def test_verify_unknown_credential(client, admin) -> None:
    r = client.post("/api/admin/credentials/crd_missing/verify", headers=admin["headers"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ── T-28：llm 凭证改成协议感知探测（spec §3.7）────────────────
def _llm_credential(client, admin, *, protocol: str, base_url: str) -> str:
    """建一个「没有 probe_path」的 llm 凭证：它的探测必须由协议注册表决定。"""
    slug = f"llm-{protocol}-{os.urandom(3).hex()}"
    r = client.post(
        "/api/admin/providers",
        headers=admin["headers"],
        json={"slug": slug, "name": f"LLM {protocol}", "category": "llm", "base_url": base_url},
    )
    assert r.status_code == 201, r.text
    provider_id = r.json()["id"]
    r = client.post(
        f"/api/admin/providers/{provider_id}/credentials",
        headers=admin["headers"],
        json={
            "name": f"{protocol} 凭证",
            "kind": "llm",
            "auth_type": "api_key",
            "secret": "sk-verifysecret0123456",
            "meta": {"llm_protocol": protocol},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_llm_credential_verify_uses_protocol_aware_endpoint(client, admin, monkeypatch) -> None:
    """Anthropic 凭证必须打 `GET /v1/models?limit=1000`，而不是 `GET /models`。

    改前：`PROBES["llm"] = GET {base}/models` → 404 → 有效凭证被误判为无效。
    改后：协议注册表给出 `/v1/models` + `anthropic-version` → 200 → 判有效。
    """
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _real_client(*args, **kwargs)

    original = httpx.AsyncClient

    def _real_client(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["transport"] = upstream.mock
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    credential_id = _llm_credential(client, admin, protocol="anthropic", base_url="http://127.0.0.1:9")
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["protocol"] == "anthropic"
    assert body["stage"] == "auth"
    assert body["model_count"] == 1
    url = upstream.models_calls[0]["url"]
    assert url.endswith("/v1/models?limit=1000"), url
    assert upstream.models_calls[0]["headers"].get("anthropic-version") == "2023-06-01"


def test_llm_credential_verify_reports_auth_failure(client, admin, provider, monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", reject_auth=True)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["transport"] = upstream.mock
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    credential_id = _llm_credential(client, admin, protocol="anthropic", base_url="http://127.0.0.1:9")
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "UPSTREAM_AUTH"
    assert body["status_code"] == 401


def test_non_llm_credential_keeps_the_legacy_probe(client, admin, monkeypatch) -> None:
    """非 llm 类别（image / video / …）行为**一字不动**：仍是 `GET {base}/models`。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    slug = "img-provider-legacy"
    r = client.post(
        "/api/admin/providers",
        headers=admin["headers"],
        json={"slug": slug, "name": "Image", "category": "image", "base_url": "http://127.0.0.1:9"},
    )
    assert r.status_code == 201, r.text
    provider_id = r.json()["id"]
    r = client.post(
        f"/api/admin/providers/{provider_id}/credentials",
        headers=admin["headers"],
        json={
            "name": "image 凭证",
            "kind": "image",
            "auth_type": "bearer",
            "secret": "sk-imgsecret012345",
            "meta": {},
        },
    )
    assert r.status_code == 201, r.text
    credential_id = r.json()["id"]
    upstream.push(200, {"data": [{"id": "img-model"}]})
    r = client.post(f"/api/admin/credentials/{credential_id}/verify", headers=admin["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert upstream.calls[-1]["url"].endswith("/models")
    assert "protocol" not in r.json()
