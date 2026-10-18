"""外部 Agent 与令牌管理路由（§9.2 /access）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...deps import page_params, require_password_changed
from ...security.sessions import SessionContext
from ...services import access

router = APIRouter(prefix="/access", tags=["access"])


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=96)
    description: str | None = None
    contact: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    contact: str | None = None
    status: str | None = None


class TokenCreate(BaseModel):
    name: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    allowed_modes: list[str] = Field(default_factory=lambda: ["sync", "async", "stream"])
    expires_in_days: int | None = None
    expires_at: str | None = None
    rpm: int | None = None
    concurrency: int | None = None
    daily_calls: int | None = None
    budget_usd: float | None = None


class QuotaUpdate(BaseModel):
    rpm: int | None = None
    concurrency: int | None = None
    daily_calls: int | None = None
    monthly_budget_usd: float | None = None


@router.get("/agents")
async def list_agents(
    paging: dict = Depends(page_params), ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return access.list_agents(q=paging["q"], page=paging["page"], page_size=paging["page_size"])


@router.post("/agents", status_code=201)
async def create_agent(payload: AgentCreate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return access.create_agent(payload.model_dump(), actor=ctx.admin_id)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return access.get_agent(agent_id)


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str, payload: AgentUpdate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return access.update_agent(agent_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return access.delete_agent(agent_id, actor=ctx.admin_id)


@router.post("/agents/{agent_id}/tokens", status_code=201)
async def create_token(
    agent_id: str, payload: TokenCreate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return access.create_token(agent_id, payload.model_dump(), actor=ctx.admin_id)


@router.get("/tokens")
async def list_tokens(
    agent_id: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return access.list_tokens(agent_id=agent_id, page=paging["page"], page_size=paging["page_size"])


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return access.revoke_token(token_id, actor=ctx.admin_id)


@router.get("/quotas")
async def quotas(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return access.quota_overview()


@router.put("/quotas/{scope_type}/{scope_id}")
async def set_quota(
    scope_type: str,
    scope_id: str,
    payload: QuotaUpdate,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return access.set_quota(scope_type, scope_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id)
