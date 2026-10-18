"""管理员会话（服务端会话 + 安全 Cookie，§7.3）。"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..models import AdminSession, AdminUser
from ..settings import settings

COOKIE_NAME = "sr_session"
CSRF_COOKIE = "sr_csrf"
SLIDE_INTERVAL_S = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class SessionContext:
    session_id: str
    admin_id: str
    username: str
    scope: str
    csrf_hash: str
    expires_at: str
    must_change_password: bool


@dataclass
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: str
    scope: str


def issue_session(
    user: AdminUser,
    *,
    ip: str | None,
    user_agent: str | None,
    scope: str = "normal",
) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(minutes=settings.session_ttl_minutes)
    with db_write() as s:
        s.add(
            AdminSession(
                id=uuid_pk("ses"),
                user_id=user.id,
                token_hash=_hash(token),
                csrf_hash=_hash(csrf_token),
                scope=scope,
                created_at=now_iso(),
                last_seen_at=now_iso(),
                expires_at=_iso(expires),
                ip=ip,
                user_agent=(user_agent or "")[:250],
            )
        )
    return IssuedSession(token=token, csrf_token=csrf_token, expires_at=_iso(expires), scope=scope)


def resolve_session(token: str | None) -> SessionContext | None:
    if not token:
        return None
    with db_write() as s:
        row = s.scalar(select(AdminSession).where(AdminSession.token_hash == _hash(token)))
        if row is None or row.revoked_at:
            return None
        expires = _parse(row.expires_at)
        if expires is None or expires <= _now():
            row.revoked_at = now_iso()
            return None
        # 滑动续期：每 >60s 才写一次，减少写放大
        last_seen = _parse(row.last_seen_at)
        if last_seen is None or (_now() - last_seen).total_seconds() > SLIDE_INTERVAL_S:
            row.last_seen_at = now_iso()
            row.expires_at = _iso(_now() + timedelta(minutes=settings.session_ttl_minutes))
        user = s.get(AdminUser, row.user_id)
        if user is None or user.disabled:
            return None
        return SessionContext(
            session_id=row.id,
            admin_id=user.id,
            username=user.username,
            scope=row.scope,
            csrf_hash=row.csrf_hash,
            expires_at=row.expires_at,
            must_change_password=bool(user.must_change_password) or row.scope == "password_change",
        )


def revoke_session(session_id: str) -> None:
    with db_write() as s:
        row = s.get(AdminSession, session_id)
        if row and not row.revoked_at:
            row.revoked_at = now_iso()


def revoke_other_sessions(user_id: str, keep_session_id: str | None = None) -> int:
    count = 0
    with db_write() as s:
        rows = list(
            s.scalars(
                select(AdminSession).where(
                    AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None)
                )
            )
        )
        for row in rows:
            if keep_session_id and row.id == keep_session_id:
                continue
            row.revoked_at = now_iso()
            count += 1
    return count


def cleanup_expired() -> int:
    removed = 0
    with db_write() as s:
        for row in s.scalars(select(AdminSession).where(AdminSession.revoked_at.is_(None))):
            exp = _parse(row.expires_at)
            if exp and exp <= _now():
                row.revoked_at = now_iso()
                removed += 1
    return removed


def session_count(user_id: str) -> int:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(AdminSession).where(
                    AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None)
                )
            )
        )
        return len(rows)


def verify_csrf(ctx: SessionContext, csrf_token: str | None) -> bool:
    if not csrf_token:
        return False
    return secrets.compare_digest(ctx.csrf_hash, _hash(csrf_token))
