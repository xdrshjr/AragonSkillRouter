"""凭证投影与 meta 密封（F-03 / §A.3.2 / 不变式 I4）。

断言目标（T-03 / T-04）：四个投影里都不出现密钥原文与 `webhook_secret` 值，
白名单键（`probe_path` 等）仍然可见；`webhook_secret` 被密封后回调验签照旧通过。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from sqlalchemy import select

from skillrouter_api.db import db_session
from skillrouter_api.models import Credential
from skillrouter_api.services.credentials import SENSITIVE_META

SECRET = "sk-projection-secret-0123456789"
WEBHOOK = "whsec-projection-0123456789"


def _create(client, admin, provider_id: str, meta: dict, *, name: str = "投影凭证") -> dict:
    r = client.post(
        f"/api/admin/providers/{provider_id}/credentials",
        headers=admin["headers"],
        json={
            "name": name,
            "kind": "llm",
            "auth_type": "bearer",
            "secret": SECRET,
            "meta": meta,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_no_plaintext_secret_in_any_credential_view(client, admin, provider) -> None:
    """T-03：四点投影无明文（含 webhook_secret 值），且 probe_path 仍可见。"""
    created = _create(
        client,
        admin,
        provider["id"],
        {"probe_path": "/models", "probe_method": "GET", "webhook_secret": WEBHOOK},
    )
    cid = created["id"]
    views = {
        "list": client.get("/api/admin/credentials", headers=admin["headers"]),
        "detail": client.get(f"/api/admin/credentials/{cid}", headers=admin["headers"]),
        "provider": client.get(f"/api/admin/providers/{provider['id']}", headers=admin["headers"]),
        "impact": client.get(f"/api/admin/credentials/{cid}/impact", headers=admin["headers"]),
    }
    for name, response in views.items():
        assert response.status_code == 200, (name, response.text)
        dumped = json.dumps(response.json(), ensure_ascii=False)
        assert SECRET not in dumped, name
        assert WEBHOOK not in dumped, name

    listed = views["list"].json()["items"]
    row = next(item for item in listed if item["id"] == cid)
    # 白名单不误伤：探测路径仍是明文可见
    assert row["meta"]["probe_path"] == "/models"
    assert row["meta"]["probe_method"] == "GET"
    assert "webhook_secret" not in row["meta"]
    assert "webhook_secret" in row["meta_redacted_keys"]
    assert row["meta_sealed"] is True
    # 前端直接读的两个列现在真的被投影出来了（列存在但此前没输出）
    assert row["secret_mask"].startswith("sk-") and SECRET not in row["secret_mask"]
    assert len(row["secret_fingerprint"]) == 12

    detail_row = views["detail"].json()
    assert detail_row["meta_sealed"] is True
    assert "webhook_secret" in detail_row["meta_redacted_keys"]
    provider_creds = views["provider"].json()["credentials"]
    summary = next(item for item in provider_creds if item["id"] == cid)
    # providers 详情复用同一投影：字段名一致（T-03 的「字段名一致」要求）
    assert summary["secret_mask"] == row["secret_mask"]
    assert summary["meta_redacted_keys"] == row["meta_redacted_keys"]
    assert summary["meta_sealed"] is True


def test_webhook_secret_is_sealed_and_still_verifies(client, admin, provider) -> None:
    """T-04：写入 webhook_secret → meta 无该键、进入 meta_redacted_keys、库里是密文、验签通过。"""
    created = _create(
        client,
        admin,
        provider["id"],
        {"provider_slug": provider["slug"], "webhook_secret": WEBHOOK},
        name="回调凭证",
    )
    cid = created["id"]
    detail = client.get(f"/api/admin/credentials/{cid}", headers=admin["headers"]).json()
    assert "webhook_secret" not in detail["meta"]
    assert detail["meta"]["provider_slug"] == provider["slug"]
    assert "webhook_secret" in detail["meta_redacted_keys"]
    assert detail["meta_sealed"] is True
    assert WEBHOOK not in json.dumps(detail, ensure_ascii=False)

    with db_session() as s:
        row = s.get(Credential, cid)
        assert row is not None
        assert row.meta_sealed and row.meta_sealed.startswith("v1."), "库里必须是密文而不是明文"
        assert "webhook_secret" not in (row.meta or {})

    body = json.dumps({"event": "ping"}).encode()
    signature = hmac.new(WEBHOOK.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(
        f"/api/v1/callbacks/{provider['slug']}",
        content=body,
        headers={
            "X-Signature": f"sha256={signature}",
            "X-Timestamp": str(int(time.time())),
            "X-Event-Id": f"evt-{os.urandom(4).hex()}",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True


def test_sensitive_meta_keys_never_stored_in_meta(client, admin, provider) -> None:
    """不变式 I4：写路径（POST + PATCH）后，全表 `meta` 都不含敏感键。"""
    created = _create(
        client,
        admin,
        provider["id"],
        {"api_key": "leaked-123", "username": "svc-account"},
        name="I4 凭证",
    )
    detail = created
    assert "api_key" not in detail["meta"]
    assert "api_key" in detail["meta_redacted_keys"]
    assert detail["meta"]["username"] == "svc-account"  # basic 用户名不是敏感键

    r = client.patch(
        f"/api/admin/credentials/{created['id']}",
        headers=admin["headers"],
        json={"meta": {"secret_token": "leaked-456", "probe_path": "/v1/models"}},
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert "secret_token" not in patched["meta"]
    assert patched["meta"]["probe_path"] == "/v1/models"
    assert "secret_token" in patched["meta_redacted_keys"]

    with db_session() as s:
        for row in s.scalars(select(Credential)):
            for key in (row.meta or {}):
                assert not SENSITIVE_META.search(str(key)), (row.id, key)
