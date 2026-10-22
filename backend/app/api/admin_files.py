"""Admin file endpoints plus the scope-checked binary download (CONTRACT §5.6).

Two audiences, one download URL:

  * the browser session (`require_admin` for the admin list/metadata/delete and
    `require_user` for the download itself);
  * an external agent bearer token, which may only fetch files that belong to
    its own task/invocation and whose capability is inside the token's scope.

Storage paths never leave the process: downloads are streamed with
`FileResponse` and every metadata payload is stripped of `storage_path`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, forbidden, unauthorized, unprocessable
from app.deps import AgentPrincipal, AuthUser, agent_principal, client_ip, require_admin, require_user
from app.services import audit
from app.services import files as file_service

router = APIRouter(tags=["files"])

MAX_LIMIT = 200

_PRIVATE_COLUMNS = (
    "id, sha256, filename, content_type, size, purpose, owner_type, owner_id, "
    "task_id, invocation_id, expires_at, created_at"
)


def _public_file(row: dict[str, Any]) -> dict[str, Any]:
    """Metadata shape: no storage path, always a mediated URL."""
    payload = {key: value for key, value in row.items() if key != "storage_path"}
    payload["url"] = f"/api/files/{row['id']}"
    return payload


# ------------------------------------------------------------------ admin ----
@router.post("/api/admin/files", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form("upload"),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Browser upload used by the floating agent panel (documents, screenshots,
    cURL examples) and by SKILL import. Size is bounded while streaming so a
    large body never lands fully in memory."""
    settings = get_settings()
    if purpose not in {"upload", "artifact"}:
        raise unprocessable("invalid_purpose", "purpose must be 'upload' or 'artifact'.")
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise unprocessable(
                "file_too_large",
                f"Files are limited to {limit // (1024 * 1024)} MB.",
                actions=["Upload a smaller file, or raise SR_MAX_UPLOAD_BYTES."],
            )
        chunks.append(chunk)
    existing = db.query_one("SELECT id FROM files WHERE sha256 = ? AND size = ? AND purpose = ?", (__import__("hashlib").sha256(b"".join(chunks)).hexdigest(), total, purpose))
    if existing:
        row = db.query_one(f"SELECT {_PRIVATE_COLUMNS} FROM files WHERE id = ?", (existing["id"],))
        assert row is not None
        return {**_public_file(row), "deduplicated": True}
    saved = file_service.save_bytes(
        b"".join(chunks),
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        purpose=purpose,
        owner_type="user",
        owner_id=user.id,
    )
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="file.uploaded",
        object_type="file",
        object_id=saved["id"],
        summary=f"{saved['filename']} ({saved['size']} bytes)",
        metadata={"content_type": saved["content_type"], "purpose": purpose},
        ip=client_ip(request),
    )
    row = db.query_one(f"SELECT {_PRIVATE_COLUMNS} FROM files WHERE id = ?", (saved["id"],))
    assert row is not None
    return {**_public_file(row), "deduplicated": False}


@router.get("/api/admin/files")
def list_files(
    purpose: str | None = None,
    task_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    rows, total = file_service.list_files(purpose=purpose, task_id=task_id, limit=limit, offset=offset)
    return {"items": [_public_file(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/api/admin/files/{file_id}")
def get_file_metadata(file_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return _public_file(file_service.get_file(file_id))


@router.delete("/api/admin/files/{file_id}")
def delete_file(
    file_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = file_service.get_file(file_id)
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="file.delete",
        object_type="file",
        object_id=file_id,
        summary=f"Deleted file {row['filename']} ({row['size']} bytes).",
        metadata={"purpose": row["purpose"], "task_id": row.get("task_id"), "invocation_id": row.get("invocation_id")},
        ip=client_ip(request),
    )
    file_service.delete_file(file_id)
    return {"deleted": True, "id": file_id}


# --------------------------------------------------------------- download ----
def _agent_may_read(principal: AgentPrincipal, row: dict[str, Any]) -> None:
    """A token may read its own task/invocation artifacts, if scoped for them."""
    if row.get("owner_type") == "agent" and row.get("owner_id") == principal.agent_id:
        return
    if row.get("task_id"):
        task = db.query_one(
            """SELECT t.agent_id, c.slug AS capability_slug
                 FROM tasks t LEFT JOIN capabilities c ON c.id = t.capability_id
                WHERE t.id = ?""",
            (row["task_id"],),
        )
        if task and task.get("agent_id") == principal.agent_id:
            _check_capability_scope(principal, task.get("capability_slug"))
            return
    if row.get("invocation_id"):
        invocation = db.query_one(
            """SELECT i.agent_id, c.slug AS capability_slug
                 FROM invocations i LEFT JOIN capabilities c ON c.id = i.capability_id
                WHERE i.id = ?""",
            (row["invocation_id"],),
        )
        if invocation and invocation.get("agent_id") == principal.agent_id:
            _check_capability_scope(principal, invocation.get("capability_slug"))
            return
    raise forbidden(
        "file_forbidden",
        "This file does not belong to any record of this agent token.",
        actions=["Use the file id from your own task or invocation response."],
    )


def _check_capability_scope(principal: AgentPrincipal, capability_slug: str | None) -> None:
    if not capability_slug:
        return
    if not principal.can_use(capability_slug):
        raise forbidden(
            "capability_not_granted",
            f"This token is not granted '{capability_slug}'.",
            actions=["Ask the administrator to add the capability to the token's scope."],
        )


@router.get("/api/files/{file_id}")
def download_file(file_id: str, request: Request) -> FileResponse:
    """Binary download: a browser session OR an agent bearer token (scope-checked)."""
    user: AuthUser | None = None
    session_error: ApiError | None = None
    try:
        user = require_user(request)
    except ApiError as exc:
        session_error = exc

    principal: AgentPrincipal | None = None
    if request.headers.get("authorization") or request.headers.get("x-api-key"):
        try:
            principal = agent_principal(request)
        except ApiError:
            # A stale bearer header must not lock a signed-in browser out of its own
            # download; only raise when the token is the sole credential.
            if user is None:
                raise

    if user is None and principal is None:
        raise session_error or unauthorized("Sign in, or send an agent bearer token, to download this file.")

    row = file_service.get_file(file_id)

    if user is None and principal is not None:
        _agent_may_read(principal, row)

    headers = {
        "content-disposition": file_service.content_disposition(row["filename"]),
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
    }
    return FileResponse(
        path=row["storage_path"],
        media_type=row["content_type"] or "application/octet-stream",
        headers=headers,
    )
