"""errors[] 匹配 → 平台错误码（§7.6 第 8 条）。"""

from __future__ import annotations

from typing import Any

from .base import STATUS_FALLBACK
from .parsers import jsonpath

DEFAULT_HINTS = {
    "UPSTREAM_AUTH": "检查凭证是否有效（凭证页可一键检测）",
    "UPSTREAM_RATE_LIMIT": "降低并发或稍后重试",
    "UPSTREAM_QUOTA": "到提供商控制台充值或更换凭证",
    "UPSTREAM_TIMEOUT": "查看任务状态，勿重复提交",
    "UPSTREAM_UNAVAILABLE": "检查上游状态页或配置备用 route",
    "UPSTREAM_BAD_REQUEST": "按上游错误信息修正参数",
    "UPSTREAM_INVALID_RESPONSE": "检查 response_map 与示例响应",
    "UPSTREAM_ERROR": "查看脱敏后的上游错误摘要",
}


def _match_one(rule: dict, status: int | None, body: Any) -> bool:
    match = rule.get("match") or {}
    if "status" in match and status is not None:
        raw = match["status"]
        candidates = raw if isinstance(raw, list) else [raw]
        if int(status) in [int(c) for c in candidates]:
            return True
        if "json" not in match:
            return False
    if "json" in match:
        try:
            value = jsonpath(body, match["json"])
        except Exception:  # noqa: BLE001
            return False
        if "equals" in match:
            return value == match["equals"]
        if "in" in match and isinstance(match["in"], list):
            return value in match["in"]
        if "regex" in match:
            import re

            return bool(re.search(str(match["regex"]), str(value)))
        return value is not None
    return False


def map_error(
    *, status: int | None, body: Any, rules: list[dict] | None, transport_error: str | None = None
) -> dict:
    """返回 `{code, retryable, hint, upstream_status}`。"""
    if transport_error:
        code = "UPSTREAM_TIMEOUT" if transport_error in ("timeout", "read_timeout") else (
            "UPSTREAM_UNAVAILABLE" if transport_error in ("connect", "network") else "UPSTREAM_ERROR"
        )
        return {
            "code": code,
            "retryable": code in ("UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE"),
            "hint": DEFAULT_HINTS.get(code, ""),
            "upstream_status": None,
        }
    for rule in rules or []:
        try:
            if _match_one(rule, status, body):
                code = rule.get("code") or STATUS_FALLBACK.get(int(status or 0), "UPSTREAM_ERROR")
                return {
                    "code": code,
                    "retryable": bool(rule.get("retryable", False)),
                    "hint": rule.get("hint") or DEFAULT_HINTS.get(code, ""),
                    "upstream_status": status,
                    "backoff_ms": rule.get("backoff_ms"),
                }
        except Exception:  # noqa: BLE001 - 单条规则解析失败不影响其它规则
            continue
    code = STATUS_FALLBACK.get(int(status or 0), "UPSTREAM_ERROR")
    if status and 200 <= status < 300:
        code = "UPSTREAM_INVALID_RESPONSE"
    return {
        "code": code,
        "retryable": code in ("UPSTREAM_RATE_LIMIT", "UPSTREAM_UNAVAILABLE", "UPSTREAM_TIMEOUT"),
        "hint": DEFAULT_HINTS.get(code, ""),
        "upstream_status": status,
    }
