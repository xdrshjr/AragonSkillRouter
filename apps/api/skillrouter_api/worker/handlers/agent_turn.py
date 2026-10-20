"""Agent 轮次：转发网关、事件落库、停止（§7.12）。"""

from __future__ import annotations

import json
import time

import httpx

from ...agent import events as agent_events
from ...agent import sessions as agent_sessions
from ...agent.client import TurnRequest, get_adapter
from ...agent.prompting import SYSTEM_PROMPT, build_turn_prompt
from ...db import db_write, now_iso
from ...errors import ApiError
from ...logging import get_logger, redact
from ...models import AgentSession
from ...security.netpolicy import client_kwargs
from ...services import agent_model, agent_queue
from ...settings import settings as app_settings
from ...sse import broker

log = get_logger("skillrouter.run")

#: 轮次预算。handler 超时是 1800s（`worker/handlers/__init__.py`）：1700s 预算 + 40s 收尾
#: = 1740s，留 60s 余量，保证 drain 一定先于 handler 超时结束（超时会被 runner 当作
#: TIMEOUT 取消协程，那一轮就没人收尾了）。
TURN_BUDGET_MS = 1_700_000
TURN_DEADLINE_S = TURN_BUDGET_MS / 1000 + 40

#: 事件流的读空闲上限。网关每 15s 发一条 `: keep-alive`（`src/sse.ts`），所以 90s
#: 一个字节都没有就等于连接已死。**不能**用 `read=None`：那会让「TCP 还在、但已经
#: 不再说话」的网关把轮次一直挂到 handler 超时，正好违反本模块「绝不留永久 running」
#: 的承诺（超时被 runner 取消时，这一轮就没人写终态了）。
STREAM_IDLE_TIMEOUT_S = 90.0


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

    # 网关需要 model{providerId,modelId,baseUrl?} + credential_refs（§9.5）。
    # 解析不出来就在这里失败并落库，绝不让一轮请求挂在 running 上没有任何事件。
    resolution = agent_model.for_turn()
    if not resolution.ready:
        message = "Agent 模型未配置完整：" + "；".join(resolution.problems)
        agent_events.append(
            turn_id=turn_id,
            session_id=session_id,
            seq=agent_events.next_seq(turn_id),
            event_type="turn.failed",
            payload={
                "code": "AGENT_MODEL_UNCONFIGURED",
                "hint": "到「设置 → Agent 模型」选择一个模型协议（anthropic/openai/google）、"
                "填模型名，并选择提供商凭证；保存后点「测试连通性」确认能打真实请求",
                "message": redact(message, max_len=600),
            },
        )
        agent_events.update_turn_status(turn_id, "failed", error_code="AGENT_MODEL_UNCONFIGURED")
        broker.publish(
            "agent",
            "turn.failed",
            {"session_id": session_id, "turn_id": turn_id, "code": "AGENT_MODEL_UNCONFIGURED"},
        )
        # 排在这一轮后面的消息也必须有去处：否则它们永久停在「排队中」，
        # 而用户修好配置再发一条时，排队的那条还会把新的一条也挡住。
        agent_queue.start_next(session_id)
        return

    prompt = build_turn_prompt(
        turn["user_text"], attachments=turn["attachments"], links=None
    )
    history = await adapter.replay_messages(session_id, limit=20)
    messages = history + [{"role": "user", "content": prompt}]
    turn_request = TurnRequest(
        turn_id=turn_id,
        messages=messages,
        system_prompt=SYSTEM_PROMPT,
        model=resolution.model,
        credential_refs=resolution.credential_refs,
        budget={"max_turns": 40, "max_duration_ms": TURN_BUDGET_MS},
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
    # 网关接单后进入 waiting：事件必须由本进程消费（网关不写平台库）
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

    # 消费网关 SSE 直到终态事件：没有这一步，面板永远收不到 assistant.delta，
    # 轮次也永远不会离开 running（真实复现的第二个缺陷）。
    await ingest_stream(session_id, turn_id, timeout_s=TURN_DEADLINE_S)
    # 这一轮已经落终态：把排队的下一条推起来（否则「排队中（第 N 条）」永远不动）
    agent_queue.start_next(session_id)


async def ingest_stream(session_id: str, turn_id: str, *, timeout_s: float | None = None) -> str:
    """从网关 SSE 拉取事件并落库 + 广播（worker 侧唯一的 drain 点）。

    **这是 Agent 面板能看到内容的前提**：网关不写平台数据库，平台必须有人消费
    `/internal/turns/{turn_id}/events`。历史实现里这个函数**没有任何调用方**，
    于是即使模型与凭证都对，面板也永远没有 `assistant.delta`、轮次永远停在
    `running`（真实复现：turn accepted 之后 0 个事件）。

    返回终止原因：`finished` / `stopped` / `failed` / `timeout` / `stream_lost`。
    任何非正常终止都会把轮次写成 `failed`，绝不留一个永久的 `running`。
    """
    started = time.monotonic()
    url = f"{app_settings.agent_gateway_url.rstrip('/')}/internal/turns/{turn_id}/events"
    headers = {"X-Internal-Token": app_settings.agent_gateway_secret}
    deadline = started + (timeout_s if timeout_s is not None else 1_860.0)

    def finish(status: str, code: str | None, detail: str | None) -> str:
        if status == "failed":
            agent_events.append(
                turn_id=turn_id,
                session_id=session_id,
                seq=agent_events.next_seq(turn_id),
                event_type="turn.failed",
                payload={"code": code or "AGENT_STREAM_LOST", "hint": detail, "message": detail},
            )
            agent_events.update_turn_status(
                turn_id, "failed", error_code=code or "AGENT_STREAM_LOST", error_detail_redacted=detail
            )
            broker.publish(
                "agent",
                "turn.failed",
                {"session_id": session_id, "turn_id": turn_id, "code": code or "AGENT_STREAM_LOST", "hint": detail},
            )
            return "failed"
        agent_events.update_turn_status(turn_id, status)
        return status

    try:
        async with httpx.AsyncClient(
            **client_kwargs(url, timeout=httpx.Timeout(60.0, read=STREAM_IDLE_TIMEOUT_S))
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code >= 400:
                    return finish(
                        "failed",
                        "AGENT_STREAM_UNAVAILABLE",
                        f"网关事件流返回 HTTP {response.status_code}；本轮结果未知",
                    )
                async for line in response.aiter_lines():
                    if time.monotonic() > deadline:
                        return finish("failed", "AGENT_TURN_TIMEOUT", "轮次超过预算时长仍未收尾")
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
                    body = event.get("payload") if isinstance(event.get("payload"), dict) else event
                    stored = agent_events.append(
                        turn_id=turn_id,
                        session_id=session_id,
                        seq=agent_events.next_seq(turn_id),
                        event_type=event_type,
                        payload=body if isinstance(body, dict) else {"value": body},
                    )
                    broker.publish("agent", event_type, {"session_id": session_id, **stored})
                    if event_type == "turn.finished":
                        return finish("succeeded", None, None)
                    if event_type == "turn.stopped":
                        return finish("stopped", None, None)
                    if event_type == "turn.failed":
                        payload = body if isinstance(body, dict) else {}
                        # 网关已经写过终态事件（上面已落库），这里只补轮次状态
                        agent_events.update_turn_status(
                            turn_id,
                            "failed",
                            error_code=str(payload.get("code") or "AGENT_FAILED"),
                            error_detail_redacted=redact(str(payload.get("message") or ""), max_len=400) or None,
                        )
                        return "failed"
    except Exception as exc:  # noqa: BLE001
        log.error("agent_ingest_failed", extra={"object": turn_id, "detail": redact(str(exc), max_len=200)})
        return finish("failed", "AGENT_STREAM_LOST", f"与网关的事件流中断：{redact(str(exc), max_len=200)}")
    return finish(
        "failed",
        "AGENT_STREAM_ENDED",
        "事件流结束但没有收到终态事件；本轮可能仍在执行，请到任务页核对",
    )
