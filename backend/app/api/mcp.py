"""MCP adapter `/api/mcp` — Streamable HTTP, JSON-RPC 2.0 (CONTRACT §7).

Transport rules that matter:

* **Auth precedes JSON-RPC.** A missing/revoked/expired token is answered with a
  plain HTTP 401 JSON body, *not* a JSON-RPC error envelope: at that point we do
  not know (and must not guess) which client is talking.
* Everything else is JSON-RPC 2.0 and always answers HTTP 200, with `-32600`,
  `-32601`, `-32602`, `-32603` or `-32700` error objects.
* `Accept: text/event-stream` switches the *response* encoding to SSE; the
  request body is still a single JSON object or a batch.
* `Mcp-Session-Id` is generated on `initialize`, echoed afterwards, and DELETEd
  for teardown. GET on this path is not an SSE transport here (405 + Allow).

Every `tools/call` goes through `services.execution.invoke` with
`surface="mcp"`, so invocation records, scopes, rate limits, budgets and
idempotency behave exactly like the `/api/v1` path. Nothing is re-implemented
here — a tool *is* a published, granted capability.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core import db
from app.core.errors import ApiError
from app.core.request_context import get_request_id
from app.deps import AgentPrincipal, require_agent
from app.services import execution

# Reuse the descriptor/visibility rules of the REST surface so both transports
# can never disagree about what a token is allowed to see.
from app.api.external import granted_descriptors

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

SERVER_NAME = "skill-router"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]
TOOLS_PAGE_SIZE = 50
STREAM_MEDIA_TYPE = "text/event-stream"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Skill Router exposes each published capability granted to this token as one tool. "
    "Tool names are capability slugs with '.' replaced by '__' (text.summarize -> text__summarize). "
    "Call a tool with `tools/call` and its input schema as `arguments`. Asynchronous "
    "capabilities return a task id in `structuredContent.task_id`; poll it with "
    "GET /api/v1/tasks/{task_id} and cancel with POST /api/v1/tasks/{task_id}/cancel. "
    "Refresh `tools/list` when GET /api/v1/capabilities returns a new version_token."
)


# --------------------------------------------------------------------- tools ---
def tool_name(slug: str) -> str:
    return slug.replace(".", "__")


def slug_from_tool(name: str) -> str:
    return name.replace("__", ".")


def _tool_input_schema(descriptor: dict[str, Any]) -> dict[str, Any]:
    schema = descriptor.get("input_schema")
    if not isinstance(schema, dict) or not schema:
        schema = {"type": "object", "properties": {}}
    schema = json.loads(json.dumps(schema, default=str))
    if not schema.get("type"):
        schema["type"] = "object"
    # MCP clients validate against this; an absent additionalProperties would let
    # them send fields the platform rejects with `unsupported_parameter`.
    schema.setdefault("additionalProperties", False)
    if schema["type"] == "object" and "properties" not in schema:
        schema["properties"] = {}
    return schema


def _tool_description(descriptor: dict[str, Any]) -> str:
    modes = descriptor.get("modes") or {}
    mode_names = [name for name in ("sync", "async", "stream") if modes.get(name)] or ["sync"]
    parts = [
        f"{descriptor.get('name') or descriptor['slug']} — {descriptor.get('type') or 'generic'} capability"
        f" (slug `{descriptor['slug']}`, version {((descriptor.get('versions') or {}).get('current'))}).",
    ]
    description = (descriptor.get("description") or "").strip()
    if description:
        parts.append(description)
    parts.append(f"Modes: {', '.join(mode_names)}.")
    if modes.get("async"):
        parts.append(
            "Asynchronous calls return a task id in structuredContent.task_id; poll "
            "/api/v1/tasks/{task_id} for the result."
        )
    if descriptor.get("needs_live_agent"):
        parts.append("Needs live agent reasoning on the caller side.")
    return " ".join(parts)


def tools_for(principal: AgentPrincipal) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for descriptor in granted_descriptors(principal):
        tools.append(
            {
                "name": tool_name(descriptor["slug"]),
                "title": descriptor.get("name") or descriptor["slug"],
                "description": _tool_description(descriptor),
                "inputSchema": _tool_input_schema(descriptor),
                "annotations": {
                    "readOnlyHint": False,
                    "openWorldHint": True,
                    "capability": descriptor["slug"],
                },
            }
        )
    return tools


# ------------------------------------------------------------------- errors ----
def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _protocol_error(
    message: str,
    *,
    code: str = "unauthenticated",
    actions: list[str] | None = None,
    header_session: str | None = None,
) -> JSONResponse:
    """Transport-level failure: plain JSON, never a JSON-RPC envelope."""
    # Defect fixed: this answered {code, message, request_id} only, while CONTRACT
    # §2.1 (and lib/api.ts in the frontend) require the uniform envelope with
    # `details` and `actions` on *every* error. A client reading
    # `error.actions` crashed on the MCP 401 path.
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": None,
                "request_id": get_request_id(),
                "actions": actions or ["Send Authorization: Bearer <agent token>."],
            }
        },
        headers={"www-authenticate": "Bearer"} | ({"mcp-session-id": header_session} if header_session else {}),
    )


# ------------------------------------------------------------------ methods ----
def _initialize(params: dict[str, Any], session_id: str) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    negotiated = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
    return {
        "protocolVersion": negotiated,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {},
            "prompts": {},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def _list_tools(params: dict[str, Any], principal: AgentPrincipal) -> dict[str, Any]:
    tools = tools_for(principal)
    cursor = params.get("cursor")
    offset = 0
    if cursor not in (None, ""):
        try:
            offset = max(0, int(str(cursor)))
        except (TypeError, ValueError):
            return _error(None, INVALID_PARAMS, "cursor is not valid for this server.", {"cursor": cursor})
    page = tools[offset : offset + TOOLS_PAGE_SIZE]
    next_cursor = offset + TOOLS_PAGE_SIZE
    return {
        "tools": page,
        "nextCursor": str(next_cursor) if next_cursor < len(tools) else None,
    }


async def _call_tool(params: dict[str, Any], principal: AgentPrincipal) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error(
            None,
            INVALID_PARAMS,
            "tools/call requires a tool name.",
            {"code": "invalid_params", "parameter": "name"},
        )
    slug = slug_from_tool(name.strip())
    row = db.query_one("SELECT slug, status, current_version_id FROM capabilities WHERE slug = ?", (slug,))
    if not row or row["status"] != "published" or not row.get("current_version_id"):
        # Unknown and not-yet-published are indistinguishable on purpose.
        return _error(
            None,
            INVALID_PARAMS,
            f"Unknown tool '{name}'.",
            {"code": "tool_not_found", "tool": name, "capability": slug},
        )
    if not principal.can_use(slug):
        return _error(
            None,
            INVALID_PARAMS,
            f"This token is not granted '{slug}'.",
            {"code": "capability_not_granted", "tool": name, "capability": slug},
        )
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(
            None,
            INVALID_PARAMS,
            "arguments must be a JSON object.",
            {"code": "invalid_arguments", "tool": name},
        )
    options = execution.InvokeOptions(
        surface="mcp",
        actor_type="agent",
        actor_id=principal.agent_id or None,
    )
    try:
        result = await execution.invoke(principal, slug, arguments, options)
    except ApiError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        request_id = str(details.get("request_id") or get_request_id())
        return _tool_error(
            exc.code,
            exc.message,
            request_id=request_id,
            actions=list(exc.actions or []),
            details=exc.details,
            tool=name,
            capability=slug,
        )
    except Exception as exc:  # noqa: BLE001 - a tool error must not kill the transport
        return _tool_error("execution_failed", str(exc), request_id=get_request_id(), tool=name, capability=slug)

    payload = result.to_dict()
    payload.pop("schema_valid", None)
    for artifact in payload.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("id"):
            artifact["url"] = f"/api/v1/files/{artifact['id']}"
    if payload.get("task_id"):
        payload["poll"] = f"/api/v1/tasks/{payload['task_id']}"
    if payload.get("task_id"):
        text_obj: Any = {
            "task_id": payload["task_id"],
            "status": payload.get("status"),
            "mode": "async",
            "poll": payload["poll"],
            "note": "This capability runs asynchronously; poll the task id for the result.",
            "request_id": payload.get("request_id"),
        }
    else:
        text_obj = payload.get("result")
        if text_obj is None:
            text_obj = {"status": payload.get("status"), "request_id": payload.get("request_id")}
    is_error = payload.get("status") in {"failed", "uncertain"} or bool(payload.get("error"))
    return {
        "content": [{"type": "text", "text": json.dumps(text_obj, ensure_ascii=False, default=str)}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


def _tool_error(
    code: str,
    message: str,
    *,
    request_id: str,
    actions: list[str] | None = None,
    details: Any = None,
    tool: str | None = None,
    capability: str | None = None,
) -> dict[str, Any]:
    """A tool-level failure: `isError: true`, never a JSON-RPC error."""
    error_body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "actions": actions or [],
        "details": details,
    }
    if tool:
        error_body["tool"] = tool
    if capability:
        error_body["capability"] = capability
    return {
        "content": [{"type": "text", "text": json.dumps({"error": error_body}, ensure_ascii=False, default=str)}],
        "structuredContent": {"error": error_body, "request_id": request_id, "isError": True},
        "isError": True,
    }


async def _dispatch(message: dict[str, Any], principal: AgentPrincipal, session_id: str) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns the response, or None for a notification."""
    request_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0":
        if is_notification:
            return None
        return _error(request_id, INVALID_REQUEST, "jsonrpc must be '2.0'.")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        if is_notification:
            return None
        return _error(request_id, INVALID_REQUEST, "A JSON-RPC method name is required.")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error(request_id, INVALID_PARAMS, "params must be a JSON object.")

    if method.startswith("notifications/"):
        # `notifications/initialized` and friends: no response by design.
        return None
    if is_notification:
        return None

    if method == "initialize":
        return _result(request_id, _initialize(params, session_id))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        listed = _list_tools(params, principal)
        if "error" in listed and isinstance(listed.get("error"), dict):
            listed["id"] = request_id
            return listed
        return _result(request_id, listed)
    if method == "tools/call":
        outcome = await _call_tool(params, principal)
        if "jsonrpc" in outcome and "error" in outcome:
            outcome["id"] = request_id
            return outcome
        return _result(request_id, outcome)
    if method == "resources/list":
        return _result(request_id, {"resources": [], "nextCursor": None})
    if method == "resources/templates/list":
        return _result(request_id, {"resourceTemplates": []})
    if method == "prompts/list":
        return _result(request_id, {"prompts": [], "nextCursor": None})
    return _error(request_id, METHOD_NOT_FOUND, f"Method '{method}' is not supported.", {"method": method})


def _negotiate_protocol(request: Request, payload: Any) -> str:
    header = request.headers.get("mcp-protocol-version")
    if header in SUPPORTED_PROTOCOLS:
        return header
    requested = None
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            requested = params.get("protocolVersion")
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("method") == "initialize":
                params = item.get("params")
                if isinstance(params, dict):
                    requested = params.get("protocolVersion")
                break
    return requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL


# ------------------------------------------------------------------- routes ----
@router.post("", summary="JSON-RPC 2.0 endpoint (Streamable HTTP)")
async def mcp_post(request: Request) -> Response:
    session_id = (request.headers.get("mcp-session-id") or "").strip() or None
    try:
        principal = require_agent(request)
    except ApiError as exc:
        return _protocol_error(exc.message, code=exc.code, header_session=session_id)

    raw = await request.body()
    if not raw:
        return JSONResponse(
            content=_error(None, INVALID_REQUEST, "The request body is empty."),
            headers={"mcp-session-id": session_id or db.new_id("sess")},
        )
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return JSONResponse(
            content=_error(None, PARSE_ERROR, "Parse error: the body is not valid JSON."),
            headers={"mcp-session-id": session_id or db.new_id("sess")},
        )

    is_batch = isinstance(payload, list)
    messages = payload if is_batch else [payload]
    if not messages:
        return JSONResponse(content=_error(None, INVALID_REQUEST, "An empty batch is not a valid request."))

    protocol_version = _negotiate_protocol(request, payload)
    new_session = session_id or db.new_id("sess")

    responses: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            responses.append(_error(None, INVALID_REQUEST, "Each batch entry must be a JSON object."))
            continue
        reply = await _dispatch(message, principal, new_session)
        if reply is not None:
            responses.append(reply)

    headers = {
        "mcp-session-id": new_session,
        "mcp-protocol-version": protocol_version,
        "cache-control": "no-store",
    }
    if not responses:
        # Notification-only request (e.g. `notifications/initialized`).
        return Response(status_code=204, headers=headers)

    body: Any = responses if is_batch else responses[0]
    accept = (request.headers.get("accept") or "").lower()
    if STREAM_MEDIA_TYPE in accept:
        payloads = responses if is_batch else [responses[0]]

        async def _events():
            for item in payloads:
                yield f"event: message\ndata: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(
            _events(),
            media_type=STREAM_MEDIA_TYPE,
            headers={**headers, "x-accel-buffering": "no"},
        )
    return JSONResponse(content=body, headers=headers)


@router.get("", summary="Not an SSE endpoint; POST JSON-RPC instead")
def mcp_get() -> Response:
    return JSONResponse(
        status_code=405,
        content={
            "error": {
                "code": "method_not_allowed",
                "message": (
                    "This MCP endpoint speaks Streamable HTTP only: POST JSON-RPC messages, "
                    "DELETE to end the session. A standalone SSE stream is not offered."
                ),
                "details": None,
                "request_id": get_request_id(),
                "actions": ["POST JSON-RPC 2.0 to /api/mcp.", "Send 'Accept: text/event-stream' to receive SSE-framed responses."],
            }
        },
        headers={"allow": "POST, DELETE"},
    )


@router.delete("", summary="End the MCP session")
def mcp_delete() -> Response:
    """Sessions are stateless on the server; teardown is acknowledged, not stored."""
    return Response(status_code=204)
