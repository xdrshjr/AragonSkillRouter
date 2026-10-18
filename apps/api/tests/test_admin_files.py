"""管理端附件上传/下载（复审补充：控制台没有平台令牌，需要会话 + CSRF 的上传通道）。"""

from __future__ import annotations


def test_admin_upload_and_download_roundtrip(client, admin) -> None:
    r = client.post(
        "/api/admin/files",
        headers=admin["headers"],
        files={"file": ("evidence.txt", b"skill-router-evidence", "text/plain")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file_id"].startswith("fil_")
    assert body["size"] == len(b"skill-router-evidence")
    assert body["download_url"] == f"/api/admin/files/{body['file_id']}"

    r = client.get(f"/api/admin/files/{body['file_id']}", headers=admin["headers"])
    assert r.status_code == 200
    assert r.content == b"skill-router-evidence"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_admin_upload_requires_csrf(client, admin) -> None:
    # 少了 X-CSRF-Token：写请求必须被拒（会话 cookie 本身不足以授权）
    r = client.post("/api/admin/files", files={"file": ("x.txt", b"x", "text/plain")})
    assert r.status_code in (401, 403), r.text


def test_admin_upload_rejects_oversized_file(client, admin, monkeypatch) -> None:
    from skillrouter_api.settings import settings

    monkeypatch.setattr(settings, "max_upload_mb", 1)
    r = client.post(
        "/api/admin/files",
        headers=admin["headers"],
        files={"file": ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


# ── 签名 URL（T-31 / T-48）与立即备份（T-32） ─────────────────────


def _split_signed_url(url: str) -> tuple[str, int]:
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    return query["sig"][0], int(query["exp"][0])


def _upload(client, admin, body: bytes = b"signed-url-bytes") -> dict:
    r = client.post(
        "/api/admin/files",
        headers=admin["headers"],
        files={"file": ("evidence.txt", body, "text/plain")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_signed_url_download_without_token(client, admin) -> None:
    """T-31：上传响应里的 `signed_url` 不带令牌即可下载；过期签名 401 + 「链接已过期」hint。"""
    from skillrouter_api.services import files as file_service

    body = b"signed-url-download"
    uploaded = _upload(client, admin, body)
    assert uploaded["signed_url"], "上传响应必须带 signed_url（§A.5.1）"

    anonymous = client.get(uploaded["signed_url"])
    assert anonymous.status_code == 200, anonymous.text
    assert anonymous.content == body

    record = file_service.get_record(uploaded["file_id"])
    expired = file_service.signed_url(record.id, record.owner_id, ttl_s=-600)
    stale = client.get(expired["url"])
    assert stale.status_code == 401, stale.text
    assert stale.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "过期" in (stale.json()["error"].get("hint") or "")


def test_single_signature_algorithm(client, admin) -> None:
    """T-48：`save_bytes()` 与 `describe()` 的 `signed_url` 同一算法、同一 TTL；两者都能验签。"""
    import time

    from skillrouter_api.services import files as file_service

    uploaded = _upload(client, admin, b"one-signature-algorithm")
    described = file_service.describe(uploaded["file_id"])
    record = file_service.get_record(uploaded["file_id"])

    saved_sig, saved_exp = _split_signed_url(uploaded["signed_url"])
    described_sig, described_exp = _split_signed_url(described["signed_url"])

    # 同一 TTL 常量：两次调用之间只允许秒级漂移（不能一个用 10 分钟、一个用 1 小时）
    assert abs(saved_exp - described_exp) <= 2, (saved_exp, described_exp)
    now = int(time.time())
    assert file_service.SIGN_TTL_S - 2 <= saved_exp - now <= file_service.SIGN_TTL_S
    assert uploaded["signed_url_expires_at"] == saved_exp

    assert file_service.verify_signature(record.id, saved_sig, saved_exp, record.owner_id) is True
    assert (
        file_service.verify_signature(record.id, described_sig, described_exp, record.owner_id)
        is True
    )


def test_backup_now_requires_post_and_csrf(client, admin) -> None:
    """T-32：GET 405；POST 无 CSRF 403；POST 有 CSRF 200 且产物带 sha256。"""
    r = client.get("/api/admin/settings/backup/now", headers=admin["headers"])
    assert r.status_code == 405, r.text

    no_csrf = client.post("/api/admin/settings/backup/now")
    assert no_csrf.status_code in (401, 403), no_csrf.text
    assert no_csrf.json()["error"]["code"] in ("CSRF_INVALID", "UNAUTHENTICATED")

    ok = client.post("/api/admin/settings/backup/now", headers=admin["headers"])
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["sha256"] and len(body["sha256"]) == 64
    assert body["product"].startswith("backup-") and body["product"].endswith(".zip")
    assert body["file_id"].startswith("fil_")
