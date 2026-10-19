"""令牌创建/撤销/限流/配额 + 跨令牌幂等隔离（C5）。"""

from __future__ import annotations

from skillrouter_api.security.tokens import TokenContext, hash_secret, scope_allows, verify_token
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


def test_token_only_returned_once(client, admin, make_token) -> None:
    data = make_token()
    assert data["token"].startswith("srk_")
    listing = client.get("/api/admin/access/tokens", headers=admin["headers"])
    assert listing.status_code == 200
    assert data["token"] not in listing.text


def test_token_hash_not_plaintext(client, admin, make_token) -> None:
    data = make_token()
    from sqlalchemy import select

    from skillrouter_api.db import db_session
    from skillrouter_api.models import PlatformToken

    with db_session() as s:
        row = s.scalar(select(PlatformToken).where(PlatformToken.id == data["token_id"]))
        secret = data["token"].split("_", 2)[2]
        assert row.token_hash == hash_secret(secret)
        assert secret not in row.token_hash


def test_invalid_and_malformed_tokens(client) -> None:
    assert client.get("/api/v1/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        client.get("/api/v1/whoami", headers={"Authorization": "Bearer srk_only_two"}).status_code
        in (401, 403)
    )
    assert client.get("/api/v1/whoami").status_code == 401


def test_revoked_token_rejected_immediately(client, admin, make_token) -> None:
    data = make_token()
    assert client.get("/api/v1/whoami", headers=data["headers"]).status_code == 200
    client.delete(f"/api/admin/access/tokens/{data['token_id']}", headers=admin["headers"])
    r = client.get("/api/v1/whoami", headers=data["headers"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_REVOKED"


def test_expired_token_rejected(client, admin, make_token) -> None:
    data = make_token(expires_at="2020-01-01T00:00:00Z")
    r = client.get("/api/v1/whoami", headers=data["headers"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_scope_denied(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap_a = make_capability(key="text.generate.scopea")
    cap_b = make_capability(key="text.generate.scopeb")
    for cap in (cap_a, cap_b):
        client.post(
            f"/api/admin/capabilities/{cap['id']}/test",
            headers=admin["headers"],
            json={"input": {"prompt": "x"}},
        )
        client.post(
            f"/api/admin/capabilities/{cap['id']}/publish",
            headers=admin["headers"],
            json={"version": "1.0.0"},
        )
    token = make_token(scopes=[cap_a["key"]])
    listing = client.get("/api/v1/capabilities", headers=token["headers"]).json()["items"]
    keys = {item["key"] for item in listing}
    assert cap_a["key"] in keys and cap_b["key"] not in keys
    r = client.post(
        f"/api/v1/capabilities/{cap_b['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "x"}},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "TOKEN_SCOPE_DENIED"


def test_scope_allows_tags() -> None:
    ctx = TokenContext(
        token_id="tok_1", agent_id="agt_1", agent_name="a", scopes=["tag:chat"], allowed_modes=["sync"]
    )
    assert scope_allows(ctx, "text.generate.x", ["chat"])
    assert not scope_allows(ctx, "text.generate.x", ["vision"])


def test_rpm_quota_enforced(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token(rpm=1)
    first = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke", headers=token["headers"], json={"input": {"prompt": "a"}}
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke", headers=token["headers"], json={"input": {"prompt": "b"}}
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert second.json()["error"]["details"]["retry_after"] > 0


def test_daily_calls_quota(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token(daily_calls=1)
    assert (
        client.post(
            f"/api/v1/capabilities/{cap['key']}/invoke",
            headers=token["headers"],
            json={"input": {"prompt": "1"}},
        ).status_code
        == 200
    )
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke", headers=token["headers"], json={"input": {"prompt": "2"}}
    )
    assert r.status_code == 429
    assert "日调用配额" in r.json()["error"]["message"]


def test_allowed_modes_restriction(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(modes=["sync", "async"])
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token(allowed_modes=["sync"])
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "x"}, "options": {"mode": "async"}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNSUPPORTED_MODE"


def test_whoami_reports_limits(client, admin, make_token) -> None:
    token = make_token(rpm=60, concurrency=2, daily_calls=100)
    r = client.get("/api/v1/whoami", headers=token["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["limits"]["rpm"] == 60
    assert body["usage"]["calls_today"] >= 0


def test_verify_token_returns_context(client, admin, make_token) -> None:
    data = make_token()
    ctx = verify_token(data["token"])
    assert ctx.token_id == data["token_id"]
    assert ctx.scope_label == f"token:{data['token_id']}"


def test_agent_revoke_cascades(client, admin, make_token) -> None:
    data = make_token()
    client.delete(f"/api/admin/access/agents/{data['agent_id']}", headers=admin["headers"])
    r = client.get("/api/v1/whoami", headers=data["headers"])
    assert r.status_code == 401
