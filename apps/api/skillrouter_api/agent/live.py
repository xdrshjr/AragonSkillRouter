"""会话事件流（跨进程）。

**为什么不能用进程内 broker**：轮次由 worker 进程执行（它去消费网关 SSE 并落库），
而浏览器连的是 API 进程的 `/api/admin/agent/sessions/{id}/stream`。`sse.broker` 是
**进程内**的内存队列，于是 worker 发布的 `assistant.delta` / `tool.*` 永远到不了
浏览器：真实复现是「turn accepted 之后 UI 一直执行中、时间线空白」，而数据库里这一轮
已经 `succeeded`。

因此 SSE 的权威来源回到 `agent_events` 表（spec P04：它是唯一权威源），用 SQLite 的
`rowid`（单调递增）做游标。副作用是正面的：断线重连只需带 `last_event_id=<rowid>`
就能补齐断连期间的事件，不依赖进程内存。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from sqlalchemy import text

from ..db import db_session
from ..logging import get_logger

log = get_logger("skillrouter.run")

#: 轮询间隔：兼顾流式手感（网关按 delta 落库）与数据库压力
POLL_INTERVAL_S = 0.35
#: 单次批量上限，避免落后太多时一次拉爆内存
BATCH_LIMIT = 200
#: 心跳间隔（spec §9.4 要求 15s）
KEEPALIVE_S = 15.0


def latest_cursor(session_id: str) -> int:
    """订阅起点：当前该会话最新事件的 `rowid`（只推之后的新事件）。"""
    with db_session() as s:
        value = s.execute(
            text("SELECT COALESCE(MAX(rowid), 0) FROM agent_events WHERE session_id = :sid"),
            {"sid": session_id},
        ).scalar()
    return int(value or 0)


def session_cursor(session_id: str) -> int:
    """从最早开始（重放整个会话）。"""
    with db_session() as s:
        value = s.execute(
            text("SELECT COALESCE(MIN(rowid), 0) - 1 FROM agent_events WHERE session_id = :sid"),
            {"sid": session_id},
        ).scalar()
    return int(value or 0)


def events_after(session_id: str, cursor: int, limit: int = BATCH_LIMIT) -> list[dict]:
    """取出 `rowid > cursor` 的事件，按写入顺序返回（含 SSE 信封字段）。"""
    with db_session() as s:
        rows = s.execute(
            text(
                "SELECT rowid AS rid, id, turn_id, seq, type, payload, at "
                "FROM agent_events WHERE session_id = :sid AND rowid > :cursor "
                "ORDER BY rowid LIMIT :limit"
            ),
            {"sid": session_id, "cursor": cursor, "limit": limit},
        ).all()
    out: list[dict] = []
    for row in rows:
        mapping = row._mapping
        payload = mapping["payload"]
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {"value": payload}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        out.append(
            {
                "rowid": int(mapping["rid"]),
                "id": mapping["id"],
                "turn_id": mapping["turn_id"],
                "seq": mapping["seq"],
                "type": mapping["type"],
                "at": mapping["at"],
                "session_id": session_id,
                "payload": payload,
            }
        )
    return out


def frame(event: dict) -> str:
    """一条 SSE 帧：`id` 用 rowid，便于 `Last-Event-ID` 精确续传。

    **不要加 `event: <type>` 行。** 浏览器 `EventSource` 只在事件名为 `message`
    时才触发 `onmessage`；带上 `event: turn.started` 会变成「命名事件」，必须用
    `addEventListener('turn.started')` 才收得到，而前端 `lib/sse.ts` 的契约就是
    「`data:` 一行 JSON + onmessage」（事件类型在 JSON 里）。真实复现过：服务端推出
    163 条事件、UI 一条都没显示，原因就是这一行。
    """
    body = {
        "id": event["rowid"],
        "type": event["type"],
        "at": event["at"],
        "turn_id": event["turn_id"],
        "session_id": event["session_id"],
        "seq": event["seq"],
        "payload": event["payload"],
    }
    return f"id: {event['rowid']}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


async def tail(session_id: str, start_cursor: int | None = None) -> AsyncIterator[str]:
    """会话语义的事件流：先 `: connected`，然后按 rowid 持续推送新事件。

    `start_cursor=None` 表示「只看新事件」（历史由 `GET /sessions/{id}` 提供）；
    传具体 rowid 表示断线续传。空转时按 `KEEPALIVE_S` 发注释行，客户端据此判断连接存活。

    抽成独立生成器（而不是写在路由里）是为了让「跨进程投递」可被直接测试：
    starlette 的 TestClient 会把响应体读完才返回，测不了无限流。
    """
    cursor = start_cursor if start_cursor is not None else latest_cursor(session_id)
    idle_since = time.monotonic()
    yield ": connected\n\n"
    while True:
        events = events_after(session_id, cursor)
        if events:
            for event in events:
                cursor = max(cursor, event["rowid"])
                yield frame(event)
            idle_since = time.monotonic()
            continue
        now = time.monotonic()
        if now - idle_since >= KEEPALIVE_S:
            yield ": keep-alive\n\n"
            idle_since = now
        await asyncio.sleep(POLL_INTERVAL_S)


__all__ = [
    "BATCH_LIMIT",
    "KEEPALIVE_S",
    "POLL_INTERVAL_S",
    "events_after",
    "frame",
    "latest_cursor",
    "session_cursor",
    "tail",
]
