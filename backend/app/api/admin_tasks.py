"""Admin records endpoints: invocations and tasks (CONTRACT §5.6).

Read-only except cancel/reconcile, which are audited and return a *truthful*
answer: "cancel accepted by the provider", "unsupported by the provider" and
"local waiting stopped" are three different things and are reported as such.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.deps import AuthUser, client_ip, require_admin
from app.services import tasks as task_service

router = APIRouter(prefix="/api/admin", tags=["tasks"])

MAX_LIMIT = 200


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _envelope(items: list[dict[str, Any]], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _actor(user: AuthUser, request: Request) -> dict[str, Any]:
    return {"type": "user", "id": user.id, "ip": client_ip(request)}


# ------------------------------------------------------------- invocations ---
@router.get("/invocations")
async def list_invocations(
    status: str | None = None,
    capability: str | None = None,
    agent: str | None = None,
    surface: str | None = None,
    search: str | None = None,
    created_from: str | None = Query(default=None, alias="from"),
    created_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    items, total = await task_service.list_invocations(
        status=status,
        capability=capability,
        agent=agent,
        surface=surface,
        search=search,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return _envelope(items, total, limit, offset)


@router.get("/invocations/{invocation_id}")
async def get_invocation(invocation_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return await task_service.get_invocation(invocation_id)


# ------------------------------------------------------------------ tasks ----
@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    capability: str | None = None,
    agent: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    items, total = await task_service.list_tasks(
        status=status, capability=capability, agent=agent, search=search, limit=limit, offset=offset
    )
    return _envelope(items, total, limit, offset)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return await task_service.get_task(task_id)


@router.get("/tasks/{task_id}/events")
async def get_task_events(task_id: str, _user: AuthUser = Depends(require_admin)) -> list[dict[str, Any]]:
    return await task_service.task_events(task_id)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    payload: CancelRequest | None = None,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    reason = payload.reason if payload else None
    return await task_service.cancel_task(task_id, _actor(user, request), reason=reason)


@router.post("/tasks/{task_id}/reconcile")
async def reconcile_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return await task_service.reconcile_task(task_id, _actor(user, request))
