"""令牌传输与日志脱敏（F-04 / §A.3.3 / T-05、T-06）。

三条行为契约：
1. `?token=` 默认被拒（400/401 `TOKEN_INVALID` + 明确 hint），只有显式开启
   `SKILLROUTER_ALLOW_QUERY_TOKEN` 才可用，且每次都记一次 `query_token_used` 告警；
2. 日志与脱敏输出里不出现令牌原值、签名 URL 的 `sig` 值，但 `exp` 必须可读；
3. 签名 URL 免令牌下载（同一套签名算法），过期签名 401 + 「链接已过期」hint。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from skillrouter_api.services import files
from skillrouter_api.settings import settings


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture(logger_name: str) -> Iterator[_Collect]:
    logger = logging.getLogger(logger_name)
    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def test_query_token_rejected_by_default(client, make_token) -> None:
    """T-05（默认关闭）：`?token=` 不通，`Authorization` 头照常可用。"""
    token = make_token()
    response = client.get(f"/api/v1/whoami?token={token['token']}")
    body = response.json()["error"]
    assert body["code"] == "TOKEN_INVALID"
    # 错误码注册表把 TOKEN_INVALID 映射为 401（spec §A.7.1 T-05 写作 400，见报告）
    assert response.status_code in (400, 401)
    assert "Authorization" in body["hint"]

    ok = client.get("/api/v1/whoami", headers=token["headers"])
    assert ok.status_code == 200, ok.text


def test_query_token_allowed_only_with_compat_flag(client, make_token, monkeypatch) -> None:
    """T-05（逃生阀）：显式开启后可用，并留下一次 `query_token_used` 告警。"""
    with _capture("skillrouter.run") as captured:
        monkeypatch.setattr(settings, "allow_query_token", True)
        token = make_token()
        response = client.get(f"/api/v1/whoami?token={token['token']}")
    assert response.status_code == 200, response.text
    events = [record.getMessage() for record in captured.records]
    assert events.count("query_token_used") == 1, events


def test_logs_never_contain_token_value(client, make_token) -> None:
    """T-06：脱敏后无令牌原值、无 `sig` 值；`exp` 仍可读。"""
    from skillrouter_api.logging import JsonFormatter, redact

    token = make_token()
    raw = token["token"]
    secret_part = raw.rsplit("_", 1)[-1]
    exp = int(time.time()) + 600
    text = f"GET /api/v1/files/fil_x?sig=SIGNATUREVALUE0123&exp={exp} token={raw}"
    out = redact(text)
    assert secret_part not in out
    assert "SIGNATUREVALUE0123" not in out
    assert "sig=***" in out
    assert f"exp={exp}" in out, "签名 URL 的 exp 是排障关键，不能被脱敏"

    record = logging.LogRecord(
        "skillrouter.access", logging.INFO, __file__, 1, "request", None, None
    )
    record.path = f"/api/v1/whoami?token={raw}&sig=SIGNATUREVALUE0123&exp={exp}"
    record.latency_ms = 7
    payload = JsonFormatter().format(record)
    assert secret_part not in payload
    assert "SIGNATUREVALUE0123" not in payload
    assert f"exp={exp}" in payload


def test_signed_url_download_without_token(client, make_token) -> None:
    """签名优先（§A.3.10 / 语义变更）：免令牌下载，过期则 401 + 专门的 hint。"""
    token = make_token()
    upload = client.post(
        "/api/v1/files",
        headers=token["headers"],
        files={"file": ("evidence.txt", b"signed-body", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    saved = upload.json()
    assert saved["signed_url"].startswith("/api/v1/files/")
    assert isinstance(saved["signed_url_expires_at"], int)

    # 同一个签名算法：upload 返回的 sig 能被 verify_signature 验证通过
    sig = saved["signed_url"].split("sig=")[1].split("&")[0]
    exp = saved["signed_url"].split("exp=")[1]
    assert files.verify_signature(saved["file_id"], sig, exp, token["token_id"])

    response = client.get(saved["signed_url"])  # 不带任何令牌
    assert response.status_code == 200
    assert response.content == b"signed-body"

    expired = files.signed_url(saved["file_id"], token["token_id"], ttl_s=-10)
    stale = client.get(expired["url"])
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "过期" in stale.json()["error"]["hint"]

    # 作用域不含 files:read 的令牌仍然被拒
    narrow = make_token(scopes=["text.chat"])
    denied = client.get(f"/api/v1/files/{saved['file_id']}", headers=narrow["headers"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "TOKEN_SCOPE_DENIED"
