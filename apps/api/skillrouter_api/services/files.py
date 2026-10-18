"""文件与产物：内容寻址、签名 URL、保留清理（§7.11）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import time
from pathlib import Path

from sqlalchemy import select

from ..crypto.masterkey import file_signing_key
from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..models import FileObject
from ..settings import settings

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
AUDIO_MIMES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a"}
VIDEO_MIMES = {"video/mp4", "video/webm"}
DOC_MIMES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/yaml",
    "text/yaml",
    "text/csv",
}
ALLOWED_MIMES = (
    IMAGE_MIMES
    | AUDIO_MIMES
    | VIDEO_MIMES
    | DOC_MIMES
    | {"application/octet-stream", "application/zip", "text/html"}
)
SIGN_TTL_S = 600


def ensure_dirs() -> None:
    settings.files_dir.mkdir(parents=True, exist_ok=True)


def storage_path(sha256: str) -> Path:
    return settings.files_dir / sha256[:2] / sha256


def _guess_mime(filename: str | None, provided: str | None) -> str:
    if provided and provided != "application/octet-stream":
        return provided
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return provided or "application/octet-stream"


def save_bytes(
    data: bytes,
    *,
    mime: str | None = None,
    filename: str | None = None,
    kind: str = "upload",
    owner_type: str | None = None,
    owner_id: str | None = None,
    expires_at: str | None = None,
    enforce_limit: bool = True,
) -> dict:
    ensure_dirs()
    mime = _guess_mime(filename, mime)
    if enforce_limit and len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(
            Code.FILE_TOO_LARGE,
            message=f"文件超过 {settings.max_upload_mb}MB 上限",
            details={"size": len(data)},
        )
    digest = hashlib.sha256(data).hexdigest()
    path = storage_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    with db_write() as s:
        existing = s.scalar(
            select(FileObject).where(
                FileObject.sha256 == digest,
                FileObject.mime == mime,
                FileObject.owner_type == owner_type,
                FileObject.owner_id == owner_id,
            )
        )
        if existing:
            existing.ref_count = int(existing.ref_count or 0) + 1
            file_id = existing.id
        else:
            file_id = uuid_pk("fil")
            s.add(
                FileObject(
                    id=file_id,
                    sha256=digest,
                    mime=mime,
                    size=len(data),
                    storage_path=str(path),
                    kind=kind,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    filename=filename,
                    expires_at=expires_at,
                    ref_count=1,
                    created_at=now_iso(),
                )
            )
    return {"file_id": file_id, "mime": mime, "size": len(data), "sha256": digest, "expires_at": expires_at}


def get_record(file_id: str) -> FileObject:
    with db_session() as s:
        row = s.get(FileObject, file_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "文件不存在或已过期")
        s.expunge(row)
        return row


def describe(file_id: str) -> dict:
    row = get_record(file_id)
    return {
        "file_id": row.id,
        "mime": row.mime,
        "size": row.size,
        "sha256": row.sha256,
        "filename": row.filename,
        "kind": row.kind,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "signed_url": signed_url(row.id, row.owner_id)["url"],
    }


def read_bytes(file_id: str) -> tuple[bytes, FileObject]:
    row = get_record(file_id)
    path = Path(row.storage_path)
    if not path.exists():
        raise ApiError(Code.NOT_FOUND, "文件内容缺失")
    return path.read_bytes(), row


def _sign(file_id: str, exp: int, owner: str | None) -> str:
    payload = f"{file_id}|{exp}|{owner or ''}".encode()
    mac = hmac.new(file_signing_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def signed_url(file_id: str, owner: str | None = None, ttl_s: int = SIGN_TTL_S) -> dict:
    exp = int(time.time()) + ttl_s
    sig = _sign(file_id, exp, owner)
    return {
        "url": f"/api/v1/files/{file_id}?sig={sig}&exp={exp}",
        "expires_at": exp,
        "expires_in": ttl_s,
    }


def verify_signature(file_id: str, sig: str | None, exp: str | int | None, owner: str | None = None) -> bool:
    if not sig or not exp:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    expected = _sign(file_id, exp_int, owner)
    return hmac.compare_digest(expected, sig)


def retention_sweep() -> dict:
    """清理 `expires_at < now` 且 `ref_count<=0` 的记录与内容。"""
    now = now_iso()
    removed = 0
    with db_write() as s:
        rows = list(s.scalars(select(FileObject).where(FileObject.deleted_at.is_(None))))
        for row in rows:
            expired = row.expires_at and row.expires_at < now
            if not expired:
                continue
            others = s.scalar(
                select(FileObject).where(
                    FileObject.sha256 == row.sha256,
                    FileObject.deleted_at.is_(None),
                    FileObject.id != row.id,
                )
            )
            row.deleted_at = now
            removed += 1
            if others is None:
                path = Path(row.storage_path)
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
    return {"removed": removed}
