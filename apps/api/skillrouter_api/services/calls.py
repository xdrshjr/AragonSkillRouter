"""调用记录查询与详情（§7.10 / §9.2 / §A.3.11）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select

from ..db import db_session
from ..errors import ApiError, Code
from ..models import CallLog, Task
from ..schemas.common import paginate
from ..settings import settings

#: `sort` 白名单（§A.3.11 `GET /api/admin/calls`）；`duration_ms` 是外部字段名，落库叫 `latency_ms`
SORT_FIELDS = {
    "created_at": "created_at",
    "duration_ms": "latency_ms",
    "status": "status",
}


def _brief(row: CallLog) -> dict:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "caller_type": row.caller_type,
        "caller_id": row.caller_id,
        "capability_key": row.capability_key,
        "capability_version_id": row.capability_version_id,
        "provider_id": row.provider_id,
        "credential_id": row.credential_id,
        "mode": row.mode,
        "status": row.status,
        "http_status": row.http_status,
        "error_code": row.error_code,
        "latency_ms": row.latency_ms,
        "duration_ms": row.latency_ms,
        "queue_ms": row.queue_ms,
        "upstream_ms": row.upstream_ms,
        "retries": row.retries,
        "needs_reconcile": bool(row.needs_reconcile),
        "task_id": row.task_id,
        "created_at": row.created_at,
    }


def _sort_items(items: list[dict], sort: str | None) -> list[dict]:
    """`sort` 白名单校验 + 多键排序（`-` 前缀降序，越界 → 400 `VALIDATION_FAILED`）。"""
    raw = (sort or "-created_at").strip()
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    keys: list[tuple[str, bool]] = []
    for token in tokens:
        field = token.lstrip("-+")
        if field not in SORT_FIELDS:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=f"不支持的排序字段：{field}",
                details={"allowed": sorted(SORT_FIELDS)},
            )
        keys.append((SORT_FIELDS[field], token.startswith("-")))
    ordered = list(items)
    for key, descending in reversed(keys):
        ordered.sort(key=_null_last_key(key), reverse=descending)
    return ordered


def list_calls(
    *,
    q: str | None = None,
    status: str | None = None,
    capability: str | None = None,
    provider: str | None = None,
    caller: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_latency: int | None = None,
    needs_reconcile: bool | None = None,
    sort: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    with db_session() as s:
        stmt = select(CallLog).order_by(desc(CallLog.created_at))
        if status:
            stmt = stmt.where(CallLog.status == status)
        if capability:
            stmt = stmt.where(CallLog.capability_key == capability)
        if provider:
            stmt = stmt.where(CallLog.provider_id == provider)
        if caller:
            stmt = stmt.where(CallLog.caller_id == caller)
        if date_from:
            stmt = stmt.where(CallLog.created_at >= date_from)
        if date_to:
            stmt = stmt.where(CallLog.created_at <= date_to)
        if min_latency is not None:
            stmt = stmt.where(CallLog.latency_ms >= min_latency)
        if needs_reconcile is not None:
            stmt = stmt.where(CallLog.needs_reconcile == (1 if needs_reconcile else 0))
        rows = list(s.scalars(stmt.limit(2000)))
    items = [_brief(r) for r in rows]
    if q:
        needle = q.lower()
        items = [
            i
            for i in items
            if needle in (i["request_id"] or "").lower()
            or needle in (i["capability_key"] or "").lower()
            or needle in (i["caller_id"] or "").lower()
        ]
    items = _sort_items(items, sort)
    return paginate(items, page, page_size)


def _null_last_key(key: str):
    """排序键：空值永远排在最后，其余按原值比较。

    用具名函数而不是带默认参数的 lambda —— mypy 推不出 lambda 的返回类型会报
    "Cannot infer type of lambda"。
    """

    def sort_key(item: dict[str, Any]) -> tuple[bool, Any]:
        value = item.get(key)
        return (value is None, value if value is not None else "")

    return sort_key


def get_call(request_id: str) -> dict:
    from . import routing

    with db_session() as s:
        row = s.scalar(select(CallLog).where(CallLog.request_id == request_id))
        if row is None:
            raise ApiError(Code.NOT_FOUND, "调用记录不存在")
        data = _brief(row)
        data["payload_policy"] = row.payload_policy
        data["request_preview"] = row.request_preview
        data["response_preview"] = row.response_preview
        data["request_fingerprint"] = row.request_fingerprint
        data["agent_session_id"] = row.agent_session_id
        data["agent_turn_id"] = row.agent_turn_id
        data["route_decision"] = routing.decision_detail(row.id)
        data["task"] = None
        if row.task_id:
            task = s.get(Task, row.task_id)
            if task:
                data["task"] = {
                    "task_id": task.id,
                    "status": task.status,
                    "progress": {"percent": task.progress_percent, "stage": task.progress_stage},
                    "upstream_task_id": task.upstream_task_id,
                }
        data["retry_chain"] = [
            _brief(r)
            for r in s.scalars(
                select(CallLog)
                .where(CallLog.request_fingerprint == row.request_fingerprint)
                .order_by(CallLog.created_at)
                .limit(10)
            )
        ]
        data["latency_breakdown"] = {
            "queue_ms": row.queue_ms,
            "upstream_ms": row.upstream_ms,
            "total_ms": row.latency_ms,
            "platform_ms": (row.latency_ms or 0) - (row.upstream_ms or 0),
        }
    return data


def export_rows(limit: int = 5000) -> list[dict]:
    with db_session() as s:
        rows = list(s.scalars(select(CallLog).order_by(desc(CallLog.created_at)).limit(limit)))
    return [{**_brief(r), "policy": settings.payload_policy} for r in rows]
