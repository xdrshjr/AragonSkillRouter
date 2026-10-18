"""Agent 会话 / 消息 / SSE / 停止 / 历史（§9.2）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ...agent import events as agent_events
from ...agent import sessions as agent_sessions
from ...deps import get_db, page_params, require_password_changed
from ...db import db_session, db_write, now_iso, uuid_pk
from ...errors import ApiError, Code
from ...models import AgentSession, AgentTurn
from ...schemas.agent import MessageRequest, RetryRequest, SessionCreate, StopRequest
from ...security.sessions import SessionContext
from ...services import audit, settings as settings_service, tasks as task_service
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
async def stream(session_id: str, request: Request, ctx: SessionContext = Depends(require_password_changed)):
    last_event_id = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    turn_id = request.query_params.get("turn_id")

    async def generator():
        queue = broker.subscribe("agent")
        try:
            cursor = int(last_event_id) if last_event_id and str(last_event_id).isdigit() else 0
            replay: list[dict] = []
            if turn_id:
                replay = [e for e in agent_events.list_events(turn_id) if e["seq"] > cursor]
            for event in replay:
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                payload = event.get("payload") or {}
                if payload.get("session_id") and payload["session_id"] != session_id:
                    continue
                seq = payload.get("seq") or event.get("id")
                yield f"id: {seq}\nevent: {event['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            broker.unsubscribe("agent", queue)

    return sse_response(generator())


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
    return result


@router.post("/sessions/{session_id}/retry", status_code=202)
async def retry(
    session_id: str, payload: RetryRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    queued = agent_sessions.retry_turn(session_id, payload.model_dump())
    task_service.enqueue("agent_turn", {"session_id": session_id, "turn_id": queued["turn_id"]}, priority=6)
    return queued


@router.post("/sessions/{session_id}/archive")
async def archive(session_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return agent_sessions.archive(session_id)


# ── Agent 配置与连通性 ───────────────────────────────────────


@router.get("/config")
async def get_config(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    from ...agent import toolspec

    return {
        "model": settings_service.agent_config(),
        "tools": toolspec.TOOL_SPECS,
        "policy": settings_service.read_agent_policy(),
    }


@router.patch("/config")
async def patch_config(payload: dict, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"model": settings_service.update_agent_config(payload, actor=ctx.admin_id)}


@router.post("/connectivity")
async def connectivity(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    from ...agent.client import get_adapter

    try:
        return {"ok": True, **(await get_adapter().connectivity())}
    except ApiError as exc:
        return {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
            "gateway": {"ok": False},
        }
