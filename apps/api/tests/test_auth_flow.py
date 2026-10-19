"""初始化 / 强制改密 / 会话过期 / 限流 / CSRF（§15.3）。"""

from __future__ import annotations

from datetime import UTC


def test_bootstrap_endpoint_anonymous(client) -> None:
    r = client.get("/api/admin/auth/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert body["initialized"] is True
    assert "must_change_password" in body


def test_unauthenticated_returns_401(client) -> None:
    r = client.get("/api/admin/overview")
    assert r.status_code == 401
    assert r.json()["error"]["code"] in ("UNAUTHENTICATED", "SESSION_EXPIRED")


def test_login_wrong_password_records_failure(client) -> None:
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "nope-nope-nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_login_throttled_after_failures(client) -> None:
    for _ in range(6):
        client.post("/api/admin/auth/login", json={"username": "ghost", "password": "x" * 12})
    r = client.post("/api/admin/auth/login", json={"username": "ghost", "password": "x" * 12})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "LOGIN_THROTTLED"
    assert r.json()["error"]["details"]["retry_after"] > 0


def test_csrf_required_for_writes(client, admin) -> None:
    r = client.post("/api/admin/providers", json={"slug": "csrf-check", "name": "CSRF"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_INVALID"


def test_password_change_strength_enforced(client, admin) -> None:
    r = client.post(
        "/api/admin/auth/change-password",
        headers=admin["headers"],
        json={"current_password": "adminjr", "new_password": "short"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_me_returns_engine_summary(client, admin) -> None:
    r = client.get("/api/admin/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["admin"]["username"] == "admin"
    assert body["admin"]["must_change_password"] is False
    assert body["engine"]["status"] in ("available", "unavailable")


def test_session_expiry_and_revocation(client, admin) -> None:
    from datetime import datetime, timedelta

    from sqlalchemy import select, text

    from skillrouter_api.db import db_write
    from skillrouter_api.models import AdminSession

    with db_write() as s:
        # 只过期「当前客户端」的会话：`created_at` 只有秒级精度，多个用例在同一秒登录时
        # 单按 created_at 排序会取到别人的会话（断言随之变成 flaky）。rowid 单调递增，
        # 作为二级排序即可稳定命中最后插入的那一行。
        row = s.scalar(
            select(AdminSession).order_by(AdminSession.created_at.desc(), text("rowid DESC"))
        )
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        row.expires_at = past
    r = client.get("/api/admin/overview")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "SESSION_EXPIRED"
    # 恢复一次登录，避免影响后续用例
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "SkillRouter-Test-2026"})
    assert r.status_code == 200


def test_logout_revokes_session(client, admin) -> None:
    r = client.post("/api/admin/auth/logout", headers=admin["headers"])
    assert r.status_code == 200
    r = client.get("/api/admin/overview")
    assert r.status_code == 401
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "SkillRouter-Test-2026"})
    assert r.status_code == 200


def test_request_id_header_present(client) -> None:
    r = client.get("/api/v1/health")
    assert r.headers.get("X-Request-Id")
    assert len(r.headers["X-Request-Id"]) >= 8


def test_error_body_shape(client) -> None:
    r = client.get("/api/admin/providers/nope")
    assert r.status_code == 401
    error = r.json()["error"]
    for key in ("code", "message", "hint", "retryable", "details", "request_id"):
        assert key in error
