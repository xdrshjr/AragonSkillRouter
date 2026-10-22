"""Upload and artifact storage.

Files live on the persistent volume; the database holds metadata only. Access
is always mediated by a route that checks ownership/scope — storage paths are
never exposed to clients, so there are no unauthenticated file URLs to guess.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, not_found, unprocessable

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-]+")


@dataclass(frozen=True)
class StoredFile:
    id: str
    filename: str
    content_type: str
    size: int
    storage_path: str
    sha256: str
    purpose: str


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "file")
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "file"
    return cleaned[:120]


def _extend_name(filename: str, content_type: str) -> str:
    if "." in filename:
        return filename
    guessed = mimetypes.guess_extension(content_type or "") or ""
    return filename + guessed


def save_bytes(
    content: bytes,
    *,
    filename: str,
    content_type: str = "application/octet-stream",
    purpose: str = "upload",
    owner_type: str = "user",
    owner_id: str | None = None,
    task_id: str | None = None,
    invocation_id: str | None = None,
    retention_days: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if len(content) > settings.max_upload_bytes and purpose == "upload":
        raise unprocessable(
            "file_too_large",
            f"Files are limited to {settings.max_upload_bytes // (1024 * 1024)} MB.",
        )
    digest = hashlib.sha256(content).hexdigest()
    file_id = db.new_id("file")
    safe_name = _extend_name(_safe_filename(filename), content_type)
    base_dir = settings.artifacts_dir if purpose == "artifact" else settings.uploads_dir
    day = db.utcnow().strftime("%Y/%m/%d")
    target_dir = Path(base_dir) / day
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{file_id}-{safe_name}"
    path.write_bytes(content)
    retention = retention_days if retention_days is not None else settings.file_retention_days
    expires_at = db.iso(db.utcnow() + timedelta(days=retention)) if retention else None
    db.insert(
        "files",
        {
            "id": file_id,
            "sha256": digest,
            "filename": safe_name,
            "content_type": content_type or "application/octet-stream",
            "size": len(content),
            "storage_path": str(path),
            "purpose": purpose,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "task_id": task_id,
            "invocation_id": invocation_id,
            "expires_at": expires_at,
            "created_at": db.iso(),
        },
    )
    return {
        "id": file_id,
        "filename": safe_name,
        "content_type": content_type or "application/octet-stream",
        "size": len(content),
        "sha256": digest,
        "purpose": purpose,
        "expires_at": expires_at,
        "url": f"/api/files/{file_id}",
    }


def save_stream(stream: BinaryIO, *, filename: str, content_type: str, **kwargs: Any) -> dict[str, Any]:
    return save_bytes(stream.read(), filename=filename, content_type=content_type, **kwargs)


def get_file(file_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row:
        raise not_found("File")
    if not Path(row["storage_path"]).exists():
        raise ApiError(
            "file_missing",
            "The stored file is no longer on disk.",
            status_code=410,
            actions=["Re-run the task that produced it."],
        )
    return row


def read_bytes(file_id: str) -> tuple[dict[str, Any], bytes]:
    row = get_file(file_id)
    return row, Path(row["storage_path"]).read_bytes()


def delete_file(file_id: str) -> None:
    row = get_file(file_id)
    try:
        Path(row["storage_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))


def list_files(
    *,
    purpose: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if purpose:
        where.append("purpose = ?")
        params.append(purpose)
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM files WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT id, sha256, filename, content_type, size, purpose, owner_type, owner_id, task_id, expires_at, created_at "
        f"FROM files WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    for row in rows:
        row["url"] = f"/api/files/{row['id']}"
    return rows, int(total)


def cleanup_expired() -> dict[str, int]:
    rows = db.query_all("SELECT id, storage_path FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?", (db.iso(),))
    removed = 0
    freed = 0
    for row in rows:
        try:
            path = Path(row["storage_path"])
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
        except OSError:
            pass
        db.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        removed += 1
    return {"removed": removed, "bytes_freed": freed}


def content_disposition(filename: str, *, inline: bool = False) -> str:
    disposition = "inline" if inline else "attachment"
    ascii_name = _safe_filename(filename)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def storage_usage() -> dict[str, int]:
    row = db.query_one("SELECT COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes FROM files") or {}
    return {"files": int(row.get("files", 0)), "bytes": int(row.get("bytes", 0))}


def ensure_storage_writable() -> None:
    settings = get_settings()
    probe = Path(settings.uploads_dir) / ".write-probe"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"ok")
    probe.unlink(missing_ok=True)


def copy_into_storage(source: Path, **kwargs: Any) -> dict[str, Any]:
    with source.open("rb") as handle:
        return save_stream(handle, content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream", filename=source.name, **kwargs)
