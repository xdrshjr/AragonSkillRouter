"""能力发现（ETag + 缓存，§9.2.1）。"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Request, Response

from ...deps import require_token
from ...errors import ApiError, Code
from ...security.tokens import TokenContext
from ...services import capabilities

router = APIRouter(tags=["v1-discovery"])


def _etag(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return 'W/"' + hashlib.sha256(raw.encode()).hexdigest()[:32] + '"'


@router.get("/capabilities")
async def list_capabilities(
    request: Request,
    response: Response,
    refresh: bool = False,
    ctx: TokenContext = Depends(require_token),
) -> Response:
    items = capabilities.catalog(ctx)
    payload = {"items": items, "total": len(items)}
    tag = _etag(payload)
    if not refresh and request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, max-age=60" if not refresh else "no-store"
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"ETag": tag, "Cache-Control": response.headers["Cache-Control"]},
    )


@router.get("/capabilities/{key}")
async def get_capability(key: str, ctx: TokenContext = Depends(require_token)) -> dict:
    from ...security.tokens import scope_allows

    description = capabilities.describe(key)
    if not scope_allows(ctx, description["key"], description.get("tags") or []):
        raise ApiError(Code.TOKEN_SCOPE_DENIED, details={"capability": key})
    if description["status"] != "published" or not description["enabled"]:
        raise ApiError(
            Code.NOT_FOUND,
            message="能力不存在或未发布",
            hint="先用 capabilities.list 获取当前可用能力",
        )
    description.pop("limits", None)
    return description
