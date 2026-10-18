"""统一调用（sync / async / stream）+ 幂等键（§9.3）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...deps import require_token
from ...errors import ApiError, Code
from ...logging import redact
from ...schemas.execution import InvokeRequest
from ...security.tokens import TokenContext
from ...services import execution
from ...sse.events import sse_response

router = APIRouter(tags=["v1-invoke"])


def _options(payload: InvokeRequest, request: Request) -> dict:
    options = payload.options.model_dump(exclude_none=True)
    header_key = request.headers.get("Idempotency-Key")
    if header_key and not options.get("idempotency_key"):
        options["idempotency_key"] = header_key
    if payload.capability_version:
        options["capability_version"] = payload.capability_version
    return options


@router.post("/capabilities/{key}/invoke")
async def invoke(
    key: str,
    payload: InvokeRequest,
    request: Request,
    ctx: TokenContext = Depends(require_token),
):
    options = _options(payload, request)
    mode = options.get("mode") or "sync"
    wants_stream = (
        mode == "stream"
        or options.get("stream") is True
        or "text/event-stream" in (request.headers.get("accept") or "")
    )
    if wants_stream and mode != "async":
        generator = execution.stream(
            capability_key=key,
            input_payload=payload.input,
            options=options,
            caller={"type": "token", "id": ctx.token_id},
            token_ctx=ctx,
        )
        return sse_response(_sse_pack(generator))
    result = await execution.invoke(
        capability_key=key,
        input_payload=payload.input,
        options=options,
        caller={"type": "token", "id": ctx.token_id},
        token_ctx=ctx,
    )
    status_code = 202 if result.get("task_id") else 200
    return JSONResponse(status_code=status_code, content=json.loads(json.dumps(result, default=str)))


async def _sse_pack(generator):  # noqa: ANN001, ANN202
    try:
        async for event in generator:
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
    except ApiError as exc:
        payload = {
            "type": "call.error",
            "error": {
                "code": exc.code,
                "message": exc.message,
                "hint": exc.hint,
                "retryable": exc.retryable,
                "details": redact(exc.details),
            },
        }
        yield f"event: call.error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = ["router", "Code"]
