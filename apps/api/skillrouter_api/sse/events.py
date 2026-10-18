"""SSE 事件格式、Last-Event-ID 重放与心跳（§9.4）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from starlette.responses import StreamingResponse

KEEPALIVE_S = 15


def format_event(event: dict) -> str:
    lines = []
    if event.get("id") is not None:
        lines.append(f"id: {event['id']}")
    lines.append(f"event: {event.get('type', 'message')}")
    payload = {k: v for k, v in event.items() if k not in ("id", "type")}
    lines.append("data: " + json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def with_keepalive(
    source: AsyncIterator[dict], *, replay: list[dict] | None = None
) -> AsyncIterator[str]:
    for event in replay or []:
        yield format_event(event)
    yield ": connected\n\n"
    import time

    last = time.monotonic()
    async for event in source:
        yield format_event(event)
        now = time.monotonic()
        if now - last > KEEPALIVE_S:
            yield ": keep-alive\n\n"
        last = now


async def heartbeat_stream() -> AsyncIterator[str]:
    """纯心跳（用于等待事件的空流）。"""
    while True:
        yield ": keep-alive\n\n"
        await asyncio.sleep(KEEPALIVE_S)
