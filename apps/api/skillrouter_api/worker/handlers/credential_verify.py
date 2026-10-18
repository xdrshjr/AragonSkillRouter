"""凭证验证批处理（后台）。"""

from __future__ import annotations

from ...services import credentials_verify


async def handle(payload: dict) -> None:
    credential_id = payload.get("credential_id")
    if credential_id:
        await credentials_verify.verify(credential_id, actor=payload.get("actor"))
        return
    await credentials_verify.verify_pending(limit=int(payload.get("limit", 20)))
