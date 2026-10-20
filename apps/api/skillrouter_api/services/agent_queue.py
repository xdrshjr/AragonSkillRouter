"""排队的 Agent 轮次：谁把它推起来（§7.12 / §9.2）。

`send_message` 在「已有 executing 轮次」时会把新消息记成 `queued`，控制台也如实显示
「排队中（第 N 条）」。但**推进**必须有唯一负责人，否则排队就是垃圾桶：

- 轮次正常收尾（`succeeded` / `stopped` / `failed`）→ worker 的 `agent_turn` handler；
- 用户点「停止」→ API 的 `/sessions/{id}/stop`（它在 worker 之外改了轮次状态）。

两处都走这里，避免「一处记得推进、另一处忘了」，也避免直接把 `services.tasks` 的
依赖塞进 `agent/sessions.py`（那是状态机，不该知道任务队列）。
"""

from __future__ import annotations

from ..agent import events as agent_events
from ..agent import sessions as agent_sessions
from ..logging import get_logger
from ..sse import broker
from . import tasks as task_service

log = get_logger("skillrouter.run")


def start_next(session_id: str) -> dict | None:
    """把这个会话最前面的排队轮次推进为 running 并排进 worker 队列。

    返回被推进的 `{turn_id, session_id}`；没有排队轮次时返回 None。
    never raises：推进失败只记日志，绝不能因为「让下一轮跑起来」而把当前请求/轮次弄挂。
    """
    try:
        promoted = agent_sessions.dequeue_next(session_id)
    except Exception as exc:  # noqa: BLE001
        log.error("agent_dequeue_failed", extra={"object": session_id, "detail": str(exc)[:200]})
        return None
    if promoted is None:
        return None
    try:
        task_service.enqueue(
            "agent_turn",
            {"session_id": session_id, "turn_id": promoted["turn_id"]},
            priority=6,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "agent_dequeue_enqueue_failed",
            extra={"object": promoted["turn_id"], "detail": str(exc)[:200]},
        )
        # 已经被改成 running 但排不进队列 → 必须当场收口，不能留一个没人执行的 running
        try:
            agent_events.append(
                turn_id=promoted["turn_id"],
                session_id=session_id,
                seq=agent_events.next_seq(promoted["turn_id"]),
                event_type="turn.failed",
                payload={
                    "code": "AGENT_ENQUEUE_FAILED",
                    "hint": "平台无法把排队的轮次排进 worker 队列，请重试这条消息",
                    "message": "queue promotion failed",
                },
            )
            agent_events.update_turn_status(
                promoted["turn_id"], "failed", error_code="AGENT_ENQUEUE_FAILED"
            )
        except Exception as inner:  # noqa: BLE001
            log.error(
                "agent_dequeue_cleanup_failed",
                extra={"object": promoted["turn_id"], "detail": str(inner)[:200]},
            )
        return None
    broker.publish(
        "agent", "turn.started", {"session_id": session_id, "turn_id": promoted["turn_id"]}
    )
    log.info("agent_turn_started", extra={"object": promoted["turn_id"]})
    return promoted


__all__ = ["start_next"]
