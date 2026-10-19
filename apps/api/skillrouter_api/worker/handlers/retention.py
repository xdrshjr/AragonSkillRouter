"""过期清理：调用正文、文件、SSE 事件、过期幂等占位（§7.11 / §10.3 / §A.3.5）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ...db import db_write, now_iso
from ...logging import get_logger
from ...models import CallLog, IdempotencyKey
from ...services import files, settings_runtime
from ...settings import settings
from ...sse import broker

log = get_logger("skillrouter.run")

#: 过期 `in_flight` 占位行的保护窗口：留 1 小时给「慢但还活着」的请求回写
IN_FLIGHT_GRACE_S = 3600


def _iso(seconds_offset: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=seconds_offset)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def handle(payload: dict) -> dict:
    """每小时清理一次；`payload.dry_run=True` 只统计不落库（`--dry-run` 语义）。"""
    dry_run = bool(payload.get("dry_run") or payload.get("dry-run"))
    days = int(
        payload.get("retention_days")
        or settings_runtime.get("retention_days", settings.retention_days)
    )
    cutoff = _iso(-days * 86400)
    inflight_cutoff = _iso(-IN_FLIGHT_GRACE_S)
    scrubbed = 0
    idem_cleared = 0
    if dry_run:
        from ...db import db_session

        with db_session() as s:
            scrubbed = len(
                list(
                    s.scalars(
                        select(CallLog.id).where(
                            CallLog.created_at < cutoff, CallLog.request_preview.is_not(None)
                        )
                    )
                )
            )
            idem_cleared = len(
                list(
                    s.scalars(
                        select(IdempotencyKey.id).where(
                            IdempotencyKey.state == "in_flight",
                            IdempotencyKey.expires_at < inflight_cutoff,
                        )
                    )
                )
            )
        log.info(
            "retention_dry_run",
            extra={
                "detail": {
                    "previews_scrubbed": scrubbed,
                    "idempotency_cleared": idem_cleared,
                    "cutoff": cutoff,
                }
            },
        )
        return {
            "terminal": True,
            "dry_run": True,
            "previews_scrubbed": scrubbed,
            "idempotency_cleared": idem_cleared,
        }
    with db_write() as s:
        for row in s.scalars(
            select(CallLog).where(CallLog.created_at < cutoff, CallLog.request_preview.is_not(None))
        ):
            row.request_preview = None
            row.response_preview = None
            scrubbed += 1
        # 占位行长期停在 in_flight 会让同一幂等键永久 409（I3）：过期的直接清掉
        for row in s.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.state == "in_flight", IdempotencyKey.expires_at < inflight_cutoff
            )
        ):
            s.delete(row)
            idem_cleared += 1
    sweep = files.retention_sweep()
    events = broker.cleanup()
    log.info(
        "retention_sweep",
        extra={
            "detail": {
                "previews_scrubbed": scrubbed,
                "idempotency_cleared": idem_cleared,
                **sweep,
                **events,
                "cutoff": cutoff,
                "at": now_iso(),
            }
        },
    )
    return {"terminal": True, "previews_scrubbed": scrubbed, "idempotency_cleared": idem_cleared}
