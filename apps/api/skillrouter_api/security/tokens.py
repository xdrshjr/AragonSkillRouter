"""平台访问令牌（§7.3 / §10.1）。

格式 `srk_<keyid>_<secret>`；库中只存 `sha256(secret)` 与前缀，创建时仅一次返回完整令牌。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import db_session, db_write, now_iso
from ..errors import ApiError, Code
from ..models import ExternalAgent, PlatformToken

PREFIX = "srk"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token_material() -> tuple[str, str, str]:
    """返回 (完整令牌, sha256(secret), prefix)。"""
    keyid = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"{PREFIX}_{keyid}_{secret}"
    return full, hash_secret(secret), full[:12]


@dataclass
class TokenContext:
    token_id: str
    agent_id: str
    agent_name: str
    scopes: list[str] = field(default_factory=list)
    allowed_modes: list[str] = field(default_factory=lambda: ["sync"])
    rpm: int | None = None
    concurrency: int | None = None
    daily_calls: int | None = None
    budget_usd: float | None = None
    expires_at: str | None = None

    @property
    def scope_label(self) -> str:
        return f"token:{self.token_id}"


def _parse(raw: str) -> str | None:
    raw = raw.strip()
    if not raw.startswith(f"{PREFIX}_"):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[2]


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt <= datetime.now(timezone.utc)


def verify_token(raw: str | None) -> TokenContext:
    if not raw:
        raise ApiError(Code.TOKEN_INVALID)
    secret = _parse(raw)
    if not secret:
        raise ApiError(Code.TOKEN_INVALID)
    digest = hash_secret(secret)
    with db_write() as s:
        row = s.scalar(select(PlatformToken).where(PlatformToken.token_hash == digest))
        if row is None:
            raise ApiError(Code.TOKEN_INVALID)
        if row.revoked_at:
            raise ApiError(Code.TOKEN_REVOKED)
        if _expired(row.expires_at):
            raise ApiError(Code.TOKEN_EXPIRED)
        agent = s.get(ExternalAgent, row.agent_id)
        if agent is not None and agent.status == "revoked":
            raise ApiError(Code.TOKEN_REVOKED)
        last = row.last_used_at
        row.last_used_at = now_iso()
        if last is None or last != row.last_used_at:
            pass
        if agent is not None:
            agent.last_seen_at = now_iso()
        return TokenContext(
            token_id=row.id,
            agent_id=row.agent_id,
            agent_name=agent.name if agent else row.agent_id,
            scopes=list(row.scopes or []),
            allowed_modes=list(row.allowed_modes or ["sync"]),
            rpm=row.rpm,
            concurrency=row.concurrency,
            daily_calls=row.daily_calls,
            budget_usd=row.budget_usd,
            expires_at=row.expires_at,
        )


def scope_allows(ctx: TokenContext, capability_key: str, tags: list[str] | None = None) -> bool:
    if "*" in ctx.scopes:
        return True
    tags = tags or []
    for scope in ctx.scopes:
        if scope == capability_key:
            return True
        if scope.startswith("tag:") and scope[4:] in tags:
            return True
        if scope.endswith("*") and capability_key.startswith(scope[:-1]):
            return True
    return False


def whoami(ctx: TokenContext) -> dict:
    with db_session() as s:
        row = s.get(PlatformToken, ctx.token_id)
        prefix = row.prefix if row else ""
    return {
        "token_id": ctx.token_id,
        "agent": {"id": ctx.agent_id, "name": ctx.agent_name},
        "prefix": prefix,
        "scopes": ctx.scopes,
        "allowed_modes": ctx.allowed_modes,
        "limits": {
            "rpm": ctx.rpm,
            "concurrency": ctx.concurrency,
            "daily_calls": ctx.daily_calls,
            "budget_usd": ctx.budget_usd,
        },
        "expires_at": ctx.expires_at,
    }
