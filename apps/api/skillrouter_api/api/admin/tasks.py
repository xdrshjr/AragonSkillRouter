"""任务路由（§9.2 `/tasks*` / §A.3.11 列表契约）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import page_params, require_password_changed
from ...security.sessions import SessionContext
from ...services import tasks

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    status: str | None = None,
    capability: str | None = None,
    capability_key: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """`q` 覆盖 `request_id`/`capability_key`/`upstream_task_id`；`sort` 走白名单。"""
    return tasks.list_tasks(
        status=status,
        capability=capability,
        capability_key=capability_key,
        q=paging["q"],
        sort=paging["sort"],
        page=paging["page"],
        page_size=paging["page_size"],
    )


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return tasks.get(task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return tasks.cancel(task_id)


@router.post("/{task_id}/reconcile")
async def reconcile_task(task_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return tasks.reconcile(task_id)
