"""Access tokens for external agents and one-time secrets.

A platform token is shown exactly once, at creation. Only its SHA-256 is
stored, and lookups are constant-time over the hash so a timing side channel
cannot help an attacker enumerate tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass

TOKEN_PREFIX = "sr_live_"
PREFIX_DISPLAY_LEN = 16


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_hash: str
    prefix: str


def issue_agent_token() -> IssuedToken:
    body = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{body}"
    return IssuedToken(token=token, token_hash=_hash(token), prefix=token[:PREFIX_DISPLAY_LEN] + "…")


def hash_agent_token(token: str) -> str:
    return _hash(token)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def random_id(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def slugify(value: str, *, fallback: str = "item") -> str:
    out = []
    prev_dash = False
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug or fallback
