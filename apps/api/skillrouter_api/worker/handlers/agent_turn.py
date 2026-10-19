"""Agent 轮次：转发网关、事件落库、停止（§7.12）。"""

from __future__ import annotations

from ...agent import events as agent_events
from ...agent import sessions as agent_sessions
from ...agent.client import TurnRequest, get_adapter
from ...agent.prompting import SYSTEM_PROMPT, build_turn_prompt
from ...db import db_write, now_iso
from ...errors import ApiError
from ...logging import get_logger, redact
from ...models import AgentSession
from ...services import settings as settings_service
from ...sse import broker

log = get_logger("skillrouter.run")


async def handle(payload: dict) -> None:
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if not session_id or not turn_id:
        return
    adapter = get_adapter()
    detail = agent_sessions.get_session(session_id)
    turn = next((t for t in detail["turns"] if t["id"] == turn_id), None)
    if turn is None:
        return
    agent_events.append(
        turn_id=turn_id,
        session_id=session_id,
        seq=agent_events.next_seq(turn_id),
        event_type="turn.started",
        payload={"ordinal": turn["ordinal"]},
    )
    broker.publish("agent", "turn.started", {"session_id": session_id, "turn_id": turn_id})
    prompt = build_turn_prompt(
        turn["user_text"], attachments=turn["attachments"], links=None
    )
    history = await adapter.replay_messages(session_id, limit=20)
    messages = history + [{"role": "user", "content": prompt}]
    turn_request = TurnRequest(
        turn_id=turn_id,
        messages=messages,
        system_prompt=SYSTEM_PROMPT,
        model=(settings_service.agent_config() or {}).get("model"),
        budget={"max_turns": 40, "max_duration_ms": 1_800_000},
    )
    try:
        await adapter.send(session_id, turn_request)
    except ApiError as exc:
        agent_events.append(
            turn_id=turn_id,
            session_id=session_id,
            seq=agent_events.next_seq(turn_id),
            event_type="turn.failed",
            payload={"code": exc.code, "hint": exc.hint},
        )
        agent_events.update_turn_status(turn_id, "failed", error_code=exc.code, error_detail_redacted=exc.message)
        broker.publish("agent", "turn.failed", {"session_id": session_id, "turn_id": turn_id, "code": exc.code})
        raise
    # 网关接单后进入 waiting；真实事件由网关 SSE 回灌（见 api/admin/agent_chat.py）
    agent_events.append(
        turn_id=turn_id,
        session_id=session_id,
        seq=agent_events.next_seq(turn_id),
        event_type="engine.notice",
        payload={"notice": "turn accepted by gateway", "stream": f"/internal/turns/{turn_id}/events"},
    )
    with db_write() as s:
        row = s.get(AgentSession, session_id)
        if row:
            row.engine_version = None
            row.updated_at = now_iso()


async def ingest_stream(session_id: str, turn_id: str) -> None:
    """从网关 SSE 拉取事件并落库 + 广播（由 API 进程的 SSE 端点驱动）。"""
    import json

    import httpx

    from ...settings import settings as app_settings

    url = f"{app_settings.agent_gateway_url.rstrip('/')}/internal/turns/{turn_id}/events"
    headers = {"X-Internal-Token": app_settings.agent_gateway_secret}
    try:
        async with httpx.AsyncClient(timeout=None) as client:  # type: ignore[arg-type]
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code >= 400:
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type") or "engine.notice"
                    body = event.get("payload") or event
                    stored = agent_events.append(
                        turn_id=turn_id,
                        session_id=session_id,
                        seq=agent_events.next_seq(turn_id),
                        event_type=event_type,
                        payload=body if isinstance(body, dict) else {"value": body},
                    )
                    broker.publish("agent", event_type, {"session_id": session_id, **stored})
                    if event_type in ("turn.finished", "turn.failed", "turn.stopped"):
                        agent_events.update_turn_status(
                            turn_id,
                            "succeeded" if event_type == "turn.finished" else (
                                "failed" if event_type == "turn.failed" else "stopped"
                            ),
                        )
                        return
    except Exception as exc:  # noqa: BLE001
        log.error("agent_ingest_failed", extra={"object": turn_id, "detail": redact(str(exc), max_len=200)})
