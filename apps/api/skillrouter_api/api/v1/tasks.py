"""异步任务查询与取消（§9.2.1）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ...deps import require_token
from ...security.tokens import TokenContext
from ...services import tasks
from ...sse import broker

router = APIRouter(prefix="/tasks", tags=["v1-tasks"])


def _owned(task_id: str, ctx: TokenContext) -> dict:
    return tasks.get_owned(task_id, ctx.token_id)


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: TokenContext = Depends(require_token)) -> dict:
    return _owned(task_id, ctx)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, ctx: TokenContext = Depends(require_token)) -> dict:
    _owned(task_id, ctx)
    result = tasks.cancel(task_id)
    broker.publish("tasks", "task.updated", {"task_id": task_id, "status": result.get("status")})
    return result


__all__ = ["router", "json"]
