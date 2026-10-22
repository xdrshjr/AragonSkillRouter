"""Audit trail.

Audit rows are written for state changes that a human must be able to explain
later: credential create/rotate/delete, capability publish, SKILL import, token
revoke, routing change, engine upgrade. They are redacted on write and kept
separate from operational logs.
"""

from __future__ import annotations

from typing import Any

from app.core import db
from app.core.request_context import get_request_id
from app.security.redact import preview


def record(
    *,
    actor_type: str,
    action: str,
    object_type: str,
    object_id: str | None = None,
    actor_id: str | None = None,
    summary: str = "",
    metadata: Any = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> int:
    payload = preview(metadata or {}, limit=2000)
    cursor_id = db.query_scalar(
        """INSERT INTO audit_logs
             (actor_type, actor_id, action, object_type, object_id, summary, metadata, ip, request_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           RETURNING id""",
        (
            actor_type,
            actor_id,
            action,
            object_type,
            object_id,
            summary[:500],
            payload,
            ip,
            request_id or get_request_id(),
            db.iso(),
        ),
    )
    return int(cursor_id or 0)


def list_entries(
    *,
    limit: int = 50,
    offset: int = 0,
    object_type: str | None = None,
    object_id: str | None = None,
    action: str | None = None,
    search: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if object_type:
        where.append("object_type = ?")
        params.append(object_type)
    if object_id:
        where.append("object_id = ?")
        params.append(object_id)
    if action:
        where.append("action = ?")
        params.append(action)
    if search:
        where.append("(summary LIKE ? OR action LIKE ? OR COALESCE(actor_id,'') LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.query_scalar(f"SELECT COUNT(*) FROM audit_logs {clause}", params) or 0
    rows = db.query_all(
        f"""SELECT * FROM audit_logs {clause}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    return rows, int(total)
