"""调用记录路由（§9.2 `/calls*`）。

任务路由在 `api/admin/tasks.py`（文件计划 §8.2 的单列）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import page_params, require_password_changed
from ...security.sessions import SessionContext
from ...services import calls

router = APIRouter(tags=["calls"])


@router.get("/calls")
async def list_calls(
    status: str | None = None,
    capability: str | None = None,
    provider: str | None = None,
    caller: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_latency: int | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return calls.list_calls(
        q=paging["q"],
        status=status,
        capability=capability,
        provider=provider,
        caller=caller,
        date_from=date_from,
        date_to=date_to,
        min_latency=min_latency,
        page=paging["page"],
        page_size=paging["page_size"],
    )


@router.get("/calls/{request_id}")
async def get_call(request_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return calls.get_call(request_id)
