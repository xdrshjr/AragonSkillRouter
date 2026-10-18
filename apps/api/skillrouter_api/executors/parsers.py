"""JSONPath-lite 与响应解析（只实现 `$.a.b[0].c` 形式，够用且可预测）。"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

_TOKEN = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]|\[['\"]([^'\"]+)['\"]\]")
MISSING = object()


def _tokens(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise ValueError(f"仅支持以 $ 开头的 JSONPath：{path}")
    rest = path[1:]
    tokens: list[str | int] = []
    for match in _TOKEN.finditer(rest):
        if match.group(1) is not None:
            tokens.append(match.group(1))
        elif match.group(2) is not None:
            tokens.append(int(match.group(2)))
        else:
            tokens.append(match.group(3))
    consumed = "".join(m.group(0) for m in _TOKEN.finditer(rest))
    if consumed.replace(" ", "") != rest.strip():
        raise ValueError(f"无法解析的 JSONPath：{path}")
    return tokens


def jsonpath(data: Any, path: str, default: Any = None) -> Any:
    if not path or path == "$":
        return data
    try:
        tokens = _tokens(path)
    except ValueError:
        return default
    current = data
    for token in tokens:
        if current is None:
            return default
        if isinstance(token, int):
            if not isinstance(current, list) or len(current) <= token:
                return default
            current = current[token]
        else:
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
            elif isinstance(current, list) and token.isdigit():
                idx = int(token)
                if len(current) <= idx:
                    return default
                current = current[idx]
            else:
                return default
    return current


def apply_response_map(response_map: dict, body: Any, *, text: str = "") -> Any:
    """按 `response_map` 抽取输出。支持 format=json|text|base64。"""
    fmt = (response_map or {}).get("format", "json")
    if fmt == "text":
        return text
    if fmt == "base64":
        return base64.b64encode(text.encode()).decode()
    path = (response_map or {}).get("jsonpath")
    if path:
        value = jsonpath(body, path, default=MISSING)
        if value is MISSING:
            raise ValueError(f"response_map.jsonpath 未命中：{path}")
        return value
    return body


def parse_usage(response_map: dict, body: Any) -> dict:
    usage_conf = (response_map or {}).get("usage") or {}
    if not usage_conf:
        return {"unit": "tokens", "input": 0, "output": 0, "total": 0, "source": "estimated"}
    raw_in = jsonpath(body, usage_conf.get("in", "$.usage.prompt_tokens"), 0) or 0
    raw_out = jsonpath(body, usage_conf.get("out", "$.usage.completion_tokens"), 0) or 0
    try:
        in_tokens = int(raw_in)
        out_tokens = int(raw_out)
    except (TypeError, ValueError):
        in_tokens, out_tokens = 0, 0
    return {
        "unit": usage_conf.get("unit", "tokens"),
        "input": in_tokens,
        "output": out_tokens,
        "total": in_tokens + out_tokens,
        "source": "provider" if (in_tokens or out_tokens) else "estimated",
    }


def load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_sse_line(line: str) -> tuple[str | None, str | None]:
    """返回 (event, data)。"""
    if not line or line.startswith(":"):
        return None, None
    if line.startswith("event:"):
        return line[6:].strip(), None
    if line.startswith("data:"):
        return None, line[5:].strip()
    return None, None


def extract_delta(payload: Any, delta_path: str | None) -> str:
    if not delta_path:
        return ""
    value = jsonpath(payload, delta_path, default="")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)
