"""errors[] 匹配 → 平台错误码（§7.6 第 8 条 / §A.3.5 超时分类）。

`transport` 细分与 `retryable` 语义（§A.3.5）：

| transport | 触发 | retryable | status_unknown |
|---|---|---|---|
| `connect_timeout` / `dns_error` / `connection_refused` | 未建立连接 | True | False |
| `read_timeout` / `upstream_hung` | 已发出请求、未拿到完整响应 | **False** | True |
| `connection_reset` + 幂等方法（GET/HEAD） | 可安全重放 | True | False |
| `connection_reset` + 非幂等方法（POST/PUT/PATCH） | 同一请求可能已执行 | **False** | True |
"""

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

#: 可安全重放（幂等）的 HTTP 方法
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: transport 分类 → `(code, retryable, status_unknown)`；`connection_reset` 需看方法，单独处理
TRANSPORT_CLASSES: dict[str, tuple[str, bool, bool]] = {
    "connect_timeout": ("UPSTREAM_UNAVAILABLE", True, False),
    "dns_error": ("UPSTREAM_UNAVAILABLE", True, False),
    "connection_refused": ("UPSTREAM_UNAVAILABLE", True, False),
    "read_timeout": ("UPSTREAM_TIMEOUT", False, True),
    "upstream_hung": ("UPSTREAM_TIMEOUT", False, True),
    # 兼容历史取值（旧调用方只传 timeout / connect / network）
    "timeout": ("UPSTREAM_TIMEOUT", False, True),
    "connect": ("UPSTREAM_UNAVAILABLE", True, False),
    "network": ("UPSTREAM_UNAVAILABLE", True, False),
}

UNKNOWN_HINT = "请求可能已在对方执行：先对账查询上游状态，勿重复提交"


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


def map_transport_error(transport: str, method: str | None = None) -> dict:
    """把 transport 细分映射成 `{code, retryable, status_unknown, hint}`。"""
    kind = transport or "network"
    kind = kind.strip().lower()
    if kind == "connection_reset":
        idempotent = (method or "POST").upper() in IDEMPOTENT_METHODS
        code = "UPSTREAM_UNAVAILABLE"
        retryable = idempotent
        status_unknown = not idempotent
    else:
        code, retryable, status_unknown = TRANSPORT_CLASSES.get(
            kind, ("UPSTREAM_ERROR", False, False)
        )
    hint = UNKNOWN_HINT if status_unknown else DEFAULT_HINTS.get(code, "")
    return {
        "code": code,
        "retryable": retryable,
        "status_unknown": status_unknown,
        "hint": hint,
        "transport": kind,
    }


def map_error(
    *,
    status: int | None,
    body: Any,
    rules: list[dict] | None,
    transport_error: str | None = None,
    method: str | None = None,
) -> dict:
    """返回 `{code, retryable, hint, upstream_status, transport, status_unknown}`。"""
    if transport_error:
        mapped = map_transport_error(transport_error, method)
        return {
            "code": mapped["code"],
            "retryable": mapped["retryable"],
            "hint": mapped["hint"],
            "upstream_status": None,
            "transport": mapped["transport"],
            "status_unknown": mapped["status_unknown"],
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
                    "transport": None,
                    "status_unknown": False,
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
        "transport": None,
        "status_unknown": False,
    }


__all__ = [
    "DEFAULT_HINTS",
    "IDEMPOTENT_METHODS",
    "TRANSPORT_CLASSES",
    "map_error",
    "map_transport_error",
]
