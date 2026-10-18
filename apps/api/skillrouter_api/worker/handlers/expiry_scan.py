"""凭证过期扫描 + 审批超时置过期（P08）。"""

from __future__ import annotations

from ...logging import get_logger
from ...services import agentic_flow, credentials

log = get_logger("skillrouter.run")


async def handle(payload: dict) -> None:
    expiring = credentials.expiring_within(days=int(payload.get("days", 7)))
    expired = agentic_flow.expire_pending()
    log.info(
        "expiry_scan",
        extra={"detail": {"expiring": len(expiring), "approvals_expired": expired}},
    )
