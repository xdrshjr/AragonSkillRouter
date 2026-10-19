"""事件总线：内存 pub/sub + DB 回放（§9.4 / P04 职责划分）。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..db import db_session, db_write, now_iso
from ..models import AgentEventRow, SseEvent

KEEP_HOURS = 24


class Broker:
    """每个 stream_key 一组内存队列；DB 只作为 24h 回放载体。

    职责划分（P04）：Agent 通道的权威源是 `agent_events`，这里只承载
    `tasks` / `calls` / `notices` 等非 Agent 通道，不双写同一事件。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._seq: int = 0

    def publish(self, stream_key: str, event_type: str, payload: dict[str, Any]) -> int:
        event_id = self._next_seq()
        body = {"id": event_id, "type": event_type, "at": now_iso(), "payload": payload}
        try:
            with db_write() as s:
                s.add(
                    SseEvent(
                        id=event_id,
                        stream_key=stream_key,
                        type=event_type,
                        payload=payload,
                        at=now_iso(),
                    )
                )
        except Exception:  # noqa: BLE001 - 事件落库失败不应中断业务
            pass
        for queue in list(self._subscribers.get(stream_key, ())):
            try:
                queue.put_nowait(body)
            except asyncio.QueueFull:
                pass
        return event_id

    def subscribe(self, stream_key: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers[stream_key].add(queue)
        return queue

    def unsubscribe(self, stream_key: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(stream_key, set()).discard(queue)
        if stream_key in self._subscribers and not self._subscribers[stream_key]:
            self._subscribers.pop(stream_key, None)

    def _next_seq(self) -> int:
        from sqlalchemy import text

        from ..db.engine import get_engine

        try:
            with get_engine().connect() as conn:
                row = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM sse_events")).scalar_one()
            return int(row) + 1
        except Exception:  # noqa: BLE001
            self._seq += 1
            return self._seq

    def replay(self, stream_key: str, last_event_id: int | None, limit: int = 500) -> list[dict]:
        with db_session() as s:
            stmt = select(SseEvent).where(SseEvent.stream_key == stream_key)
            if last_event_id:
                stmt = stmt.where(SseEvent.id > last_event_id)
            rows = list(s.scalars(stmt.order_by(SseEvent.id).limit(limit)))
        return [
            {"id": row.id, "type": row.type, "at": row.at, "payload": row.payload or {}}
            for row in rows
        ]

    def cleanup(self) -> dict:
        cutoff = (
            datetime.now(UTC) - timedelta(hours=KEEP_HOURS)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        removed = 0
        with db_write() as s:
            for row in s.scalars(select(SseEvent).where(SseEvent.at < cutoff)):
                s.delete(row)
                removed += 1
            for agent_row in s.scalars(
                select(AgentEventRow).where(AgentEventRow.at < cutoff)
            ):
                s.delete(agent_row)
                removed += 1
        return {"removed": removed}


broker = Broker()


def publish(stream_key: str, event_type: str, payload: dict[str, Any]) -> int:
    return broker.publish(stream_key, event_type, payload)


__all__ = ["broker", "publish", "Broker"]
