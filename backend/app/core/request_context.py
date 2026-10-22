"""Per-request correlation id, shared by logs, errors and stored records."""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("skillrouter_request_id", default="")
_SAFE = re.compile(r"^[A-Za-z0-9._\-]{6,64}$")


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def set_request_id(value: str | None) -> str:
    if value and _SAFE.match(value):
        rid = value
    else:
        rid = new_request_id()
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    rid = _request_id.get()
    if not rid:
        rid = new_request_id()
        _request_id.set(rid)
    return rid
