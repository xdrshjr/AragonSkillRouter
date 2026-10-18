"""接入流程状态机：七步 + 证据 + 从失败步骤继续 + 审批（§7.12 / R10）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import (
    AgentApproval,
    AgentFlow,
    AgentFlowStep,
    Capability,
    CapabilityTestRun,
)
from ..schemas.common import paginate
from . import audit, capabilities as capability_service

log = get_logger("skillrouter.run")

STEPS = ["parse", "draft", "validate", "test", "sample", "check", "publish"]
STEP_LABELS = {
    "parse": "资料解析",
    "draft": "配置生成",
    "validate": "结构校验",
    "test": "连接测试",
    "sample": "最小样例",
    "check": "结果检查",
    "publish": "能力发布",
}
EVIDENCE_TTL_HOURS = 24
APPROVAL_TTL_MINUTES = 10


def _ensure_steps(session, flow_id: str) -> None:  # noqa: ANN001
    existing = {
        row.step for row in session.scalars(select(AgentFlowStep).where(AgentFlowStep.flow_id == flow_id))
    }
    for index, step in enumerate(STEPS):
        if step in existing:
            continue
        session.add(
            AgentFlowStep(
                id=uuid_pk("afs"),
                flow_id=flow_id,
                step=step,
                ordinal=index,
                status="pending",
                evidence={},
            )
        )


def create_flow(payload: dict, *, actor: str | None = None) -> dict:
    from . import settings as settings_service

    agent_conf = settings_service.agent_config()
    budget = {
        "allowed_providers": payload.get("budget", {}).get("allowed_providers")
        or agent_conf.get("allowed_providers")
        or [],
        "max_test_calls": payload.get("budget", {}).get("max_test_calls")
        or agent_conf.get("max_test_calls_per_flow")
        or 10,
        "max_cost_usd": payload.get("budget", {}).get("max_cost_usd")
        or agent_conf.get("max_cost_usd_per_flow")
        or 1.0,
        "window_hours": payload.get("budget", {}).get("window_hours")
        or agent_conf.get("allowed_window_hours")
        or 24,
    }
    flow_id = uuid_pk("afl")
    with db_write() as s:
        s.add(
            AgentFlow(
                id=flow_id,
                title=payload.get("title") or "接入流程",
                capability_id=payload.get("capability_id"),
                credential_id=payload.get("credential_id"),
                status="running",
                auto_publish=1 if payload.get("auto_publish") else 0,
                budget=budget,
                spent={"calls": 0, "cost_usd": 0.0},
                current_step="parse",
                created_by=actor,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        s.flush()
        _ensure_steps(s, flow_id)
    audit.write("agent_flow.started", object_type="agent_flow", object_id=flow_id, summary=budget, actor_id=actor)
    return get_flow(flow_id)


def get_flow(flow_id: str) -> dict:
    with db_session() as s:
        flow = s.get(AgentFlow, flow_id)
        if flow is None:
            raise ApiError(Code.NOT_FOUND, "接入流程不存在")
        steps = sorted(
            s.scalars(select(AgentFlowStep).where(AgentFlowStep.flow_id == flow_id)),
            key=lambda r: r.ordinal,
        )
        approvals = list(
            s.scalars(select(AgentApproval).where(AgentApproval.flow_id == flow_id))
        )
        capability = s.get(Capability, flow.capability_id) if flow.capability_id else None
        return {
            "id": flow.id,
            "title": flow.title,
            "status": flow.status,
            "auto_publish": bool(flow.auto_publish),
            "budget": flow.budget,
            "spent": flow.spent,
            "current_step": flow.current_step,
            "capability": (
                {"id": capability.id, "key": capability.key, "status": capability.status}
                if capability
                else None
            ),
            "credential_id": flow.credential_id,
            "steps": [
                {
                    "step": row.step,
                    "label": STEP_LABELS.get(row.step, row.step),
                    "ordinal": row.ordinal,
                    "status": row.status,
                    "detail": row.detail,
                    "evidence": row.evidence or {},
                    "error_code": row.error_code,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "can_resume": row.status in ("failed", "pending"),
                }
                for row in steps
            ],
            "approvals": [_approval(a) for a in approvals],
            "created_at": flow.created_at,
            "finished_at": flow.finished_at,
        }


def _approval(row: AgentApproval) -> dict:
    return {
        "id": row.id,
        "flow_id": row.flow_id,
        "action": row.action,
        "target": row.target,
        "impact": row.impact,
        "requested_scope": row.requested_scope,
        "status": row.status,
        "expires_at": row.expires_at,
        "decided_at": row.decided_at,
        "note": row.note,
        "created_at": row.created_at,
    }


def list_flows(*, status: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    with db_session() as s:
        stmt = select(AgentFlow).order_by(AgentFlow.updated_at.desc())
        if status:
            stmt = stmt.where(AgentFlow.status == status)
        rows = list(s.scalars(stmt.limit(200)))
    items = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status,
            "current_step": r.current_step,
            "capability_id": r.capability_id,
            "auto_publish": bool(r.auto_publish),
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]
    return paginate(items, page, page_size)


def _set_step(
    flow_id: str,
    step: str,
    status: str,
    *,
    detail: str | None = None,
    evidence: dict | None = None,
    error_code: str | None = None,
) -> None:
    with db_write() as s:
        row = s.scalar(
            select(AgentFlowStep).where(AgentFlowStep.flow_id == flow_id, AgentFlowStep.step == step)
        )
        if row is None:
            return
        row.status = status
        if detail is not None:
            row.detail = detail
        if evidence is not None:
            row.evidence = {**(row.evidence or {}), **evidence}
        row.error_code = error_code
        if status == "running":
            row.started_at = now_iso()
        if status in ("passed", "failed", "skipped"):
            row.finished_at = now_iso()
        flow = s.get(AgentFlow, flow_id)
        if flow:
            flow.current_step = step
            flow.updated_at = now_iso()
            if status == "failed":
                flow.status = "failed"
            elif status == "passed":
                flow.status = "running"


def _budget_ok(flow: AgentFlow) -> tuple[bool, str]:
    spent = flow.spent or {}
    budget = flow.budget or {}
    if int(spent.get("calls", 0)) >= int(budget.get("max_test_calls", 10)):
        return False, f"测试调用次数已达上限（{budget.get('max_test_calls')}）"
    if float(spent.get("cost_usd", 0.0)) >= float(budget.get("max_cost_usd", 1.0)):
        return False, f"流程预算已用尽（${budget.get('max_cost_usd')}）"
    return True, ""


def record_spend(flow_id: str, *, calls: int = 1, cost_usd: float = 0.0) -> None:
    with db_write() as s:
        flow = s.get(AgentFlow, flow_id)
        if flow is None:
            return
        spent = dict(flow.spent or {})
        spent["calls"] = int(spent.get("calls", 0)) + calls
        spent["cost_usd"] = round(float(spent.get("cost_usd", 0.0)) + cost_usd, 6)
        flow.spent = spent
        flow.updated_at = now_iso()


async def run_step(
    flow_id: str, step: str, *, payload: dict | None = None, actor: str | None = None
) -> dict:
    """执行/续跑单步。前置步骤必须 passed（否则提示先跑前置步骤）。"""
    if step not in STEPS:
        raise ApiError(Code.VALIDATION_FAILED, f"未知步骤：{step}")
    flow = get_flow(flow_id)
    payload = payload or {}
    order = {name: index for index, name in enumerate(STEPS)}
    for prev in STEPS[: order[step]]:
        prev_step = next((s for s in flow["steps"] if s["step"] == prev), None)
        if prev_step and prev_step["status"] != "passed":
            raise ApiError(
                Code.INVALID_STATE_TRANSITION,
                message=f"前置步骤「{STEP_LABELS[prev]}」尚未通过",
                hint="按顺序执行，或先修复失败的前置步骤",
                details={"blocked_by": prev},
            )
    _set_step(flow_id, step, "running")
    try:
        result = await _execute_step(flow, step, payload, actor=actor)
    except ApiError as exc:
        _set_step(flow_id, step, "failed", detail=exc.message, error_code=exc.code)
        raise
    except Exception as exc:  # noqa: BLE001
        _set_step(flow_id, step, "failed", detail=redact(str(exc), max_len=400), error_code="INTERNAL")
        raise ApiError(Code.INTERNAL, f"步骤执行失败：{redact(str(exc), max_len=200)}") from exc
    _set_step(
        flow_id,
        step,
        result.get("status", "passed"),
        detail=result.get("detail"),
        evidence=result.get("evidence"),
        error_code=result.get("error_code"),
    )
    await _maybe_auto_publish(flow_id, actor=actor)
    return {"flow": get_flow(flow_id), "result": result}


async def _execute_step(flow: dict, step: str, payload: dict, *, actor: str | None) -> dict:
    flow_id = flow["id"]
    capability_id = (flow.get("capability") or {}).get("id")
    if step == "parse":
        urls = payload.get("urls") or []
        return {
            "status": "passed",
            "detail": f"已登记 {len(urls)} 份资料" if urls else "未提供外部资料（手工配置流程）",
            "evidence": {"document_ids": payload.get("document_ids") or [], "urls": urls},
        }
    if step == "draft":
        if not capability_id:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="缺少能力草稿",
                hint="先让 Agent 生成草案（sr_propose_capability_draft）或手工创建能力",
            )
        return {"status": "passed", "detail": "草案已就绪", "evidence": {"capability_id": capability_id}}
    if step == "validate":
        if not capability_id:
            raise ApiError(Code.VALIDATION_FAILED, "缺少能力草稿")
        result = capability_service.validate(capability_id)
        return {
            "status": "passed" if result["ok"] else "failed",
            "detail": f"错误 {result['error_count']} 项 / 警告 {result['warn_count']} 项",
            "evidence": {"issues": result["issues"][:20]},
            "error_code": None if result["ok"] else "VALIDATION_FAILED",
        }
    if step in ("test", "sample"):
        if not capability_id:
            raise ApiError(Code.VALIDATION_FAILED, "缺少能力草稿")
        with db_session() as s:
            stored = s.get(AgentFlow, flow_id)
            budget_ok, why = _budget_ok(stored) if stored else (True, "")
        if not budget_ok:
            approval = request_approval(
                action="exceed_budget",
                target=flow_id,
                impact={"reason": why},
                flow_id=flow_id,
                actor=actor,
            )
            raise ApiError(
                Code.APPROVAL_REQUIRED,
                message=why,
                hint="等待管理员在 Agent 面板批准更大的预算",
                details={"approval_id": approval["id"]},
            )
        cap = capability_service.get_capability(capability_id)
        examples = (cap["definition"].get("examples") or [])
        if payload.get("input"):
            test_input = payload["input"]
        elif step == "test" and examples:
            test_input = examples[0].get("input") or {}
        elif step == "sample" and len(examples) > 1:
            test_input = examples[1].get("input") or {}
        elif examples:
            test_input = examples[0].get("input") or {}
        else:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="能力定义里没有可用的调用示例",
                hint="在能力定义中添加 examples[]，或直接提供 input",
            )
        result = await capability_service.test_capability(
            capability_id, {"input": test_input, "mode": "sync"}, actor=actor
        )
        record_spend(flow_id, calls=1, cost_usd=float((result.get("usage") or {}).get("cost_usd") or 0.0))
        ok = result.get("status") == "succeeded"
        return {
            "status": "passed" if ok else "failed",
            "detail": (
                f"HTTP {result.get('http_status')} / {result.get('latency_ms')}ms"
                if ok
                else f"失败：{(result.get('error') or {}).get('code')}"
            ),
            "evidence": {
                "test_run_ids": [result.get("test_run_id")],
                "request_id": None,
                "route": result.get("used_route"),
            },
            "error_code": None if ok else (result.get("error") or {}).get("code"),
        }
    if step == "check":
        if not capability_id:
            raise ApiError(Code.VALIDATION_FAILED, "缺少能力草稿")
        with db_session() as s:
            runs = list(
                s.scalars(
                    select(CapabilityTestRun)
                    .where(CapabilityTestRun.capability_id == capability_id)
                    .order_by(CapabilityTestRun.at.desc())
                    .limit(10)
                )
            )
        ok_runs = [r for r in runs if r.ok]
        if not ok_runs:
            return {
                "status": "failed",
                "detail": "没有成功的测试记录，不能发布",
                "evidence": {"test_run_ids": []},
                "error_code": "PUBLISH_EVIDENCE_MISSING",
            }
        return {
            "status": "passed",
            "detail": f"命中 {len(ok_runs)} 条成功记录",
            "evidence": {"test_run_ids": [r.id for r in ok_runs[:5]]},
        }
    if step == "publish":
        if not capability_id:
            raise ApiError(Code.VALIDATION_FAILED, "缺少能力草稿")
        result = capability_service.publish(
            capability_id,
            version=payload.get("version"),
            changelog=payload.get("changelog") or "接入流程自动发布",
            actor=actor,
        )
        with db_write() as s:
            stored = s.get(AgentFlow, flow_id)
            if stored:
                stored.status = "published"
                stored.finished_at = now_iso()
                stored.updated_at = now_iso()
        audit.write(
            "agent_flow.published",
            object_type="agent_flow",
            object_id=flow_id,
            summary=result,
            actor_id=actor,
        )
        return {
            "status": "passed",
            "detail": f"已发布 {result.get('version')}",
            "evidence": {"version": result.get("version"), "version_id": result.get("version_id")},
        }
    raise ApiError(Code.VALIDATION_FAILED, f"未知步骤：{step}")


async def _maybe_auto_publish(flow_id: str, *, actor: str | None) -> None:
    flow = get_flow(flow_id)
    if not flow["auto_publish"] or flow["status"] == "published":
        return
    steps = {s["step"]: s["status"] for s in flow["steps"]}
    if all(steps.get(name) == "passed" for name in STEPS[:-1]) and steps.get("publish") != "passed":
        try:
            await _execute_step(flow, "publish", {}, actor=actor)
            _set_step(flow_id, "publish", "passed", detail="自动发布完成")
        except ApiError as exc:
            _set_step(flow_id, "publish", "failed", detail=exc.message, error_code=exc.code)


# ── 审批 ─────────────────────────────────────────────────────


def request_approval(
    *,
    action: str,
    target: str | None,
    impact: dict | None = None,
    requested_scope: dict | None = None,
    flow_id: str | None = None,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
    actor: str | None = None,
) -> dict:
    approval_id = uuid_pk("apv")
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_TTL_MINUTES)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_write() as s:
        s.add(
            AgentApproval(
                id=approval_id,
                flow_id=flow_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                action=action,
                target=target,
                impact=redact(impact or {}),
                requested_scope=requested_scope or {},
                status="pending",
                expires_at=expires,
                created_at=now_iso(),
            )
        )
    audit.write(
        "agent_approval.requested",
        object_type="agent_approval",
        object_id=approval_id,
        summary={"action": action, "target": target},
        actor_id=actor,
    )
    return {"id": approval_id, "status": "pending", "expires_at": expires, "action": action}


def list_approvals(*, status: str | None = "pending", page: int = 1, page_size: int = 20) -> dict:
    expire_pending()
    with db_session() as s:
        stmt = select(AgentApproval).order_by(AgentApproval.created_at.desc())
        if status:
            stmt = stmt.where(AgentApproval.status == status)
        rows = list(s.scalars(stmt.limit(200)))
    return paginate([_approval(r) for r in rows], page, page_size)


def decide(approval_id: str, *, approve: bool, note: str | None = None, actor: str | None = None) -> dict:
    expire_pending()
    with db_write() as s:
        row = s.get(AgentApproval, approval_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "审批请求不存在")
        if row.status != "pending":
            raise ApiError(
                Code.INVALID_STATE_TRANSITION,
                message=f"审批已处于 {row.status} 状态",
                hint="pending 之外的审批不能再次决策",
            )
        row.status = "approved" if approve else "rejected"
        row.decided_by = actor
        row.decided_at = now_iso()
        row.note = note
    audit.write(
        "agent_approval.approved" if approve else "agent_approval.rejected",
        object_type="agent_approval",
        object_id=approval_id,
        summary={"note": note},
        actor_id=actor,
    )
    return {"ok": True, "id": approval_id, "status": "approved" if approve else "rejected"}


def expire_pending() -> int:
    now = now_iso()
    expired = 0
    with db_write() as s:
        rows = list(s.scalars(select(AgentApproval).where(AgentApproval.status == "pending")))
        for row in rows:
            if row.expires_at and row.expires_at < now:
                row.status = "expired"
                row.decided_at = now
                row.note = (row.note or "") + " 超时自动拒绝"
                expired += 1
    if expired:
        audit.write(
            "agent_approval.expired",
            object_type="agent_approval",
            summary={"count": expired},
            actor_type="system",
        )
    return expired
