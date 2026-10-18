"""调用记录路由（§9.2 `/calls*` / §A.3.11 列表契约）。

任务路由在 `api/admin/tasks.py`（文件计划 §8.2 的单列）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from ...deps import page_params, require_password_changed
from ...security.sessions import SessionContext
from ...services import calls

router = APIRouter(tags=["calls"])


@router.get("/calls")
async def list_calls(
    response: Response,
    status: str | None = None,
    capability: str | None = None,
    provider: str | None = None,
    caller: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    from_: str | None = Query(None, alias="from", description="deprecated：用 date_from"),
    to: str | None = Query(None, alias="to", description="deprecated：用 date_to"),
    min_latency: int | None = None,
    needs_reconcile: bool | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """`from`/`to` 是 `date_from`/`date_to` 的别名：命中时回 `X-Deprecated-Param` 提示迁移。"""
    deprecated: list[str] = []
    if from_ is not None:
        deprecated.append("from")
        date_from = date_from or from_
    if to is not None:
        deprecated.append("to")
        date_to = date_to or to
    if deprecated:
        response.headers["X-Deprecated-Param"] = ",".join(deprecated)
    return calls.list_calls(
        q=paging["q"],
        status=status,
        capability=capability,
        provider=provider,
        caller=caller,
        date_from=date_from,
        date_to=date_to,
        min_latency=min_latency,
        needs_reconcile=needs_reconcile,
        sort=paging["sort"],
        page=paging["page"],
        page_size=paging["page_size"],
    )


@router.get("/calls/{request_id}")
async def get_call(request_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return calls.get_call(request_id)
