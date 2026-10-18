"""SKILL 导入流水线（后台执行）。"""

from __future__ import annotations

from ...logging import get_logger, redact
from ...services import skillimport

log = get_logger("skillrouter.run")


async def handle(payload: dict) -> None:
    if payload.get("action") == "commit":
        skillimport.commit(payload, actor=payload.get("actor"))
        log.info("skill_import_committed", extra={"object": payload.get("preview_token")})
        return
    try:
        result = await skillimport.preview(payload)
        log.info("skill_import_previewed", extra={"object": result.get("import_id")})
    except Exception as exc:  # noqa: BLE001
        log.error("skill_import_failed", extra={"detail": redact(str(exc), max_len=300)})
        raise
