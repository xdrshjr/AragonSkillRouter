"""异步任务查询与取消（§9.2.1 / §A.5.1）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response

from ...deps import require_token
from ...security.tokens import TokenContext
from ...services import tasks
from ...sse import broker

router = APIRouter(prefix="/tasks", tags=["v1-tasks"])


def _owned(task_id: str, ctx: TokenContext) -> dict:
    return tasks.get_owned(task_id, ctx.token_id)


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: TokenContext = Depends(require_token)) -> dict:
    """详情带 `cancel_requested_at` / `cancel_scope`（`none/pending_submit/remote/local_only`）。"""
    return _owned(task_id, ctx)


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str, response: Response, ctx: TokenContext = Depends(require_token)
) -> dict:
    """提交中取消返回 **202**（已受理、等待上游），上游已受理时仍 200（§A.5.1）。"""
    _owned(task_id, ctx)
    result = tasks.cancel(task_id)
    if result.get("cancel_scope") == "pending_submit":
        response.status_code = 202
    broker.publish("tasks", "task.updated", {"task_id": task_id, "status": result.get("status")})
    return result


__all__ = ["router", "json"]
