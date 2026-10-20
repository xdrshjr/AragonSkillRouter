"""Agent 会话 / 消息 / SSE / 停止 / 历史（§9.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...agent import events as agent_events
from ...agent import sessions as agent_sessions
from ...deps import page_params, require_password_changed
from ...errors import ApiError, Code
from ...schemas.agent import MessageRequest, RetryRequest, SessionCreate, StopRequest
from ...security.sessions import SessionContext
from ...services import agent_model, agent_queue
from ...services import settings as settings_service
from ...services import tasks as task_service
from ...sse import broker
from ...sse.events import sse_response

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/sessions", status_code=201)
async def create_session(payload: SessionCreate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agent_sessions.create_session(payload.model_dump(), actor=ctx.admin_id)


@router.get("/sessions")
async def list_sessions(
    paging: dict = Depends(page_params), ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return agent_sessions.list_sessions(q=paging["q"], page=paging["page"], page_size=paging["page_size"])


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agent_sessions.get_session(session_id)


@router.post("/sessions/{session_id}/messages", status_code=202)
async def send_message(
    session_id: str, payload: MessageRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    # 先确认这一轮有可用的模型与凭证：否则排进队列后网关会拒绝整轮，
    # 面板只会看到一轮永远「执行中」且没有任何事件（历史缺陷）。
    resolution = agent_model.resolve()
    if not resolution.ready:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="Agent 模型未配置完整，无法发送",
            hint="到「设置 → Agent 模型」填 provider / 模型名并选择凭证；保存后点「测试连通性」",
            details={"problems": resolution.problems},
        )
    queued = agent_sessions.queue_turn(session_id, payload.model_dump(), actor=ctx.admin_id)
    agent_sessions.append_user_event(queued["turn_id"], session_id, payload.text)
    if queued["queue_position"]:
        seq = agent_events.next_seq(queued["turn_id"])
        agent_events.append(
            turn_id=queued["turn_id"],
            session_id=session_id,
            seq=seq,
            event_type="turn.queued",
            payload={"queue_position": queued["queue_position"]},
        )
        broker.publish(
            "agent",
            "turn.queued",
            {"session_id": session_id, "turn_id": queued["turn_id"], "queue_position": queued["queue_position"]},
        )
        return {
            "turn_id": queued["turn_id"],
            "queue_position": queued["queue_position"],
            "status": "queued",
            "note": "当前有轮次正在执行，本条已排队",
        }
    task_service.enqueue(
        "agent_turn",
        {"session_id": session_id, "turn_id": queued["turn_id"]},
        priority=6,
    )
    broker.publish(
        "agent", "turn.started", {"session_id": session_id, "turn_id": queued["turn_id"]}
    )
    return {
        "turn_id": queued["turn_id"],
        "queue_position": 0,
        "status": "running",
    }


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: str, request: Request, ctx: SessionContext = Depends(require_password_changed)
) -> StreamingResponse:
    """会话事件流（跨进程）。

    事件来源是 `agent_events` 表而不是进程内 broker：轮次在 worker 进程执行，
    浏览器连的是 API 进程，内存队列跨不过进程边界（见 `agent/live.py`）。
    `last_event_id`（或 `Last-Event-ID` 头）是事件 rowid，可用于断线续传。
    """
    raw_last = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    start_cursor: int | None = None
    if raw_last is not None and str(raw_last).strip() != "":
        try:
            start_cursor = int(str(raw_last).strip())
        except ValueError:
            start_cursor = None

    from ...agent import live

    return sse_response(live.tail(session_id, start_cursor))


@router.post("/sessions/{session_id}/stop")
async def stop(
    session_id: str, payload: StopRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    from ...agent.client import get_adapter

    detail = agent_sessions.get_session(session_id)
    turn_id = payload.turn_id or next(
        (t["id"] for t in detail["turns"] if t["status"] in ("queued", "running")), None
    )
    if turn_id is None:
        raise ApiError(Code.NOT_FOUND, "没有正在执行的轮次")
    report = await get_adapter().stop(session_id, turn_id)
    calls = agent_events.tool_calls(turn_id)
    stopped_steps = report.get("stopped_steps") or [
        f"{c['tool_name']}（已{'完成' if c['ok'] else '中断'}）" for c in calls
    ]
    external_pending = report.get("external_pending") or [
        c["upstream_task_id"] for c in calls if c.get("upstream_task_id")
    ]
    normalized = {
        "stopped_steps": stopped_steps,
        "external_pending": external_pending,
        "note": report.get("note")
        or "已停止当前轮次；标记为 external_pending 的上游请求可能仍在远端执行并产生费用",
    }
    result = agent_sessions.stop(session_id, turn_id, normalized)
    broker.publish("agent", "turn.stopped", {"session_id": session_id, **normalized})
    # 停止之后同样要把排队的那条推起来：否则「已停止」之后队列永久静止
    agent_queue.start_next(session_id)
    return result


@router.post("/sessions/{session_id}/retry", status_code=202)
async def retry(
    session_id: str, payload: RetryRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    queued = agent_sessions.retry_turn(session_id, payload.model_dump())
    # 有轮次正在跑时，重试只能排队：此时**不能**入队，否则两条轮次会同时执行
    # （同一会话的顺序被打破，网关里也会有两个 agent 抢同一份上下文）。
    if not queued["queue_position"]:
        task_service.enqueue("agent_turn", {"session_id": session_id, "turn_id": queued["turn_id"]}, priority=6)
    return queued


@router.post("/sessions/{session_id}/archive")
async def archive(session_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agent_sessions.archive(session_id)


# ── Agent 配置与连通性 ───────────────────────────────────────


@router.get("/config")
async def get_config(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    from ...agent import toolspec

    resolution = agent_model.resolve()
    return {
        "model": settings_service.agent_config(),
        "effective": resolution.as_dict(),
        "providers": list(agent_model.PROVIDER_IDS),
        "tools": toolspec.TOOL_SPECS,
        "policy": settings_service.read_agent_policy(),
    }


@router.patch("/config")
async def patch_config(payload: dict, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    """保存 Agent 模型配置（白名单 + 取值校验），并立刻做一次能力解析。

    只写白名单键：过去这里把任意 dict 直接落库，`credential_id` 打错、`provider`
    写成 `zhipu` 都能存进去，错误要等到发消息时才以「网关拒绝」的形式出现。
    """
    cleaned = agent_model.validate_patch(payload)
    model = settings_service.update_agent_config(cleaned, actor=ctx.admin_id)
    resolution = agent_model.resolve()
    return {
        "model": model,
        "effective": resolution.as_dict(),
    }


@router.post("/config/test")
async def test_config(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    """真实打一次模型（最小请求），把结果作为「Agent 可用」的证据。"""
    return await agent_model.check()


@router.post("/connectivity")
async def connectivity(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    from ...agent.client import get_adapter

    try:
        return {
            "ok": True,
            "model": agent_model.resolve().as_dict(),
            **(await get_adapter().connectivity()),
        }
    except ApiError as exc:
        return {
            "ok": False,
            "model": agent_model.resolve().as_dict(),
            "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
            "gateway": {"ok": False},
        }
