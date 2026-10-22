"""The execution engine.

Answers "call this published capability with this input" for every surface:
the admin test panel, `/api/v1`, MCP and the worker's async polling. It owns:

  * input validation (declared fields only; unknown fields are an explicit error)
  * route selection and *bounded* failover
  * credential injection at send time (never earlier)
  * idempotency, request fingerprints and deduplicated replay
  * error mapping into stable codes with an action
  * usage/cost extraction, artifact capture, invocation records

Published capabilities never depend on the agent engine: this module has no
import of the AragonAgent adapter, so an agent crash or upgrade cannot stop a
plain API call.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
import jsonschema

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.deps import AgentPrincipal
from app.security.redact import Redactor, preview
from app.services import credentials as credential_service
from app.services import files as file_service
from app.services import routing, usage
from app.services.capability_config import (
    check_unknown_inputs,
    jsonpath_get,
    merge_defaults,
    render,
)
from app.services.network import GuardedResponse, NetworkPolicyError, classify_network_error, guarded_request, validate_url

SAFE_FAILOVER_CODES = {
    "connect_failed",
    "connect_timeout",
    "rate_limited",
    "upstream_auth_failed",
    "upstream_5xx",
    "network_blocked",
    "private_target_blocked",
    "dns_failed",
    "pool_timeout",
    "unsupported_scheme",
}

DEFAULT_ERRORS: dict[int, tuple[str, str, str]] = {
    400: ("upstream_bad_request", "The provider rejected the request as invalid.", "Check the input against the provider's documentation."),
    401: ("upstream_auth_failed", "The provider rejected the credential.", "Verify the credential in Providers → Credentials."),
    403: ("upstream_forbidden", "The credential is valid but not allowed to use this model or endpoint.", "Check the provider account's permissions and region."),
    404: ("upstream_not_found", "The provider endpoint or model does not exist.", "Check the request URL and model name."),
    413: ("upstream_payload_too_large", "The provider refused the request as too large.", "Reduce the input size or file size."),
    422: ("upstream_unprocessable", "The provider could not process the input.", "Inspect the provider's message and adjust the input."),
    429: ("rate_limited", "The provider is rate limiting this credential.", "Wait and retry, or raise the provider quota."),
}


@dataclass
class InvokeOptions:
    mode: str | None = None
    idempotency_key: str | None = None
    timeout_s: float | None = None
    provider_id: str | None = None
    surface: str = "http"
    allow_draft: bool = False
    version_id: str | None = None
    dry_run: bool = False
    max_retries: int | None = None
    save_bodies: bool | None = None
    actor_type: str = "agent"
    actor_id: str | None = None


@dataclass
class InvokeResult:
    request_id: str
    invocation_id: str
    status: str
    mode: str
    capability: str
    version: int | None = None
    task_id: str | None = None
    result: Any = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] | None = None
    route_reason: str = ""
    error: dict[str, Any] | None = None
    http_status: int | None = None
    latency_ms: int = 0
    attempts: int = 0
    deduplicated: bool = False
    schema_valid: bool | None = None
    raw_request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "invocation_id": self.invocation_id,
            "status": self.status,
            "mode": self.mode,
            "capability": self.capability,
            "version": self.version,
            "task_id": self.task_id,
            "result": self.result,
            "artifacts": self.artifacts,
            "usage": self.usage,
            "cost": self.cost,
            "provider": self.provider,
            "route_reason": self.route_reason,
            "error": self.error,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "deduplicated": self.deduplicated,
            "schema_valid": self.schema_valid,
        }
        return payload


# ------------------------------------------------------------------ lookup ---
def get_capability(slug_or_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT c.*, p.name AS provider_name, p.category AS provider_category
             FROM capabilities c LEFT JOIN providers p ON p.id = c.provider_id
            WHERE c.slug = ? OR c.id = ?""",
        (slug_or_id, slug_or_id),
    )
    if not row:
        raise not_found("Capability")
    return row


def get_version(capability: dict[str, Any], *, version_id: str | None = None, published_only: bool = True) -> dict[str, Any]:
    if version_id:
        row = db.query_one("SELECT * FROM capability_versions WHERE id = ? AND capability_id = ?", (version_id, capability["id"]))
        if not row:
            raise not_found("Capability version")
        return row
    if capability.get("current_version_id"):
        row = db.query_one("SELECT * FROM capability_versions WHERE id = ?", (capability["current_version_id"],))
        if row and (not published_only or row["status"] == "published"):
            return row
    if published_only:
        row = db.query_one(
            "SELECT * FROM capability_versions WHERE capability_id = ? AND status = 'published' ORDER BY version DESC LIMIT 1",
            (capability["id"],),
        )
    else:
        row = db.query_one(
            "SELECT * FROM capability_versions WHERE capability_id = ? ORDER BY version DESC LIMIT 1",
            (capability["id"],),
        )
    if not row:
        raise unprocessable(
            "no_version",
            "This capability has no version yet.",
            actions=["Create a draft version and publish it after a successful test."],
        )
    return row


def _config(version: dict[str, Any], capability_type: str) -> dict[str, Any]:
    raw = version.get("config")
    parsed = raw if isinstance(raw, dict) else db.loads(raw, {})
    return merge_defaults(parsed, capability_type)


# ------------------------------------------------------------------ input ----
def validate_input(config: dict[str, Any], payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise unprocessable("invalid_input", "The input must be a JSON object.")
    schema = config.get("input_schema") or {"type": "object"}
    unknown = check_unknown_inputs(payload, config)
    if unknown:
        declared = sorted((schema.get("properties") or {}).keys())
        raise unprocessable(
            "unsupported_parameter",
            f"Unsupported input parameter(s): {', '.join(unknown)}.",
            details={"unsupported": unknown, "declared": declared, "passthrough": config.get("provider_params") or []},
            actions=["Remove the parameter, or declare it in the input schema / provider passthrough list."],
        )
    try:
        jsonschema.validate(instance=payload, schema=schema, format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "input"
        raise unprocessable(
            "input_schema_mismatch",
            f"{location}: {exc.message}",
            details={"field": location, "schema_path": ".".join(str(p) for p in exc.absolute_schema_path)},
            actions=["Fix the field and retry.", "See the capability's input schema."],
        ) from exc
    limit = (config.get("limits") or {}).get("max_input_bytes")
    if limit:
        size = len(json.dumps(payload).encode("utf-8"))
        if size > limit:
            raise unprocessable(
                "input_too_large",
                f"The input is {size} bytes; this capability allows {limit}.",
                actions=["Shorten the input or raise the limit on the version."],
            )
    return payload


# --------------------------------------------------------------- prepared ---
@dataclass
class PreparedCall:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any]
    json_body: Any = None
    data: Any = None
    files: Any = None
    content: bytes | None = None
    redactor: Redactor = field(default_factory=Redactor)
    context: dict[str, Any] = field(default_factory=dict)


def _credential_secrets(credential_id: str | None, config: dict[str, Any]) -> tuple[dict[str, Any] | None, credential_service.CredentialSecrets | None]:
    if not credential_id or (config.get("auth") or {}).get("mode") == "none":
        return None, None
    row = credential_service.get_row(credential_id)
    secrets = credential_service.get_secret_map(credential_id)
    return row, secrets


def build_call(
    *,
    candidate: routing.RouteCandidate,
    payload: dict[str, Any],
    mode: str,
    timeout_s: float | None = None,
) -> PreparedCall:
    config = candidate.config
    request = config.get("request") or {}
    credential_row, secrets = _credential_secrets(
        (candidate.credential or {}).get("id") or (candidate.route.get("credential_id")), config
    )
    ctx = {
        "input": payload,
        "model": config.get("model"),
        "mode": mode,
        "task_id": payload.get("task_id"),
    }
    url = str(render(request.get("url") or "", ctx)).strip()
    if not url:
        raise unprocessable("missing_url", "This capability version has no request URL.")
    if not url.lower().startswith(("http://", "https://")):
        raise unprocessable("invalid_url", "The request URL must start with http:// or https://.")

    query: dict[str, Any] = {}
    for item in request.get("query") or []:
        if not isinstance(item, dict):
            continue
        when = item.get("when_mode")
        if when and when != mode:
            continue
        value = render(item.get("value", item.get("default", "")), ctx)
        if value in (None, "") and item.get("required"):
            raise unprocessable(
                "missing_query_parameter",
                f"Query parameter '{item.get('name')}' is required for this mode.",
            )
        if value not in (None, ""):
            query[str(item.get("name"))] = value

    headers: dict[str, str] = {}
    for name, value in (request.get("headers") or {}).items():
        rendered = render(value, ctx)
        if rendered is None:
            continue
        headers[str(name)] = str(rendered)
    if secrets and credential_row:
        auth_headers = credential_service.build_auth_headers(credential_row, secrets)
        headers.update(auth_headers)
    headers.setdefault("accept", "application/json" if (config.get("response") or {}).get("kind", "json") == "json" else "*/*")

    encoding = request.get("encoding", "json")
    json_body = None
    data = None
    files = None
    content = None

    file_field = (request.get("multipart_file_field") or "").strip()
    file_ref = None
    if file_field and file_field in payload:
        file_ref = payload.get(file_field)

    if encoding == "json" and request.get("body_template") is not None:
        json_body = render(request["body_template"], ctx)
        if isinstance(json_body, dict) and not json_body:
            json_body = None
    elif encoding == "form":
        form = render(request.get("form_fields") or {}, ctx)
        data = {str(k): v for k, v in form.items() if v is not None}
    elif encoding == "multipart":
        form = render(request.get("form_fields") or {}, ctx)
        data = {str(k): str(v) for k, v in form.items() if v is not None}
        if file_ref:
            meta, raw = file_service.read_bytes(str(file_ref))
            files = {file_field: (meta["filename"], raw, meta["content_type"])}
    elif encoding == "none":
        if request.get("body_template") is not None:
            rendered = render(request["body_template"], ctx)
            content = rendered.encode("utf-8") if isinstance(rendered, str) else json.dumps(rendered).encode("utf-8")

    redactor = secrets.redactor() if secrets else Redactor()
    prepared = PreparedCall(
        method=str(request.get("method") or "POST").upper(),
        url=url,
        headers=headers,
        params=query,
        json_body=json_body,
        data=data,
        files=files,
        content=content,
        redactor=redactor,
        context=ctx,
    )
    if timeout_s:
        prepared.context["timeout_override"] = timeout_s
    return prepared


def request_preview(prepared: PreparedCall, *, include_body: bool = False) -> dict[str, Any]:
    body: Any = prepared.json_body if prepared.json_body is not None else prepared.data
    preview_obj = {
        "method": prepared.method,
        "url": prepared.redactor.text(prepared.url),
        "headers": {k: ("[REDACTED]" if k.lower() in {"authorization", "x-api-key", "api-key", "cookie"} else v) for k, v in prepared.headers.items()},
        "query": {k: prepared.redactor.text(str(v)) for k, v in prepared.params.items()},
        "body": preview(body, limit=1500, secrets=None) if include_body else ("[omitted by policy]" if body is not None else None),
        "files": list(prepared.files.keys()) if prepared.files else None,
    }
    return prepared.redactor.value(preview_obj)


# ----------------------------------------------------------------- errors ---
def _no_route_error(selection: "routing.RouteSelection", *, streaming: bool = False) -> ApiError:
    """The error for "nothing could serve this call", with the most useful code.

    Defect fixed: a disabled/expired credential used to surface as a generic
    `no_compatible_route` (503) even when it was the *only* reason nothing could
    serve the call, hiding the one action the caller can actually take. The status
    vocabulary already has `credential_inactive`, so report that instead and keep
    the full rejection list in `details`.
    """
    rejected = selection.rejected or []
    reasons = [str(item.get("reason") or "") for item in rejected if isinstance(item, dict)]
    if rejected and all("credential is not active" in reason for reason in reasons):
        return ApiError(
            "credential_inactive",
            "The credential behind every route for this capability is disabled or expired.",
            status_code=409,
            details=routing.explain_selection(selection),
            actions=[
                "Enable the credential in Providers → Credentials.",
                "Or point the route at another active credential.",
            ],
        )
    return ApiError(
        "no_compatible_route",
        "No compatible streaming route." if streaming else "No compatible provider route can serve this request.",
        status_code=503,
        details=routing.explain_selection(selection),
        actions=["Add or enable a route for this capability.", "Check credential status and version compatibility."],
    )


def map_upstream_error(
    *,
    status_code: int | None,
    body: Any,
    config: dict[str, Any],
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    errors_cfg = config.get("errors") or {}
    for rule in errors_cfg.get("rules") or []:
        match = rule.get("match") or {}
        matched = True
        if "http_status" in match and status_code not in (match["http_status"] or []):
            matched = False
        if matched and match.get("body_path"):
            candidate = jsonpath_get(body, match["body_path"])
            if "equals" in match and candidate != match["equals"]:
                matched = False
            if "contains" in match and str(match["contains"]).lower() not in str(candidate).lower():
                matched = False
        if matched:
            return {
                "code": rule.get("code", "upstream_error"),
                "message": (redactor.text(rule.get("message", "")) if redactor else rule.get("message", "")) or "The provider returned an error.",
                "action": rule.get("action"),
                "upstream_status": status_code,
                "retryable": bool(rule.get("retryable", False)),
            }
    if status_code in DEFAULT_ERRORS:
        code, message, action = DEFAULT_ERRORS[status_code]
        return {"code": code, "message": message, "action": action, "upstream_status": status_code, "retryable": code == "rate_limited"}
    if status_code and status_code >= 500:
        return {
            "code": "upstream_5xx",
            "message": "The provider failed to process the request.",
            "action": "Retry later; if it persists, check the provider's status page.",
            "upstream_status": status_code,
            "retryable": True,
        }
    return {
        "code": errors_cfg.get("default_code", "upstream_error"),
        "message": "The provider returned an unexpected response.",
        "action": "Inspect the invocation record and the provider documentation.",
        "upstream_status": status_code,
        "retryable": False,
    }


def _body_excerpt(response: GuardedResponse, redactor: Redactor) -> str:
    try:
        text = response.text()
    except Exception:  # noqa: BLE001
        return ""
    return preview(redactor.text(text), limit=1200)


# ---------------------------------------------------------------- extract ---
def _wrap_pcm_wav(raw: bytes, mime: str, sample_rate: int) -> bytes:
    """Wrap headerless PCM in a minimal 44-byte WAV container."""
    import struct

    channels = 1
    bit_depth = 16
    match = None
    if mime:
        import re as _re

        match = _re.search(r"rate=(\d+)", mime)
    if match:
        sample_rate = int(match.group(1))
    if mime and "stereo" in mime:
        channels = 2
    byte_rate = sample_rate * channels * bit_depth // 8
    block_align = channels * bit_depth // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bit_depth)
    header += b"data" + struct.pack("<I", len(raw))
    return header + raw


def _decode_artifact(
    config: dict[str, Any],
    body: Any,
    *,
    invocation_id: str,
    task_id: str | None,
    redactor: Redactor,
) -> tuple[list[dict[str, Any]], Any]:
    """Capture inline artifacts (base64/url) and return them with a slimmed result."""
    artifact_cfg = (config.get("response") or {}).get("artifact")
    if not artifact_cfg:
        return [], None
    path = artifact_cfg.get("path")
    value = jsonpath_get(body, path) if path else None
    if value is None:
        return [], None
    kind = artifact_cfg.get("kind", "file")
    mime = artifact_cfg.get("mime") or jsonpath_get(body, artifact_cfg.get("mime_path")) or "application/octet-stream"
    filename = artifact_cfg.get("filename") or f"{kind}-{invocation_id[:8]}"
    encoding = artifact_cfg.get("encoding", "base64")
    try:
        if encoding == "base64":
            if isinstance(value, dict):
                mime = value.get("mimeType") or value.get("mime_type") or value.get("content_type") or mime
                raw = base64.b64decode(str(value.get("data") or value.get("b64_json") or ""), validate=False)
            else:
                raw = base64.b64decode(str(value), validate=False)
        elif encoding == "url":
            return [], {"pending_url": str(value), "kind": kind, "mime": mime, "filename": filename}
        else:
            raw = str(value).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ApiError(
            "artifact_decode_failed",
            "The provider returned artifact data that could not be decoded.",
            status_code=502,
            details={"path": path, "encoding": encoding},
        ) from exc
    # Some providers return headerless PCM (Gemini TTS returns audio/L16). A WAV
    # header makes the file playable and verifiable instead of a raw blob.
    if artifact_cfg.get("container") == "wav" and not raw[:4] == b"RIFF":
        raw = _wrap_pcm_wav(raw, mime, artifact_cfg.get("sample_rate") or 24000)
        mime = "audio/wav"
        filename = filename.rsplit(".", 1)[0] + ".wav"
    if "." not in filename:
        ext = {"image": ".png", "audio": ".mp3", "video": ".mp4"}.get(kind, "")
        filename += ext
    saved = file_service.save_bytes(
        raw,
        filename=filename,
        content_type=mime,
        purpose="artifact",
        task_id=task_id,
        invocation_id=invocation_id,
        owner_type="invocation",
        owner_id=invocation_id,
    )
    saved["kind"] = kind
    return [saved], None


def extract_result(
    *,
    config: dict[str, Any],
    body: Any,
    response: GuardedResponse | None,
    invocation_id: str,
    task_id: str | None,
    latency_ms: int,
    redactor: Redactor,
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], dict[str, Any], bool | None]:
    response_cfg = config.get("response") or {}
    kind = response_cfg.get("kind", "json")
    if kind == "text" and response is not None:
        result: Any = redactor.text(response.text())
    else:
        path = (response_cfg.get("extract") or {}).get("result")
        result = jsonpath_get(body, path) if path else body
        if result is None and path:
            raise ApiError(
                "result_path_empty",
                f"The configured result path {path!r} matched nothing in the provider response.",
                status_code=502,
                details={"path": path, "body_preview": preview(redactor.value(body), limit=800)},
                actions=["Open the capability version and correct response.extract.result."],
            )
        if isinstance(result, str):
            result = redactor.text(result)
        elif isinstance(result, (dict, list)):
            result = redactor.value(result)

    artifacts, pending = _decode_artifact(config, body, invocation_id=invocation_id, task_id=task_id, redactor=redactor)

    extract = response_cfg.get("extract") or {}
    usage: dict[str, Any] = {"source": "provider"}
    for key, target in (("usage_in", "input_tokens"), ("usage_out", "output_tokens"), ("usage_total", "total_tokens")):
        if extract.get(key):
            raw_value = jsonpath_get(body, extract[key])
            if isinstance(raw_value, (int, float)):
                usage[target] = raw_value
    if extract.get("usage_json"):
        extra = jsonpath_get(body, extract["usage_json"])
        if isinstance(extra, dict):
            usage["raw"] = redactor.value(extra)

    cost: dict[str, Any] = {"currency": (config.get("pricing") or {}).get("currency", "USD"), "source": "estimated"}
    pricing = config.get("pricing") or {}
    estimated = float(pricing.get("per_call") or 0)
    if usage.get("input_tokens"):
        estimated += float(usage["input_tokens"]) / 1000 * float(pricing.get("input_per_1k") or 0)
    if usage.get("output_tokens"):
        estimated += float(usage["output_tokens"]) / 1000 * float(pricing.get("output_per_1k") or 0)
    if pricing.get("per_second") and latency_ms:
        estimated += (latency_ms / 1000) * float(pricing["per_second"])
    cost["estimated_usd"] = round(estimated, 8)
    if extract.get("cost"):
        actual = jsonpath_get(body, extract["cost"])
        if isinstance(actual, (int, float)):
            cost["actual_usd"] = float(actual)
            cost["source"] = "provider"

    schema_valid: bool | None = None
    output_schema = config.get("output_schema") or {}
    if output_schema.get("properties") and isinstance(result, (dict, list)):
        try:
            jsonschema.validate(instance=result, schema=output_schema)
            schema_valid = True
        except jsonschema.ValidationError:
            schema_valid = False

    if pending:
        usage["pending_artifact"] = pending
    return result, artifacts, usage, cost, schema_valid


# ----------------------------------------------------------------- invoke ---
def _fingerprint(capability_slug: str, version: int | None, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"c": capability_slug, "v": version, "i": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_existing_invocation(principal: AgentPrincipal, idempotency_key: str) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    agent_id = principal.agent_id or None
    row = db.query_one(
        """SELECT * FROM invocations
            WHERE idempotency_key = ? AND COALESCE(agent_id,'') = COALESCE(?, '')
              AND status IN ('succeeded','running','queued','uncertain','failed')
              AND created_at >= ?
            ORDER BY created_at DESC LIMIT 1""",
        (idempotency_key, agent_id, db.iso(db.utcnow() - __import__("datetime").timedelta(hours=24))),
    )
    return row


def _existing_result(row: dict[str, Any], capability_slug: str) -> InvokeResult:
    response = db.loads(row.get("response_preview"), {}) if row.get("response_preview") else {}
    # Defect fixed: an async submit that is deduplicated *while still in flight*
    # had no task_id yet on the invocation row, so the duplicate answer carried
    # `task_id: null` and the caller had no way to poll the job it just created
    # (and would be tempted to submit it again). Resolve it from the task row.
    task_id = row.get("task_id") or db.query_scalar(
        "SELECT id FROM tasks WHERE request_id = ? ORDER BY created_at DESC LIMIT 1",
        (row["request_id"],),
    )
    return InvokeResult(
        request_id=row["request_id"],
        invocation_id=row["id"],
        status=row["status"],
        mode=row["mode"],
        capability=capability_slug,
        task_id=task_id,
        result=response.get("result") if isinstance(response, dict) else None,
        usage=db.loads(row.get("usage"), {}),
        cost={"estimated_usd": row.get("cost_estimate_usd"), "actual_usd": row.get("cost_actual_usd")},
        provider={"id": row.get("provider_id")},
        route_reason=row.get("route_reason") or "deduplicated replay",
        http_status=row.get("http_status"),
        latency_ms=row.get("latency_ms") or 0,
        deduplicated=True,
        error=db.loads(row["error_message"], None) if row.get("error_message") else None,
    )


def _create_invocation(
    *,
    capability: dict[str, Any],
    version: dict[str, Any],
    principal: AgentPrincipal,
    payload: dict[str, Any],
    mode: str,
    options: InvokeOptions,
    idempotency_key: str | None,
    fingerprint: str,
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = db.new_id("req")
    invocation_id = db.new_id("inv")
    while db.query_one("SELECT 1 FROM invocations WHERE request_id = ?", (request_id,)):
        request_id = db.new_id("req")
    now = db.iso()
    store_bodies = options.save_bodies
    if store_bodies is None:
        store_bodies = bool(db.get_setting("logging.store_request_bodies", False))
    db.insert(
        "invocations",
        {
            "id": invocation_id,
            "request_id": request_id,
            "caller_type": "agent" if principal.token_id else "user",
            "agent_id": principal.agent_id or None,
            "surface": options.surface,
            "capability_id": capability["id"],
            "version_id": version["id"],
            "provider_id": (version.get("provider_id") if isinstance(version, dict) else None) or capability.get("provider_id"),
            "mode": mode,
            "status": "running",
            "idempotency_key": idempotency_key,
            "request_digest": fingerprint,
            "request_preview": preview(payload, limit=1500) if store_bodies else "[request body not stored by policy]",
            "created_at": now,
        },
    )
    if gate is not None:
        db.execute(
            "UPDATE invocations SET route_reason = ? WHERE id = ?", (gate.get("reason", ""), invocation_id)
        )
    return db.query_one("SELECT * FROM invocations WHERE id = ?", (invocation_id,)) or {}


def _finish_invocation(invocation_id: str, **fields: Any) -> None:
    payload = {k: v for k, v in fields.items() if v is not None}
    payload.setdefault("finished_at", db.iso())
    if payload:
        db.update("invocations", invocation_id, payload)


async def _send(
    prepared: PreparedCall,
    *,
    config: dict[str, Any],
    timeout_s: float,
    max_attempts: int,
) -> tuple[GuardedResponse | None, dict[str, Any] | None, int]:
    """Send with bounded retries for provably-safe failures only."""
    retry_cfg = config.get("retry") or {}
    retry_on = set(retry_cfg.get("retry_on") or [])
    backoff = list(retry_cfg.get("backoff_ms") or [500, 1500])
    attempts = 0
    last_error: dict[str, Any] | None = None
    while attempts <= max_attempts:
        attempts += 1
        try:
            response = await guarded_request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                params=prepared.params,
                json_body=prepared.json_body,
                data=prepared.data,
                files=prepared.files,
                content=prepared.content,
                timeout_s=timeout_s,
            )
        except NetworkPolicyError as exc:
            last_error = {
                "code": exc.code,
                "message": prepared.redactor.text(str(exc)),
                "action": "Adjust the capability URL or the outbound allowlist.",
                "retryable": False,
            }
            if exc.code in retry_on and attempts <= max_attempts and exc.code in {"connect_timeout"}:
                await asyncio.sleep(backoff[min(attempts - 1, len(backoff) - 1)] / 1000)
                continue
            return None, last_error, attempts
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            code, message = classify_network_error(exc)
            last_error = {
                "code": code,
                "message": prepared.redactor.text(message),
                "action": "Retry; if it persists the provider may be unreachable from this network.",
                "retryable": True,
            }
            if attempts <= max_attempts and (code in retry_on or not retry_on):
                await asyncio.sleep(backoff[min(attempts - 1, len(backoff) - 1)] / 1000)
                continue
            return None, last_error, attempts
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            code, message = classify_network_error(exc)
            # The provider may have accepted the request: we must not resend.
            return None, {
                "code": "upstream_timeout_uncertain",
                "message": prepared.redactor.text(message),
                "action": "Check the provider's task list or the invocation record before retrying; the request may have been accepted and billed.",
                "retryable": False,
                "uncertain": True,
            }, attempts

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            if attempts <= max_attempts and ("rate_limited" in retry_on or not retry_on):
                delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else backoff[min(attempts - 1, len(backoff) - 1)] / 1000
                await asyncio.sleep(min(delay, 30))
                last_error = {"code": "rate_limited", "message": "Provider rate limit; retrying.", "retryable": True}
                continue
        if response.status_code >= 500 and attempts <= max_attempts and ("upstream_5xx" in retry_on):
            await asyncio.sleep(backoff[min(attempts - 1, len(backoff) - 1)] / 1000)
            last_error = {"code": "upstream_5xx", "message": "Provider error; retrying.", "retryable": True}
            continue
        return response, None, attempts
    return None, last_error or {"code": "retry_exhausted", "message": "Retries exhausted.", "retryable": False}, attempts


async def invoke(
    principal: AgentPrincipal,
    capability_ref: str,
    payload: dict[str, Any] | None,
    options: InvokeOptions | None = None,
) -> InvokeResult:
    options = options or InvokeOptions()
    capability = get_capability(capability_ref)
    if capability["status"] != "published" and not options.allow_draft:
        raise conflict(
            "capability_not_published",
            f"'{capability['slug']}' is not published yet, so it cannot be called.",
            details={"status": capability["status"]},
            actions=["Publish the capability after a successful test."],
        )
    if principal.agent_id and not principal.can_use(capability["slug"]):
        raise ApiError(
            "capability_not_granted",
            f"This token is not granted '{capability['slug']}'.",
            status_code=403,
            actions=["Ask the administrator to add the capability to the token's scope."],
        )
    version = get_version(
        capability,
        version_id=options.version_id,
        published_only=not options.allow_draft,
    )
    config = _config(version, capability.get("type", "generic"))
    modes = config.get("modes") or {}
    mode = options.mode or ("sync" if modes.get("sync") else "async" if modes.get("async") else "stream")
    if not modes.get(mode):
        raise unprocessable(
            "mode_not_supported",
            f"'{capability['slug']}' does not support {mode} calls.",
            details={"supported": [name for name in ("sync", "async", "stream") if modes.get(name)]},
        )
    payload = validate_input(config, payload or {})

    fingerprint = _fingerprint(capability["slug"], version["version"], payload)
    if options.idempotency_key:
        existing = _find_existing_invocation(principal, options.idempotency_key)
        if existing and existing["status"] in {"succeeded", "running", "queued", "uncertain"}:
            return _existing_result(existing, capability["slug"])

    selection = routing.candidates_for(
        capability=capability,
        version=version,
        input_payload=payload,
        mode=mode,
        preferred_provider_id=options.provider_id,
        allow_draft=options.allow_draft,
    )
    if not selection.candidates:
        raise _no_route_error(selection)
    usage.assert_within_limits(principal, capability["id"], (selection.chosen.provider or {}).get("id"))

    if options.dry_run:
        prepared = build_call(candidate=selection.chosen, payload=payload, mode=mode, timeout_s=options.timeout_s)
        return InvokeResult(
            request_id=db.new_id("req"),
            invocation_id="",
            status="preview",
            mode=mode,
            capability=capability["slug"],
            version=version["version"],
            provider={"id": (selection.chosen.provider or {}).get("id"), "name": (selection.chosen.provider or {}).get("name")},
            route_reason=selection.reason,
            raw_request=request_preview(prepared, include_body=True),
            result=None,
        )

    invocation = _create_invocation(
        capability=capability,
        version=version,
        principal=principal,
        payload=payload,
        mode=mode,
        options=options,
        idempotency_key=options.idempotency_key,
        fingerprint=fingerprint,
        gate={"reason": selection.reason},
    )
    invocation_id = invocation["id"]
    request_id = invocation["request_id"]
    timeout_s = float(options.timeout_s or (config.get("limits") or {}).get("timeout_s") or 60)
    max_attempts = options.max_retries if options.max_retries is not None else int((config.get("retry") or {}).get("max_attempts") or 0)
    max_attempts = max(0, min(max_attempts, 4))

    candidate = selection.chosen
    assert candidate is not None
    last_error: dict[str, Any] | None = None
    total_attempts = 0
    for index, candidate in enumerate(selection.candidates):
        try:
            prepared = build_call(candidate=candidate, payload=payload, mode=mode, timeout_s=options.timeout_s)
        except ApiError as exc:
            last_error = {"code": exc.code, "message": exc.message, "action": (exc.actions or [None])[0]}
            break
        except credential_service.crypto.CryptoError as exc:  # type: ignore[attr-defined]
            last_error = {"code": "credential_decrypt_failed", "message": str(exc), "action": "Check the master keyring."}
            break

        started = time.perf_counter()
        response, error, attempts = await _send(prepared, config=config, timeout_s=timeout_s, max_attempts=max_attempts)
        total_attempts += attempts
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response is None:
            last_error = error or {"code": "network_error", "message": "The request failed.", "retryable": False}
            if last_error.get("code") in {"upstream_timeout_uncertain"}:
                # Unknown outcome: record it and do not fail over.
                _finish_invocation(
                    invocation_id,
                    status="uncertain",
                    provider_id=(candidate.provider or {}).get("id"),
                    route_id=None if candidate.route_id == "implicit" else candidate.route_id,
                    route_reason=selection.reason,
                    latency_ms=latency_ms,
                    retries=max(0, total_attempts - 1),
                    error_code=last_error["code"],
                    error_message=last_error.get("message"),
                )
                return InvokeResult(
                    request_id=request_id,
                    invocation_id=invocation_id,
                    status="uncertain",
                    mode=mode,
                    capability=capability["slug"],
                    version=version["version"],
                    provider=candidate.provider,
                    route_reason=selection.reason,
                    error=last_error,
                    latency_ms=latency_ms,
                    attempts=total_attempts,
                )
            if last_error.get("code") in SAFE_FAILOVER_CODES and index < len(selection.candidates) - 1:
                routing.mark_failure(candidate.route_id)
                continue
            break

        if response.status_code >= 400:
            body = response.json() if response.content_type == "application/json" else None
            mapped = map_upstream_error(status_code=response.status_code, body=body, config=config, redactor=prepared.redactor)
            mapped["upstream_body"] = _body_excerpt(response, prepared.redactor)
            last_error = mapped
            if mapped.get("code") == "upstream_auth_failed":
                credential_id = (candidate.credential or {}).get("id")
                if credential_id:
                    db.execute(
                        "UPDATE credentials SET last_verify_status = 'invalid', last_verify_message = ?, last_verified_at = ? WHERE id = ?",
                        ("Provider rejected the credential during a call.", db.iso(), credential_id),
                    )
            if mapped.get("code") in SAFE_FAILOVER_CODES and index < len(selection.candidates) - 1:
                routing.mark_failure(candidate.route_id)
                continue
            break

        body = response.json() if response.content_type == "application/json" else None
        if response.content_type != "application/json" and (config.get("response") or {}).get("kind", "json") == "json":
            last_error = {
                "code": "unexpected_content_type",
                "message": f"The provider returned {response.content_type or 'an unknown content type'} where JSON was expected.",
                "action": "Set response.kind to 'text' or correct the endpoint.",
                "upstream_status": response.status_code,
            }
            break

        # ---- async submission: hand the upstream job to the durable worker --
        if mode == "async":
            async_cfg = config.get("async") or {}
            task_path = (async_cfg.get("submit_extract") or {}).get("task_id")
            upstream_task_id = jsonpath_get(body, task_path) if task_path else None
            if upstream_task_id in (None, ""):
                last_error = {
                    "code": "upstream_task_id_missing",
                    "message": f"The provider accepted the request but no task id was found at {task_path!r}.",
                    "action": "Correct async.submit_extract.task_id in this capability version.",
                    "upstream_status": response.status_code,
                    "upstream_body": _body_excerpt(response, prepared.redactor),
                }
                break
            provider = candidate.provider or {}
            task_id = db.new_id("task")
            now_iso = db.iso()
            poll_interval = float((async_cfg.get("status") or {}).get("poll_interval_s") or 5)
            deadline = db.iso(db.utcnow() + __import__("datetime").timedelta(
                seconds=float((config.get("limits") or {}).get("max_duration_s") or 3600)
            ))
            db.insert(
                "tasks",
                {
                    "id": task_id,
                    "request_id": request_id,
                    "kind": "capability_async",
                    "capability_id": capability["id"],
                    "version_id": version["id"],
                    "provider_id": provider.get("id"),
                    "route_id": None if candidate.route_id == "implicit" else candidate.route_id,
                    "agent_id": principal.agent_id or None,
                    "status": "queued",
                    "upstream_task_id": str(upstream_task_id),
                    "input": preview(payload, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
                    "idempotency_key": options.idempotency_key,
                    "fingerprint": fingerprint,
                    "attempts": 0,
                    "max_attempts": int((config.get("retry") or {}).get("max_attempts") or 3),
                    "poll_after": db.iso(db.utcnow() + __import__("datetime").timedelta(seconds=poll_interval)),
                    "deadline": deadline,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
            )
            usage_info = {"source": "provider"}
            _finish_invocation(
                invocation_id,
                status="queued",
                provider_id=provider.get("id"),
                route_id=None if candidate.route_id == "implicit" else candidate.route_id,
                route_reason=selection.reason,
                http_status=response.status_code,
                latency_ms=latency_ms,
                retries=max(0, total_attempts - 1),
                task_id=task_id,
                usage=db.dumps(usage_info),
                response_preview=preview(
                    {"upstream_task_id": upstream_task_id, "submitted": True}, limit=800
                ),
            )
            routing.mark_success(candidate.route_id)
            return InvokeResult(
                request_id=request_id,
                invocation_id=invocation_id,
                status="queued",
                mode="async",
                capability=capability["slug"],
                version=version["version"],
                task_id=task_id,
                usage=usage_info,
                provider=provider,
                route_reason=selection.reason,
                http_status=response.status_code,
                latency_ms=latency_ms,
                attempts=total_attempts,
                raw_request=request_preview(prepared),
            )

        try:
            result, artifacts, usage_info, cost, schema_valid = extract_result(
                config=config,
                body=body,
                response=response,
                invocation_id=invocation_id,
                task_id=None,
                latency_ms=latency_ms,
                redactor=prepared.redactor,
            )
        except ApiError as exc:
            last_error = {"code": exc.code, "message": exc.message, "action": (exc.actions or [None])[0]}
            break

        provider = candidate.provider or {}
        routing.mark_success(candidate.route_id)

        response_snapshot = {"result": result, "artifacts": artifacts}
        _finish_invocation(
            invocation_id,
            status="succeeded",
            provider_id=provider.get("id"),
            route_id=None if candidate.route_id == "implicit" else candidate.route_id,
            route_reason=selection.reason,
            http_status=response.status_code,
            latency_ms=latency_ms,
            retries=max(0, total_attempts - 1),
            usage=db.dumps(usage_info),
            cost_estimate_usd=cost.get("estimated_usd"),
            cost_actual_usd=cost.get("actual_usd"),
            response_preview=preview(response_snapshot, limit=2000, secrets=prepared.redactor._values),  # noqa: SLF001
        )
        return InvokeResult(
            request_id=request_id,
            invocation_id=invocation_id,
            status="succeeded",
            mode=mode,
            capability=capability["slug"],
            version=version["version"],
            result=result,
            artifacts=artifacts,
            usage=usage_info,
            cost=cost,
            provider=provider,
            route_reason=selection.reason,
            http_status=response.status_code,
            latency_ms=latency_ms,
            attempts=total_attempts,
            schema_valid=schema_valid,
            raw_request=request_preview(prepared),
        )

    # ---- all candidates failed -------------------------------------------------
    _finish_invocation(
        invocation_id,
        status="failed",
        latency_ms=0,
        retries=max(0, total_attempts - 1),
        error_code=(last_error or {}).get("code", "execution_failed"),
        error_message=(last_error or {}).get("message", "The call failed."),
        route_reason=selection.reason,
    )
    status_code = 502
    code = (last_error or {}).get("code", "execution_failed")
    if code in {"rate_limited", "upstream_5xx"}:
        status_code = 429 if code == "rate_limited" else 502
    raise ApiError(
        code,
        (last_error or {}).get("message", "The call failed."),
        status_code=status_code,
        details={
            "request_id": request_id,
            "upstream_status": (last_error or {}).get("upstream_status"),
            "upstream_body": (last_error or {}).get("upstream_body"),
            "route": routing.explain_selection(selection),
        },
        actions=[a for a in [(last_error or {}).get("action")] if a],
    )


async def test_version(
    *,
    capability_id: str,
    version_id: str,
    payload: dict[str, Any],
    actor_id: str | None = None,
    mode: str = "sync",
    save_bodies: bool = True,
) -> dict[str, Any]:
    """Admin test panel: run a draft version without publishing it."""
    capability = db.query_one("SELECT * FROM capabilities WHERE id = ?", (capability_id,))
    if not capability:
        raise not_found("Capability")
    principal = AgentPrincipal(
        agent_id="",
        agent_name="admin-test",
        token_id="",
        scopes=["*"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )
    version = get_version(capability, version_id=version_id, published_only=False)
    config = _config(version, capability.get("type", "generic"))
    payload = validate_input(config, payload or {})
    credential_id = capability.get("credential_id")
    provider_id = capability.get("provider_id")
    pseudo = routing.RouteCandidate(
        route_id="admin-test",
        route={"id": None, "credential_id": credential_id, "provider_id": provider_id, "priority": 0},
        version={"id": version["id"], "version": version["version"], "status": version["status"], "config": config},
        config=config,
        provider={"id": provider_id} if provider_id else None,
        credential={"id": credential_id} if credential_id else None,
        capability_id=capability["id"],
        capability_slug=capability["slug"],
        capability_type=capability.get("type", "generic"),
    )
    prepared = build_call(candidate=pseudo, payload=payload, mode=mode)
    request_snapshot = request_preview(prepared, include_body=True)
    if version["status"] == "published":
        # A published version test still goes through the real executor so the
        # recorded evidence matches what external callers will get.
        result = await invoke(
            principal,
            capability["id"],
            payload,
            InvokeOptions(surface="admin", allow_draft=True, version_id=version["id"], mode=mode, actor_type="user", actor_id=actor_id, save_bodies=save_bodies),
        )
        return {"request": request_snapshot, **result.to_dict()}

    started = time.perf_counter()
    timeout_s = float((config.get("limits") or {}).get("timeout_s") or 60)
    response, error, attempts = await _send(prepared, config=config, timeout_s=timeout_s, max_attempts=0)
    latency_ms = int((time.perf_counter() - started) * 1000)
    record_id = db.new_id("captest")
    if response is None:
        db.insert(
            "capability_tests",
            {
                "id": record_id,
                "capability_id": capability_id,
                "version_id": version_id,
                "kind": "sample_call",
                "status": "failed",
                "request": preview(request_snapshot, limit=4000),
                "response": "{}",
                "latency_ms": latency_ms,
                "error": preview(error, limit=1000),
                "actor": actor_id,
                "created_at": db.iso(),
            },
        )
        return {
            "request": request_snapshot,
            "status": "failed",
            "error": error,
            "latency_ms": latency_ms,
            "attempts": attempts,
            "test_id": record_id,
        }
    body = response.json() if response.content_type == "application/json" else None

    # An async capability's submit response carries no result yet. Judging it
    # with the sync result path would fail a perfectly good submission (and
    # leaving the submitted job untracked would strand a paid upstream task),
    # so: verify the task id, persist a real task row, and let the worker poll it.
    if mode == "async":
        async_cfg = config.get("async") or {}
        task_path = (async_cfg.get("submit_extract") or {}).get("task_id")
        upstream_task_id = jsonpath_get(body, task_path) if task_path else None
        submit_ok = 200 <= response.status_code < 400 and upstream_task_id not in (None, "")
        task_id = None
        submit_error: dict[str, Any] | None = None
        if not submit_ok:
            submit_error = {
                "code": "upstream_task_id_missing",
                "message": f"The provider answered HTTP {response.status_code} but no task id was found at {task_path!r}.",
                "action": "Correct async.submit_extract.task_id, then test again.",
            }
        else:
            task_id = db.new_id("task")
            poll_interval = float((async_cfg.get("status") or {}).get("poll_interval_s") or 5)
            now_iso = db.iso()
            db.insert(
                "tasks",
                {
                    "id": task_id,
                    "request_id": db.new_id("req"),
                    "kind": "capability_async",
                    "capability_id": capability_id,
                    "version_id": version_id,
                    "provider_id": capability.get("provider_id"),
                    "route_id": None,
                    "agent_id": None,
                    "status": "queued",
                    "upstream_task_id": str(upstream_task_id),
                    "input": preview(payload, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
                    "idempotency_key": None,
                    "fingerprint": None,
                    "attempts": 0,
                    "max_attempts": int((config.get("retry") or {}).get("max_attempts") or 3),
                    "poll_after": db.iso(db.utcnow() + __import__("datetime").timedelta(seconds=poll_interval)),
                    "deadline": db.iso(db.utcnow() + __import__("datetime").timedelta(
                        seconds=float((config.get("limits") or {}).get("max_duration_s") or 3600)
                    )),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
            )
        snapshot = {"status": response.status_code, "body": body, "upstream_task_id": upstream_task_id, "task_id": task_id}
        db.insert(
            "capability_tests",
            {
                "id": record_id,
                "capability_id": capability_id,
                "version_id": version_id,
                "kind": "sample_call",
                "status": "passed" if submit_ok else "failed",
                "request": preview(request_snapshot, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
                "response": preview(snapshot, limit=6000, secrets=prepared.redactor._values),  # noqa: SLF001
                "latency_ms": latency_ms,
                "http_status": response.status_code,
                "error": preview(submit_error, limit=800) if submit_error else None,
                "actor": actor_id,
                "created_at": db.iso(),
            },
        )
        return {
            "request": request_snapshot,
            "response": preview(snapshot, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
            "status": "passed" if submit_ok else "failed",
            "mode": "async",
            "upstream_task_id": upstream_task_id,
            "task_id": task_id,
            "tracked": bool(task_id),
            "http_status": response.status_code,
            "latency_ms": latency_ms,
            "attempts": attempts,
            "error": submit_error,
            "test_id": record_id,
        }

    result_payload: Any = None
    extraction_error: dict[str, Any] | None = None
    try:
        result_payload, artifacts, usage_info, cost, schema_valid = extract_result(
            config=config,
            body=body,
            response=response,
            invocation_id=record_id,
            task_id=None,
            latency_ms=latency_ms,
            redactor=prepared.redactor,
        )
    except ApiError as exc:
        extraction_error = {"code": exc.code, "message": exc.message}
        artifacts, usage_info, cost, schema_valid = [], {}, {}, None
    ok = 200 <= response.status_code < 400 and extraction_error is None
    db.insert(
        "capability_tests",
        {
            "id": record_id,
            "capability_id": capability_id,
            "version_id": version_id,
            "kind": "sample_call",
            "status": "passed" if ok else "failed",
            "request": preview(request_snapshot, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
            "response": preview(
                {"status": response.status_code, "body": body, "result": result_payload},
                limit=6000,
                secrets=prepared.redactor._values,  # noqa: SLF001
            ),
            "latency_ms": latency_ms,
            "http_status": response.status_code,
            "error": preview(extraction_error, limit=800) if extraction_error else None,
            "actor": actor_id,
            "created_at": db.iso(),
        },
    )
    return {
        "request": request_snapshot,
        "response": preview({"status": response.status_code, "body": body}, limit=4000, secrets=prepared.redactor._values),  # noqa: SLF001
        "status": "passed" if ok else "failed",
        "result": result_payload,
        "artifacts": artifacts,
        "usage": usage_info,
        "cost": cost,
        "schema_valid": schema_valid,
        "http_status": response.status_code,
        "latency_ms": latency_ms,
        "attempts": attempts,
        "error": extraction_error,
        "test_id": record_id,
    }


# --------------------------------------------------------------- streaming ---
async def stream_invocation(
    principal: AgentPrincipal,
    capability_ref: str,
    payload: dict[str, Any],
    options: InvokeOptions | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """SSE/NDJSON relay with structured events.

    The chosen route is locked for the duration: once bytes have been forwarded
    a different model is never substituted, because the caller already sees the
    answer forming.
    """
    options = options or InvokeOptions(mode="stream")
    capability = get_capability(capability_ref)
    if capability["status"] != "published" and not options.allow_draft:
        raise conflict("capability_not_published", f"'{capability['slug']}' is not published yet.")
    version = get_version(capability, published_only=not options.allow_draft)
    config = _config(version, capability.get("type", "generic"))
    if not (config.get("modes") or {}).get("stream"):
        raise unprocessable("mode_not_supported", f"'{capability['slug']}' does not support streaming.")
    payload = validate_input(config, payload or {})
    selection = routing.candidates_for(
        capability=capability, version=version, input_payload=payload, mode="stream", preferred_provider_id=options.provider_id,
        allow_draft=options.allow_draft,
    )
    if not selection.candidates:
        raise _no_route_error(selection, streaming=True)
    candidate = selection.candidates[0]
    prepared = build_call(candidate=candidate, payload=payload, mode="stream", timeout_s=options.timeout_s)
    validate_url(prepared.url)
    invocation = _create_invocation(
        capability=capability,
        version=version,
        principal=principal,
        payload=payload,
        mode="stream",
        options=options,
        idempotency_key=options.idempotency_key,
        fingerprint=_fingerprint(capability["slug"], version["version"], payload),
        gate={"reason": selection.reason},
    )
    channel = db.new_id("chan")
    yield {"event": "start", "request_id": invocation["request_id"], "invocation_id": invocation["id"], "channel": channel, "provider": candidate.provider, "route_reason": selection.reason}

    stream_cfg = config.get("stream") or {}
    delta_path = stream_cfg.get("delta_path")
    done_marker = stream_cfg.get("done_marker") or "[DONE]"
    protocol = stream_cfg.get("protocol", "sse")
    headers = dict(prepared.headers)
    headers.setdefault("accept", "text/event-stream" if protocol == "sse" else "application/x-ndjson")
    text_parts: list[str] = []
    started = time.perf_counter()
    status = "failed"
    error_payload: dict[str, Any] | None = None
    timeout = httpx.Timeout(float(options.timeout_s or (config.get("limits") or {}).get("timeout_s") or 120))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                prepared.method,
                prepared.url,
                headers=headers,
                params=prepared.params,
                json=prepared.json_body,
                data=prepared.data,
                content=prepared.content,
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    body = None
                    try:
                        body = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        body = None
                    mapped = map_upstream_error(status_code=response.status_code, body=body, config=config, redactor=prepared.redactor)
                    error_payload = {**mapped, "upstream_body": preview(prepared.redactor.text(raw.decode("utf-8", errors="replace")), limit=1000)}
                    status = "failed"
                    yield {"event": "error", "error": error_payload, "http_status": response.status_code}
                    return
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if protocol == "sse":
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            elif line.startswith(("event:", "id:", ":")):
                                continue
                        if line == done_marker:
                            status = "succeeded"
                            break
                        try:
                            parsed = json.loads(line)
                        except ValueError:
                            continue
                        delta = jsonpath_get(parsed, delta_path)
                        if isinstance(delta, str) and delta:
                            text_parts.append(delta)
                            yield {"event": "delta", "text": delta}
                        elif delta is not None and not isinstance(delta, str):
                            text_parts.append(str(delta))
                            yield {"event": "delta", "text": str(delta)}
                    if status == "succeeded":
                        break
        if status != "succeeded":
            status = "succeeded" if text_parts else "failed"
            if not text_parts and not error_payload:
                error_payload = {
                    "code": "empty_stream",
                    "message": "The provider closed the stream without sending content.",
                    "action": "Check the delta path in the capability version.",
                }
                yield {"event": "error", "error": error_payload}
    except (httpx.HTTPError, NetworkPolicyError) as exc:
        code, message = classify_network_error(exc)
        error_payload = {"code": code, "message": prepared.redactor.text(message)}
        status = "failed"
        yield {"event": "error", "error": error_payload}
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        _finish_invocation(
            invocation["id"],
            status=status,
            provider_id=(candidate.provider or {}).get("id"),
            latency_ms=latency_ms,
            error_code=(error_payload or {}).get("code"),
            error_message=(error_payload or {}).get("message"),
            response_preview=preview({"text": "".join(text_parts)[:2000]}, limit=2000),
            usage=db.dumps({"source": "provider"}),
        )
        if status == "succeeded":
            yield {"event": "done", "text": "".join(text_parts), "latency_ms": latency_ms, "invocation_id": invocation["id"]}


# ------------------------------------------------------------------ cancel ---
async def fetch_status(
    *,
    config: dict[str, Any],
    upstream_task_id: str,
    input_payload: dict[str, Any],
    credential_id: str | None,
) -> dict[str, Any]:
    status_cfg = (config.get("async") or {}).get("status") or {}
    ctx = {"task_id": upstream_task_id, "input": input_payload, "model": config.get("model"), "mode": "async"}
    url = str(render(status_cfg.get("url") or "", ctx))
    if not url:
        raise unprocessable("missing_status_url", "This capability has no async status URL.")
    headers: dict[str, str] = {}
    for name, value in (status_cfg.get("headers") or {}).items():
        headers[str(name)] = str(render(value, ctx))
    if credential_id and (config.get("auth") or {}).get("mode") != "none":
        row = credential_service.get_row(credential_id)
        secrets = credential_service.get_secret_map(credential_id)
        headers.update(credential_service.build_auth_headers(row, secrets))
        redactor = secrets.redactor()
    else:
        redactor = Redactor()
    response = await guarded_request(
        str(status_cfg.get("method") or "GET").upper(),
        url,
        headers=headers,
        timeout_s=float((config.get("limits") or {}).get("timeout_s") or 30),
    )
    body = response.json() if response.content_type == "application/json" else None
    if response.status_code >= 400:
        mapped = map_upstream_error(status_code=response.status_code, body=body, config=config, redactor=redactor)
        return {"ok": False, "error": mapped, "http_status": response.status_code}
    extract = status_cfg.get("extract") or {}
    raw_status = jsonpath_get(body, extract.get("status"))
    mapping = status_cfg.get("map") or {}
    normalized = "unknown"
    for state, values in mapping.items():
        if str(raw_status).lower() in [str(v).lower() for v in (values or [])]:
            normalized = state
            break
    result_path = (status_cfg.get("result_extract") or {}).get("result")
    result = jsonpath_get(body, result_path) if result_path else None
    progress = jsonpath_get(body, extract.get("progress")) if extract.get("progress") else None
    return {
        "ok": True,
        "upstream_status": raw_status,
        "status": normalized,
        "result": redactor.value(result) if result is not None else None,
        "progress": progress,
        "body_preview": preview(redactor.value(body), limit=1500),
    }


async def request_cancel(
    *,
    config: dict[str, Any],
    upstream_task_id: str,
    input_payload: dict[str, Any],
    credential_id: str | None,
) -> dict[str, Any]:
    cancel_cfg = (config.get("async") or {}).get("cancel") or {}
    ctx = {"task_id": upstream_task_id, "input": input_payload, "mode": "async", "model": config.get("model")}
    url = str(render(cancel_cfg.get("url") or "", ctx))
    if not url:
        return {
            "supported": False,
            "message": "The provider exposes no cancel endpoint, so the request keeps running upstream.",
            "status": "unsupported",
        }
    headers: dict[str, str] = {}
    if credential_id and (config.get("auth") or {}).get("mode") != "none":
        row = credential_service.get_row(credential_id)
        secrets = credential_service.get_secret_map(credential_id)
        headers.update(credential_service.build_auth_headers(row, secrets))
    try:
        response = await guarded_request(
            str(cancel_cfg.get("method") or "POST").upper(),
            url,
            headers=headers,
            json_body=render(cancel_cfg.get("body_template"), ctx) if cancel_cfg.get("body_template") else None,
            timeout_s=30,
        )
    except Exception as exc:  # noqa: BLE001 - cancellation must always answer
        code, message = classify_network_error(exc)
        return {"supported": True, "status": "failed", "message": message, "error_code": code}
    body = response.json() if response.content_type == "application/json" else None
    extract = cancel_cfg.get("extract") or {}
    remote = jsonpath_get(body, extract.get("status")) if extract.get("status") else None
    return {
        "supported": True,
        "status": "accepted" if 200 <= response.status_code < 300 else "rejected",
        "http_status": response.status_code,
        "upstream_status": remote,
        "message": "The provider accepted the cancellation request." if 200 <= response.status_code < 300 else "The provider rejected the cancellation request.",
    }


async def capture_artifact_url(
    *,
    config: dict[str, Any],
    body: Any,
    task_id: str,
    invocation_id: str | None = None,
    redactor: Redactor | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Download a URL-shaped artifact (e.g. a finished video) into managed storage.

    The URL comes from the provider, so it is fetched through the same SSRF guard
    as every other outbound request, with the response size capped.
    """
    artifact_cfg = (config.get("response") or {}).get("artifact") or {}
    path = artifact_cfg.get("path")
    if not path:
        return [], None
    url = jsonpath_get(body, path)
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return [], None
    mime = artifact_cfg.get("mime") or "application/octet-stream"
    filename = artifact_cfg.get("filename") or f"{artifact_cfg.get('kind', 'file')}-{task_id[:8]}"
    if "." not in filename:
        filename += {"image": ".png", "audio": ".mp3", "video": ".mp4"}.get(artifact_cfg.get("kind", ""), "")
    try:
        response = await guarded_request(
            "GET",
            url,
            timeout_s=120,
            max_bytes=int((config.get("limits") or {}).get("max_output_bytes") or 8 * 1024 * 1024),
        )
    except NetworkPolicyError as exc:
        return [], {"code": exc.code, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - transport failures are data here
        code, message = classify_network_error(exc)
        return [], {"code": code, "message": message}
    if response.status_code >= 400:
        return [], {"code": "artifact_download_failed", "message": f"The artifact URL returned HTTP {response.status_code}."}
    content_type = response.content_type or mime
    saved = file_service.save_bytes(
        response.content,
        filename=filename,
        content_type=content_type,
        purpose="artifact",
        task_id=task_id,
        invocation_id=invocation_id,
        owner_type="task",
        owner_id=task_id,
    )
    saved["kind"] = artifact_cfg.get("kind", "file")
    return [saved], None


def execution_stats() -> dict[str, Any]:
    row = db.query_one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  AVG(latency_ms) AS avg_latency
             FROM invocations WHERE created_at >= ?""",
        (db.iso(db.utcnow() - __import__("datetime").timedelta(hours=24)),),
    ) or {}
    total = int(row.get("total") or 0)
    succeeded = int(row.get("succeeded") or 0)
    return {
        "calls_24h": total,
        "success_rate_24h": round(succeeded / total, 4) if total else None,
        "failed_24h": int(row.get("failed") or 0),
        "avg_latency_ms_24h": round(float(row.get("avg_latency") or 0), 1) if total else None,
    }


def shutdown_executor() -> None:
    """Placeholder for symmetry: connections are per-request, nothing to drain."""
    return None
