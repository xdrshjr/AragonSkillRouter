"""概览与全局事件（§9.6 / §9.2 `/events`）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ...deps import require_password_changed
from ...security.sessions import SessionContext
from ...services import overview
from ...sse import broker
from ...sse.events import sse_response

router = APIRouter(tags=["overview"])


@router.get("/overview")
async def get_overview(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return overview.overview()


@router.get("/events")
async def events(
    request: Request, ctx: SessionContext = Depends(require_password_changed)
) -> StreamingResponse:
    last_event_id = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")

    async def generator():
        queue = broker.subscribe("admin")
        try:
            replay = broker.replay("admin", int(last_event_id) if last_event_id and last_event_id.isdigit() else None)
            for event in replay:
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
        finally:
            broker.unsubscribe("admin", queue)

    return sse_response(generator())


@router.get("/search")
async def search(
    q: str = Query(min_length=1),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """全局搜索（Ctrl+K）：能力 / 提供商 / SKILL / 令牌。"""
    from ...services import access, capabilities, providers, skills

    needle = q.lower()
    results: list[dict] = []
    for item in capabilities.list_capabilities(page_size=100)["items"]:
        if needle in item["key"].lower() or needle in item["name"].lower():
            results.append({"type": "capability", "id": item["id"], "title": item["key"], "subtitle": item["name"], "href": f"/capabilities/{item['id']}"})
    for item in providers.list_providers(page_size=100)["items"]:
        if needle in item["name"].lower() or needle in item["slug"].lower():
            results.append({"type": "provider", "id": item["id"], "title": item["name"], "subtitle": item["category"], "href": f"/providers/{item['id']}"})
    for item in skills.list_skills(page_size=100)["items"]:
        if needle in item["name"].lower() or needle in item["title"].lower():
            results.append({"type": "skill", "id": item["id"], "title": item["name"], "subtitle": item["title"], "href": f"/skills/{item['id']}"})
    for item in access.list_agents(page_size=100)["items"]:
        if needle in item["name"].lower():
            results.append({"type": "agent", "id": item["id"], "title": item["name"], "subtitle": item.get("description") or "", "href": "/access"})
    return {"q": q, "results": results[:30]}
