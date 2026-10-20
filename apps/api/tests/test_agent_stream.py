"""会话事件流必须跨进程（真实缺陷的回归测试）。

缺陷现象：轮次由 worker 进程执行并把事件写库，浏览器连的是 API 进程的
`/api/admin/agent/sessions/{id}/stream`。旧实现从**进程内 broker** 取事件，
于是网关的事件永远到不了浏览器：UI 一直「执行中」，时间线空白，而库里这一轮
已经 `succeeded`。

现在事件源是 `agent_events` 表（P04 权威源），用 rowid 做游标，因此：
- 另一个进程写入的事件，流里必须能推出来；
- 断线重连按 `last_event_id=<rowid>` 只补之后的事件。

> 用 `live.tail()`（路由实际调用的生成器）而不是 HTTP 层：starlette 的 TestClient
> 会把响应体读完才返回，无限流会把它挂死（这是实测结论，不是偏好）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skillrouter_api.agent import events as agent_events
from skillrouter_api.agent import live
from skillrouter_api.agent import sessions as agent_sessions


def _run(coro):
    """运行协程，并把事件循环还给线程。

    不用 `_run()`：它会清空线程的 current event loop，而既有测试
    （tests/test_budget_accounting.py）仍依赖 `asyncio.get_event_loop()`，
    结果是「谁先跑」决定成败的顺序性失败。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture()
def session_id() -> str:
    return agent_sessions.create_session({"title": "stream-test"})["id"]


def _append(session_id: str, turn_id: str, event_type: str, payload: dict) -> dict:
    return agent_events.append(
        turn_id=turn_id,
        session_id=session_id,
        seq=agent_events.next_seq(turn_id),
        event_type=event_type,
        payload=payload,
    )


def _parse(frame: str) -> dict | None:
    for line in frame.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


async def _next_data(agen, timeout: float = 10.0) -> dict:  # noqa: ANN001
    """跳过注释行（`: connected` / `: keep-alive`），取下一条数据事件。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "等待事件超时"
        chunk = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
        body = _parse(chunk)
        if body is not None:
            return body


def test_events_after_returns_rows_in_write_order(session_id: str) -> None:
    cursor = live.latest_cursor(session_id)
    _append(session_id, "trn_s1", "turn.started", {"ordinal": 1})
    _append(session_id, "trn_s1", "assistant.delta", {"text": "你好"})

    rows = live.events_after(session_id, cursor)
    assert [row["type"] for row in rows] == ["turn.started", "assistant.delta"]
    assert rows[1]["payload"] == {"text": "你好"}
    assert rows[0]["rowid"] < rows[1]["rowid"]  # 可作 Last-Event-ID
    assert live.events_after(session_id, rows[-1]["rowid"]) == []


def test_latest_cursor_skips_history_but_session_cursor_replays(session_id: str) -> None:
    _append(session_id, "trn_s2", "turn.started", {"ordinal": 1})
    _append(session_id, "trn_s2", "turn.finished", {"usage": {}})
    assert live.events_after(session_id, live.latest_cursor(session_id)) == []
    assert len(live.events_after(session_id, live.session_cursor(session_id))) >= 2


def test_frame_carries_rowid_and_session(session_id: str) -> None:
    _append(session_id, "trn_s3", "assistant.delta", {"text": "hi"})
    row = live.events_after(session_id, live.session_cursor(session_id))[-1]
    text = live.frame(row)
    assert text.startswith(f"id: {row['rowid']}\n")
    # 契约：只有 id + data（浏览器 EventSource 只在事件名 message 时触发 onmessage；
    # 写成 `event: <type>` 会变成命名事件，UI 的 onmessage 永远收不到 → 面板空白）
    assert "\nevent:" not in text
    assert _parse(text) == {
        "id": row["rowid"],
        "type": "assistant.delta",
        "at": row["at"],
        "turn_id": "trn_s3",
        "session_id": session_id,
        "seq": row["seq"],
        "payload": {"text": "hi"},
    }


def test_tail_delivers_events_written_by_another_process(session_id: str) -> None:
    """worker 进程写库 → API 进程的流必须推给浏览器（旧实现这里永远收不到）。"""

    async def scenario() -> dict:
        agen = live.tail(session_id, live.latest_cursor(session_id))
        try:
            # 连接建立后，另一个进程写了两条事件
            _append(session_id, "trn_cross", "turn.started", {"ordinal": 1})
            _append(session_id, "trn_cross", "assistant.delta", {"text": "来自 worker"})
            first = await _next_data(agen)
            second = await _next_data(agen)
            assert first["type"] == "turn.started"
            return second
        finally:
            await agen.aclose()

    second = _run(scenario())
    assert second["type"] == "assistant.delta"
    assert second["payload"]["text"] == "来自 worker"
    # UI 的 handleEvent 依赖这些字段
    assert second["session_id"] == session_id
    assert second["turn_id"] == "trn_cross"


def test_tail_resumes_from_last_event_id(session_id: str) -> None:
    first = _append(session_id, "trn_resume", "turn.started", {"ordinal": 1})
    _append(session_id, "trn_resume", "assistant.delta", {"text": "补发"})
    rows = live.events_after(session_id, live.session_cursor(session_id))
    rowid = next(row["rowid"] for row in rows if row["id"] == first["id"])

    async def scenario() -> dict:
        agen = live.tail(session_id, rowid)
        try:
            return await _next_data(agen)
        finally:
            await agen.aclose()

    body = _run(scenario())
    assert body["type"] == "assistant.delta"
    assert body["payload"]["text"] == "补发"


def test_tail_emits_keepalive_when_idle(session_id: str, monkeypatch) -> None:
    monkeypatch.setattr(live, "KEEPALIVE_S", 0.0)

    async def scenario() -> str:
        agen = live.tail(session_id)
        try:
            await agen.__anext__()  # ": connected"
            return await asyncio.wait_for(agen.__anext__(), timeout=5)
        finally:
            await agen.aclose()

    assert "keep-alive" in _run(scenario())
