"""LLM 服务配置的管理端路由（spec §5.1–§5.5）。

四条路由 + 一条挂在凭证下的「取模型」：

- `GET  /api/admin/llm/protocols`        协议注册表（前端**唯一**的协议知识来源）
- `POST /api/admin/llm/test-connection`  实时连通测试（草稿可用，默认不计费）
- `POST /api/admin/llm/models`           一键取回该凭证/草稿的全部可用模型
- `GET  /api/admin/llm/probes`           探测历史（列表项已含完整 `stages`）
- `GET  /api/admin/credentials/{id}/models` —— 见 `api/admin/providers.py`

两类结果必须分清（spec §5.2 的注）：鉴权失败 / 上游错误这类「正常的坏结果」
回 `200 + ok=false + error_code + hint`；只有「请求本身不成立」或「凭证不能用」
才走 4xx/5xx。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from ...db import db_session
from ...deps import page_params, require_password_changed
from ...errors import ApiError, Code
from ...models import LlmConnectionProbe
from ...schemas.llm import ModelsRequest, TestConnectionRequest
from ...security.sessions import SessionContext
from ...services.llm import catalog as llm_catalog
from ...services.llm import identity as llm_identity
from ...services.llm import probe as llm_probe
from ...services.llm import protocols as llm_protocols

router = APIRouter(prefix="/llm", tags=["llm"])


@router.get("/protocols")
async def list_protocols(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    """四种 API 规范的全部差异（地址 / 路径 / 认证 / 分页），前端不得自己写。"""
    return {"items": llm_protocols.public_catalog(), "wires": list(llm_protocols.WIRE_KEYS)}


@router.post("/test-connection")
async def test_connection(
    payload: TestConnectionRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    """用**当前表单值**做一次真实探测（不要求先保存凭证）。"""
    identity = llm_identity.resolve_identity(payload)
    return await llm_probe.test_connection(
        identity,
        mode=payload.mode,
        model=payload.model,
        actor=ctx.admin_id,
    )


@router.post("/models")
async def list_models(
    payload: ModelsRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    """一键取回该身份的全部可用模型（缓存优先，失败降级到 24h 内的旧清单）。"""
    identity = llm_identity.resolve_identity(payload)
    return await llm_catalog.list_models(
        identity,
        refresh=payload.refresh,
        include_non_llm=payload.include_non_llm,
        limit=payload.limit,
        actor=ctx.admin_id,
    )


@router.get("/probes")
async def list_probes(
    credential_id: str | None = Query(default=None, max_length=64),
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """探测历史。列表项**已带完整 `stages`**，不再提供详情端点（spec §5.5 / R-14）。"""
    with db_session() as s:
        stmt = select(LlmConnectionProbe)
        if credential_id:
            stmt = stmt.where(LlmConnectionProbe.credential_id == credential_id)
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(
            s.scalars(
                stmt.order_by(desc(LlmConnectionProbe.at))
                .limit(paging["page_size"])
                .offset((paging["page"] - 1) * paging["page_size"])
            )
        )
    items = [llm_probe.probe_row_to_dict(row) for row in rows]
    return {
        "items": items,
        "page": paging["page"],
        "page_size": paging["page_size"],
        "total": total,
    }


async def credential_models(
    credential_id: str,
    *,
    protocol: str | None,
    refresh: bool,
    include_non_llm: bool,
    limit: int,
    actor: str | None,
) -> dict:
    """`GET /api/admin/credentials/{id}/models` 的实现（供 providers 路由复用）。"""
    if protocol is not None and not llm_protocols.is_supported(protocol):
        raise ApiError(
            Code.LLM_PROTOCOL_UNSUPPORTED,
            message=f"不支持的协议规范：{protocol}",
            details={"allowed": list(llm_protocols.PROTOCOL_KEYS)},
        )
    identity = llm_identity.resolve_identity_for_credential(credential_id, protocol=protocol)
    return await llm_catalog.list_models(
        identity,
        refresh=refresh,
        include_non_llm=include_non_llm,
        limit=limit,
        actor=actor,
    )


__all__ = ["credential_models", "router"]
