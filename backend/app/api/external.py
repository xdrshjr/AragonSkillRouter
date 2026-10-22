"""External API `/api/v1` — the surface an outside agent talks to (CONTRACT §6, §8).

Design rules this module follows:

* Every route is authenticated by an agent bearer token (`deps.require_agent`).
  There is no session cookie path here, no CSRF, and no browser redirect.
* Nothing is visible unless the token is *granted* the capability
  (`AgentPrincipal.can_use`) **and** the capability is `published` with a
  published version. An unknown slug and an ungranted slug answer exactly the
  same way, so the endpoint cannot be used to enumerate capabilities.
* Execution is never re-implemented: the routes call `services.execution` and
  `services.tasks`, which own validation, routing, limits, records and the
  idempotency ledger. The routes only shape the response and pick the
  *surface* label (`http`, `stream`) that lands on the invocation row.
* No secrets and no internal identifiers leave the building: agent id/name,
  capability slugs, file ids and request ids only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from functools import lru_cache
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.core.request_context import get_request_id
from app.deps import AgentPrincipal, require_agent
from app.services import capabilities as capability_service
from app.services import execution, export, usage
from app.services import files as file_service
from app.services import tasks as task_service

router = APIRouter(prefix="/api/v1", tags=["external"])

API_VERSION = "1"
MCP_URL = "/api/mcp"
SSE_KEEPALIVE_SECONDS = 15.0
STREAM_MEDIA_TYPE = "text/event-stream"

#: The machine-readable route index served at `GET /api/v1`. Kept next to the
#: handlers on purpose: an installer can show the surface without fetching the
#: OpenAPI document.
ROUTE_INDEX: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/v1", "This index."),
    ("GET", "/api/v1/whoami", "Agent identity, scopes and limits (connectivity check)."),
    ("GET", "/api/v1/capabilities", "Granted, published capabilities plus a version token."),
    ("GET", "/api/v1/capabilities/{slug}", "Full descriptor for one granted capability."),
    ("POST", "/api/v1/invocations", "Call a capability (JSON or SSE)."),
    ("GET", "/api/v1/invocations/{request_id}", "Replay one of your own recorded calls."),
    ("GET", "/api/v1/tasks/{task_id}", "Async task status."),
    ("POST", "/api/v1/tasks/{task_id}/cancel", "Truthful cancel result."),
    ("GET", "/api/v1/files/{file_id}", "Download an artifact your calls produced."),
    ("GET", "/api/v1/skill", "Installable connection SKILL for this token."),
    ("GET", "/api/v1/openapi.json", "OpenAPI 3.1 document for this surface."),
)


# --------------------------------------------------------------- descriptors --
def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _published_version_rows(capability_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        """SELECT * FROM capability_versions
            WHERE capability_id = ? AND status = 'published'
            ORDER BY version ASC""",
        (capability_id,),
    )


def _descriptor(row: dict[str, Any]) -> dict[str, Any] | None:
    """The public descriptor for one capability, or None when nothing is live."""
    if row.get("status") != "published":
        return None
    versions = _published_version_rows(row["id"])
    if not versions:
        return None
    current = None
    if row.get("current_version_id"):
        current = next((v for v in versions if v["id"] == row["current_version_id"]), None)
    current = current or versions[-1]
    config = capability_service.redacted_config(
        capability_service.config_of(current, row.get("type") or "generic")
    )
    limits = config.get("limits") or {}
    input_schema = config.get("input_schema") or {"type": "object", "properties": {}}
    output_schema = config.get("output_schema") or None
    descriptor: dict[str, Any] = {
        "slug": row["slug"],
        "name": row["name"],
        "type": row.get("type") or "generic",
        "description": row.get("description") or "",
        "versions": {
            "current": current["version"],
            "available": [int(v["version"]) for v in versions],
        },
        "modes": config.get("modes") or {},
        "input_schema": input_schema,
        "output_schema": output_schema,
        "limits": {
            key: limits.get(key)
            for key in (
                "timeout_s",
                "max_input_bytes",
                "max_output_bytes",
                "file_input",
                "max_file_mb",
                "max_duration_s",
            )
        },
        "examples": (config.get("examples") or [])[:5],
        "needs_live_agent": bool(row.get("needs_live_agent")),
        "updated_at": row.get("updated_at"),
    }
    descriptor["version_token"] = _digest(descriptor)
    return descriptor


def granted_descriptors(principal: AgentPrincipal) -> list[dict[str, Any]]:
    """Published + granted capabilities, the only ones an external caller sees."""
    rows = db.query_all(
        """SELECT c.* FROM capabilities c
            WHERE c.status = 'published' AND c.current_version_id IS NOT NULL
            ORDER BY c.slug ASC"""
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if not principal.can_use(row["slug"]):
            continue
        descriptor = _descriptor(row)
        if descriptor is not None:
            out.append(descriptor)
    return out


def descriptors_version_token(descriptors: list[dict[str, Any]]) -> str:
    """sha256 over the descriptors — clients refresh when this changes (§8)."""
    return _digest(descriptors)


def _granted_descriptor(principal: AgentPrincipal, slug: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM capabilities WHERE slug = ?", (slug,))
    # An ungranted capability must be indistinguishable from a missing one.
    if not row or not principal.can_use(slug):
        raise not_found("Capability")
    descriptor = _descriptor(row)
    if descriptor is None:
        raise not_found("Capability")
    return descriptor


# ------------------------------------------------------------------ helpers ---
def _matches_etag(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = [part.strip() for part in header.split(",") if part.strip()]
    return "*" in candidates or etag in candidates or etag.strip('"') in {c.strip('"') for c in candidates}


def _public_artifacts(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        file_id = item["id"]
        out.append(
            {
                "id": file_id,
                "url": f"/api/v1/files/{file_id}",
                "content_type": item.get("content_type"),
                "size": item.get("size"),
                "filename": item.get("filename"),
                "kind": item.get("kind"),
            }
        )
    return out


def _public_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape one execution result for an external caller (CONTRACT §6)."""
    body = dict(payload)
    body["artifacts"] = _public_artifacts(body.get("artifacts"))
    task_id = body.get("task_id")
    if task_id:
        body["poll"] = f"/api/v1/tasks/{task_id}"
    body.pop("schema_valid", None)
    return body


def _actor(principal: AgentPrincipal) -> dict[str, Any]:
    return {"type": "agent", "id": principal.agent_id or None}


def _owns_agent_row(principal: AgentPrincipal, agent_id: str | None) -> bool:
    """Admin sessions (empty agent id) may inspect everything; tokens may not."""
    if not principal.agent_id:
        return True
    return (agent_id or "") == principal.agent_id


def _can_read_file(principal: AgentPrincipal, row: dict[str, Any]) -> bool:
    if not principal.agent_id:
        return True
    # Uploaded user files are never readable with an agent token.
    if row.get("purpose") == "upload" or row.get("owner_type") == "user":
        return False
    if row.get("owner_type") == "agent" and row.get("owner_id") == principal.agent_id:
        return True
    if row.get("invocation_id"):
        owner = db.query_scalar("SELECT agent_id FROM invocations WHERE id = ?", (row["invocation_id"],))
        if owner and owner == principal.agent_id:
            return True
    if row.get("task_id"):
        owner = db.query_scalar("SELECT agent_id FROM tasks WHERE id = ?", (row["task_id"],))
        if owner and owner == principal.agent_id:
            return True
    return False


def _resolve_version_id(capability_ref: str, version: int | str | None) -> str | None:
    """Turn `version: 2` (or a version id) into a confirmed *published* version id."""
    if version in (None, ""):
        return None
    capability = execution.get_capability(capability_ref)
    row = None
    if isinstance(version, str) and not version.isdigit():
        row = db.query_one(
            "SELECT * FROM capability_versions WHERE id = ? AND capability_id = ?",
            (version, capability["id"]),
        )
    else:
        row = db.query_one(
            "SELECT * FROM capability_versions WHERE capability_id = ? AND version = ?",
            (capability["id"], int(version)),
        )
    if not row:
        raise not_found("Capability version")
    if row["status"] != "published":
        raise conflict(
            "capability_not_published",
            f"Version {row['version']} of '{capability['slug']}' is {row['status']}, not published.",
            details={"version": row["version"], "status": row["status"]},
            actions=["Publish the version first, or omit `version` to use the current one."],
        )
    return row["id"]


# ------------------------------------------------------------------- schemas ---
class InvocationOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str | None = Field(default=None, description="sync | async | stream")
    idempotency_key: str | None = Field(default=None, max_length=200)
    timeout_s: float | None = Field(default=None, gt=0, le=600)
    stream: bool | None = Field(default=None, description="Ask for a text/event-stream response.")


class InvocationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capability: str = Field(min_length=1, max_length=200, description="Capability slug.")
    version: int | str | None = Field(default=None, description="Published version number (or version id).")
    input: dict[str, Any] = Field(default_factory=dict, description="Inputs matching the input schema.")
    options: InvocationOptions = Field(default_factory=InvocationOptions)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = Field(default=None, max_length=300)


# -------------------------------------------------------------------- routes ---
@router.get("", summary="Route index for the external API")
def index() -> dict[str, Any]:
    """Discovery aid for installers: what this surface offers, without auth."""
    return {
        "service": "skill-router",
        "api_version": API_VERSION,
        "auth": "Authorization: Bearer <agent token>",
        "routes": [{"method": method, "path": path, "summary": summary} for method, path, summary in ROUTE_INDEX],
        "mcp": {"url": MCP_URL, "transport": "streamable-http", "protocol_versions": ["2025-06-18", "2024-11-05"]},
        "content_types": {"invocations": ["application/json", STREAM_MEDIA_TYPE]},
        "error_envelope": {"error": {"code": "string", "message": "string", "details": {}, "request_id": "req_…", "actions": []}},
    }


@router.get("/whoami", summary="Agent identity, scopes and limits")
def whoami(principal: AgentPrincipal = Depends(require_agent)) -> dict[str, Any]:
    """The installer's connectivity check. Never carries the token or its hash."""
    descriptors = granted_descriptors(principal)
    counters = usage.current_counters(principal.agent_id or None) if principal.agent_id else {}
    return {
        "service": "skill-router",
        "api_version": API_VERSION,
        "agent": {"id": principal.agent_id or None, "name": principal.agent_name},
        "scopes": list(principal.scopes),
        "limits": {
            "rate_limit_per_min": principal.rate_limit_per_min,
            "max_concurrency": principal.max_concurrency,
            "budget_usd": principal.budget_usd,
        },
        "counters": counters,
        "capabilities_count": len(descriptors),
        "capabilities_url": "/api/v1/capabilities",
        "mcp_url": MCP_URL,
        "server_time": db.iso(),
    }


@router.get("/capabilities", summary="Granted published capabilities")
def list_capabilities(
    request: Request,
    refresh: bool = Query(default=False, description="Recompute and skip If-None-Match."),
    principal: AgentPrincipal = Depends(require_agent),
) -> Response:
    descriptors = granted_descriptors(principal)
    version_token = descriptors_version_token(descriptors)
    etag = f'"{version_token}"'
    headers = {
        "etag": etag,
        "cache-control": "no-store" if refresh else "private, max-age=0, must-revalidate",
        "vary": "authorization",
    }
    if not refresh and _matches_etag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    body = {
        "capabilities": descriptors,
        "count": len(descriptors),
        "version_token": version_token,
        "generated_at": db.iso(),
        "refresh_url": "/api/v1/capabilities?refresh=1",
        "notes": [
            "Refresh when version_token changes; a breaking change ships as a new version.",
            "Only published capabilities granted to this token are listed.",
        ],
    }
    if not descriptors:
        # An empty list is ambiguous to the caller: "the platform has nothing published" and
        # "everything published is outside my scope" look identical. An agent cannot act on
        # a guess and will burn turns probing, so say what the two cases are and what to do
        # — without disclosing which one applies (that would let a token enumerate the
        # platform's catalogue by guessing slugs).
        body["empty_hint"] = (
            "Nothing is available to this token. Either no capability is published, or the published ones are "
            "outside this token's scopes. Ask the administrator to publish a tested capability and include its "
            "slug in this token's scope (Access → Tokens), then call this endpoint with ?refresh=1."
        )
        body["notes"] = [
            *body["notes"],
            "If a capability was just published for you, re-request with ?refresh=1 to bypass the cached ETag.",
        ]
    return JSONResponse(body, headers=headers)


@router.get("/capabilities/{slug}", summary="Full descriptor for one capability")
def get_capability(slug: str, principal: AgentPrincipal = Depends(require_agent)) -> dict[str, Any]:
    descriptor = _granted_descriptor(principal, slug)
    descriptor["mcp_tool"] = slug.replace(".", "__")
    descriptor["invoke_url"] = "/api/v1/invocations"
    return descriptor


@router.post(
    "/invocations",
    summary="Call a capability (JSON or SSE)",
    responses={
        200: {
            "description": "Invocation result as JSON, or an SSE stream when `Accept: text/event-stream` is sent.",
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": "event: start\ndata: {...}\n\nevent: delta\ndata: {...}\n\nevent: usage\ndata: {...}\n\nevent: done\ndata: {...}\n\n",
                },
            },
        }
    },
)
async def create_invocation(
    payload: InvocationRequest,
    request: Request,
    principal: AgentPrincipal = Depends(require_agent),
) -> Response:
    mode = payload.options.mode
    if mode is not None and mode not in {"sync", "async", "stream"}:
        raise unprocessable(
            "mode_not_supported",
            f"'{mode}' is not a supported mode.",
            details={"supported": ["sync", "async", "stream"]},
            actions=["Use sync, async or stream."],
        )
    wants_stream = bool(payload.options.stream) or STREAM_MEDIA_TYPE in (request.headers.get("accept") or "")
    version_id = _resolve_version_id(payload.capability, payload.version)
    options = execution.InvokeOptions(
        mode="stream" if wants_stream else mode,
        idempotency_key=payload.options.idempotency_key,
        timeout_s=payload.options.timeout_s,
        version_id=version_id,
        surface="stream" if wants_stream else "http",
        actor_type="agent",
        actor_id=principal.agent_id or None,
    )
    if wants_stream:
        return StreamingResponse(
            _sse_events(principal, payload.capability, payload.input, options),
            media_type=STREAM_MEDIA_TYPE,
            headers={
                "cache-control": "no-cache, no-transform",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )
    result = await execution.invoke(principal, payload.capability, payload.input, options)
    body = _public_invocation(result.to_dict())
    body["request_id"] = body.get("request_id") or get_request_id()
    return JSONResponse(body)


@router.get("/invocations/{request_id}", summary="Replay one of your recorded calls")
def replay_invocation(request_id: str, principal: AgentPrincipal = Depends(require_agent)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM invocations WHERE request_id = ?", (request_id,))
    if not row or not _owns_agent_row(principal, row.get("agent_id")):
        raise not_found("Invocation")
    capability = db.query_one("SELECT slug, name FROM capabilities WHERE id = ?", (row.get("capability_id"),)) or {}
    provider = (
        db.query_one("SELECT id, name FROM providers WHERE id = ?", (row["provider_id"],))
        if row.get("provider_id")
        else None
    )
    response_doc = db.loads(row.get("response_preview"), {}) if row.get("response_preview") else {}
    if not isinstance(response_doc, dict):
        response_doc = {}
    error = None
    if row.get("error_code"):
        error = {"code": row["error_code"], "message": row.get("error_message"), "action": None}
    body: dict[str, Any] = {
        "request_id": row["request_id"],
        "invocation_id": row["id"],
        "status": row["status"],
        "mode": row["mode"],
        "surface": row["surface"],
        "capability": capability.get("slug"),
        "version": db.query_scalar("SELECT version FROM capability_versions WHERE id = ?", (row.get("version_id"),)),
        "task_id": row.get("task_id"),
        "result": response_doc.get("result"),
        "artifacts": _public_artifacts(response_doc.get("artifacts")),
        "usage": db.loads(row.get("usage"), {}),
        "cost": {
            "estimated_usd": row.get("cost_estimate_usd"),
            "actual_usd": row.get("cost_actual_usd"),
            "currency": "USD",
        },
        "provider": {"id": provider["id"], "name": provider["name"]} if provider else None,
        "route_reason": row.get("route_reason"),
        "error": error,
        "http_status": row.get("http_status"),
        "latency_ms": row.get("latency_ms"),
        "attempts": int(row.get("retries") or 0) + 1,
        "deduplicated": False,
        "replay": True,
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
    }
    if body["task_id"]:
        body["poll"] = f"/api/v1/tasks/{body['task_id']}"
    return body


@router.get("/tasks/{task_id}", summary="Async task status")
async def get_task(task_id: str, principal: AgentPrincipal = Depends(require_agent)) -> dict[str, Any]:
    row = task_service.get_row(task_id)  # raises not_found → 404 for anyone else too
    if not _owns_agent_row(principal, row.get("agent_id")):
        raise not_found("Task")
    detail = task_service.get_task_row(task_id)
    body = task_service.public_task(detail, include_result=True)
    body["poll"] = f"/api/v1/tasks/{task_id}"
    body["cancel_url"] = f"/api/v1/tasks/{task_id}/cancel"
    return body


@router.post("/tasks/{task_id}/cancel", summary="Truthful cancel result")
async def cancel_task(
    task_id: str,
    payload: CancelRequest | None = None,
    principal: AgentPrincipal = Depends(require_agent),
) -> dict[str, Any]:
    row = task_service.get_row(task_id)
    if not _owns_agent_row(principal, row.get("agent_id")):
        raise not_found("Task")
    reason = (payload.reason if payload else None) or "Cancelled by the calling agent."
    outcome = await task_service.cancel_task(task_id, actor=_actor(principal), reason=reason)
    return {
        "task_id": outcome.get("task_id", task_id),
        "status": outcome.get("status"),
        "supported": bool(outcome.get("supported")),
        "canceled": bool(outcome.get("canceled")),
        "cancel_requested": bool(outcome.get("cancel_requested")),
        "upstream_may_continue": bool(outcome.get("upstream_may_continue")),
        "message": outcome.get("message"),
        "task": outcome.get("task"),
        "poll": f"/api/v1/tasks/{task_id}",
    }


@router.get("/files/{file_id}", summary="Download an artifact your calls produced")
def download_file(file_id: str, principal: AgentPrincipal = Depends(require_agent)) -> Response:
    row = file_service.get_file(file_id)  # raises 404 when missing on disk/record
    if not _can_read_file(principal, row):
        # Same answer as a missing file: ownership is not disclosed.
        raise not_found("File")
    return FileResponse(
        row["storage_path"],
        media_type=row.get("content_type") or "application/octet-stream",
        headers={"content-disposition": file_service.content_disposition(row["filename"])},
    )


@router.get("/skill", summary="Installable connection SKILL for this token")
def get_skill(request: Request, principal: AgentPrincipal = Depends(require_agent)) -> dict[str, Any]:
    """The secret-free connection SKILL. `content` is the markdown document."""
    # The agent reached us on this origin, so that is the URL the SKILL must
    # document (behind a proxy it is the proxy, not the configured default).
    base_url = str(request.base_url).rstrip("/")
    bundle = export.build_external_skill_bundle(principal, base_url=base_url)
    markdown = export.bundle_as_markdown(bundle)
    document = bundle.pop("content", "")
    body = dict(bundle)
    body["format"] = "skill"
    body["content_type"] = "text/markdown"
    body["content"] = markdown
    body["sha256"] = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    body["document"] = document  # the JSON variant of the same bundle
    body["includes_credentials"] = False
    return body


@router.get("/openapi.json", summary="OpenAPI 3.1 document for this surface")
def openapi_document() -> dict[str, Any]:
    return _openapi_document()


# ------------------------------------------------------------------ streaming --
def _sse_event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _usage_event(invocation_id: str, done: dict[str, Any], request_id: str) -> dict[str, Any]:
    row = (
        db.query_one(
            "SELECT usage, cost_estimate_usd, cost_actual_usd, latency_ms, status FROM invocations WHERE id = ?",
            (invocation_id,),
        )
        if invocation_id
        else None
    ) or {}
    return {
        "request_id": request_id or None,
        "invocation_id": invocation_id or None,
        "status": row.get("status") or done.get("status"),
        "usage": db.loads(row.get("usage"), {}),
        "cost": {
            "estimated_usd": row.get("cost_estimate_usd"),
            "actual_usd": row.get("cost_actual_usd"),
            "currency": "USD",
        },
        "latency_ms": row.get("latency_ms") if row.get("latency_ms") is not None else done.get("latency_ms"),
    }


async def _sse_events(
    principal: AgentPrincipal,
    capability: str,
    payload: dict[str, Any],
    options: execution.InvokeOptions,
) -> AsyncIterator[str]:
    """Relay `execution.stream_invocation` as SSE, with 15s keepalive comments.

    Events: `start`, `delta`, `usage` (once, before `done`), `done`, `error`.
    """
    agen = execution.stream_invocation(principal, capability, payload, options)
    pending: asyncio.Future[Any] | None = None
    request_id = ""
    invocation_id = ""
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=SSE_KEEPALIVE_SECONDS)
            if not done:
                yield ": keepalive\n\n"
                continue
            finished, pending = pending, None
            try:
                event = finished.result()
            except StopAsyncIteration:
                break
            except ApiError as exc:
                yield _sse_event(
                    "error",
                    {
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                            "actions": exc.actions,
                            "request_id": get_request_id(),
                        }
                    },
                )
                break
            except Exception as exc:  # noqa: BLE001 - a stream must always terminate cleanly
                yield _sse_event(
                    "error",
                    {"error": {"code": "execution_failed", "message": str(exc), "request_id": get_request_id()}},
                )
                break
            name = str(event.get("event") or "delta")
            body = {key: value for key, value in event.items() if key != "event"}
            if name == "start":
                request_id = str(body.get("request_id") or request_id)
                invocation_id = str(body.get("invocation_id") or invocation_id)
                body["request_id"] = request_id
            if name == "done":
                body["request_id"] = request_id or body.get("request_id")
                yield _sse_event("usage", _usage_event(invocation_id, body, request_id))
            yield _sse_event(name, body)
            if name in {"done", "error"}:
                break
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(BaseException):
                await pending
        with suppress(BaseException):
            await agen.aclose()


# ------------------------------------------------------------------- openapi ---
EXTERNAL_API_DESCRIPTION = """\
Bearer-token API for external agents and the `skill-router-connect` installer.

* Authenticate with `Authorization: Bearer <agent token>` (`sr_live_…`).
* Only capabilities that are **published** and **granted to the token** appear.
* Poll `/api/v1/capabilities` and refresh when `version_token` changes; a
  breaking change ships as a new capability version with a note in the
  descriptor.
* Errors always use the envelope
  `{"error": {"code", "message", "details", "request_id", "actions"}}`.
* `POST /api/v1/invocations` answers JSON, or `text/event-stream` when
  `Accept: text/event-stream` (or `options.stream: true`) is used.
"""


def _example_document() -> dict[str, Any]:
    return {
        "request_id": "req_9f2c1b6a8d3f4e10",
        "invocation_id": "inv_5d1c0a7f0b2e4c33",
        "status": "succeeded",
        "mode": "sync",
        "capability": "text.summarize",
        "version": 2,
        "task_id": None,
        "result": {"summary": "Skill Router routes capability calls across providers."},
        "artifacts": [],
        "usage": {"input_tokens": 128, "output_tokens": 42, "source": "provider"},
        "cost": {"estimated_usd": 0.00021, "actual_usd": None, "currency": "USD"},
        "provider": {"id": "prov_1", "name": "Acme AI"},
        "route_reason": "chose Acme AI via route route_1 (strategy=priority, health=healthy, priority=10); 0 fallback(s) available",
        "error": None,
        "http_status": 200,
        "latency_ms": 840,
        "attempts": 1,
        "deduplicated": False,
    }


_REQUESTS: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/v1/invocations", "post"): {
        "capability": "text.summarize",
        "version": 2,
        "input": {"prompt": "Summarise this contract in two sentences.", "max_words": 60},
        "options": {"mode": "sync", "idempotency_key": "summary-run-42", "timeout_s": 30, "stream": False},
    },
    ("/api/v1/tasks/{task_id}/cancel", "post"): {"reason": "User changed the brief."},
}

_RESPONSES: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/v1/whoami", "get"): {
        "service": "skill-router",
        "api_version": "1",
        "agent": {"id": "agent_2b7c", "name": "research-bot"},
        "scopes": ["text.summarize", "image.generate"],
        "limits": {"rate_limit_per_min": 60, "max_concurrency": 4, "budget_usd": 25.0},
        "counters": {"agent_id": "agent_2b7c", "calls_last_minute": 3, "concurrency": 1},
        "capabilities_count": 2,
        "server_time": "2026-10-08T09:14:05.123456Z",
    },
    ("/api/v1/capabilities", "get"): {
        "capabilities": [
            {
                "slug": "text.summarize",
                "name": "Summarize",
                "type": "text",
                "description": "Condense a document into a short summary.",
                "versions": {"current": 2, "available": [1, 2]},
                "modes": {"sync": True, "async": False, "stream": True},
                "input_schema": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}, "max_words": {"type": "integer"}},
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
                "limits": {"timeout_s": 60, "max_input_bytes": 262144, "max_output_bytes": 8388608},
                "examples": [{"input": {"prompt": "…", "max_words": 60}, "note": "Short summary."}],
                "needs_live_agent": False,
                "updated_at": "2026-10-07T18:22:41.000000Z",
                "version_token": "b0f7…",
            }
        ],
        "count": 1,
        "version_token": "3c9a…",
        "generated_at": "2026-10-08T09:14:05.123456Z",
    },
    ("/api/v1/invocations", "post"): _example_document(),
    ("/api/v1/invocations/{request_id}", "get"): {**_example_document(), "replay": True},
    ("/api/v1/tasks/{task_id}", "get"): {
        "id": "task_7a1f",
        "request_id": "req_9f2c1b6a8d3f4e10",
        "kind": "capability_async",
        "status": "running",
        "progress": 40,
        "progress_note": "Rendering frame 96/240.",
        "upstream_task_id": "job_8831",
        "attempts": 1,
        "capability": {"id": "cap_12", "slug": "video.render", "name": "Render video"},
        "provider": {"id": "prov_3", "name": "Acme Video"},
        "result_summary": "",
        "artifacts": [],
        "error": None,
        "poll": "/api/v1/tasks/task_7a1f",
        "cancel_url": "/api/v1/tasks/task_7a1f/cancel",
    },
    ("/api/v1/tasks/{task_id}/cancel", "post"): {
        "task_id": "task_7a1f",
        "status": "cancel_requested",
        "supported": True,
        "canceled": False,
        "cancel_requested": True,
        "upstream_may_continue": True,
        "message": "The provider accepted the cancellation request; the job is still running upstream.",
    },
    ("/api/v1/skill", "get"): {
        "schema": "skill-router.connection/v1",
        "generated_at": "2026-10-08T09:14:05.123456Z",
        "agent": {"id": "agent_2b7c", "name": "research-bot"},
        "platform": {
            "base_url": "https://router.example.com",
            "api_base": "/api/v1",
            "mcp_url": "/api/mcp",
            "openapi_url": "/api/v1/openapi.json",
        },
        "version_token": "3c9a…",
        "content_type": "text/markdown",
        "content": "---\nname: skill-router-research-bot\n---\n…",
        "sha256": "9f1d…",
        "includes_credentials": False,
    },
}

_ERROR_EXAMPLE = {
    "error": {
        "code": "rate_limited",
        "message": "This token is limited to 60 calls per minute.",
        "details": {"limit": 60, "used": 60, "window": "1m"},
        "request_id": "req_9f2c1b6a8d3f4e10",
        "actions": ["Retry after a short wait.", "Ask the administrator to raise the limit."],
    }
}


def _apply_examples(document: dict[str, Any]) -> None:
    paths = document.get("paths") or {}
    for (path, method), example in _REQUESTS.items():
        operation = (paths.get(path) or {}).get(method)
        if not isinstance(operation, dict):
            continue
        body = operation.setdefault("requestBody", {"required": True, "content": {}})
        content = body.setdefault("content", {})
        media = content.setdefault("application/json", {"schema": {"type": "object"}})
        media["example"] = example
    for (path, method), example in _RESPONSES.items():
        operation = (paths.get(path) or {}).get(method)
        if not isinstance(operation, dict):
            continue
        response = (operation.get("responses") or {}).get("200") or (operation.get("responses") or {}).get("201")
        if not isinstance(response, dict):
            continue
        content = response.setdefault("content", {})
        media = content.setdefault("application/json", {"schema": {"type": "object"}})
        media["example"] = example
    for path, method in _RESPONSES:
        operation = (paths.get(path) or {}).get(method)
        if not isinstance(operation, dict):
            continue
        responses = operation.setdefault("responses", {})
        error_response = responses.setdefault(
            "429",
            {"description": "Rate, concurrency or budget limit reached (any documented error code)."},
        )
        error_response.setdefault("content", {}).setdefault(
            "application/json", {"schema": {"type": "object"}}
        )["example"] = _ERROR_EXAMPLE


@lru_cache(maxsize=1)
def _openapi_document() -> dict[str, Any]:
    """OpenAPI 3.1 built from a dedicated app that mounts *only* the v1 routes."""
    from fastapi import FastAPI

    sub_app = FastAPI(
        title="Skill Router — external API",
        version=API_VERSION,
        description=EXTERNAL_API_DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    sub_app.include_router(router)
    document: dict[str, Any] = json.loads(json.dumps(sub_app.openapi(), default=str))
    document["openapi"] = "3.1.0"
    document["info"] = {
        "title": "Skill Router — external API",
        "version": API_VERSION,
        "description": EXTERNAL_API_DESCRIPTION,
        "contact": {"name": "Skill Router", "url": "https://github.com/xdrshjr/AragonSkillRouter"},
    }
    document["servers"] = [{"url": "/", "description": "Same origin as the platform"}]
    components = document.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "sr_live_… agent token",
        "description": (
            "Agent access token issued in Access → Tokens. Only capabilities granted to the "
            "token are visible; the token itself is never echoed back."
        ),
    }
    for path_item in (document.get("paths") or {}).values():
        for method, operation in list(path_item.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if isinstance(operation, dict):
                operation["security"] = [{"bearerAuth": []}]
    _apply_examples(document)
    tags = {
        "external": {
            "name": "external",
            "description": "Bearer-token surface for outside agents (`/api/v1`).",
        }
    }
    document["tags"] = [tags["external"]]
    return document
