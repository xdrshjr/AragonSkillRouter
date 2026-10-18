"""接入流程与审批路由（§9.2 /agent/flows*、/agent/approvals*）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import require_password_changed
from ...schemas.agent import ApprovalDecision, FlowCreate
from ...security.sessions import SessionContext
from ...services import agentic_flow

router = APIRouter(prefix="/agent", tags=["agent-flows"])


@router.post("/flows", status_code=201)
async def create_flow(payload: FlowCreate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agentic_flow.create_flow(payload.model_dump(), actor=ctx.admin_id)


@router.get("/flows")
async def list_flows(
    status: str | None = None, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return agentic_flow.list_flows(status=status)


@router.get("/flows/{flow_id}")
async def get_flow(flow_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agentic_flow.get_flow(flow_id)


@router.post("/flows/{flow_id}/steps/{step}/run")
async def run_step(
    flow_id: str,
    step: str,
    payload: dict | None = None,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return await agentic_flow.run_step(flow_id, step, payload=payload or {}, actor=ctx.admin_id)


@router.post("/flows/{flow_id}/publish")
async def publish_flow(
    flow_id: str,
    payload: dict | None = None,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return await agentic_flow.run_step(flow_id, "publish", payload=payload or {}, actor=ctx.admin_id)


@router.get("/approvals")
async def list_approvals(
    status: str | None = "pending", ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return agentic_flow.list_approvals(status=status)


@router.post("/approvals/{approval_id}/decide")
async def decide(
    approval_id: str, payload: ApprovalDecision, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return agentic_flow.decide(approval_id, approve=payload.approve, note=payload.note, actor=ctx.admin_id)
