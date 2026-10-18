"""审计写入与查询（脱敏，§7.10）。"""

from __future__ import annotations

from sqlalchemy import desc, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..logging import current_request_id, get_logger, redact
from ..models import AuditLog
from ..schemas.common import paginate

log = get_logger("skillrouter.audit")


def write(
    action: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    summary: dict | None = None,
    actor_type: str = "admin",
    actor_id: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> str:
    entry_id = uuid_pk("aud")
    with db_write() as s:
        s.add(
            AuditLog(
                id=entry_id,
                at=now_iso(),
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                object_type=object_type,
                object_id=object_id,
                summary=redact(summary or {}),
                request_id=request_id or current_request_id(),
                ip=ip,
            )
        )
    log.info(
        "audit",
        extra={"actor": actor_id, "object": object_id, "detail": {"action": action}},
    )
    return entry_id


def list_logs(
    *,
    action: str | None = None,
    actor: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    with db_session() as s:
        stmt = select(AuditLog).order_by(desc(AuditLog.at))
        if action:
            stmt = stmt.where(AuditLog.action.like(f"{action}%"))
        if actor:
            stmt = stmt.where(AuditLog.actor_id == actor)
        if object_type:
            stmt = stmt.where(AuditLog.object_type == object_type)
        if object_id:
            stmt = stmt.where(AuditLog.object_id == object_id)
        if date_from:
            stmt = stmt.where(AuditLog.at >= date_from)
        if date_to:
            stmt = stmt.where(AuditLog.at <= date_to)
        rows = list(s.scalars(stmt.limit(2000)))
    items = [
        {
            "id": r.id,
            "at": r.at,
            "actor_type": r.actor_type,
            "actor_id": r.actor_id,
            "action": r.action,
            "object_type": r.object_type,
            "object_id": r.object_id,
            "summary": r.summary,
            "request_id": r.request_id,
            "ip": r.ip,
        }
        for r in rows
    ]
    if q:
        needle = q.lower()
        items = [
            i
            for i in items
            if needle in (i["action"] or "").lower()
            or needle in str(i["summary"]).lower()
            or needle in (i["object_id"] or "").lower()
        ]
    return paginate(items, page, page_size)


AUDIT_ACTIONS = [
    "admin.password_changed",
    "credential.created",
    "credential.replaced",
    "credential.rotated",
    "credential.disabled",
    "credential.verified",
    "credential.deleted",
    "capability.created",
    "capability.published",
    "capability.deprecated",
    "capability.rolled_back",
    "capability.route_changed",
    "capability.compatibility_confirmed",
    "skill.imported",
    "skill.published",
    "skill.deleted",
    "skill.enabled",
    "skill.disabled",
    "token.created",
    "token.revoked",
    "agent_flow.started",
    "agent_flow.published",
    "agent_approval.requested",
    "agent_approval.approved",
    "agent_approval.rejected",
    "agent_approval.expired",
    "route.changed",
    "engine.upgrade_started",
    "engine.verified",
    "engine.switched",
    "engine.rolled_back",
    "master_key.rotated",
    "backup.created",
    "restore.completed",
    "settings.updated",
]
