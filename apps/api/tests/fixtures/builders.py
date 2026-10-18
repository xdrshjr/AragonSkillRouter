"""能力定义构造器（测试共用）。"""

from __future__ import annotations

FAKE_BASE = "http://fake-upstream.test"


def route(
    provider_id: str,
    credential_id: str,
    route_id: str = "rt_primary",
    priority: int = 0,
    *,
    url_suffix: str = "/v1/generate",
    enabled: bool = True,
) -> dict:
    return {
        "id": route_id,
        "provider_id": provider_id,
        "credential_id": credential_id,
        "priority": priority,
        "enabled": enabled,
        "model_hint": "fake-model",
        "request": {
            "method": "POST",
            "url_template": "{base_url}" + url_suffix,
            "headers": {"Content-Type": "application/json"},
            "query": [],
            "body_template": {"model": "${model_hint}", "prompt": "${prompt}"},
            "encoding": "json",
        },
        "param_map": {
            "prompt": {"from": "input.prompt", "transform": "str"},
            "model": {"from": "route.model_hint"},
        },
        "response_map": {
            "format": "json",
            "jsonpath": "$.result.text",
            "usage": {"in": "$.usage.input_tokens", "out": "$.usage.output_tokens"},
        },
        "stream": {"protocol": "sse", "delta_path": "$.delta", "done_markers": ["[DONE]"]},
        "async": {
            "submit": {"url_template": "{base_url}/v1/jobs", "task_id_path": "$.id"},
            "query": {
                "url_template": "{base_url}/v1/tasks/{task_id}",
                "status_path": "$.status",
                "success": ["succeeded"],
                "failure": ["failed"],
            },
            "poll": {"initial_ms": 1, "max_ms": 5, "timeout_s": 30},
        },
        "limits": {"max_input_bytes": 262144, "allowed_mime": []},
        "errors": [
            {"match": {"status": 401}, "code": "UPSTREAM_AUTH", "retryable": False},
            {"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT", "retryable": True, "backoff_ms": 1},
        ],
        "timeout_ms": 5000,
        "retry": {"max_attempts": 1, "base_ms": 1, "max_ms": 5},
    }


def capability_payload(
    provider_id: str, credential_id: str, *, key: str, modes=None, routes=None
) -> dict:
    return {
        "key": key,
        "name": f"能力 {key}",
        "kind": "text.generate",
        "modes": modes or ["sync"],
        "file_io": {"input": [], "output": []},
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"prompt": {"type": "string"}, "max_tokens": {"type": "integer"}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        "examples": [{"title": "示例", "input": {"prompt": "你好"}}],
        "compatibility_group": None,
        "failover": {"enabled": True, "max_switches": 2},
        "requires_agent_reasoning": False,
        "tags": ["chat"],
        "routes": routes if routes is not None else [route(provider_id, credential_id)],
    }
