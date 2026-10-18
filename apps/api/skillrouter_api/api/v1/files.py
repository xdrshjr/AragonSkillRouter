"""文件上传与受控访问（§7.11 / §9.2.1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from ...deps import require_token
from ...errors import ApiError, Code
from ...security.tokens import TokenContext
from ...services import files

router = APIRouter(prefix="/files", tags=["v1-files"])


@router.post("", status_code=201)
async def upload(
    file: UploadFile = File(...),
    expires_in_days: int | None = Form(default=None),
    ctx: TokenContext = Depends(require_token),
) -> dict:
    data = await file.read()
    expires = None
    if expires_in_days:
        from datetime import datetime, timedelta, timezone

        expires = (
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
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
    request: Request,
    sig: str | None = Query(default=None),
    exp: str | None = Query(default=None),
    ctx: TokenContext = Depends(require_token),
) -> Response:
    record = files.get_record(file_id)
    if sig:
        if not files.verify_signature(file_id, sig, exp, record.owner_id):
            raise ApiError(Code.NOT_FOUND, "签名无效或已过期")
    elif record.owner_type == "token" and record.owner_id != ctx.token_id:
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
