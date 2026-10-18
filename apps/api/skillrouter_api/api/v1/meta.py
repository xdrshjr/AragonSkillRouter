"""对外 `/api/v1` 元信息（§9.2.1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import __version__
from ...db import schema_version
from ...deps import is_initialized, require_token
from ...security.ratelimit import quota_snapshot
from ...security.tokens import TokenContext, whoami
from ...settings import settings

router = APIRouter(tags=["v1-meta"])


@router.get("/health")
async def health() -> dict:
    from ...db.base import now_iso

    return {
        "status": "ok" if is_initialized() else "initializing",
        "version": __version__,
        "engine": {
            "version": settings.gateway_pin_version,
            "status": "declared",
            "note": "引擎健康需管理员在 /settings/engine 查看（本端点不探测网关）",
        },
        "schema_version": schema_version(),
        "time": now_iso(),
    }


@router.get("/whoami")
async def me(ctx: TokenContext = Depends(require_token)) -> dict:
    return {**whoami(ctx), "usage": quota_snapshot("token", ctx.token_id)}
