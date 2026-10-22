"""Agent console API (docs/CONTRACT.md §5.10, owner agent-engine).

Two kinds of route live here, and their authentication is deliberately different:

  * ``/api/admin/agent/*`` — cookie session + CSRF, for the browser. These are the
    routes the floating agent panel calls.
  * ``POST /api/admin/agent/tools/{name}`` — bearer **run-scoped tool token**, for
    the sidecar. It carries no cookie, so the CSRF middleware does not apply, and
    the token (HMAC-signed, 15-minute TTL, bound to one run) is the only authority.
    That route is how the engine reaches the platform: it never holds a platform
    credential, a database handle or a file path.

Runs execute as a FastAPI background task so the panel's POST returns immediately
and progress arrives over ``/api/stream/agent/{run_id}``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from app.core import db
from app.core.errors import conflict, not_found, unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.services import agent_runtime, audit

log = logging.getLogger("skillrouter.agent_api")

router = APIRouter(prefix="/api/admin/agent", tags=["agent"])

# The sidecar's callback route is registered on THIS router (path `/tools/{name}`)
# so it is mounted by the existing single include in `app.main`. It is still a
# machine endpoint: bearer scoped token, no cookie, and therefore no CSRF
# interaction. `POST /api/admin/agent/tools/{name}` is the exact URL the sidecar's
# ToolDispatcher calls.


# ------------------------------------------------------------------- models ---
class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class AttachmentRef(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    filename: str | None = Field(default=None, max_length=260)


class MessageRequest(BaseModel):
    text: str = Field(default="", max_length=200_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    #: When the session already has a live run, queue instead of starting a second.
    queue_if_busy: bool = True


class QueueRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class ToolCallRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=80)
    session_id: str | None = Field(default=None, max_length=80)
    tool: str | None = Field(default=None, max_length=120)
    tool_call_id: str | None = Field(default=None, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------- sessions ---
@router.get("/sessions")
def list_sessions(
    user: AuthUser = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    items, total = agent_runtime.list_sessions(limit=limit, offset=offset, search=search)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(
    body: SessionCreateRequest | None = None,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return agent_runtime.ensure_session(body.title if body else None, actor_id=user.id)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return agent_runtime.get_session(session_id)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    row = db.query_one("SELECT id FROM agent_sessions WHERE id = ?", (session_id,))
    if not row:
        raise not_found("Agent session")
    db.execute("UPDATE agent_sessions SET archived_at = ?, updated_at = ? WHERE id = ?", (db.iso(), db.iso(), session_id))
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="agent.session_archived",
        object_type="agent_session",
        object_id=session_id,
        summary="agent session archived",
    )
    return {"id": session_id, "archived": True}


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
def send_message(
    session_id: str,
    body: MessageRequest,
    request: Request,
    background: BackgroundTasks,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Accept a message, start (or queue) the run, and return immediately."""
    session = agent_runtime.get_session(session_id, include_messages=False, include_runs=True)
    live = [run for run in session.get("runs", []) if run["status"] in {"queued", "running"}]

    if live and body.queue_if_busy:
        queued = agent_runtime.queue_message(live[0]["id"], body.text)
        db.insert(
            "agent_events",
            {
                "run_id": live[0]["id"],
                "seq": agent_runtime.next_event_seq(live[0]["id"]),
                "type": "message.queued",
                "payload": db.dumps(
                    {
                        "run_id": live[0]["id"],
                        "session_id": session_id,
                        "type": "message.queued",
                        "text_chars": len(body.text),
                        "note": "processed after the current turn",
                    }
                ),
                "created_at": db.iso(),
            },
        )
        return {
            "queued": True,
            "run": queued,
            "events": f"/api/stream/agent/{queued['id']}",
            "note": "A run is already live in this session; the message will be processed after it.",
        }
    if live:
        raise conflict(
            "session_busy",
            "This session already has a live run.",
            details={"run_id": live[0]["id"]},
            actions=["Wait for it, stop it, or send with queue_if_busy=true."],
        )

    run = agent_runtime.create_run(
        session_id,
        body.text,
        [item.model_dump() for item in body.attachments],
        actor_id=user.id,
    )
    background.add_task(_run_in_background, run["id"])
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="agent.run_started",
        object_type="agent_run",
        object_id=run["id"],
        summary=f"agent run started in session {session_id}",
        ip=client_ip(request),
    )
    return {
        "queued": False,
        "run": run,
        "message": agent_runtime.get_session(session_id, include_runs=False)["messages"][-1],
        "events": f"/api/stream/agent/{run['id']}",
    }


async def _run_in_background(run_id: str) -> None:
    """Executor wrapper: a background task must never raise into the ASGI server."""
    try:
        await agent_runtime.run_agent(run_id)
    except Exception:  # noqa: BLE001
        log.exception("agent run %s crashed", run_id)
        try:
            agent_runtime.finish_run(
                run_id,
                "failed",
                error={"code": "run_crashed", "message": "The run crashed; see the server log."},
            )
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.exception("could not record the crash for run %s", run_id)


# --------------------------------------------------------------------- runs ---
@router.get("/runs/{run_id}")
def get_run(run_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    if not row:
        raise not_found("Agent run")
    return agent_runtime.serialize_run(row)


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    user: AuthUser = Depends(require_admin),
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    """Recorded event replay — the same rows the SSE endpoint streams."""
    row = db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    if not row:
        raise not_found("Agent run")
    events = agent_runtime.load_events(run_id, after_seq=after, limit=limit)
    return {
        "run_id": run_id,
        "status": row["status"],
        "items": events,
        "total": len(events),
        "limit": limit,
        "offset": after,
        "next_after": events[-1]["seq"] if events else after,
    }


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Truthful stop report: what stopped, and what may still complete upstream."""
    return await agent_runtime.stop_run(run_id)


@router.post("/runs/{run_id}/queue")
def queue_message(run_id: str, body: QueueRequest, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return agent_runtime.queue_message(run_id, body.text)


# ------------------------------------------------------------------- health ---
@router.get("/health")
async def agent_health(user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return await agent_runtime.health()


@router.get("/tools")
def list_tools(user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    """The permission ceiling, for the UI to show rather than hide."""
    return {
        "allowed": list(agent_runtime.PLATFORM_TOOL_NAMES),
        "mutating": sorted(agent_runtime.MUTATING_TOOLS),
        "definitions": agent_runtime.TOOL_DEFINITIONS,
        "policy": agent_runtime.AutoExecutePolicy.load().__dict__,
    }


# ------------------------------------------------------- sidecar callbacks ---
@router.post("/tools/{tool_name}")
async def call_platform_tool(
    tool_name: str,
    body: ToolCallRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Run one platform tool for the engine.

    Authentication is the run-scoped token, not a cookie: the caller is the
    sidecar, and it must be able to do exactly this one run's tool calls and
    nothing else. The name is checked against the ceiling before the token is
    even inspected so an unknown tool cannot be used to probe the token space.
    """
    if tool_name not in agent_runtime.TOOL_HANDLERS:
        raise not_found("Platform tool")

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    payload = agent_runtime.verify_tool_token(token)

    # The token names its run; a body that disagrees is a client bug or an
    # attempt to act on another run, and both are refused.
    if body.run_id and body.run_id != payload["r"]:
        raise unprocessable(
            "tool_token_mismatch",
            "This tool token belongs to a different run.",
            details={"token_run": payload["r"], "body_run": body.run_id},
        )
    if body.tool and body.tool != tool_name:
        raise unprocessable(
            "tool_name_mismatch",
            "The tool named in the body does not match the path.",
            details={"path": tool_name, "body": body.tool},
        )

    result = await agent_runtime.execute_tool(
        tool_name,
        body.arguments,
        token_payload=payload,
        actor_id=f"agent:{payload['s']}",
    )
    return result
