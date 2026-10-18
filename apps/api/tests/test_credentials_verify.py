"""凭证有效性探测：成功 / 401 / 429 / 超时，结果落库（§15.3 / v2 R14）。"""

from __future__ import annotations

import httpx

from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


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
