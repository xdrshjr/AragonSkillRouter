"""文件上传与受控访问（§7.11 / §9.2.1 / §A.3.10）。"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response

from ...deps import optional_token, require_token
from ...errors import ApiError, Code
from ...security.tokens import TokenContext
from ...services import files

router = APIRouter(prefix="/files", tags=["v1-files"])

#: 下载所需的作用域（除 `*` 之外）
DOWNLOAD_SCOPE = "files:read"


@router.post("", status_code=201)
async def upload(
    file: UploadFile = File(...),
    expires_in_days: int | None = Form(default=None),
    ctx: TokenContext = Depends(require_token),
) -> dict:
    data = await file.read()
    expires = None
    if expires_in_days:
        from datetime import datetime, timedelta

        expires = (
            datetime.now(UTC) + timedelta(days=expires_in_days)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    saved = files.save_bytes(
        data,
        mime=file.content_type,
        filename=file.filename,
        kind="upload",
        owner_type="token",
        owner_id=ctx.token_id,
        expires_at=expires,
    )
    return saved


@router.get("/{file_id}")
async def download(
    file_id: str,
    sig: str | None = Query(default=None),
    exp: str | None = Query(default=None),
    ctx: TokenContext | None = Depends(optional_token),
) -> Response:
    """签名 URL 优先，其次平台令牌（§A.3.10 / §A.5.1）。

    顺序固定：`sig+exp` 有效即放行（**免令牌**）；否则要求令牌且作用域含
    `files:read`；两者都没有 → `401 UNAUTHENTICATED`。此前无效签名返回 `404`，
    会被误读成「文件不存在」，本轮统一为 401 + 区分 hint。
    """
    record = files.get_record(file_id)
    signed_ok = bool(sig and files.verify_signature(file_id, sig, exp, record.owner_id))
    if not signed_ok:
        if ctx is None:
            hint = (
                "链接已过期，请重新获取（签名 URL 有效期 10 分钟）"
                if sig
                else "缺少 Authorization 头"
            )
            raise ApiError(
                Code.UNAUTHENTICATED,
                message="下载需要有效的签名链接或平台令牌",
                hint=hint,
            )
        if "*" not in ctx.scopes and DOWNLOAD_SCOPE not in ctx.scopes:
            raise ApiError(
                Code.TOKEN_SCOPE_DENIED,
                message=f"令牌缺少 {DOWNLOAD_SCOPE} 作用域",
                hint="在管理端为令牌追加 files:read",
            )
        if record.owner_type == "token" and record.owner_id != ctx.token_id:
            raise ApiError(Code.NOT_FOUND, "文件不存在或无权访问")
    data, record = files.read_bytes(file_id)
    disposition = "inline" if (record.mime or "").startswith(("image/", "audio/", "video/")) else "attachment"
    return Response(
        content=data,
        media_type=record.mime or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{disposition}; filename="{record.filename or record.id}"',
            "Cache-Control": "private, max-age=600",
        },
    )


def signed_url(file_id: str, ctx: TokenContext) -> dict:
    record = files.get_record(file_id)
    return files.signed_url(file_id, record.owner_id)
