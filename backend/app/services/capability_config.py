"""Capability configuration: the JSON contract stored on a version.

A capability version holds one declarative document describing how to call an
upstream API and how to read its answer. It is data, not code: validation,
diffing, publishing and rollback all operate on this document, and the executor
interprets it. That is what makes "draft → test → publish → rollback" real.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

CAPABILITY_TYPES = {"text", "image", "video", "speech", "transcription", "generic"}
MODES = ("sync", "async", "stream")
ENCODINGS = {"json", "form", "multipart", "none"}

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]-]+)\s*\}\}")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_config(capability_type: str = "text") -> dict[str, Any]:
    mode_defaults = {
        "text": {"sync": True, "async": False, "stream": True},
        "image": {"sync": True, "async": False, "stream": False},
        "video": {"sync": False, "async": True, "stream": False},
        "speech": {"sync": True, "async": False, "stream": False},
        "transcription": {"sync": True, "async": False, "stream": False},
        "generic": {"sync": True, "async": False, "stream": False},
    }
    return {
        "model": None,
        "request": {
            "method": "POST",
            "url": "",
            "headers": {"content-type": "application/json"},
            "query": [],
            "encoding": "json",
            "form_fields": {},
            "body_template": None,
        },
        "auth": {"mode": "credential"},
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}},
        "response": {
            "kind": "json",
            "extract": {"result": ""},
            "artifact": None,
        },
        "async": {
            "enabled": mode_defaults.get(capability_type, mode_defaults["generic"])["async"],
            "submit_extract": {"task_id": ""},
            "status": {"url": "", "method": "GET", "extract": {"status": ""}, "map": {
                "queued": ["queued", "pending", "submitted"],
                "running": ["running", "processing", "in_progress"],
                "succeeded": ["succeeded", "completed", "success", "done"],
                "failed": ["failed", "error"],
                "cancelled": ["cancelled", "canceled"],
            }, "poll_interval_s": 5, "result_extract": {"result": ""}},
            "cancel": {"method": "POST", "url": "", "extract": {"status": ""}},
        },
        "stream": {"enabled": mode_defaults.get(capability_type, mode_defaults["generic"])["stream"], "protocol": "sse", "delta_path": "", "done_marker": "[DONE]"},
        "errors": {"rules": [], "default_code": "upstream_error"},
        "limits": {
            "timeout_s": 60,
            "max_output_bytes": 8388608,
            "max_input_bytes": 262144,
            "file_input": False,
            "max_file_mb": 10,
            "allowed_mime": [],
            "max_duration_s": None,
        },
        "modes": mode_defaults.get(capability_type, mode_defaults["generic"]),
        "provider_params": [],
        "retry": {"max_attempts": 2, "backoff_ms": [500, 1500], "retry_on": ["connect_timeout", "read_timeout", "upstream_5xx", "rate_limited"]},
        "pricing": {"per_call": 0.0, "input_per_1k": 0.0, "output_per_1k": 0.0, "per_second": 0.0, "currency": "USD"},
        "dedupe_window_s": 0,
        "examples": [],
    }


def merge_defaults(config: dict[str, Any] | None, capability_type: str = "text") -> dict[str, Any]:
    """Deep-merge a stored config over the defaults (forward-compatible)."""
    base = default_config(capability_type)

    def merge(target: Any, patch: Any) -> Any:
        if isinstance(target, dict) and isinstance(patch, dict):
            out = dict(target)
            for key, value in patch.items():
                out[key] = merge(target.get(key), value)
            return out
        return patch if patch is not None else target

    return merge(base, config or {})


# ------------------------------------------------------------------ jsonpath --
_TOKEN = re.compile(r"\.?([A-Za-z0-9_\-]+)|\[(\d+)\]|\['([^']*)'\]|\[\"([^\"]*)\"\]")


def jsonpath_get(data: Any, path: str | None, default: Any = None) -> Any:
    """Minimal JSONPath: $.a.b[0].c, $.a['b-c'], $[0].  No wildcards by design."""
    if not path:
        return default
    expr = path.strip()
    if expr in {"$", ""}:
        return data
    if expr.startswith("$"):
        expr = expr[1:]
    current = data
    pos = 0
    while pos < len(expr):
        match = _TOKEN.match(expr, pos)
        if not match:
            return default
        pos = match.end()
        key = match.group(1) or match.group(3) or match.group(4)
        index = match.group(2)
        if index is not None:
            if not isinstance(current, (list, tuple)):
                return default
            idx = int(index)
            if idx >= len(current):
                return default
            current = current[idx]
        elif key is not None:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
    return current


def set_path(root: dict[str, Any], path: str, value: Any) -> None:
    """Set a dotted path in a dict, creating intermediate objects."""
    parts = [p for p in re.split(r"\.", path) if p]
    cursor = root
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    if parts:
        cursor[parts[-1]] = value


# ------------------------------------------------------------------ template --
def _resolve(ctx: dict[str, Any], path: str) -> Any:
    current: Any = ctx
    for raw in re.split(r"\.", path):
        if not raw:
            continue
        index_match = re.fullmatch(r"([A-Za-z0-9_\-]+)\[(\d+)\]", raw)
        if index_match:
            key, idx = index_match.group(1), int(index_match.group(2))
            current = current.get(key) if isinstance(current, dict) else None
            if isinstance(current, (list, tuple)) and idx < len(current):
                current = current[idx]
            else:
                return None
            continue
        if isinstance(current, dict):
            current = current.get(raw)
        else:
            return None
        if current is None:
            return None
    return current


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Render {{ placeholders }}. A whole-string placeholder keeps its type."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole:
            return _resolve(ctx, whole.group(1))
        return _PLACEHOLDER.sub(lambda m: _stringify(_resolve(ctx, m.group(1))), value)
    if isinstance(value, dict):
        return {key: render(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, ctx) for item in value]
    return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ------------------------------------------------------------------ validate --
def validate_config(config: dict[str, Any], *, capability_type: str = "generic") -> list[dict[str, Any]]:
    """Return issues: {severity: error|warning, field, message}."""
    issues: list[dict[str, Any]] = []
    cfg = merge_defaults(config, capability_type)

    def error(field: str, message: str) -> None:
        issues.append({"severity": "error", "field": field, "message": message})

    def warn(field: str, message: str) -> None:
        issues.append({"severity": "warning", "field": field, "message": message})

    request = cfg.get("request", {})
    url = (request.get("url") or "").strip()
    if not url:
        error("request.url", "A request URL is required.")
    else:
        parsed = urlparse(url.replace("{{", "").replace("}}", ""))
        if parsed.scheme not in {"http", "https"}:
            error("request.url", "The URL must start with http:// or https://.")
        elif parsed.scheme == "http":
            warn("request.url", "Plain HTTP sends credentials unencrypted. Use HTTPS in production.")
        if not parsed.netloc:
            error("request.url", "The URL has no host.")

    method = (request.get("method") or "").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        error("request.method", "Method must be one of GET, POST, PUT, PATCH, DELETE.")

    encoding = request.get("encoding", "json")
    if encoding not in ENCODINGS:
        error("request.encoding", f"Encoding must be one of {sorted(ENCODINGS)}.")
    if encoding == "json" and method in {"POST", "PUT", "PATCH"} and request.get("body_template") is None:
        warn("request.body_template", "A POST/PUT/PATCH request with no body template sends an empty body.")
    if encoding == "multipart" and not request.get("form_fields"):
        warn("request.form_fields", "Multipart encoding without form fields will send an empty form.")

    headers = request.get("headers") or {}
    if isinstance(headers, dict):
        for name in headers:
            lowered = name.lower()
            if lowered in {"authorization", "x-api-key", "api-key", "cookie"} and "{{" not in str(headers.get(name, "")):
                error(
                    f"request.headers.{name}",
                    f"'{name}' must come from the credential, not a static header, so rotation works.",
                )

    input_schema = cfg.get("input_schema") or {}
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        error("input_schema", "The input schema must be a JSON Schema object with type 'object'.")
    else:
        props = input_schema.get("properties")
        if props is not None and not isinstance(props, dict):
            error("input_schema.properties", "properties must be an object.")
        for field_name, spec in (props or {}).items():
            if isinstance(spec, dict) and "type" not in spec and "$ref" not in spec:
                error(f"input_schema.properties.{field_name}", "Each input field needs a type.")

    modes = cfg.get("modes") or {}
    if not any(modes.get(mode) for mode in MODES):
        error("modes", "Select at least one invocation mode: sync, async or stream.")

    response = cfg.get("response") or {}
    extract = response.get("extract") or {}
    artifact = response.get("artifact")
    if modes.get("sync") and not (extract.get("result") or artifact):
        error("response.extract.result", "Synchronous capabilities need a result path (or an artifact definition).")
    if extract.get("result") and not str(extract["result"]).strip().startswith("$"):
        error("response.extract.result", "Result paths must start with '$' (e.g. $.choices[0].message.content).")

    if modes.get("async"):
        async_cfg = cfg.get("async") or {}
        if not (async_cfg.get("submit_extract") or {}).get("task_id"):
            error("async.submit_extract.task_id", "Async capabilities must extract the upstream task id.")
        status_cfg = async_cfg.get("status") or {}
        if not status_cfg.get("url"):
            error("async.status.url", "Async capabilities need a status URL template.")
        if not (status_cfg.get("extract") or {}).get("status"):
            error("async.status.extract.status", "Async capabilities need a status field path.")
        interval = status_cfg.get("poll_interval_s")
        if not isinstance(interval, (int, float)) or not (1 <= float(interval) <= 300):
            warn("async.status.poll_interval_s", "Polling interval should be between 1 and 300 seconds.")
        if not ((async_cfg.get("status") or {}).get("result_extract") or {}).get("result"):
            warn("async.status.result_extract.result", "Without a result path the finished task returns metadata only.")

    if modes.get("stream"):
        stream_cfg = cfg.get("stream") or {}
        if not stream_cfg.get("delta_path"):
            error("stream.delta_path", "Streaming capabilities need a delta path (e.g. $.choices[0].delta.content).")
        if stream_cfg.get("protocol") not in {"sse", "ndjson"}:
            error("stream.protocol", "Stream protocol must be sse or ndjson.")

    limits = cfg.get("limits") or {}
    timeout = limits.get("timeout_s")
    if not isinstance(timeout, (int, float)) or not (1 <= float(timeout) <= 600):
        error("limits.timeout_s", "Timeout must be between 1 and 600 seconds.")
    if limits.get("max_input_bytes", 0) and limits["max_input_bytes"] > 10 * 1024 * 1024:
        warn("limits.max_input_bytes", "Request bodies above 10 MB are unlikely to work on most providers.")

    retry = cfg.get("retry") or {}
    attempts = retry.get("max_attempts")
    if not isinstance(attempts, int) or not (0 <= attempts <= 4):
        error("retry.max_attempts", "Retry attempts must be between 0 and 4.")
    if any(mode in {"async", "stream"} for mode in MODES if modes.get(mode)) and (attempts or 0) > 2:
        warn("retry.max_attempts", "Long-running modes should not retry aggressively; prefer polling and reconciliation.")

    pricing = cfg.get("pricing") or {}
    for key in ("per_call", "input_per_1k", "output_per_1k", "per_second"):
        value = pricing.get(key, 0)
        if not isinstance(value, (int, float)) or value < 0:
            error(f"pricing.{key}", "Prices must be zero or positive numbers.")

    examples = cfg.get("examples") or []
    if not isinstance(examples, list):
        error("examples", "Examples must be a list.")
    elif not examples:
        warn("examples", "Add at least one example input so external agents see a call shape.")

    errors_cfg = cfg.get("errors") or {}
    for index, rule in enumerate(errors_cfg.get("rules") or []):
        if not isinstance(rule, dict) or "code" not in rule:
            error(f"errors.rules[{index}]", "Each error rule needs a code.")
        if rule.get("action") is None:
            warn(f"errors.rules[{index}]", "An action tells the caller what to do next; add one.")

    return issues


def has_blocking_issues(issues: Iterable[dict[str, Any]]) -> bool:
    return any(issue["severity"] == "error" for issue in issues)


def config_checksum(config: dict[str, Any]) -> str:
    import hashlib
    import json

    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def check_unknown_inputs(input_payload: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Provider-specific passthrough is explicit: everything else must be declared."""
    schema = (config.get("input_schema") or {})
    properties = set((schema.get("properties") or {}).keys())
    extensions = set(config.get("provider_params") or [])
    if schema.get("additionalProperties") is True:
        return []
    return sorted(set(input_payload.keys()) - properties - extensions)


def mask_config_for_export(config: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that could carry secret material before export."""
    import copy

    clone = copy.deepcopy(config)
    headers = ((clone.get("request") or {}).get("headers") or {})
    for name in list(headers.keys()):
        if name.lower() in {"authorization", "x-api-key", "api-key", "cookie"}:
            headers.pop(name, None)
    clone.pop("_internal", None)
    return clone
