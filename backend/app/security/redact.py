"""Secret redaction.

Every log line, audit row, error message, exported SKILL and API response that
could echo request material passes through here. Two layers:

  1. shape-based patterns (vendor key formats, Authorization headers, JWTs)
  2. exact-value scrubbing for the concrete secrets a request used

Layer 2 matters most: a provider can hand back an opaque key format we have
never seen, and it must still never reach the log.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers, in JSON or plain form
    (re.compile(r'(?i)("?(?:authorization|x-api-key|api[_-]?key|access[_-]?token|bearer)"?\s*[:=]\s*"?)([^"\n,}]{6,})'), r"\1" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), "Basic " + REDACTED),
    # Vendor key shapes
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{12,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), REDACTED),
    # data URLs carrying inline model output keep their prefix but lose the blob
    (re.compile(r"(data:[a-z0-9.+/\-]+;base64,)[A-Za-z0-9+/=]{200,}"), r"\1" + REDACTED),
]

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "client_secret",
    "x-api-key",
    "key",
    "credential",
    "bearer",
    "private_key",
}

_SAFE_KEY_SUFFIXES = ("_id", "_name", "_ref", "_count", "_hint", "_prefix", "_field", "_at", "_type")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith(_SAFE_KEY_SUFFIXES):
        return False
    return lowered in _SENSITIVE_KEYS or "secret" in lowered or "password" in lowered or lowered.endswith("_key")


class Redactor:
    """Scrub known secret values plus everything that looks like a secret."""

    def __init__(self, secrets: Iterable[str] | None = None, *, max_items: int = 64) -> None:
        values = sorted({s for s in (secrets or []) if s and len(s) >= 4}, key=len, reverse=True)
        self._values = values[:max_items]

    def text(self, value: str | None) -> str:
        if not value:
            return value or ""
        out = value
        for secret in self._values:
            out = out.replace(secret, REDACTED)
        for pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    def value(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 8:
            return REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and _is_sensitive_key(key):
                    result[key] = REDACTED
                else:
                    result[key] = self.value(item, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [self.value(item, depth=depth + 1) for item in value]
        return value


def redact_text(value: str | None) -> str:
    return Redactor().text(value)


def redact_value(value: Any) -> Any:
    return Redactor().value(value)


def truncate(value: str, limit: int = 4000) -> str:
    if value is None:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + f"… [truncated {len(value) - limit} chars]"


def preview(value: Any, limit: int = 1200, secrets: Iterable[str] | None = None) -> str:
    """A redacted, truncated JSON-ish preview safe to persist."""
    import json

    redactor = Redactor(secrets)
    payload = redactor.value(value)
    if isinstance(payload, str):
        rendered = payload
    else:
        try:
            rendered = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(payload)
    return truncate(redactor.text(rendered), limit)
