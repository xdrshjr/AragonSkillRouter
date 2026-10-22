"""Admin dashboard: counts, health and the actionable problem list (§5.1).

Every problem is derived from a real row — no placeholder content. Each entry
carries an ``id``, a ``severity``, a ``title``/``detail`` that says what is
actually wrong, and an ``href``/``action`` pointing at the route that fixes it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core import db
from app.deps import AuthUser, require_admin
from app.services import bootstrap

router = APIRouter(prefix="/api/admin", tags=["overview"])

CREDENTIAL_EXPIRY_WARNING_DAYS = 14
TOKEN_EXPIRY_WARNING_DAYS = 7
TASK_STALL_MINUTES = 15
RECENT_LIMIT = 8


def _problem(
    *, problem_id: str, severity: str, title: str, detail: str, href: str, action: str
) -> dict[str, Any]:
    return {
        "id": problem_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "href": href,
        "action": action,
    }


def _counts() -> dict[str, Any]:
    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(db.query_scalar(sql, params) or 0)

    expiring_before = db.iso(db.utcnow() + timedelta(days=CREDENTIAL_EXPIRY_WARNING_DAYS))
    return {
        "capabilities": {
            "total": scalar("SELECT COUNT(*) FROM capabilities"),
            "published": scalar("SELECT COUNT(*) FROM capabilities WHERE status = 'published'"),
            "draft": scalar("SELECT COUNT(*) FROM capabilities WHERE status = 'draft'"),
            "testing": scalar("SELECT COUNT(*) FROM capabilities WHERE status = 'testing'"),
            "disabled": scalar("SELECT COUNT(*) FROM capabilities WHERE status = 'disabled'"),
        },
        "providers": {
            "total": scalar("SELECT COUNT(*) FROM providers"),
            "active": scalar("SELECT COUNT(*) FROM providers WHERE status = 'active'"),
        },
        "credentials": {
            "total": scalar("SELECT COUNT(*) FROM credentials"),
            "active": scalar("SELECT COUNT(*) FROM credentials WHERE status = 'active'"),
            "expiring_soon": scalar(
                "SELECT COUNT(*) FROM credentials WHERE status = 'active' AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (expiring_before,),
            ),
            "invalid": scalar("SELECT COUNT(*) FROM credentials WHERE last_verify_status = 'invalid'"),
            "expired": scalar("SELECT COUNT(*) FROM credentials WHERE status = 'expired'"),
        },
        "skills": {
            "total": scalar("SELECT COUNT(*) FROM skills"),
            "published": scalar("SELECT COUNT(*) FROM skills WHERE status = 'published'"),
            "draft": scalar("SELECT COUNT(*) FROM skills WHERE status = 'draft'"),
        },
        "agents": {
            "total": scalar("SELECT COUNT(*) FROM agents"),
            "active": scalar("SELECT COUNT(*) FROM agents WHERE status = 'active'"),
        },
        "tasks": {
            "queued": scalar("SELECT COUNT(*) FROM tasks WHERE status = 'queued'"),
            "running": scalar("SELECT COUNT(*) FROM tasks WHERE status = 'running'"),
            "failed": scalar("SELECT COUNT(*) FROM tasks WHERE status = 'failed'"),
            "uncertain": scalar("SELECT COUNT(*) FROM tasks WHERE status = 'uncertain'"),
        },
    }


def _engine_health() -> dict[str, Any]:
    active = db.query_one("SELECT * FROM engine_releases WHERE status = 'active' ORDER BY activated_at DESC LIMIT 1")
    if not active:
        return {
            "status": "not_configured",
            "version": None,
            "adapter": None,
            "channel": db.get_setting("agent.upgrade_channel", "stable"),
        }
    return {
        "status": "active",
        "version": active["version"],
        "adapter": active["adapter_version"] or None,
        "channel": active["channel"],
        "release_id": active["id"],
        "activated_at": active["activated_at"],
    }


def _health() -> dict[str, Any]:
    since = db.iso(db.utcnow() - timedelta(hours=24))
    row = db.query_one(
        """SELECT COUNT(*) AS calls,
                  SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status IN ('failed','rejected') THEN 1 ELSE 0 END) AS failed,
                  AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) AS avg_latency
             FROM invocations WHERE created_at >= ?""",
        (since,),
    ) or {}
    calls = int(row.get("calls") or 0)
    succeeded = int(row.get("succeeded") or 0)
    failed_tasks = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND COALESCE(finished_at, updated_at) >= ?",
            (since,),
        )
        or 0
    )
    return {
        "success_rate_24h": round(succeeded / calls, 4) if calls else None,
        "calls_24h": calls,
        "failed_calls_24h": int(row.get("failed") or 0),
        "avg_latency_ms_24h": round(float(row.get("avg_latency") or 0), 1) if calls else None,
        "failed_tasks_24h": failed_tasks,
        "engine": _engine_health(),
        "generated_at": db.iso(),
    }


def _problems() -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    now = db.utcnow()

    # ---- credentials --------------------------------------------------------
    for row in db.query_all(
        "SELECT c.id, c.name, c.status, c.last_verify_status, c.last_verify_message, c.expires_at, p.name AS provider "
        "FROM credentials c LEFT JOIN providers p ON p.id = c.provider_id "
        "WHERE c.last_verify_status = 'invalid' ORDER BY c.updated_at DESC LIMIT 20"
    ):
        problems.append(
            _problem(
                problem_id=f"credential-invalid:{row['id']}",
                severity="error",
                title=f"Credential '{row['name']}' was rejected",
                detail=(
                    f"{row['provider'] or 'provider'} answered with an authentication error"
                    + (f": {row['last_verify_message']}" if row["last_verify_message"] else ".")
                    + " Calls routed through it cannot succeed."
                ),
                href=f"/providers?credential={row['id']}",
                action="Verify credential",
            )
        )
    for row in db.query_all(
        "SELECT c.id, c.name, c.expires_at, c.status, p.name AS provider FROM credentials c "
        "LEFT JOIN providers p ON p.id = c.provider_id "
        "WHERE c.expires_at IS NOT NULL AND c.status != 'disabled' AND c.expires_at <= ? "
        "ORDER BY c.expires_at ASC LIMIT 20",
        (db.iso(now + timedelta(days=CREDENTIAL_EXPIRY_WARNING_DAYS)),),
    ):
        expired = (db.parse_iso(row["expires_at"]) or now) <= now
        problems.append(
            _problem(
                problem_id=f"credential-expiring:{row['id']}",
                severity="error" if expired else "warning",
                title=(
                    f"Credential '{row['name']}' has expired"
                    if expired
                    else f"Credential '{row['name']}' expires soon"
                ),
                detail=f"{row['provider'] or 'provider'} · expires {row['expires_at']}.",
                href=f"/providers?credential={row['id']}",
                action="Rotate the secret",
            )
        )

    # ---- routing ------------------------------------------------------------
    for row in db.query_all(
        """SELECT r.id, r.capability_id, r.health_status, r.last_health_at,
                  c.slug, c.name
             FROM routes r JOIN capabilities c ON c.id = r.capability_id
            WHERE r.enabled = 1 AND r.health_status IN ('unhealthy', 'degraded')
            ORDER BY r.last_health_at DESC LIMIT 20"""
    ):
        problems.append(
            _problem(
                problem_id=f"route-{row['health_status']}:{row['id']}",
                severity="error" if row["health_status"] == "unhealthy" else "warning",
                title=f"Route for '{row['slug']}' is {row['health_status']}",
                detail=(
                    f"Last health check {row['last_health_at'] or 'never'}."
                    " Traffic falls back to the next compatible route only when one exists."
                ),
                href=f"/capabilities/{row['capability_id']}?route={row['id']}",
                action="Run health check",
            )
        )

    # ---- capabilities with failing tests ------------------------------------
    for row in db.query_all(
        """SELECT c.id, c.slug, c.status, t.status AS test_status, t.created_at
             FROM capabilities c
             JOIN capability_tests t ON t.id = (
                    SELECT t2.id FROM capability_tests t2
                     WHERE t2.capability_id = c.id
                     ORDER BY t2.created_at DESC, t2.id DESC LIMIT 1)
            WHERE c.status IN ('draft', 'testing') AND t.status = 'failed'
            LIMIT 20"""
    ):
        problems.append(
            _problem(
                problem_id=f"capability-test-failed:{row['id']}",
                severity="warning",
                title=f"'{row['slug']}' has a failing test",
                detail=f"The newest test ({row['created_at']}) failed, so the capability cannot be published.",
                href=f"/capabilities/{row['id']}",
                action="Fix the configuration and re-test",
            )
        )

    # ---- tasks --------------------------------------------------------------
    for row in db.query_all(
        "SELECT id, status, capability_id, progress_note, updated_at FROM tasks "
        "WHERE status = 'uncertain' ORDER BY updated_at DESC LIMIT 20"
    ):
        problems.append(
            _problem(
                problem_id=f"task-uncertain:{row['id']}",
                severity="error",
                title="A task has an uncertain outcome",
                detail=(
                    f"Task {row['id']} may have produced upstream output"
                    + (f" ({row['progress_note']})" if row["progress_note"] else "")
                    + ". Reconcile it before retrying so a paid call is not repeated."
                ),
                href=f"/records/{row['id']}",
                action="Reconcile task",
            )
        )
    stalled_before = db.iso(now - timedelta(minutes=TASK_STALL_MINUTES))
    for row in db.query_all(
        "SELECT id, status, updated_at FROM tasks WHERE status IN ('queued','running') "
        "AND updated_at <= ? ORDER BY updated_at ASC LIMIT 20",
        (stalled_before,),
    ):
        problems.append(
            _problem(
                problem_id=f"task-stalled:{row['id']}",
                severity="warning",
                title="A task looks stalled",
                detail=f"Task {row['id']} is {row['status']} but has not moved since {row['updated_at']}.",
                href=f"/records/{row['id']}",
                action="Check the worker and reconcile",
            )
        )

    # ---- tokens -------------------------------------------------------------
    for row in db.query_all(
        "SELECT t.id, t.name, t.expires_at, a.name AS agent FROM agent_tokens t "
        "JOIN agents a ON a.id = t.agent_id "
        "WHERE t.status = 'active' AND t.expires_at IS NOT NULL AND t.expires_at <= ? "
        "ORDER BY t.expires_at ASC LIMIT 20",
        (db.iso(now + timedelta(days=TOKEN_EXPIRY_WARNING_DAYS)),),
    ):
        problems.append(
            _problem(
                problem_id=f"token-expiring:{row['id']}",
                severity="warning",
                title=f"Token '{row['name']}' expires within {TOKEN_EXPIRY_WARNING_DAYS} days",
                detail=f"Agent {row['agent']} · expires {row['expires_at']}. The agent will stop working.",
                href=f"/access?token={row['id']}",
                action="Issue a replacement token",
            )
        )

    # ---- engine -------------------------------------------------------------
    for row in db.query_all(
        "SELECT id, version, ref, channel, created_at FROM engine_releases "
        "WHERE status = 'pending_approval' ORDER BY created_at DESC LIMIT 5"
    ):
        problems.append(
            _problem(
                problem_id=f"engine-pending-approval:{row['id']}",
                severity="warning",
                title=f"Engine release {row['version']} awaits approval",
                detail=f"{row['channel']}/{row['ref']} passed validation and needs a decision.",
                href="/settings/engine",
                action="Activate or roll back",
            )
        )
    for row in db.query_all(
        "SELECT id, version, checks FROM engine_releases WHERE status = 'failed' "
        "ORDER BY created_at DESC LIMIT 5"
    ):
        problems.append(
            _problem(
                problem_id=f"engine-failed:{row['id']}",
                severity="error",
                title=f"Engine release {row['version']} failed validation",
                detail="The current release stays active. Inspect the recorded checks before retrying.",
                href="/settings/engine",
                action="Review the checks",
            )
        )

    # ---- first-run security -------------------------------------------------
    if db.query_scalar("SELECT COUNT(*) FROM users WHERE must_change_password = 1"):
        problems.append(
            _problem(
                problem_id="bootstrap-password-unchanged",
                severity="warning",
                title="The bootstrap admin password has not been changed",
                detail="The initial password comes from deployment configuration and is still active.",
                href="/account",
                action="Change the password",
            )
        )

    # ---- SKILLs -------------------------------------------------------------
    from app.services import skills as skill_service

    for row in db.query_all("SELECT * FROM skills WHERE status != 'disabled' ORDER BY updated_at DESC LIMIT 30"):
        version = skill_service.current_version(row)
        if not version:
            continue
        validation = skill_service.validate_manifest(db.loads(version["manifest"], {}))
        if validation["blocking"]:
            first = validation["issues"][0] if validation["issues"] else {}
            problems.append(
                _problem(
                    problem_id=f"skill-invalid:{row['id']}",
                    severity="warning",
                    title=f"SKILL '{row['name']}' cannot be published",
                    detail=f"{first.get('field', 'manifest')}: {first.get('message', 'has blocking issues')}",
                    href=f"/skills/{row['id']}",
                    action="Fix the dependency or plan",
                )
            )
    for row in db.query_all("SELECT id, source_uri, created_at FROM skill_imports WHERE status = 'pending' LIMIT 10"):
        problems.append(
            _problem(
                problem_id=f"import-pending:{row['id']}",
                severity="warning",
                title="An import review is waiting",
                detail=f"{row['source_uri']} was staged on {row['created_at']} and has not been imported or rejected.",
                href=f"/skills?import={row['id']}",
                action="Review the import",
            )
        )

    problems.sort(key=lambda item: (0 if item["severity"] == "error" else 1, item["id"]))
    return problems


def _recent_invocations() -> list[dict[str, Any]]:
    rows = db.query_all(
        """SELECT i.*, c.slug AS capability_slug, c.name AS capability_name,
                  p.name AS provider_name, a.name AS agent_name
             FROM invocations i
             LEFT JOIN capabilities c ON c.id = i.capability_id
             LEFT JOIN providers p ON p.id = i.provider_id
             LEFT JOIN agents a ON a.id = i.agent_id
            ORDER BY i.created_at DESC LIMIT ?""",
        (RECENT_LIMIT,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        version = None
        if row.get("version_id"):
            version_row = db.query_one("SELECT version FROM capability_versions WHERE id = ?", (row["version_id"],))
            version = version_row["version"] if version_row else None
        out.append(
            {
                "id": row["id"],
                "request_id": row["request_id"],
                "caller_type": row["caller_type"],
                "agent": {"id": row["agent_id"], "name": row["agent_name"]} if row["agent_id"] else None,
                "surface": row["surface"],
                "capability": (
                    {"slug": row["capability_slug"], "name": row["capability_name"]}
                    if row["capability_id"]
                    else None
                ),
                "version": version,
                "provider": {"id": row["provider_id"], "name": row["provider_name"]} if row["provider_id"] else None,
                "mode": row["mode"],
                "status": row["status"],
                "http_status": row["http_status"],
                "latency_ms": row["latency_ms"],
                "retries": row["retries"],
                "task_id": row["task_id"],
                "usage": db.loads(row["usage"], {}),
                "cost": {
                    "estimated_usd": row["cost_estimate_usd"],
                    "actual_usd": row["cost_actual_usd"],
                    "currency": "USD",
                },
                "error": (
                    {"code": row["error_code"], "message": row["error_message"], "action": None}
                    if row["error_code"]
                    else None
                ),
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
        )
    return out


def _onboarding() -> dict[str, Any]:
    status = bootstrap.get_status()
    return {
        "steps": status.steps,
        "agent_configured": status.agent_configured,
        "initialized": status.initialized,
        "admin_username": status.admin_username,
        "must_change_password": status.must_change_password,
        "published_capabilities": status.published_capabilities,
        "active_agent_tokens": status.active_agent_tokens,
    }


@router.get("/overview")
def overview(_user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    problems = _problems()
    return {
        "counts": _counts(),
        "health": _health(),
        "problems": problems[:12],
        "problems_total": len(problems),
        "recent_invocations": _recent_invocations(),
        "onboarding": _onboarding(),
        "generated_at": db.iso(),
    }


@router.get("/overview/problems")
def overview_problems(
    severity: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    items = _problems()
    if severity:
        items = [item for item in items if item["severity"] == severity]
    total = len(items)
    return {"items": items[offset : offset + limit], "total": total, "limit": limit, "offset": offset}
