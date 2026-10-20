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
    from ...services.llm import protocols as llm_protocols

    resolution = agent_model.resolve()
    return {
        "model": settings_service.agent_config(),
        "effective": resolution.as_dict(),
        "providers": list(agent_model.PROVIDER_IDS),
        # 协议注册表（INV-1：前端不得硬编码协议差异）
        "protocols": llm_protocols.public_catalog(),
        "wires": list(llm_protocols.WIRE_KEYS),
        "tools": toolspec.TOOL_SPECS,
        "policy": settings_service.read_agent_policy(),
    }


@router.patch("/config")
async def patch_config(payload: dict, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    """保存 Agent 模型配置（白名单 + 取值校验），并立刻做一次能力解析。

    只写白名单键：过去这里把任意 dict 直接落库，`credential_id` 打错、`provider`
    写成 `zhipu` 都能存进去，错误要等到发消息时才以「网关拒绝」的形式出现。
    """
    from ...services.llm import catalog as llm_catalog
    from ...services.llm import protocols as llm_protocols

    before = agent_model.resolve()
    cleaned = agent_model.validate_patch(payload)
    model = settings_service.update_agent_config(cleaned, actor=ctx.admin_id)
    resolution = agent_model.resolve()
    # 地址/协议/凭证变了 → 旧模型清单就是「别人的清单」。
    # 失效放在后端的写路径里，不依赖前端叫 refresh（curl / CLI / 脚本都不会叫）。
    if any(key in cleaned for key in ("protocol", "provider", "wire", "base_url", "credential_id")):
        for candidate in (before, resolution):
            llm_catalog.invalidate_by_target(
                candidate.protocol,
                candidate.wire,
                (candidate.model or {}).get("baseUrl") or "",
            )
    return {
        "model": model,
        "effective": resolution.as_dict(),
        "protocols": llm_protocols.public_catalog(),
    }


@router.post("/config/test")
async def test_config(
    payload: dict | None = None, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    """真实打一次模型（最小请求），把结果作为「Agent 可用」的证据。

    可选 body（`protocol` / `model` / `mode`）只用于覆盖本次测试，
    **不改**已保存配置；缺省 `mode="completion"`，与改动前的行为逐字节等价
    （今天这个端点就是一次生成请求，先插一个 GET 会让「不实现 /models 的网关」从可用变成不可用）。
    白名单外的键一律 `VALIDATION_FAILED`：静默丢弃参数会让调用方以为它生效了。
    """
    overrides = dict(payload or {})
    unknown = sorted(set(overrides) - {"protocol", "model", "mode"})
    if unknown:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"该端点不支持这些字段：{', '.join(unknown)}",
            hint="本端点固定为一次生成请求（mode=completion）；auth / both 请用 /api/admin/llm/test-connection",
            details={"allowed": ["protocol", "model", "mode"]},
        )
    mode = str(overrides.get("mode") or "completion").strip().lower()
    if mode != "completion":
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="本端点的 mode 只能是 completion",
            hint="要只拉模型清单（不计费）或两阶段都跑，请用 /api/admin/llm/test-connection",
            details={"field": "mode", "allowed": ["completion"]},
        )
    config = None
    if overrides.get("protocol") or overrides.get("model"):
        current = dict(settings_service.get_value("agent_model", {}) or {})
        if overrides.get("protocol"):
            current["protocol"] = str(overrides["protocol"]).strip().lower()
        if overrides.get("model"):
            current["model"] = str(overrides["model"]).strip()
        config = current
    return await agent_model.check(config)


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
