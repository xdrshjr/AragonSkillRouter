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
