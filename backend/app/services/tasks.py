"""Durable task lifecycle, the generic job queue and invocation records.

Everything an async capability call needs after the provider accepted it: status
polling, truthful cancellation, deadline/lease recovery, plus the job queue the
worker drains and the read models for `/api/admin/invocations` and
`/api/admin/tasks`.

Two rules shape this module:

  * A transient network error never becomes a task failure. It becomes a retry
    with backoff, and only an expired deadline or exhausted attempts make the
    outcome ``uncertain`` — never a silent success and never a silent failure.
  * "We stopped waiting" is not "the provider cancelled". When the upstream
    exposes no cancel endpoint the task keeps its running status; only a
    terminal state reported by the provider sets ``cancelled``.

Leases: a claimed task/job carries ``lease_owner``/``lease_until`` set inside a
``BEGIN IMMEDIATE`` transaction, so two worker processes can never pick up the
same row, and a crashed worker's rows become claimable again once the lease
expires.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Iterable

import httpx

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.security.redact import Redactor, preview
from app.services import audit, execution
from app.services.capability_config import merge_defaults

# ------------------------------------------------------------------ vocab ----
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "uncertain")
ACTIVE_TASK_STATUSES = ("queued", "running", "cancel_requested")
TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled", "uncertain")

POLL_FAILED_CODE = "task_poll_failed"
TIMEOUT_CODE = "task_timeout"
MAX_BACKOFF_SECONDS = 300.0

_TASK_SELECT = """
    SELECT t.*,
           c.slug AS capability_slug,
           c.name AS capability_name,
           c.type AS capability_type,
           p.name AS provider_name,
           a.name AS agent_name
      FROM tasks t
      LEFT JOIN capabilities c ON c.id = t.capability_id
      LEFT JOIN providers p ON p.id = t.provider_id
      LEFT JOIN agents a ON a.id = t.agent_id
"""

_INVOCATION_SELECT = """
    SELECT i.*,
           c.slug AS capability_slug,
           c.name AS capability_name,
           p.name AS provider_name,
           v.version AS version_number,
           a.name AS agent_name
      FROM invocations i
      LEFT JOIN capabilities c ON c.id = i.capability_id
      LEFT JOIN providers p ON p.id = i.provider_id
      LEFT JOIN capability_versions v ON v.id = i.version_id
      LEFT JOIN agents a ON a.id = i.agent_id
"""


# ------------------------------------------------------------------ helpers --
def _now() -> str:
    return db.iso()


def _settings():
    return get_settings()


def _actor_fields(actor: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(actor, str):
        return {"type": "user", "id": actor, "ip": None}
    if isinstance(actor, dict):
        return {
            "type": actor.get("type") or actor.get("actor_type") or "user",
            "id": actor.get("id") or actor.get("actor_id"),
            "ip": actor.get("ip"),
        }
    return {"type": "system", "id": None, "ip": None}


def _poll_interval(config: dict[str, Any]) -> float:
    status_cfg = (config.get("async") or {}).get("status") or {}
    try:
        interval = float(status_cfg.get("poll_interval_s") or 5)
    except (TypeError, ValueError):
        interval = 5.0
    return max(1.0, interval)


def _backoff_seconds(config: dict[str, Any], attempts: int) -> float:
    base = _poll_interval(config)
    return float(min(MAX_BACKOFF_SECONDS, base * (2 ** max(0, attempts - 1))))


def _coerce_progress(value: Any, row: dict[str, Any]) -> int:
    current = int(row.get("progress") or 0)
    if value is None or isinstance(value, bool):
        return current
    try:
        number = float(value)
    except (TypeError, ValueError):
        return current
    if isinstance(value, str):
        number = float(number)
    elif 0 < number <= 1 and isinstance(value, float):
        number = number * 100
    return max(0, min(100, int(round(number))))


def _result_summary(result_doc: Any) -> str:
    value = result_doc.get("result") if isinstance(result_doc, dict) and "result" in result_doc else result_doc
    if value is None:
        return ""
    if isinstance(value, str):
        return preview(value, limit=300)
    if isinstance(value, dict):
        for key in ("text", "summary", "message", "output", "content", "answer"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return preview(candidate, limit=300)
    return preview(value, limit=300)


def _body_from_payload(payload: dict[str, Any]) -> Any:
    """Recover the provider body from a `fetch_status` payload, when possible.

    `execution.fetch_status` exposes a redacted/truncated preview rather than the
    raw body, so artifact capture is only attempted for bodies that are complete
    and parseable. A truncated preview is skipped instead of being mis-parsed.
    """
    raw = payload.get("body_preview")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    if "[truncated" in raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _classify_poll_error(exc: BaseException) -> tuple[bool, str, str]:
    """(transient?, code, message) for anything raised by a status probe."""
    from app.services.network import NetworkPolicyError, classify_network_error

    if isinstance(exc, NetworkPolicyError):
        return False, exc.code, str(exc)
    if isinstance(exc, ApiError):
        return False, exc.code, exc.message
    if isinstance(exc, httpx.HTTPError):
        code, message = classify_network_error(exc)
        return True, code, message
    if isinstance(exc, (TimeoutError, OSError)):
        return True, "network_error", str(exc) or exc.__class__.__name__
    return False, "task_poll_error", str(exc) or exc.__class__.__name__


# ------------------------------------------------------------- serialisation --
def _artifact_rows(task_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    ids = [task_id for task_id in task_ids if task_id]
    if not ids:
        return {}
    rows = db.query_all(
        f"""SELECT id, filename, content_type, size, purpose, sha256, task_id, invocation_id, created_at
              FROM files WHERE task_id IN ({db.sql_placeholders(len(ids))})
             ORDER BY created_at ASC""",
        ids,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        item["url"] = f"/api/files/{item['id']}"
        grouped.setdefault(item.pop("task_id") or "", []).append(item)
    return grouped


def public_task(
    row: dict[str, Any],
    *,
    artifacts: list[dict[str, Any]] | None = None,
    include_result: bool = False,
) -> dict[str, Any]:
    """Contract §5.6 task row. Never exposes leases or raw storage paths."""
    task_id = row["id"]
    raw_result = row.get("result")
    result_doc = db.loads(raw_result, {}) if raw_result else {}
    if not isinstance(result_doc, dict):
        result_doc = {"result": result_doc}
    if artifacts is None:
        artifacts = _artifact_rows([task_id]).get(task_id, [])
    payload: dict[str, Any] = {
        "id": task_id,
        "request_id": row.get("request_id"),
        "kind": row.get("kind") or "capability_async",
        "status": row.get("status"),
        "progress": int(row.get("progress") or 0),
        "progress_note": row.get("progress_note"),
        "upstream_task_id": row.get("upstream_task_id"),
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "capability": None,
        "provider": None,
        "agent": None,
        "route_id": row.get("route_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
        "deadline": row.get("deadline"),
        "poll_after": row.get("poll_after"),
        "result_summary": _result_summary(result_doc),
        "artifacts": artifacts,
        "error": db.loads(row.get("error"), None) if row.get("error") else None,
        "budget_usd": row.get("budget_usd"),
        "cost": {
            "actual_usd": row.get("cost_actual_usd"),
            "currency": "USD",
        },
    }
    if row.get("capability_id"):
        payload["capability"] = {
            "id": row["capability_id"],
            "slug": row.get("capability_slug"),
            "name": row.get("capability_name"),
        }
    if row.get("provider_id"):
        payload["provider"] = {"id": row["provider_id"], "name": row.get("provider_name")}
    if row.get("agent_id"):
        payload["agent"] = {"id": row["agent_id"], "name": row.get("agent_name")}
    if include_result:
        payload["result"] = result_doc.get("result") if "result" in result_doc else result_doc
        payload["result_document"] = result_doc
    return payload


def serialize_invocation(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """Contract §5.7/§5.6 invocation row. Previews are already redacted at rest."""
    error = None
    if row.get("error_code"):
        error = {"code": row["error_code"], "message": row.get("error_message"), "action": None}
    payload: dict[str, Any] = {
        "id": row["id"],
        "request_id": row.get("request_id"),
        "caller_type": row.get("caller_type"),
        "agent": {"id": row["agent_id"], "name": row.get("agent_name")} if row.get("agent_id") else None,
        "surface": row.get("surface"),
        "capability": (
            {"id": row["capability_id"], "slug": row.get("capability_slug"), "name": row.get("capability_name")}
            if row.get("capability_id")
            else None
        ),
        "version": row.get("version_number"),
        "provider": {"id": row["provider_id"], "name": row.get("provider_name")} if row.get("provider_id") else None,
        "mode": row.get("mode"),
        "status": row.get("status"),
        "http_status": row.get("http_status"),
        "latency_ms": row.get("latency_ms"),
        "retries": int(row.get("retries") or 0),
        "task_id": row.get("task_id"),
        "usage": db.loads(row.get("usage"), {}),
        "cost": {
            "estimated_usd": row.get("cost_estimate_usd"),
            "actual_usd": row.get("cost_actual_usd"),
            "currency": "USD",
        },
        "error": error,
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
    }
    if detail:
        payload.update(
            {
                "route_id": row.get("route_id"),
                "route_reason": row.get("route_reason"),
                "idempotency_key": row.get("idempotency_key"),
                "request_digest": row.get("request_digest"),
                "request_preview": row.get("request_preview"),
                "response_preview": row.get("response_preview"),
            }
        )
    return payload


def build_timeline(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Timeline for §5.6 task detail: task fields plus the invocation rows."""
    events: list[dict[str, Any]] = [
        {
            "at": task.get("created_at"),
            "type": "task.created",
            "status": "queued",
            "title": "Task created",
            "detail": f"Queued for {task.get('capability_slug') or task.get('kind') or 'the worker'}.",
            "source": "task",
        }
    ]
    if task.get("upstream_task_id"):
        events.append(
            {
                "at": task.get("created_at"),
                "type": "task.submitted",
                "status": "queued",
                "title": "Upstream job accepted",
                "detail": f"Provider job id {task['upstream_task_id']}.",
                "source": "task",
            }
        )
    for invocation in _invocations_for_task(task, limit=25):
        events.append(
            {
                "at": invocation.get("created_at"),
                "type": f"invocation.{invocation.get('status')}",
                "status": invocation.get("status"),
                "title": f"Invocation {invocation.get('request_id')} opened",
                "detail": preview(
                    {
                        "mode": invocation.get("mode"),
                        "surface": invocation.get("surface"),
                        "http_status": invocation.get("http_status"),
                        "error": invocation.get("error_code"),
                    },
                    limit=300,
                ),
                "source": "invocation",
                "invocation_id": invocation["id"],
            }
        )
        if invocation.get("finished_at"):
            events.append(
                {
                    "at": invocation["finished_at"],
                    "type": "invocation.finished",
                    "status": invocation.get("status"),
                    "title": "Invocation finished",
                    "detail": preview(
                        {"status": invocation.get("status"), "error": invocation.get("error_code")},
                        limit=300,
                    ),
                    "source": "invocation",
                    "invocation_id": invocation["id"],
                }
            )
    if task.get("attempts"):
        events.append(
            {
                "at": task.get("updated_at"),
                "type": "task.polled",
                "status": task.get("status"),
                "title": "Status polling",
                "detail": f"{int(task['attempts'])} status check(s) performed against the provider.",
                "source": "task",
            }
        )
    if task.get("progress_note"):
        events.append(
            {
                "at": task.get("updated_at"),
                "type": "task.note",
                "status": task.get("status"),
                "title": "Latest progress note",
                "detail": task.get("progress_note"),
                "source": "task",
            }
        )
    if task.get("finished_at"):
        error = db.loads(task.get("error"), None) if task.get("error") else None
        events.append(
            {
                "at": task["finished_at"],
                "type": f"task.{task.get('status')}",
                "status": task.get("status"),
                "title": f"Task {task.get('status')}",
                "detail": (error or {}).get("message") or task.get("progress_note") or "",
                "source": "task",
            }
        )
    events.sort(key=lambda event: event.get("at") or "")
    return events


# ------------------------------------------------------------------- reads ---
def get_row(task_id: str) -> dict[str, Any]:
    """Raw task row (internal: includes leases). Raises not_found."""
    row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        raise not_found("Task")
    return row


def get_task_row(task_id: str) -> dict[str, Any]:
    """Task row joined with capability/provider/agent names."""
    row = db.query_one(_TASK_SELECT + " WHERE t.id = ?", (task_id,))
    if not row:
        raise not_found("Task")
    return row


async def list_tasks(
    *,
    status: str | None = None,
    capability: str | None = None,
    agent: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        statuses = [part.strip() for part in status.split(",") if part.strip()]
        if statuses:
            where.append(f"t.status IN ({db.sql_placeholders(len(statuses))})")
            params.extend(statuses)
    if capability:
        where.append("(c.slug = ? OR t.capability_id = ?)")
        params.extend([capability, capability])
    if agent:
        where.append("(t.agent_id = ? OR a.name = ?)")
        params.extend([agent, agent])
    if search:
        like = f"%{search}%"
        where.append(
            "(t.id LIKE ? OR t.request_id LIKE ? OR COALESCE(t.upstream_task_id,'') LIKE ? "
            "OR COALESCE(t.progress_note,'') LIKE ? OR COALESCE(c.name,'') LIKE ?)"
        )
        params.extend([like] * 5)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.query_scalar(
        "SELECT COUNT(*) FROM tasks t "
        "LEFT JOIN capabilities c ON c.id = t.capability_id "
        "LEFT JOIN agents a ON a.id = t.agent_id "
        f"{clause}",
        params,
    ) or 0
    rows = db.query_all(
        _TASK_SELECT + f" {clause} ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    artifacts = _artifact_rows(row["id"] for row in rows)
    return [public_task(row, artifacts=artifacts.get(row["id"], [])) for row in rows], int(total)


async def get_task(task_id: str) -> dict[str, Any]:
    """Task detail: contract row + `events[]` timeline + artifacts."""
    row = get_task_row(task_id)
    payload = public_task(row, include_result=True)
    payload["events"] = build_timeline(row)
    return payload


async def task_events(task_id: str) -> list[dict[str, Any]]:
    return build_timeline(get_task_row(task_id))


async def list_invocations(
    *,
    status: str | None = None,
    capability: str | None = None,
    agent: str | None = None,
    surface: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        statuses = [part.strip() for part in status.split(",") if part.strip()]
        if statuses:
            where.append(f"i.status IN ({db.sql_placeholders(len(statuses))})")
            params.extend(statuses)
    if capability:
        where.append("(c.slug = ? OR i.capability_id = ?)")
        params.extend([capability, capability])
    if agent:
        where.append("(i.agent_id = ? OR a.name = ?)")
        params.extend([agent, agent])
    if surface:
        where.append("i.surface = ?")
        params.append(surface)
    if search:
        like = f"%{search}%"
        where.append(
            "(i.id LIKE ? OR i.request_id LIKE ? OR COALESCE(i.error_code,'') LIKE ? "
            "OR COALESCE(i.error_message,'') LIKE ? OR COALESCE(c.name,'') LIKE ?)"
        )
        params.extend([like] * 5)
    if created_from:
        where.append("i.created_at >= ?")
        params.append(created_from)
    if created_to:
        where.append("i.created_at <= ?")
        params.append(created_to)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.query_scalar(
        "SELECT COUNT(*) FROM invocations i "
        "LEFT JOIN capabilities c ON c.id = i.capability_id "
        "LEFT JOIN agents a ON a.id = i.agent_id "
        f"{clause}",
        params,
    ) or 0
    rows = db.query_all(
        _INVOCATION_SELECT + f" {clause} ORDER BY i.created_at DESC, i.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [serialize_invocation(row) for row in rows], int(total)


async def get_invocation(invocation_id: str) -> dict[str, Any]:
    row = db.query_one(_INVOCATION_SELECT + " WHERE i.id = ? OR i.request_id = ?", (invocation_id, invocation_id))
    if not row:
        raise not_found("Invocation")
    return serialize_invocation(row, detail=True)


def _invocations_for_task(task: dict[str, Any], *, limit: int = 25) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM invocations WHERE task_id = ? OR request_id = ? ORDER BY created_at ASC LIMIT ?",
        (task["id"], task.get("request_id"), limit),
    )
    return rows


def _invocation_for_task(task: dict[str, Any]) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM invocations WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task["id"],))
    if row:
        return row
    return db.query_one(
        "SELECT * FROM invocations WHERE request_id = ? ORDER BY created_at DESC LIMIT 1",
        (task.get("request_id"),),
    )


# ----------------------------------------------------------- config lookup ---
def _resolve_task_config(
    task: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Capability + (pinned or current published) version config for a task."""
    capability: dict[str, Any] | None = None
    if task.get("capability_id"):
        capability = db.query_one("SELECT * FROM capabilities WHERE id = ?", (task["capability_id"],))
    if not capability:
        return None, None, {}
    version: dict[str, Any] | None = None
    if task.get("version_id"):
        version = db.query_one("SELECT * FROM capability_versions WHERE id = ?", (task["version_id"],))
    if version is None:
        try:
            version = execution.get_version(capability, published_only=False)
        except ApiError:
            version = None
    raw_config = version.get("config") if version else None
    parsed = raw_config if isinstance(raw_config, dict) else db.loads(raw_config, {})
    return capability, version, merge_defaults(parsed or {}, capability.get("type", "generic"))


def _credential_id(task: dict[str, Any], capability: dict[str, Any] | None) -> str | None:
    route_id = task.get("route_id")
    if route_id:
        route = db.query_one("SELECT credential_id FROM routes WHERE id = ?", (route_id,))
        if route and route.get("credential_id"):
            return route["credential_id"]
    return (capability or {}).get("credential_id")


# ------------------------------------------------------------- transitions ---
def _finish_task(
    task: dict[str, Any],
    *,
    status: str,
    error: dict[str, Any] | None = None,
    progress_note: str | None = None,
    progress: int | None = None,
    result_document: dict[str, Any] | None = None,
    invocation_status: str | None = None,
    usage: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = task["id"]
    now = _now()
    updates: dict[str, Any] = {
        "status": status,
        "updated_at": now,
        "finished_at": now,
        "poll_after": None,
        "lease_owner": None,
        "lease_until": None,
        "error": db.dumps(error) if error else None,
    }
    if progress is not None:
        updates["progress"] = max(0, min(100, int(progress)))
    if status == "succeeded":
        updates["progress"] = 100
    if progress_note is not None:
        updates["progress_note"] = preview(progress_note, limit=500)
    if result_document is not None:
        updates["result"] = db.dumps(result_document)
    if cost is not None and cost.get("actual_usd") is not None:
        updates["cost_actual_usd"] = cost["actual_usd"]
    db.update("tasks", task_id, updates)

    invocation = _invocation_for_task(task)
    if invocation and invocation_status:
        inv_updates: dict[str, Any] = {"status": invocation_status, "finished_at": now}
        if usage:
            inv_updates["usage"] = db.dumps(usage)
        if cost:
            if cost.get("estimated_usd") is not None:
                inv_updates["cost_estimate_usd"] = cost["estimated_usd"]
            if cost.get("actual_usd") is not None:
                inv_updates["cost_actual_usd"] = cost["actual_usd"]
        if result_document is not None:
            inv_updates["response_preview"] = preview(
                {"result": result_document.get("result"), "artifacts": result_document.get("artifacts") or []},
                limit=2000,
            )
        if error:
            inv_updates["error_code"] = error.get("code")
            inv_updates["error_message"] = preview(error.get("message"), limit=500)
        db.update("invocations", invocation["id"], inv_updates)

    audit.record(
        actor_type="system",
        action=f"task.{status}",
        object_type="task",
        object_id=task_id,
        summary=f"Task {task_id} -> {status}" + (f" ({(error or {}).get('code')})" if error else ""),
        metadata={
            "status": status,
            "error_code": (error or {}).get("code"),
            "attempts": int(task.get("attempts") or 0),
            "upstream_task_id": task.get("upstream_task_id"),
        },
    )
    return public_task(get_task_row(task_id), include_result=True)


def _register_poll_error(
    task: dict[str, Any],
    *,
    config: dict[str, Any],
    attempts: int,
    code: str,
    message: str,
    transient: bool,
) -> dict[str, Any]:
    """A failed/rejected status probe: retry with backoff, or end up uncertain."""
    task_id = task["id"]
    now_dt = db.utcnow()
    deadline = db.parse_iso(task.get("deadline"))
    expired = bool(deadline and deadline <= now_dt)
    settings = _settings()
    max_attempts = max(1, int(task.get("max_attempts") or settings.task_max_attempts or 3))

    if transient and not expired and attempts < max_attempts:
        delay = _backoff_seconds(config, attempts)
        note = (
            f"Status check failed ({code}): {message} "
            f"Retrying in about {int(round(delay))}s (attempt {attempts}/{max_attempts})."
        )
        db.update(
            "tasks",
            task_id,
            {
                "progress_note": preview(note, limit=500),
                "poll_after": db.iso(now_dt + timedelta(seconds=delay)),
                "updated_at": db.iso(now_dt),
                "lease_owner": None,
                "lease_until": None,
            },
        )
        return public_task(get_task_row(task_id), include_result=True)

    if expired:
        error = {
            "code": TIMEOUT_CODE,
            "message": preview(
                f"The task passed its deadline before the provider reported a final state. "
                f"Last status error: {code} — {message}",
                limit=600,
            ),
            "action": "Check the provider dashboard for this job; it may still complete and be billed.",
            "attempts": attempts,
        }
        return _finish_task(
            task,
            status="uncertain",
            error=error,
            progress_note="Deadline passed while the provider status was still unknown.",
            invocation_status="uncertain",
        )
    if transient:
        error = {
            "code": POLL_FAILED_CODE,
            "message": preview(f"Status polling failed after {attempts} attempt(s): {code} — {message}", limit=600),
            "action": "Reconcile the task manually once the provider is reachable again.",
            "attempts": attempts,
        }
        return _finish_task(
            task,
            status="uncertain",
            error=error,
            progress_note=f"Gave up polling after {attempts} attempt(s); the upstream outcome is unknown.",
            invocation_status="uncertain",
        )
    error = {
        "code": code,
        "message": preview(message, limit=600),
        "action": "Fix the capability's async.status configuration and reconcile the task.",
        "attempts": attempts,
    }
    return _finish_task(
        task,
        status="failed",
        error=error,
        progress_note=f"The provider status probe failed permanently ({code}).",
        invocation_status="failed",
    )


def _extract_poll_body(
    config: dict[str, Any],
    body: Any,
    *,
    invocation_id: str,
    task_id: str,
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None:
    """Reuse the executor's extraction rules (artifacts, usage, cost)."""
    try:
        result, artifacts, usage, cost, _schema_valid = execution.extract_result(
            config=config,
            body=body,
            response=None,
            invocation_id=invocation_id,
            task_id=task_id,
            latency_ms=0,
            redactor=Redactor(),
        )
        return result, artifacts, usage, cost
    except Exception:  # noqa: BLE001 - extraction must never break polling
        return None


async def _complete_success(
    task: dict[str, Any],
    *,
    config: dict[str, Any],
    payload: dict[str, Any],
    progress: int,
) -> dict[str, Any]:
    task_id = task["id"]
    invocation = _invocation_for_task(task)
    invocation_id = invocation["id"] if invocation else None
    result_value = payload.get("result")
    artifacts: list[dict[str, Any]] = []
    usage: dict[str, Any] = {"source": "provider"}
    cost: dict[str, Any] = {
        "estimated_usd": None,
        "actual_usd": None,
        "currency": (config.get("pricing") or {}).get("currency", "USD"),
    }
    body = _body_from_payload(payload)
    if body is not None and invocation_id:
        extracted = _extract_poll_body(config, body, invocation_id=invocation_id, task_id=task_id)
        if extracted:
            ex_result, ex_artifacts, ex_usage, ex_cost = extracted
            if result_value is None and ex_result is not None:
                result_value = ex_result
            artifacts = ex_artifacts or []
            usage = ex_usage or usage
            cost = ex_cost or cost
        # A finished video/image often arrives as a URL rather than inline
        # bytes. Fetch it into managed storage now, while the URL is fresh.
        if not artifacts:
            downloaded, artifact_error = await execution.capture_artifact_url(
                config=config,
                body=body,
                task_id=task_id,
                invocation_id=invocation_id,
            )
            if downloaded:
                artifacts = downloaded
            elif artifact_error:
                usage = {**usage, "artifact_error": artifact_error}
    elif invocation and invocation.get("usage"):
        usage = db.loads(invocation["usage"], usage) or usage

    result_document = {
        "result": result_value,
        "artifacts": artifacts,
        "upstream_status": payload.get("upstream_status"),
        "captured_at": _now(),
    }
    return _finish_task(
        task,
        status="succeeded",
        progress=100 if progress <= 0 else progress,
        progress_note="The provider reported the job as finished.",
        result_document=result_document,
        invocation_status="succeeded",
        usage=usage,
        cost=cost,
    )


async def _apply_status(
    task: dict[str, Any],
    *,
    config: dict[str, Any],
    payload: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    task_id = task["id"]
    normalized = str(payload.get("status") or "unknown").lower()
    upstream_status = payload.get("upstream_status")
    progress = _coerce_progress(payload.get("progress"), task)
    current = task["status"]
    now_dt = db.utcnow()
    now = db.iso(now_dt)

    if normalized == "succeeded":
        return await _complete_success(task, config=config, payload=payload, progress=progress)
    if normalized == "failed":
        error = {
            "code": "task_failed",
            "message": preview(f"The provider reported the job as failed (status={upstream_status}).", limit=500),
            "action": "Open the invocation record; the provider's own message is kept in the task result.",
            "attempts": attempts,
        }
        return _finish_task(
            task,
            status="failed",
            error=error,
            progress=progress,
            progress_note=f"Upstream reported {upstream_status!r}; the job failed.",
            invocation_status="failed",
        )
    if normalized == "cancelled":
        return _finish_task(
            task,
            status="cancelled",
            progress=progress,
            progress_note="The provider confirmed the job was cancelled.",
            invocation_status="cancelled",
        )
    if normalized in {"queued", "running"}:
        deadline = db.parse_iso(task.get("deadline"))
        if deadline and deadline <= now_dt:
            # The provider is still working but our window closed: the outcome is
            # unknown to *us*, not a failure of theirs.
            return _finish_task(
                task,
                status="uncertain",
                error={
                    "code": TIMEOUT_CODE,
                    "message": preview(
                        f"The task passed its deadline while the provider still reported {upstream_status!r}.",
                        limit=500,
                    ),
                    "action": "Check the provider dashboard for this job; it may still complete and be billed.",
                    "attempts": attempts,
                },
                progress=progress,
                progress_note="Deadline passed before the provider reported a final state.",
                invocation_status="uncertain",
            )
        if current == "cancel_requested":
            new_status = "cancel_requested"
        elif normalized == "running" or current == "running":
            new_status = "running"
        else:
            new_status = "queued"
        note = f"Upstream reports {upstream_status!r}."
        if new_status == "cancel_requested":
            note += " A cancellation was requested; the provider has not confirmed it yet."
        delay = _poll_interval(config)
        db.update(
            "tasks",
            task_id,
            {
                "status": new_status,
                "progress": progress,
                "progress_note": preview(note, limit=500),
                "poll_after": db.iso(now_dt + timedelta(seconds=delay)),
                "updated_at": now,
                "lease_owner": None,
                "lease_until": None,
                "error": None,
            },
        )
        return public_task(get_task_row(task_id), include_result=True)

    delay = _poll_interval(config)
    note = (
        f"The provider returned an unrecognised status {upstream_status!r}; "
        f"keeping the task as {current!r} and retrying in about {int(round(delay))}s."
    )
    db.update(
        "tasks",
        task_id,
        {
            "progress": progress,
            "progress_note": preview(note, limit=500),
            "poll_after": db.iso(now_dt + timedelta(seconds=delay)),
            "updated_at": now,
            "lease_owner": None,
            "lease_until": None,
        },
    )
    return public_task(get_task_row(task_id), include_result=True)


async def poll_task(task_id: str) -> dict[str, Any]:
    """One upstream status fetch, mapped onto the task row."""
    task = get_row(task_id)
    if task["status"] in TERMINAL_TASK_STATUSES:
        return public_task(get_task_row(task_id), include_result=True)
    if not task.get("upstream_task_id"):
        return _finish_task(
            task,
            status="failed",
            error={
                "code": "task_not_submitted",
                "message": "This task has no upstream job id, so it can never report a status.",
                "action": "Re-run the call; the provider never accepted it.",
            },
            progress_note="No upstream job id was recorded for this task.",
            invocation_status="failed",
        )

    capability, _version, config = _resolve_task_config(task)
    credential_id = _credential_id(task, capability)
    attempts = int(task.get("attempts") or 0) + 1
    db.update("tasks", task_id, {"attempts": attempts, "updated_at": _now()})

    input_payload = db.loads(task.get("input"), {}) or {}
    if not isinstance(input_payload, dict):
        input_payload = {}

    try:
        payload = await execution.fetch_status(
            config=config,
            upstream_task_id=str(task["upstream_task_id"]),
            input_payload=input_payload,
            credential_id=credential_id,
        )
    except Exception as exc:  # noqa: BLE001 - classified below, never fatal
        transient, code, message = _classify_poll_error(exc)
        return _register_poll_error(
            get_row(task_id),
            config=config,
            attempts=attempts,
            code=code,
            message=message,
            transient=transient,
        )

    if not isinstance(payload, dict):
        return _register_poll_error(
            get_row(task_id),
            config=config,
            attempts=attempts,
            code="task_poll_error",
            message="The status probe returned no usable payload.",
            transient=True,
        )

    if not payload.get("ok", True):
        error = payload.get("error") or {}
        status_code = int(payload.get("http_status") or 0)
        transient = bool(error.get("retryable")) or status_code >= 500 or status_code == 429
        return _register_poll_error(
            get_row(task_id),
            config=config,
            attempts=attempts,
            code=error.get("code") or "upstream_status_error",
            message=error.get("message") or "The provider refused the status request.",
            transient=transient,
        )

    return await _apply_status(get_row(task_id), config=config, payload=payload, attempts=attempts)


async def cancel_task(task_id: str, actor: dict[str, Any] | str | None = None, *, reason: str | None = None) -> dict[str, Any]:
    """Truthful cancellation.

    Never reports ``cancelled`` because the local poll stopped: only a terminal
    provider state (or the absence of any upstream job) does.
    """
    task = get_row(task_id)
    actor_fields = _actor_fields(actor)

    if task["status"] in TERMINAL_TASK_STATUSES:
        payload = public_task(get_task_row(task_id), include_result=True)
        return {
            "task": payload,
            "task_id": task_id,
            "status": task["status"],
            "supported": False,
            "canceled": False,
            "cancel_requested": False,
            "upstream_may_continue": task["status"] == "uncertain",
            "message": f"The task is already {task['status']}; there is nothing to cancel.",
        }

    audit.record(
        actor_type=actor_fields["type"],
        actor_id=actor_fields["id"],
        action="task.cancel_requested",
        object_type="task",
        object_id=task_id,
        summary=f"Cancel requested for task {task_id}.",
        metadata={"reason": reason, "status": task["status"], "upstream_task_id": task.get("upstream_task_id")},
        ip=actor_fields["ip"],
    )

    if not task.get("upstream_task_id"):
        payload = _finish_task(
            get_row(task_id),
            status="cancelled",
            progress_note="No upstream job was submitted, so the local task was cancelled.",
            invocation_status="cancelled",
        )
        return {
            "task": payload,
            "task_id": task_id,
            "status": "cancelled",
            "supported": True,
            "canceled": True,
            "cancel_requested": False,
            "upstream_may_continue": False,
            "message": "The task was cancelled before any upstream job existed.",
        }

    capability, _version, config = _resolve_task_config(task)
    credential_id = _credential_id(task, capability)
    input_payload = db.loads(task.get("input"), {}) or {}
    if not isinstance(input_payload, dict):
        input_payload = {}

    try:
        outcome = await execution.request_cancel(
            config=config,
            upstream_task_id=str(task["upstream_task_id"]),
            input_payload=input_payload,
            credential_id=credential_id,
        )
    except Exception as exc:  # noqa: BLE001 - cancellation must always answer
        _transient, code, message = _classify_poll_error(exc)
        outcome = {"supported": True, "status": "failed", "message": message, "error_code": code}

    supported = bool(outcome.get("supported", True))
    now = _now()

    if not supported:
        note = (
            f"Cancel requested at {now}, but this provider exposes no cancel endpoint: "
            "nothing was cancelled upstream. The job may still complete and be billed, so the "
            "task keeps tracking the real outcome instead of claiming a cancellation."
        )
        db.update(
            "tasks",
            task_id,
            {
                "progress_note": preview(note, limit=500),
                "poll_after": now,
                "updated_at": now,
                "lease_owner": None,
                "lease_until": None,
            },
        )
        payload = public_task(get_task_row(task_id), include_result=True)
        return {
            "task": payload,
            "task_id": task_id,
            "status": payload["status"],
            "supported": False,
            "canceled": False,
            "cancel_requested": True,
            "upstream_may_continue": True,
            "message": (
                "The provider has no cancel endpoint, so the upstream job was not cancelled. "
                "It may still complete and be billed; reconcile the task to pick up the result."
            ),
        }

    if outcome.get("status") != "accepted":
        note = f"The provider did not accept the cancellation request: {outcome.get('message') or 'no reason given'}"
        db.update("tasks", task_id, {"progress_note": preview(note, limit=500), "updated_at": now})
        payload = public_task(get_task_row(task_id), include_result=True)
        return {
            "task": payload,
            "task_id": task_id,
            "status": payload["status"],
            "supported": True,
            "canceled": False,
            "cancel_requested": False,
            "upstream_may_continue": True,
            "message": note,
        }

    db.update(
        "tasks",
        task_id,
        {
            "status": "cancel_requested",
            "progress_note": "The provider accepted the cancellation request; waiting for its final state.",
            "updated_at": now,
            "lease_owner": None,
            "lease_until": None,
        },
    )
    refreshed = await poll_task(task_id)
    canceled = refreshed.get("status") == "cancelled"
    return {
        "task": refreshed,
        "task_id": task_id,
        "status": refreshed.get("status"),
        "supported": True,
        "canceled": canceled,
        "cancel_requested": True,
        "upstream_may_continue": not canceled,
        "message": (
            "The provider accepted the cancellation and confirmed the job as cancelled."
            if canceled
            else f"The provider accepted the cancellation request; the job is still {refreshed.get('status')} upstream."
        ),
    }


async def reconcile_task(task_id: str, actor: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """Force one status fetch, bypassing the scheduled poll time."""
    task = get_row(task_id)
    actor_fields = _actor_fields(actor)
    audit.record(
        actor_type=actor_fields["type"],
        actor_id=actor_fields["id"],
        action="task.reconcile",
        object_type="task",
        object_id=task_id,
        summary=f"Manual upstream reconciliation of task {task_id}.",
        metadata={"status": task["status"], "upstream_task_id": task.get("upstream_task_id")},
        ip=actor_fields["ip"],
    )
    if task["status"] in TERMINAL_TASK_STATUSES:
        return public_task(get_task_row(task_id), include_result=True)
    return await poll_task(task_id)


async def recover_stale_tasks() -> dict[str, Any]:
    """Worker startup / periodic recovery.

    * active tasks past their deadline -> ``uncertain`` with ``task_timeout``
      (never ``failed``: the provider may still be working);
    * overdue tasks with an expired lease -> requeued immediately.
    """
    settings = _settings()
    now_dt = db.utcnow()
    now = db.iso(now_dt)

    stale = db.query_all(
        "SELECT id FROM tasks WHERE status IN ('queued','running','cancel_requested') "
        "AND deadline IS NOT NULL AND deadline <= ?",
        (now,),
    )
    timed_out_ids: list[str] = []
    for row in stale:
        task = db.query_one("SELECT * FROM tasks WHERE id = ?", (row["id"],))
        if not task:
            continue
        _finish_task(
            task,
            status="uncertain",
            error={
                "code": TIMEOUT_CODE,
                "message": "The task passed its deadline before the provider reported a final state.",
                "action": "Check the provider dashboard for this job; it may still complete and be billed.",
            },
            progress_note="Deadline passed without a final provider state.",
            invocation_status="uncertain",
        )
        timed_out_ids.append(task["id"])

    grace = max(60.0, float(settings.worker_poll_seconds or 1.0) * 20)
    overdue = db.iso(now_dt - timedelta(seconds=grace))
    requeued = db.execute(
        "UPDATE tasks SET poll_after = ?, lease_owner = NULL, lease_until = NULL, updated_at = ? "
        "WHERE status IN ('queued','running','cancel_requested') "
        "AND (poll_after IS NULL OR poll_after <= ?) "
        "AND (lease_until IS NULL OR lease_until <= ?)",
        (now, now, overdue, now),
    )
    return {"timed_out": len(timed_out_ids), "task_ids": timed_out_ids, "requeued": int(requeued or 0), "checked_at": now}


# ------------------------------------------------------------------ leases ---
async def claim_due_tasks(owner: str, limit: int = 5) -> list[dict[str, Any]]:
    """Claim due tasks with a lease inside BEGIN IMMEDIATE (single-writer safe)."""
    now_dt = db.utcnow()
    now = db.iso(now_dt)
    lease_until = db.iso(now_dt + timedelta(seconds=int(_settings().worker_lease_seconds)))
    claimed: list[dict[str, Any]] = []
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks "
            "WHERE status IN ('queued','running','cancel_requested') "
            "AND (poll_after IS NULL OR poll_after <= ?) "
            "AND (lease_until IS NULL OR lease_until <= ?) "
            "ORDER BY COALESCE(poll_after, created_at) ASC LIMIT ?",
            (now, now, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            cursor = conn.execute(
                "UPDATE tasks SET lease_owner = ?, lease_until = ?, updated_at = ? "
                "WHERE id = ? AND (lease_until IS NULL OR lease_until <= ?)",
                (owner, lease_until, now, row["id"], now),
            )
            if cursor.rowcount:
                claimed.append(dict(row))
        for item in claimed:
            full = conn.execute("SELECT * FROM tasks WHERE id = ?", (item["id"],)).fetchone()
            item.update(dict(full) if full is not None else {})
    return claimed


async def release_task_lease(task_id: str, owner: str | None = None) -> None:
    if owner:
        db.execute(
            "UPDATE tasks SET lease_owner = NULL, lease_until = NULL WHERE id = ? AND lease_owner = ?",
            (task_id, owner),
        )
    else:
        db.execute("UPDATE tasks SET lease_owner = NULL, lease_until = NULL WHERE id = ?", (task_id,))


async def release_owner_leases(owner: str) -> dict[str, int]:
    """Graceful shutdown: hand everything this worker holds back to the queue."""
    now = _now()
    tasks_released = db.execute(
        "UPDATE tasks SET lease_owner = NULL, lease_until = NULL, updated_at = ? "
        "WHERE lease_owner = ? AND status IN ('queued','running','cancel_requested')",
        (now, owner),
    )
    jobs_released = db.execute(
        "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_until = NULL, updated_at = ? "
        "WHERE lease_owner = ? AND status = 'running'",
        (now, owner),
    )
    return {"tasks": int(tasks_released or 0), "jobs": int(jobs_released or 0)}


# -------------------------------------------------------------- job queue ----
async def enqueue_job(
    kind: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    run_at: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    job_id = db.new_id("job")
    now = _now()
    row = {
        "id": job_id,
        "kind": kind,
        "payload": db.dumps(payload or {}),
        "status": "queued",
        "priority": int(priority),
        "run_at": run_at or now,
        "attempts": 0,
        "max_attempts": max(1, int(max_attempts)),
        "created_at": now,
        "updated_at": now,
    }
    db.insert("jobs", row)
    return {**row, "payload": payload or {}}


async def claim_jobs(owner: str, limit: int = 5) -> list[dict[str, Any]]:
    """Claim due jobs; expired leases are reclaimed first so nothing is stuck."""
    now_dt = db.utcnow()
    now = db.iso(now_dt)
    lease_until = db.iso(now_dt + timedelta(seconds=int(_settings().worker_lease_seconds)))
    claimed: list[dict[str, Any]] = []
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?",
            (now, now),
        )
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' AND run_at <= ? "
            "ORDER BY priority ASC, run_at ASC, created_at ASC LIMIT ?",
            (now, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'running', lease_owner = ?, lease_until = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'queued'",
                (owner, lease_until, now, row["id"]),
            )
            if cursor.rowcount:
                claimed.append(dict(row))
        for item in claimed:
            full = conn.execute("SELECT * FROM jobs WHERE id = ?", (item["id"],)).fetchone()
            if full is not None:
                item.update(dict(full))
                item["payload"] = db.loads(item.get("payload"), {})
    return claimed


async def finish_job(job_id: str, *, error: str | None = None, retry_at: str | None = None) -> dict[str, Any]:
    """Close a claimed job: success, retry (``retry_at``) or terminal failure."""
    now = _now()
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return {}
    if error is None:
        updates: dict[str, Any] = {
            "status": "succeeded",
            "finished_at": now,
            "updated_at": now,
            "lease_owner": None,
            "lease_until": None,
            "last_error": None,
        }
    elif retry_at:
        updates = {
            "status": "queued",
            "run_at": retry_at,
            "updated_at": now,
            "lease_owner": None,
            "lease_until": None,
            "last_error": preview(error, limit=500),
        }
    else:
        updates = {
            "status": "failed",
            "finished_at": now,
            "updated_at": now,
            "lease_owner": None,
            "lease_until": None,
            "last_error": preview(error, limit=500),
        }
    db.update("jobs", job_id, updates)
    refreshed = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {}
    refreshed["payload"] = db.loads(refreshed.get("payload"), {})
    return refreshed


async def queue_depth() -> dict[str, int]:
    return queue_depth_now()


def queue_depth_now() -> dict[str, int]:
    """Synchronous queue depth (health/readiness probes, CLI, tests)."""
    return {
        "tasks_active": int(
            db.query_scalar(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','cancel_requested')"
            )
            or 0
        ),
        "jobs_queued": int(db.query_scalar("SELECT COUNT(*) FROM jobs WHERE status = 'queued'") or 0),
        "jobs_running": int(db.query_scalar("SELECT COUNT(*) FROM jobs WHERE status = 'running'") or 0),
    }
