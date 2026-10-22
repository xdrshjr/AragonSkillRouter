"""Live SSE for agent runs (docs/CONTRACT.md §5.10, owner agent-engine).

``GET /api/stream/agent/{run_id}`` replays persisted events from
``Last-Event-ID`` and then tails new ones until the run reaches a terminal state.

Why poll the database instead of piping the sidecar stream straight through:

  * the events the client sees must be the *same rows* the replay endpoint and the
    audit trail show — one history, not two;
  * the run may be executing in a worker process, whose memory this request cannot
    reach;
  * a client that connects late, or reconnects, must get exactly what it missed,
    and the sequence number in the database is the only authority for that.

The cost is a 400 ms poll on a local SQLite row range — cheap, and bounded by the
run's own lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core import db
from app.core.errors import not_found
from app.deps import AuthUser, require_admin
from app.services import agent_runtime

log = logging.getLogger("skillrouter.agent_stream")

router = APIRouter(prefix="/api/stream", tags=["stream"])

TERMINAL = frozenset({"succeeded", "failed", "stopped"})
POLL_SECONDS = 0.4
KEEPALIVE_SECONDS = 15.0
#: After the terminal event the stream closes, so an EventSource that reconnects
#: gets an immediate replay + close instead of hanging.
MAX_IDLE_SECONDS = 300.0


def _frame(event: dict[str, Any]) -> str:
    seq = event.get("seq", 0)
    event_type = event.get("type", "run.progress")
    body = json.dumps(event, ensure_ascii=False, default=str)
    return f"id: {seq}\nevent: {event_type}\ndata: {body}\n\n"


def _keepalive(run_id: str, seq: int) -> str:
    payload = {
        "seq": seq,
        "run_id": run_id,
        "type": "keepalive",
        "at": db.iso(),
        "note": "no events yet",
    }
    return f"event: keepalive\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _parse_last_event_id(header: str | None, query_value: int | None) -> int:
    if query_value is not None and query_value >= 0:
        return query_value
    if not header:
        return 0
    try:
        return max(0, int(header))
    except (TypeError, ValueError):
        return 0


@router.get("/agent/{run_id}")
async def stream_agent_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    since: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    row = db.query_one("SELECT id, status FROM agent_runs WHERE id = ?", (run_id,))
    if not row:
        raise not_found("Agent run")
    after = _parse_last_event_id(last_event_id, since)

    async def generator() -> AsyncIterator[bytes]:
        cursor = after
        idle_since = asyncio.get_event_loop().time()
        last_keepalive = idle_since
        # A `retry` hint keeps a browser EventSource from hammering us if it
        # reconnects after the terminal frame.
        yield b"retry: 5000\n\n"
        while True:
            if await request.is_disconnected():
                return
            events = agent_runtime.load_events(run_id, after_seq=cursor, limit=200)
            if events:
                for event in events:
                    yield _frame(event).encode("utf-8")
                    cursor = int(event.get("seq", cursor))
                idle_since = asyncio.get_event_loop().time()
                last_keepalive = idle_since
                continue

            status_row = db.query_one("SELECT status FROM agent_runs WHERE id = ?", (run_id,))
            status = status_row["status"] if status_row else row["status"]
            if status in TERMINAL:
                # One final keepalive so a client that only listens for *something*
                # learns the stream is over, then close.
                yield _keepalive(run_id, cursor).encode("utf-8")
                return

            now = asyncio.get_event_loop().time()
            if now - last_keepalive >= KEEPALIVE_SECONDS:
                yield _keepalive(run_id, cursor).encode("utf-8")
                last_keepalive = now
            if now - idle_since >= MAX_IDLE_SECONDS:
                log.info("closing idle agent stream for run %s after %.0fs", run_id, MAX_IDLE_SECONDS)
                yield f": idle timeout after {int(MAX_IDLE_SECONDS)}s\n\n".encode("utf-8")
                return
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        },
    )
