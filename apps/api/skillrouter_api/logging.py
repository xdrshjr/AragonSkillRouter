"""结构化日志与脱敏（§7.14 / §11.1）。"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_request_id: ContextVar[str] = ContextVar("sr_request_id", default="-")

_SENSITIVE_KEYS = re.compile(
    r"(password|secret|token|api[_-]?key|authorization|ciphertext|credential_value|key)$",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsrk_[A-Za-z0-9_\-]{6,}"), "srk_***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)basic\s+[A-Za-z0-9+/=]{8,}"), "Basic ***"),
    # 查询串里的凭据/签名：`?token=`、`?sig=`、`?api_key=`…（§A.3.3）
    # `exp` 故意不匹配：签名 URL 的过期时间是排障关键（「链接是否过期」），
    # `sig` 已足以阻止重放。
    (
        re.compile(r"(?i)\b(token|access_token|sig|signature|api_key|key|secret)=([^&\s\"']+)"),
        r"\1=***",
    ),
    (re.compile(r"[A-Za-z0-9+/]{60,}={0,2}"), "<long-base64>"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "1**********"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "***@***"),
)


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def current_request_id() -> str:
    return _request_id.get()


def redact(value: Any, *, max_len: int = 2048) -> Any:
    """递归脱敏：密钥模式、邮箱、手机号、超长 base64。"""
    if isinstance(value, str):
        out = value
        for pattern, repl in _PATTERNS:
            out = pattern.sub(repl, out)
        if len(out) > max_len:
            out = out[:max_len] + f"…（已截断，原长 {len(value)} 字符）"
        return out
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if _SENSITIVE_KEYS.search(str(k)):
                result[k] = "***"
            else:
                result[k] = redact(v, max_len=max_len)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v, max_len=max_len) for v in value]
    return value


_VENDOR_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9]{0,3}[-_])")


def mask_secret(secret: str) -> str:
    """凭证掩码：保留厂商前缀与末 4 位，中间固定 `***`。

    `sk-1234abcd5678` → `sk-***5678`；`srk_ab12_xxx` → `srk_***xxxx`。
    短于 12 字符时只保留末 2 位，避免掩码本身泄漏可用信息。
    """
    if not secret:
        return "***"
    if len(secret) < 12:
        return "***" + secret[-2:]
    suffix = secret[-4:]
    match = _VENDOR_PREFIX.match(secret)
    prefix = match.group(1) if match else ""
    if prefix and len(secret) - len(prefix) - len(suffix) < 3:
        prefix = ""
    return f"{prefix}***{suffix}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "request_id": getattr(record, "rid", None) or current_request_id(),
        }
        for key in ("actor", "object", "latency_ms", "code", "path", "detail"):
            if hasattr(record, key):
                payload[key] = redact(getattr(record, key))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_CONFIGURED = False


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    for name in ("skillrouter.access", "skillrouter.run", "skillrouter.audit", "skillrouter.errors"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        if logger.handlers:
            continue
        handler = RotatingFileHandler(
            logs_dir / f"{name.split('.')[-1]}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("skillrouter."):
        name = f"skillrouter.{name}"
    return logging.getLogger(name)
