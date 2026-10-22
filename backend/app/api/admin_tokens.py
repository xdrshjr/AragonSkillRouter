"""Admin agents, access tokens and budget policies (docs/CONTRACT.md §5.8).

Rules this module enforces:

  * an agent token is generated once, shown exactly once and stored only as a
    SHA-256 hash plus a display prefix — the plaintext never touches the database;
  * revocation is immediate: the row flips to ``revoked`` with ``revoked_at`` and
    ``resolve_token`` stops recognising it on the next request;
  * scopes are validated against existing capability slugs (``*`` and
    ``slug:*`` wildcards are allowed);
  * budget policies are validated (window ∈ minute|day|month, non-negative
    numbers) before they can influence live traffic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from app.core import db
from app.core.errors import conflict, not_found, unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.security import tokens as token_security
from app.services import audit

router = APIRouter(prefix="/api/admin", tags=["access"])

TOKEN_WARNING = (
    "Copy this token now — it is shown only once. Skill Router stores only a hash, "
    "so it cannot be displayed again. Store it in the agent's own configuration."
)
BUDGET_WINDOWS = ("minute", "day", "month")
BUDGET_SCOPES = ("global", "provider", "capability", "agent")


# ------------------------------------------------------------------- models ---
class AgentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(default=None, max_length=20)


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=200)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=100000)
    max_concurrency: int | None = Field(default=None, ge=1, le=1000)
    budget_usd: float | None = Field(default=None, ge=0, le=1_000_000)


class BudgetCreateRequest(BaseModel):
    scope_type: str = Field(max_length=20)
    scope_id: str | None = Field(default=None, max_length=80)
    window: str = Field(default="day", max_length=10)
    max_calls: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    rate_per_min: int | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=0)
    enabled: bool = True


class BudgetUpdateRequest(BaseModel):
    window: str | None = Field(default=None, max_length=10)
    max_calls: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    rate_per_min: int | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


# ------------------------------------------------------------------ helpers ---
def _envelope(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))


def _days_until(value: str | None) -> int | None:
    parsed = db.parse_iso(value)
    if not parsed:
        return None
    return int((parsed - db.utcnow()).total_seconds() // 86400)


def serialise_token(row: dict[str, Any], *, agent_name: str | None = None) -> dict[str, Any]:
    """Public token shape — never contains the secret."""
    days = _days_until(row.get("expires_at"))
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "agent_name": agent_name,
        "name": row["name"],
        "token_prefix": row["token_prefix"],
        "scopes": db.loads(row["scopes"], []) or [],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "days_until_expiry": days,
        "expiring_soon": bool(days is not None and 0 <= days <= 7 and row["status"] == "active"),
        "rate_limit_per_min": row["rate_limit_per_min"],
        "max_concurrency": row["max_concurrency"],
        "budget_usd": row["budget_usd"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
    }


def serialise_agent(row: dict[str, Any], *, counts: dict[str, int] | None = None) -> dict[str, Any]:
    if counts is None:
        counts = {
            "tokens": int(db.query_scalar("SELECT COUNT(*) FROM agent_tokens WHERE agent_id = ?", (row["id"],)) or 0),
            "active_tokens": int(
                db.query_scalar(
                    "SELECT COUNT(*) FROM agent_tokens WHERE agent_id = ? AND status = 'active'", (row["id"],)
                )
                or 0
            ),
        }
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row.get("description") or "",
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revoked_at": row.get("revoked_at"),
        "counts": counts,
    }


def resolve_token(token: str) -> dict[str, Any] | None:
    """Look a plaintext token up by hash; ``None`` when unknown, revoked or expired.

    Used by tests and by verification helpers: a revoked token must stop
    resolving immediately.
    """
    if not token:
        return None
    row = db.query_one(
        "SELECT * FROM agent_tokens WHERE token_hash = ?", (token_security.hash_agent_token(token),)
    )
    if not row:
        return None
    if row["status"] != "active":
        return None
    expires = db.parse_iso(row["expires_at"])
    if expires and expires <= db.utcnow():
        return None
    agent = db.query_one("SELECT status FROM agents WHERE id = ?", (row["agent_id"],))
    if not agent or agent["status"] != "active":
        return None
    return row


def _get_agent(agent_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM agents WHERE id = ? OR slug = ?", (agent_id, agent_id))
    if not row:
        raise not_found("Agent")
    return row


def _unique_agent_slug(name: str) -> str:
    base = token_security.slugify(name or "agent", fallback="agent")[:60]
    candidate = base
    suffix = 1
    while db.query_scalar("SELECT 1 FROM agents WHERE slug = ?", (candidate,)):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _validate_scopes(scopes: list[str]) -> list[str]:
    known = {row["slug"] for row in db.query_all("SELECT slug FROM capabilities")}
    cleaned: list[str] = []
    unknown: list[str] = []
    for raw in scopes or []:
        value = str(raw).strip()
        if not value:
            continue
        if value == "*":
            cleaned.append("*")
            continue
        base = value[:-2] if value.endswith(":*") else value
        if base in known:
            cleaned.append(value)
        else:
            unknown.append(value)
    if unknown:
        raise unprocessable(
            "validation_failed",
            f"Unknown capability scope(s): {', '.join(sorted(unknown))}.",
            details=[
                {"field": f"scopes[{index}]", "message": f"Unknown capability slug '{value}'."}
                for index, value in enumerate(unknown)
            ],
            actions=["Pick existing capability slugs, or use '*' to grant everything."],
        )
    return sorted(set(cleaned))


# ------------------------------------------------------------------- agents ---
@router.get("/agents")
def list_agents(
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter:
        where.append("status = ?")
        params.append(status_filter)
    if search:
        where.append("(name LIKE ? OR slug LIKE ? OR description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM agents WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM agents WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    items = []
    for row in rows:
        counts = db.query_one(
            """SELECT COUNT(*) AS tokens,
                      SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_tokens
                 FROM agent_tokens WHERE agent_id = ?""",
            (row["id"],),
        ) or {}
        items.append(
            serialise_agent(
                row,
                counts={
                    "tokens": int(counts.get("tokens") or 0),
                    "active_tokens": int(counts.get("active_tokens") or 0),
                },
            )
        )
    return _envelope(items, int(total), limit, offset)


@router.post("/agents", status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: AgentCreateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    now = db.iso()
    agent_id = db.new_id("agent")
    rows = {
        "id": agent_id,
        "name": payload.name.strip()[:120],
        "slug": _unique_agent_slug(payload.name),
        "description": payload.description.strip()[:1000],
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "revoked_at": None,
    }
    db.insert("agents", rows)
    audit.record(
        actor_type="user",
        action="agent.create",
        object_type="agent",
        object_id=agent_id,
        actor_id=user.id,
        summary=f"created agent {rows['name']}",
        metadata={"slug": rows["slug"]},
        ip=client_ip(request),
    )
    return serialise_agent(_get_agent(agent_id))


@router.patch("/agents/{agent_id}")
def update_agent(
    agent_id: str,
    payload: AgentUpdateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    agent = _get_agent(agent_id)
    values: dict[str, Any] = {}
    if payload.name is not None:
        if not payload.name.strip():
            raise unprocessable("validation_failed", "The agent name cannot be empty.")
        values["name"] = payload.name.strip()[:120]
    if payload.description is not None:
        values["description"] = payload.description.strip()[:1000]
    if payload.status is not None:
        if payload.status not in {"active", "disabled"}:
            raise unprocessable(
                "validation_failed",
                f"Unknown agent status {payload.status!r}.",
                details=[{"field": "status", "message": "Use 'active' or 'disabled'."}],
            )
        values["status"] = payload.status
    if values:
        values["updated_at"] = db.iso()
        db.update("agents", agent["id"], values)
        if values.get("status") == "disabled":
            # A disabled agent must not keep working tokens.
            db.execute(
                "UPDATE agent_tokens SET status = 'revoked', revoked_at = ? "
                "WHERE agent_id = ? AND status = 'active'",
                (db.iso(), agent["id"]),
            )
        audit.record(
            actor_type="user",
            action="agent.update",
            object_type="agent",
            object_id=agent["id"],
            actor_id=user.id,
            summary=f"updated agent {agent['name']}",
            metadata={"changes": {k: v for k, v in values.items() if k != "updated_at"}},
            ip=client_ip(request),
        )
    return serialise_agent(_get_agent(agent["id"]))


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    agent = _get_agent(agent_id)
    now = db.iso()
    revoked = db.execute(
        "UPDATE agent_tokens SET status = 'revoked', revoked_at = ? WHERE agent_id = ? AND status = 'active'",
        (now, agent["id"]),
    )
    # The row is kept: invocations and audit entries point at it.
    db.update(
        "agents", agent["id"], {"status": "disabled", "revoked_at": now, "updated_at": now}
    )
    audit.record(
        actor_type="user",
        action="agent.delete",
        object_type="agent",
        object_id=agent["id"],
        actor_id=user.id,
        summary=f"deleted (disabled) agent {agent['name']} and revoked {revoked} token(s)",
        metadata={"revoked_tokens": int(revoked or 0)},
        ip=client_ip(request),
    )
    return {"ok": True, "id": agent["id"], "revoked_tokens": int(revoked or 0), "status": "disabled"}


# ------------------------------------------------------------------- tokens ---
@router.get("/agents/{agent_id}/tokens")
def list_agent_tokens(
    agent_id: str,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    agent = _get_agent(agent_id)
    limit, offset = _page(limit, offset)
    total = db.query_scalar("SELECT COUNT(*) FROM agent_tokens WHERE agent_id = ?", (agent["id"],)) or 0
    rows = db.query_all(
        "SELECT * FROM agent_tokens WHERE agent_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (agent["id"], limit, offset),
    )
    items = [serialise_token(row, agent_name=agent["name"]) for row in rows]
    return _envelope(items, int(total), limit, offset)


@router.post("/agents/{agent_id}/tokens", status_code=status.HTTP_201_CREATED)
def create_agent_token(
    agent_id: str,
    payload: TokenCreateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    agent = _get_agent(agent_id)
    if agent["status"] != "active":
        raise conflict(
            "agent_disabled",
            "This agent is disabled; re-enable it before issuing tokens.",
            actions=["Set the agent status to active."],
        )
    scopes = _validate_scopes(payload.scopes)
    issued = token_security.issue_agent_token()
    now = db.iso()
    expires_at = (
        db.iso(db.utcnow() + timedelta(days=int(payload.expires_in_days)))
        if payload.expires_in_days
        else None
    )
    token_id = db.new_id("tok")
    db.insert(
        "agent_tokens",
        {
            "id": token_id,
            "agent_id": agent["id"],
            "name": payload.name.strip()[:120],
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(scopes),
            "expires_at": expires_at,
            "rate_limit_per_min": payload.rate_limit_per_min,
            "max_concurrency": payload.max_concurrency,
            "budget_usd": payload.budget_usd,
            "status": "active",
            "created_at": now,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    audit.record(
        actor_type="user",
        action="token.create",
        object_type="agent_token",
        object_id=token_id,
        actor_id=user.id,
        summary=f"issued token {payload.name[:60]} for agent {agent['name']}",
        metadata={
            "agent_id": agent["id"],
            "token_prefix": issued.prefix,
            "scopes": scopes,
            "expires_in_days": payload.expires_in_days,
        },
        ip=client_ip(request),
    )
    row = db.query_one("SELECT * FROM agent_tokens WHERE id = ?", (token_id,)) or {}
    return {
        # The only response in the whole API that carries the plaintext token.
        "token": issued.token,
        "shown_once": True,
        "warning": TOKEN_WARNING,
        "token_record": serialise_token(row, agent_name=agent["name"]),
    }


@router.get("/tokens")
def list_tokens(
    agent_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    where = ["1 = 1"]
    params: list[Any] = []
    if agent_id:
        where.append("t.agent_id = ?")
        params.append(agent_id)
    if status_filter:
        where.append("t.status = ?")
        params.append(status_filter)
    if search:
        where.append("(t.name LIKE ? OR t.token_prefix LIKE ? OR a.name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    clause = " AND ".join(where)
    total = db.query_scalar(
        f"SELECT COUNT(*) FROM agent_tokens t JOIN agents a ON a.id = t.agent_id WHERE {clause}", params
    ) or 0
    rows = db.query_all(
        f"""SELECT t.*, a.name AS agent_name FROM agent_tokens t
              JOIN agents a ON a.id = t.agent_id
             WHERE {clause} ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    items = [serialise_token(row, agent_name=row.get("agent_name")) for row in rows]
    return _envelope(items, int(total), limit, offset)


@router.post("/tokens/{token_id}/revoke")
def revoke_token(
    token_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM agent_tokens WHERE id = ?", (token_id,))
    if not row:
        raise not_found("Token")
    already = row["status"] == "revoked"
    if not already:
        db.update("agent_tokens", token_id, {"status": "revoked", "revoked_at": db.iso()})
        audit.record(
            actor_type="user",
            action="token.revoke",
            object_type="agent_token",
            object_id=token_id,
            actor_id=user.id,
            summary=f"revoked token {row['token_prefix']} ({row['name'][:60]})",
            metadata={"agent_id": row["agent_id"]},
            ip=client_ip(request),
        )
    updated = db.query_one("SELECT * FROM agent_tokens WHERE id = ?", (token_id,)) or row
    agent = db.query_one("SELECT name FROM agents WHERE id = ?", (row["agent_id"],)) or {}
    payload = serialise_token(updated, agent_name=agent.get("name"))
    payload["already_revoked"] = already
    payload["revoked"] = True
    payload["effective"] = "immediate"
    return payload


# ------------------------------------------------------------------ budgets ---
def serialise_budget(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "window": row["window"],
        "max_calls": row["max_calls"],
        "max_cost_usd": row["max_cost_usd"],
        "rate_per_min": row["rate_per_min"],
        "max_concurrency": row["max_concurrency"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_scope(scope_type: str, scope_id: str | None) -> str | None:
    if scope_type not in BUDGET_SCOPES:
        raise unprocessable(
            "validation_failed",
            f"Unknown budget scope {scope_type!r}.",
            details=[{"field": "scope_type", "message": f"Use one of {', '.join(BUDGET_SCOPES)}."}],
        )
    if scope_type == "global":
        return None
    if not scope_id:
        raise unprocessable(
            "validation_failed",
            f"A {scope_type} budget needs a scope_id.",
            details=[{"field": "scope_id", "message": "Required for this scope."}],
        )
    tables = {"provider": "providers", "capability": "capabilities", "agent": "agents"}
    table = tables[scope_type]
    if not db.query_one(f"SELECT 1 AS ok FROM {table} WHERE id = ? OR slug = ?", (scope_id, scope_id)):
        raise unprocessable(
            "validation_failed",
            f"No {scope_type} matches {scope_id!r}.",
            details=[{"field": "scope_id", "message": "Unknown target."}],
        )
    return scope_id


def _validate_window(window: str) -> str:
    if window not in BUDGET_WINDOWS:
        raise unprocessable(
            "validation_failed",
            f"Unknown budget window {window!r}.",
            details=[{"field": "window", "message": f"Use one of {', '.join(BUDGET_WINDOWS)}."}],
        )
    return window


@router.get("/budgets")
def list_budgets(
    scope_type: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    where = ["1 = 1"]
    params: list[Any] = []
    if scope_type:
        where.append("scope_type = ?")
        params.append(scope_type)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM budget_policies WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM budget_policies WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return _envelope([serialise_budget(row) for row in rows], int(total), limit, offset)


@router.post("/budgets", status_code=status.HTTP_201_CREATED)
def create_budget(
    payload: BudgetCreateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    scope_id = _validate_scope(payload.scope_type, payload.scope_id)
    window = _validate_window(payload.window)
    now = db.iso()
    budget_id = db.new_id("bud")
    db.insert(
        "budget_policies",
        {
            "id": budget_id,
            "scope_type": payload.scope_type,
            "scope_id": scope_id,
            "window": window,
            "max_calls": payload.max_calls,
            "max_cost_usd": payload.max_cost_usd,
            "rate_per_min": payload.rate_per_min,
            "max_concurrency": payload.max_concurrency,
            "enabled": 1 if payload.enabled else 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    audit.record(
        actor_type="user",
        action="budget.create",
        object_type="budget_policy",
        object_id=budget_id,
        actor_id=user.id,
        summary=f"created {payload.scope_type} budget ({window})",
        metadata={
            "scope_type": payload.scope_type,
            "scope_id": scope_id,
            "window": window,
            "max_calls": payload.max_calls,
            "max_cost_usd": payload.max_cost_usd,
        },
        ip=client_ip(request),
    )
    return serialise_budget(db.query_one("SELECT * FROM budget_policies WHERE id = ?", (budget_id,)) or {})


@router.patch("/budgets/{budget_id}")
def update_budget(
    budget_id: str,
    payload: BudgetUpdateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM budget_policies WHERE id = ?", (budget_id,))
    if not row:
        raise not_found("Budget policy")
    values: dict[str, Any] = {}
    if payload.window is not None:
        values["window"] = _validate_window(payload.window)
    for field in ("max_calls", "max_cost_usd", "rate_per_min", "max_concurrency"):
        value = getattr(payload, field)
        if value is not None:
            values[field] = value
    if payload.enabled is not None:
        values["enabled"] = 1 if payload.enabled else 0
    if values:
        values["updated_at"] = db.iso()
        db.update("budget_policies", budget_id, values)
        audit.record(
            actor_type="user",
            action="budget.update",
            object_type="budget_policy",
            object_id=budget_id,
            actor_id=user.id,
            summary=f"updated budget for {row['scope_type']}",
            metadata={"changes": {k: v for k, v in values.items() if k != "updated_at"}},
            ip=client_ip(request),
        )
    return serialise_budget(db.query_one("SELECT * FROM budget_policies WHERE id = ?", (budget_id,)) or row)


@router.delete("/budgets/{budget_id}")
def delete_budget(
    budget_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM budget_policies WHERE id = ?", (budget_id,))
    if not row:
        raise not_found("Budget policy")
    db.execute("DELETE FROM budget_policies WHERE id = ?", (budget_id,))
    audit.record(
        actor_type="user",
        action="budget.delete",
        object_type="budget_policy",
        object_id=budget_id,
        actor_id=user.id,
        summary=f"deleted budget for {row['scope_type']}",
        metadata={"scope_type": row["scope_type"], "scope_id": row["scope_id"]},
        ip=client_ip(request),
    )
    return {"ok": True, "id": budget_id}
