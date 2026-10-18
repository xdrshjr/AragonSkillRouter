"""过期清理：调用正文、文件、SSE 事件（§7.11 / §10.3）。"""

from __future__ import annotations

from ...db import db_write, now_iso
from ...logging import get_logger
from ...models import CallLog
from ...services import files
from ...sse import broker
from ...settings import settings
from sqlalchemy import select

log = get_logger("skillrouter.run")


async def handle(payload: dict) -> None:
    days = int(payload.get("retention_days") or settings.retention_days)
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    scrubbed = 0
    with db_write() as s:
        for row in s.scalars(
            select(CallLog).where(CallLog.created_at < cutoff, CallLog.request_preview.is_not(None))
        ):
            row.request_preview = None
            row.response_preview = None
            scrubbed += 1
    sweep = files.retention_sweep()
    events = broker.cleanup()
    log.info(
        "retention_sweep",
        extra={"detail": {"previews_scrubbed": scrubbed, **sweep, **events, "cutoff": cutoff}},
    )
