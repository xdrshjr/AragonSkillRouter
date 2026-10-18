"""Agent 事件模型与持久化（`agent_events` 是权威源，P04）。"""

from __future__ import annotations

from sqlalchemy import select

from ..db import db_session, db_write, now_iso
from ..models import AgentEventRow, AgentToolCall, AgentTurn

EVENT_TYPES = (
    "session.started",
    "turn.started",
    "assistant.delta",
    "thinking.delta",
    "tool.started",
    "tool.progress",
    "tool.finished",
    "todo.updated",
    "turn.queued",
    "turn.finished",
    "turn.stopped",
    "turn.failed",
    "engine.notice",
)


def append(
    *,
    turn_id: str,
    session_id: str,
    seq: int,
    event_type: str,
    payload: dict,
) -> dict:
    with db_write() as s:
        row = AgentEventRow(
            turn_id=turn_id,
            session_id=session_id,
            seq=seq,
            type=event_type,
            payload=payload,
            at=now_iso(),
        )
        s.add(row)
        s.flush()
        return {"id": row.id, "turn_id": turn_id, "seq": seq, "type": event_type, "at": row.at, "payload": payload}


def next_seq(turn_id: str) -> int:
    with db_session() as s:
        rows = list(
            s.scalars(select(AgentEventRow.seq).where(AgentEventRow.turn_id == turn_id).order_by(AgentEventRow.seq.desc()).limit(1))
        )
        return (rows[0] if rows else 0) + 1


def list_events(turn_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict]:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(AgentEventRow)
                .where(AgentEventRow.turn_id == turn_id, AgentEventRow.seq > after_seq)
                .order_by(AgentEventRow.seq)
                .limit(limit)
            )
        )
        return [
            {"id": row.id, "turn_id": row.turn_id, "seq": row.seq, "type": row.type, "at": row.at, "payload": row.payload}
            for row in rows
        ]


def session_timeline(session_id: str, limit: int = 200) -> list[dict]:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(AgentEventRow)
                .where(AgentEventRow.session_id == session_id)
                .order_by(AgentEventRow.id.desc())
                .limit(limit)
            )
        )
        return [
            {"id": row.id, "turn_id": row.turn_id, "seq": row.seq, "type": row.type, "at": row.at, "payload": row.payload}
            for row in reversed(rows)
        ]


def record_tool_call(
    *,
    turn_id: str,
    tool_name: str,
    args_redacted: dict,
    result_summary: str,
    ok: bool,
    duration_ms: int,
    upstream_task_id: str | None = None,
) -> str:
    from ..db import uuid_pk

    call_id = uuid_pk("atc")
    with db_write() as s:
        s.add(
            AgentToolCall(
                id=call_id,
                turn_id=turn_id,
                tool_name=tool_name,
                args_redacted=args_redacted,
                result_summary=result_summary,
                ok=1 if ok else 0,
                duration_ms=duration_ms,
                upstream_task_id=upstream_task_id,
                at=now_iso(),
            )
        )
    return call_id


def tool_calls(turn_id: str) -> list[dict]:
    with db_session() as s:
        rows = list(
            s.scalars(select(AgentToolCall).where(AgentToolCall.turn_id == turn_id).order_by(AgentToolCall.at))
        )
        return [
            {
                "id": row.id,
                "tool_name": row.tool_name,
                "args_redacted": row.args_redacted,
                "result_summary": row.result_summary,
                "ok": bool(row.ok),
                "duration_ms": row.duration_ms,
                "upstream_task_id": row.upstream_task_id,
                "at": row.at,
            }
            for row in rows
        ]


def update_turn_status(turn_id: str, status: str, **extra) -> None:
    with db_write() as s:
        row = s.get(AgentTurn, turn_id)
        if row is None:
            return
        row.status = status
        for key, value in extra.items():
            if hasattr(row, key):
                setattr(row, key, value)
        if status in ("succeeded", "failed", "stopped"):
            row.finished_at = now_iso()
