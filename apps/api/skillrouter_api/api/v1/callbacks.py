"""第三方回调：签名 / 时间戳窗口 / event_id 去重 / 来源校验（§7.11）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Request
from sqlalchemy import select

from ...db import db_session, db_write, now_iso, uuid_pk
from ...errors import ApiError, Code
from ...logging import get_logger, redact
from ...models import CallbackEvent, Credential
from ...services import credentials
from ...settings import settings

router = APIRouter(tags=["v1-callbacks"])
log = get_logger("skillrouter.run")


@router.post("/callbacks/{provider_slug}")
async def callback(provider_slug: str, request: Request) -> dict:
    raw = await request.body()
    signature = request.headers.get("x-signature") or request.headers.get("x-sr-signature")
    timestamp = request.headers.get("x-timestamp")
    event_id = request.headers.get("x-event-id")
    if not signature or not timestamp or not event_id:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="缺少签名所需头",
            hint="需要 X-Signature / X-Timestamp / X-Event-Id",
        )
    try:
        skew = abs(int(time.time()) - int(timestamp))
    except ValueError:
        raise ApiError(Code.VALIDATION_FAILED, "X-Timestamp 必须是秒级时间戳") from None
    if skew > 300:
        raise ApiError(Code.VALIDATION_FAILED, "回调时间戳超出 ±300s 窗口")

    secret = _webhook_secret(provider_slug)
    if not secret:
        raise ApiError(
            Code.NOT_FOUND,
            message=f"提供商 {provider_slug} 未配置 webhook_secret",
            hint="在凭证 meta 里配置 webhook_secret（写入时自动信封加密，不回显明文）",
        )
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.replace("sha256=", "")):
        with db_write() as s:
            s.add(
                CallbackEvent(
                    id=uuid_pk("cbe"),
                    provider_slug=provider_slug,
                    event_id=f"{event_id}:bad-signature",
                    signature_ok=0,
                    payload_redacted={},
                    at=now_iso(),
                )
            )
        raise ApiError(Code.UPSTREAM_AUTH, "回调签名校验失败")

    with db_session() as s:
        exists = s.scalar(
            select(CallbackEvent).where(
                CallbackEvent.provider_slug == provider_slug, CallbackEvent.event_id == event_id
            )
        )
    if exists:
        return {"ok": True, "duplicate": True, "event_id": event_id}

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw": raw.decode("utf-8", errors="replace")[:1000]}
    with db_write() as s:
        s.add(
            CallbackEvent(
                id=uuid_pk("cbe"),
                provider_slug=provider_slug,
                event_id=event_id,
                signature_ok=1,
                payload_redacted=redact(body, max_len=2000),
                at=now_iso(),
            )
        )
    log.info("callback_received", extra={"object": provider_slug, "detail": {"event_id": event_id}})
    return {"ok": True, "event_id": event_id, "accepted": True}


def _webhook_secret(provider_slug: str) -> str | None:
    """共享密钥：先解密 `meta_sealed`，再回落旧 `meta['webhook_secret']`（§A.3.2-4）。

    回落分支只为尚未跑 `scripts/migrate_seal_meta.py` 的存量库保留，命中时记
    `deprecated_meta_secret` 告警；回填完成后这条分支可以删掉。
    """
    with db_session() as s:
        rows = list(s.scalars(select(Credential).where(Credential.deleted_at.is_(None))))
        for row in rows:
            meta = row.meta or {}
            if meta.get("provider_slug") != provider_slug:
                continue
            sealed = credentials.sealed_meta_of(row)
            if sealed.get("webhook_secret"):
                return str(sealed["webhook_secret"])
            if meta.get("webhook_secret"):
                log.warning("deprecated_meta_secret", extra={"object": row.id, "detail": {"key": "webhook_secret"}})
                return str(meta["webhook_secret"])
    return None


__all__ = ["router", "settings"]
