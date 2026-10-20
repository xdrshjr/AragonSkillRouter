"""排队的 Agent 轮次必须被推起来（真实缺陷的回归测试）。

缺陷现象：`send_message` 在「已有 running 轮次」时把新消息记成 `queued`，控制台的输入框
还明确写着「执行中，可继续输入（Enter 发送，会排队）」，但**没有任何代码**把 `queued`
推进成 `running`。于是第二条消息永久停在「排队中（第 N 条）」，而它还会把之后的新消息
一起挡住（`queue_turn` 把 queued 也算 busy）。

同一处还有第二条：`POST /retry` 不管有没有 running 轮次都直接 `enqueue`，等于让两条
轮次并发跑同一会话（顺序被打破，网关里两个 agent 抢同一份上下文）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from skillrouter_api.agent import sessions as agent_sessions
from skillrouter_api.db import db_session, db_write
from skillrouter_api.models import AgentTurn, TaskQueue
from skillrouter_api.services import agent_model, agent_queue
from skillrouter_api.worker.handlers import agent_turn as agent_turn_handler
from tests.fixtures.fake_gateway import FakeGateway, patch_gateway


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture()
def gateway(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    patch_gateway(monkeypatch, fake)
    return fake


@pytest.fixture()
def configured(client, admin, credential) -> dict:
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "provider": "anthropic",
            "model": "glm-5.3-flash",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "credential_id": credential["id"],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _turns(session_id: str) -> list[AgentTurn]:
    with db_session() as s:
        return list(
            s.scalars(
                select(AgentTurn).where(AgentTurn.session_id == session_id).order_by(AgentTurn.ordinal)
            )
        )


def _queued_jobs(turn_id: str) -> list[TaskQueue]:
    with db_session() as s:
        return [
            row
            for row in s.scalars(select(TaskQueue).where(TaskQueue.kind == "agent_turn"))
            if (row.payload or {}).get("turn_id") == turn_id
        ]


def _queue_two(session_title: str) -> tuple[str, str, str]:
    """第一个轮次 running、第二个轮次 queued（就是「执行中又发了一条」）。"""
    session = agent_sessions.create_session({"title": session_title})
    first = agent_sessions.queue_turn(session["id"], {"text": "第一轮", "attachments": []})
    second = agent_sessions.queue_turn(session["id"], {"text": "第二轮", "attachments": []})
    assert first["queue_position"] == 0
    assert second["queue_position"] == 1
    return session["id"], first["turn_id"], second["turn_id"]


def _finish(turn_id: str, status: str = "succeeded") -> None:
    with db_write() as s:
        row = s.get(AgentTurn, turn_id)
        assert row is not None
        row.status = status


# ── 状态机：只在「没有 running」时推进 ─────────────────────────


def test_dequeue_next_waits_while_a_turn_is_running() -> None:
    session_id, first, second = _queue_two("dequeue-busy")
    assert agent_sessions.dequeue_next(session_id) is None
    assert [t.status for t in _turns(session_id)] == ["running", "queued"]


def test_dequeue_next_promotes_the_oldest_queued_turn() -> None:
    session_id, first, second = _queue_two("dequeue-fifo")
    _finish(first)

    promoted = agent_sessions.dequeue_next(session_id)
    assert promoted is not None
    assert promoted["turn_id"] == second

    turns = {t.id: t for t in _turns(session_id)}
    assert turns[second].status == "running"
    assert turns[second].queue_position is None
    assert turns[second].started_at is not None
    # 推完就没了：再叫一次不能凭空造出第三个轮次
    assert agent_sessions.dequeue_next(session_id) is None


def test_dequeue_next_renumbers_the_remaining_queue() -> None:
    session_id, first, second = _queue_two("dequeue-renumber")
    third = agent_sessions.queue_turn(session_id, {"text": "第三轮", "attachments": []})
    _finish(first)

    assert agent_sessions.dequeue_next(session_id) is not None
    turns = {t.id: t for t in _turns(session_id)}
    assert turns[second].status == "running"
    # 剩下的那条不再显示「第 2 条」
    assert turns[third["turn_id"]].queue_position == 1


def test_start_next_enqueues_a_worker_job(configured) -> None:
    session_id, first, second = _queue_two("start-next")
    _finish(first)

    promoted = agent_queue.start_next(session_id)
    assert promoted is not None and promoted["turn_id"] == second
    jobs = _queued_jobs(second)
    assert len(jobs) == 1
    assert jobs[0].status == "queued"
    # 没有排队轮次时什么都不做（不能刷出空任务）
    assert agent_queue.start_next(session_id) is None


def test_start_next_fails_the_turn_when_enqueue_breaks(configured, monkeypatch) -> None:
    """推进成功但排不进队列时，不能留一个没人执行的 running。"""

    session_id, first, second = _queue_two("start-next-broken")
    _finish(first)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("queue table is locked")

    monkeypatch.setattr(agent_queue.task_service, "enqueue", _boom)
    assert agent_queue.start_next(session_id) is None

    with db_session() as s:
        turn = s.get(AgentTurn, second)
    assert turn is not None
    assert turn.status == "failed"
    assert turn.error_code == "AGENT_ENQUEUE_FAILED"


# ── 触发点：worker 收尾、停止、以及「不能并发跑」 ─────────────


def test_worker_promotes_the_next_turn_after_a_normal_drain(
    gateway, configured, monkeypatch
) -> None:
    session = agent_sessions.create_session({"title": "worker-promote"})
    first = agent_sessions.queue_turn(session["id"], {"text": "第一轮", "attachments": []})
    second = agent_sessions.queue_turn(session["id"], {"text": "第二轮", "attachments": []})

    from skillrouter_api.agent import events as agent_events

    async def _drain(session_id: str, turn_id: str, *, timeout_s: float | None = None) -> str:
        agent_events.update_turn_status(turn_id, "succeeded")
        return "succeeded"

    monkeypatch.setattr(agent_turn_handler, "ingest_stream", _drain)
    _run(agent_turn_handler.handle({"session_id": session["id"], "turn_id": first["turn_id"]}))

    turns = {t.id: t for t in _turns(session["id"])}
    assert turns[first["turn_id"]].status == "succeeded"
    assert turns[second["turn_id"]].status == "running"
    assert len(_queued_jobs(second["turn_id"])) == 1


def test_worker_promotes_the_next_turn_when_the_model_is_unconfigured(
    client, admin, gateway
) -> None:
    client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"model": "", "credential_id": None},
    )
    session = agent_sessions.create_session({"title": "unconfigured-promote"})
    first = agent_sessions.queue_turn(session["id"], {"text": "第一轮", "attachments": []})
    second = agent_sessions.queue_turn(session["id"], {"text": "第二轮", "attachments": []})

    _run(agent_turn_handler.handle({"session_id": session["id"], "turn_id": first["turn_id"]}))

    turns = {t.id: t for t in _turns(session["id"])}
    assert turns[first["turn_id"]].status == "failed"
    # 修好配置后用户不该被「一条永久排队的旧消息」挡住
    assert turns[second["turn_id"]].status == "running"
    assert len(_queued_jobs(second["turn_id"])) == 1


def test_stop_promotes_the_queued_turn(client, admin, gateway, configured) -> None:
    session = agent_sessions.create_session({"title": "stop-promote"})
    first = agent_sessions.queue_turn(session["id"], {"text": "第一轮", "attachments": []})
    second = agent_sessions.queue_turn(session["id"], {"text": "第二轮", "attachments": []})

    r = client.post(
        f"/api/admin/agent/sessions/{session['id']}/stop",
        headers=admin["headers"],
        json={},
    )
    assert r.status_code == 200, r.text
    assert r.json()["turn_id"] == first["turn_id"]

    turns = {t.id: t for t in _turns(session["id"])}
    assert turns[first["turn_id"]].status == "stopped"
    assert turns[second["turn_id"]].status == "running"
    assert len(_queued_jobs(second["turn_id"])) == 1


def test_retry_does_not_enqueue_while_another_turn_is_running(client, admin, gateway, configured) -> None:
    session = agent_sessions.create_session({"title": "retry-queue"})
    agent_sessions.queue_turn(session["id"], {"text": "第一轮", "attachments": []})
    # 先造一条失败轮次，作为重试目标
    failed = agent_sessions.queue_turn(session["id"], {"text": "会失败的一轮", "attachments": []})
    with db_write() as s:
        row = s.get(AgentTurn, failed["turn_id"])
        assert row is not None
        row.status = "failed"

    r = client.post(
        f"/api/admin/agent/sessions/{session['id']}/retry",
        headers=admin["headers"],
        json={"turn_id": failed["turn_id"]},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queue_position"] == 1
    # 关键：排队状态下不允许并发入队，否则同一会话会同时跑两轮
    assert _queued_jobs(body["turn_id"]) == []


# ── 连通性检测不能把「凭证不可用」报成 500 ────────────────────


def test_check_reports_disabled_credential_instead_of_raising(client, admin, configured, credential) -> None:
    r = client.patch(
        f"/api/admin/credentials/{credential['id']}",
        headers=admin["headers"],
        json={"status": "disabled"},
    )
    assert r.status_code == 200, r.text

    result = _run(agent_model.check())
    assert result["ok"] is False
    assert result["checked"] is False
    assert any("凭证" in problem for problem in result["problems"])


def test_validate_patch_rejects_negative_policy_numbers() -> None:
    from skillrouter_api.errors import ApiError

    with pytest.raises(ApiError):
        agent_model.validate_patch({"max_test_calls_per_flow": -1})
    with pytest.raises(ApiError):
        agent_model.validate_patch({"allowed_window_hours": -3})
    with pytest.raises(ApiError):
        agent_model.validate_patch({"max_cost_usd_per_flow": -0.5})
    cleaned = agent_model.validate_patch({"max_test_calls_per_flow": 0})
    assert cleaned["max_test_calls_per_flow"] == 0
