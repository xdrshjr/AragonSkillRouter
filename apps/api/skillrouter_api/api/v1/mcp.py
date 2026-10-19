"""MCP Streamable HTTP 适配（§9.7）。"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ...db import now_iso
from ...deps import require_token
from ...errors import ApiError, Code
from ...security.tokens import TokenContext
from ...services import capabilities, execution, skills, tasks
from ...settings import settings

router = APIRouter(tags=["v1-mcp"])

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26"]
SESSIONS: dict[str, dict] = {}

META_TOOLS = [
    {
        "name": "capabilities_list",
        "title": "列出能力",
        "description": "列出该令牌可见的已发布能力目录",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "skills_list",
        "title": "列出 SKILL",
        "description": "列出已发布且启用的 SKILL",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "skills_read",
        "title": "读取 SKILL",
        "description": "读取某个 SKILL 的正文与依赖",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_status",
        "title": "任务状态",
        "description": "查询异步任务状态",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_cancel",
        "title": "取消任务",
        "description": "取消异步任务（明确返回 cancel_scope）",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]


def tool_name(key: str) -> str:
    return "capability__" + key.replace(".", "_").replace("-", "_")


def tool_list(ctx: TokenContext) -> list[dict]:
    tools = list(META_TOOLS)
    for item in capabilities.catalog(ctx):
        description = capabilities.describe(item["key"])
        tools.append(
            {
                "name": tool_name(item["key"]),
                "title": item["name"],
                "description": item["name"] + (f"（{item['kind']}）" if item.get("kind") else ""),
                "inputSchema": description.get("input_schema") or {"type": "object"},
            }
        )
    return tools


def _ok(request_id, result: dict) -> JSONResponse:  # noqa: ANN001
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _err(request_id, code: int, message: str, data: dict | None = None) -> JSONResponse:  # noqa: ANN001
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "data": data or {}}}
    )


@router.post("/mcp")
async def mcp(request: Request, ctx: TokenContext = Depends(require_token)):
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    session_id = request.headers.get("Mcp-Session-Id")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        new_session = session_id or uuid.uuid4().hex
        SESSIONS[new_session] = {"version": version, "token_id": ctx.token_id, "at": now_iso()}
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "skill-router", "version": "0.1.0"},
                    "instructions": "通过 tools/list 获取能力工具；异步能力用 task_status 轮询。",
                },
            },
            headers={"Mcp-Session-Id": new_session},
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return Response(status_code=202)
    if method == "ping":
        return _ok(request_id, {})
    if session_id and session_id not in SESSIONS:
        return _err(request_id, -32600, "未知的 Mcp-Session-Id，请重新 initialize")
    if method == "tools/list":
        return _ok(request_id, {"tools": tool_list(ctx)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return await _call_tool(request_id, name, arguments, ctx)
    return _err(request_id, -32601, f"不支持的方法：{method}")


@router.get("/mcp")
async def mcp_stream(ctx: TokenContext = Depends(require_token)) -> Response:
    """服务端消息通道（本阶段无主动推送，返回空流）。"""
    return Response(status_code=204, headers={"Mcp-Session-Id": ""})


async def _call_tool(request_id, name: str | None, arguments: dict, ctx: TokenContext) -> JSONResponse:  # noqa: ANN001
    try:
        if name == "capabilities_list":
            items = capabilities.catalog(ctx)
            return _ok(request_id, _text(items))
        if name == "skills_list":
            return _ok(request_id, _text(skills.catalog()))
        if name == "skills_read":
            return _ok(request_id, _text(skills.public_skill(arguments["name"])))
        if name == "task_status":
            return _ok(request_id, _text(tasks.get_owned(arguments["task_id"], ctx.token_id)))
        if name == "task_cancel":
            tasks.get_owned(arguments["task_id"], ctx.token_id)
            return _ok(request_id, _text(tasks.cancel(arguments["task_id"])))
        if not name or not name.startswith("capability__"):
            return _err(request_id, -32602, f"未知工具：{name}")
        key = _resolve_key(name, ctx)
        description = capabilities.describe(key)
        mode = (arguments.get("options") or {}).get("mode") or (description.get("modes") or ["sync"])[0]
        input_payload = arguments.get("input") if "input" in arguments else {
            k: v for k, v in arguments.items() if k != "options"
        }
        result = await execution.invoke(
            capability_key=key,
            input_payload=input_payload or {},
            options={**(arguments.get("options") or {}), "mode": mode},
            caller={"type": "token", "id": ctx.token_id},
            token_ctx=ctx,
        )
        payload = {k: v for k, v in result.items() if k != "timings"}
        return _ok(request_id, {**_text(payload), "structuredContent": result})
    except ApiError as exc:
        return _ok(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"code": exc.code, "message": exc.message, "hint": exc.hint}, ensure_ascii=False)}],
                "isError": True,
            },
        )
    except KeyError as exc:
        return _err(request_id, -32602, f"缺少参数：{exc}")


def _text(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _resolve_key(name: str, ctx: TokenContext) -> str:
    for item in capabilities.catalog(ctx):
        if tool_name(item["key"]) == name:
            return item["key"]
    raise ApiError(Code.NOT_FOUND, f"工具不存在：{name}")


__all__ = ["router", "tool_name", "tool_list", "settings"]
