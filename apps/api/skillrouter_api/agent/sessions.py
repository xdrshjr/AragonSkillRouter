"""会话 / 轮次状态机、排队、停止报告（§7.12 / §9.2）。"""

from __future__ import annotations

from sqlalchemy import func, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger
from ..models import AgentSession, AgentToolCall, AgentTurn
from ..schemas.common import paginate
from . import events as agent_events

log = get_logger("skillrouter.run")


def create_session(payload: dict, *, actor: str | None = None) -> dict:
    session_id = uuid_pk("ags")
    with db_write() as s:
        s.add(
            AgentSession(
                id=session_id,
                title=payload.get("title") or "新会话",
                model_profile=payload.get("model_profile") or {},
                permission_profile=payload.get("permission_profile") or {"mode": "default"},
                status="active",
                turn_count=0,
                created_by=actor,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    return get_session(session_id)


def list_sessions(*, q: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    with db_session() as s:
        stmt = select(AgentSession).order_by(AgentSession.updated_at.desc())
        rows = list(s.scalars(stmt.limit(200)))
        items = [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "turn_count": r.turn_count,
                "engine_version": r.engine_version,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in str(i["title"] or "").lower()]
    return paginate(items, page, page_size)


def get_session(session_id: str) -> dict:
    with db_session() as s:
        row = s.get(AgentSession, session_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "会话不存在")
        turns = list(
            s.scalars(select(AgentTurn).where(AgentTurn.session_id == session_id).order_by(AgentTurn.ordinal))
        )
        # 前端 `AgentSessionDetail.tool_calls` 是契约的一部分（用于从历史恢复时间线），
        # 这里必须真的返回；此前漏掉该字段会让控制台读 `detail.tool_calls.length` 直接抛
        # TypeError，把整个 (app) 布局打成 client-side exception。
        tool_calls: list[dict] = []
        if turns:
            call_rows = list(
                s.scalars(
                    select(AgentToolCall)
                    .where(AgentToolCall.turn_id.in_([t.id for t in turns]))
                    .order_by(AgentToolCall.at)
                )
            )
            tool_calls = [
                {
                    "id": c.id,
                    "turn_id": c.turn_id,
                    "tool_name": c.tool_name,
                    # 前端字段名是 args_summary；库里叫 args_redacted（已脱敏）
                    "args_summary": c.args_redacted or {},
                    "result_summary": c.result_summary,
                    "ok": None if c.ok is None else bool(c.ok),
                    "duration_ms": c.duration_ms,
                    "at": c.at,
                }
                for c in call_rows
            ]
        return {
            "id": row.id,
            "title": row.title,
            "status": row.status,
            "model_profile": row.model_profile or {},
            "permission_profile": row.permission_profile or {},
            "engine_version": row.engine_version,
            "turn_count": row.turn_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "turns": [
                {
                    "id": t.id,
                    "ordinal": t.ordinal,
                    "user_text": t.user_text,
                    "attachments": t.attachments or [],
                    "status": t.status,
                    "queue_position": t.queue_position,
                    "stop_report": t.stop_report,
                    "usage": t.usage or {},
                    "error_code": t.error_code,
                    "started_at": t.started_at,
                    "finished_at": t.finished_at,
                }
                for t in turns
            ],
            "tool_calls": tool_calls,
            "events": agent_events.session_timeline(session_id, limit=100),
        }


def active_turn(session_id: str) -> AgentTurn | None:
    with db_session() as s:
        return s.scalar(
            select(AgentTurn)
            .where(AgentTurn.session_id == session_id, AgentTurn.status.in_(("queued", "running")))
            .order_by(AgentTurn.ordinal)
        )


def queue_turn(
    session_id: str, payload: dict, *, actor: str | None = None
) -> dict:
    """创建轮次；若已有执行中的轮次，本轮回 queued 并给出真实 queue_position。"""
    with db_write() as s:
        session = s.get(AgentSession, session_id)
        if session is None:
            raise ApiError(Code.NOT_FOUND, "会话不存在")
        running = s.scalar(
            select(AgentTurn).where(
                AgentTurn.session_id == session_id, AgentTurn.status.in_(("queued", "running"))
            )
        )
        ordinal = (
            s.scalar(select(func.max(AgentTurn.ordinal)).where(AgentTurn.session_id == session_id)) or 0
        ) + 1
        turn_id = uuid_pk("trn")
        queued_count = len(
            list(
                s.scalars(
                    select(AgentTurn).where(
                        AgentTurn.session_id == session_id, AgentTurn.status == "queued"
                    )
                )
            )
        )
        position = queued_count + 1 if running is not None else 0
        s.add(
            AgentTurn(
                id=turn_id,
                session_id=session_id,
                ordinal=ordinal,
                user_text=payload.get("text") or "",
                attachments=payload.get("attachments") or [],
                status="queued" if running is not None else "running",
                queue_position=position if position else None,
                retry_of=payload.get("retry_of"),
                created_at=now_iso(),
                started_at=None if position else now_iso(),
            )
        )
        session.turn_count = int(session.turn_count or 0) + 1
        session.updated_at = now_iso()
    return {
        "turn_id": turn_id,
        "queue_position": position,
        "status": "queued" if position else "running",
        "queued": position > 0,
    }


def append_user_event(turn_id: str, session_id: str, text: str) -> None:
    seq = agent_events.next_seq(turn_id)
    agent_events.append(
        turn_id=turn_id, session_id=session_id, seq=seq, event_type="user.message", payload={"text": text}
    )


def stop(session_id: str, turn_id: str | None, stop_report: dict) -> dict:
    with db_write() as s:
        turn = None
        if turn_id:
            turn = s.get(AgentTurn, turn_id)
        if turn is None:
            turn = s.scalar(
                select(AgentTurn)
                .where(AgentTurn.session_id == session_id, AgentTurn.status.in_(("queued", "running")))
                .order_by(AgentTurn.ordinal)
            )
        if turn is None:
            raise ApiError(Code.NOT_FOUND, "没有正在执行的轮次")
        turn.status = "stopped"
        turn.stop_report = stop_report
        turn.finished_at = now_iso()
        resolved_turn_id = turn.id
    seq = agent_events.next_seq(resolved_turn_id)
    event = agent_events.append(
        turn_id=resolved_turn_id,
        session_id=session_id,
        seq=seq,
        event_type="turn.stopped",
        payload=stop_report,
    )
    return {"turn_id": resolved_turn_id, "event": event, **stop_report}


def retry_turn(session_id: str, payload: dict) -> dict:
    with db_session() as s:
        turn_id = payload.get("turn_id")
        if turn_id:
            turn = s.get(AgentTurn, turn_id)
        else:
            turn = s.scalar(
                select(AgentTurn)
                .where(AgentTurn.session_id == session_id, AgentTurn.status.in_(("failed", "stopped")))
                .order_by(AgentTurn.ordinal.desc())
            )
        if turn is None:
            raise ApiError(Code.NOT_FOUND, "没有可重试的轮次")
        text = turn.user_text
        attachments = turn.attachments or []
        original = turn.id
    return queue_turn(
        session_id,
        {"text": text, "attachments": attachments, "retry_of": original},
    )


def archive(session_id: str) -> dict:
    with db_write() as s:
        row = s.get(AgentSession, session_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "会话不存在")
        row.status = "archived"
        row.updated_at = now_iso()
    return {"ok": True, "id": session_id}
