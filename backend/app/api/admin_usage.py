"""Usage, limits, audit and operational logs (docs/CONTRACT.md §5.9).

Two different things live here and must never be conflated:

  * **audit** — durable rows written by ``app.services.audit`` for every state
    change. It is the compliance trail and it is never truncated.
  * **logs** — an in-process ring buffer of operational log records, fed by a
    logging handler installed lazily on first use. It is a debugging aid, holds
    no database rows, survives no restart and is only ever a tail.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core import db
from app.core.request_context import get_request_id
from app.deps import AuthUser, require_admin
from app.security.redact import Redactor
from app.services import audit, usage

router = APIRouter(prefix="/api/admin", tags=["usage"])

RING_SIZE = 500
_RING: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_RING_LOCK = threading.Lock()
_HANDLER_INSTALLED = False
_SEQ = 0


class _RingBufferHandler(logging.Handler):
    """Copy log records into a bounded in-process buffer (redacted)."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - logging plumbing
        global _SEQ
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = "<unformattable log record>"
        try:
            with _RING_LOCK:
                _SEQ += 1
                _RING.append(
                    {
                        "seq": _SEQ,
                        "at": db.iso(),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": Redactor().text(message)[:2000],
                        "request_id": get_request_id(),
                        "exc": bool(record.exc_info),
                    }
                )
        except Exception:  # pragma: no cover - logging must never raise
            pass


def install_log_handler() -> None:
    """Attach the ring buffer handler once, lazily."""
    global _HANDLER_INSTALLED
    if _HANDLER_INSTALLED:
        return
    with _RING_LOCK:
        if _HANDLER_INSTALLED:
            return
        handler = _RingBufferHandler()
        handler.setLevel(logging.INFO)
        # Only the root logger gets the handler: children propagate into it, so
        # attaching to "skillrouter" as well would record every line twice.
        logging.getLogger().addHandler(handler)
        skillrouter_log = logging.getLogger("skillrouter")
        if skillrouter_log.level > logging.INFO or skillrouter_log.level == logging.NOTSET:
            skillrouter_log.setLevel(logging.INFO)
        _HANDLER_INSTALLED = True


def log_tail(
    *, limit: int = 200, level: str | None = None, search: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    install_log_handler()
    with _RING_LOCK:
        records = list(_RING)
    if level:
        wanted = level.upper()
        records = [item for item in records if item["level"] == wanted]
    if search:
        needle = search.lower()
        records = [item for item in records if needle in item["message"].lower() or needle in item["logger"].lower()]
    total = len(records)
    tail = records[-limit:] if limit else records
    return tail, total


def _envelope(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))


@router.get("/usage")
def usage_overview(days: int = Query(default=7, ge=1, le=90), _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    payload = usage.usage_overview(days=days)
    payload["generated_at"] = db.iso()
    return payload


@router.get("/usage/limits")
def usage_limits(_user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    policies = db.query_all("SELECT * FROM budget_policies ORDER BY created_at DESC")
    agents = db.query_all("SELECT id, name, status FROM agents ORDER BY name")
    counters = []
    for agent in agents:
        counters.append(
            {
                "agent": {"id": agent["id"], "name": agent["name"], "status": agent["status"]},
                **usage.current_counters(agent["id"]),
                "limits": _agent_limits(agent["id"]),
            }
        )
    return {
        "global": usage.current_counters(),
        "agents": counters,
        "policies": [
            {
                "id": row["id"],
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "window": row["window"],
                "max_calls": row["max_calls"],
                "max_cost_usd": row["max_cost_usd"],
                "rate_per_min": row["rate_per_min"],
                "max_concurrency": row["max_concurrency"],
                "enabled": bool(row["enabled"]),
            }
            for row in policies
        ],
        "generated_at": db.iso(),
    }


def _agent_limits(agent_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT MAX(rate_limit_per_min) AS rate_limit_per_min,
                  MAX(max_concurrency) AS max_concurrency,
                  MAX(budget_usd) AS budget_usd
             FROM agent_tokens WHERE agent_id = ? AND status = 'active'""",
        (agent_id,),
    ) or {}
    return {
        "rate_limit_per_min": row.get("rate_limit_per_min"),
        "max_concurrency": row.get("max_concurrency"),
        "budget_usd": row.get("budget_usd"),
    }


@router.get("/audit")
def list_audit(
    object_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    rows, total = audit.list_entries(
        limit=limit,
        offset=offset,
        object_type=object_type,
        object_id=object_id,
        action=action,
        search=search,
    )
    items = [
        {
            "id": row["id"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "summary": row["summary"],
            "metadata": db.loads(row["metadata"], {}),
            "ip": row["ip"],
            "request_id": row["request_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return _envelope(items, int(total), limit, offset)


@router.get("/logs")
def operational_logs(
    limit: int = Query(default=200),
    level: str | None = Query(default=None),
    search: str | None = Query(default=None),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, _offset = _page(limit, 0)
    items, total = log_tail(limit=limit, level=level, search=search)
    payload = _envelope(items, total, limit, 0)
    payload["kind"] = "operational"
    payload["note"] = (
        "Operational log tail from an in-process ring buffer (last 500 records). "
        "It is not the audit trail: audit rows are durable and live at /api/admin/audit."
    )
    payload["ring_size"] = RING_SIZE
    return payload
