"""Browser sessions and CSRF.

Sessions are server-side rows; the cookie only carries an opaque random token
whose SHA-256 is the primary key. Revocation is therefore immediate and a
stolen database dump does not yield usable session cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from app.core import db
from app.core.config import get_settings


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionRecord:
    id: str
    user_id: str
    csrf_token: str
    created_at: str
    last_seen_at: str
    expires_at: str


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    record: SessionRecord


def create_session(
    user_id: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    ttl_minutes: int | None = None,
) -> IssuedSession:
    settings = get_settings()
    ttl = ttl_minutes or settings.session_ttl_minutes
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    session_id = _hash_token(token)
    now = db.utcnow()
    expires = now + timedelta(minutes=ttl)
    record = SessionRecord(
        id=session_id,
        user_id=user_id,
        csrf_token=csrf,
        created_at=db.iso(now),
        last_seen_at=db.iso(now),
        expires_at=db.iso(expires),
    )
    db.execute(
        """INSERT INTO sessions (id, user_id, csrf_token, created_at, last_seen_at, expires_at, ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, user_id, csrf, record.created_at, record.last_seen_at, record.expires_at, ip, user_agent),
    )
    return IssuedSession(token=token, csrf_token=csrf, record=record)


def get_session(token: str | None) -> SessionRecord | None:
    """Return a live session, enforcing absolute and idle expiry."""
    if not token:
        return None
    session_id = _hash_token(token)
    row = db.query_one(
        "SELECT id, user_id, csrf_token, created_at, last_seen_at, expires_at FROM sessions WHERE id = ?",
        (session_id,),
    )
    if not row:
        return None
    settings = get_settings()
    now = db.utcnow()
    expires_at = db.parse_iso(row["expires_at"])
    last_seen = db.parse_iso(row["last_seen_at"])
    if not expires_at or expires_at <= now:
        delete_session(token)
        return None
    idle_limit = settings.session_idle_timeout_minutes
    if idle_limit and last_seen and (now - last_seen) > timedelta(minutes=idle_limit):
        delete_session(token)
        return None
    # Touching the row on every request turns every authenticated GET into a
    # write, and with a single SQLite writer that is the dominant cost under
    # concurrency. One update per 30s is plenty for idle expiry.
    if last_seen is None or (now - last_seen) > timedelta(seconds=30):
        db.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (db.iso(now), session_id))
    return SessionRecord(
        id=row["id"],
        user_id=row["user_id"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
    )


def delete_session(token: str | None) -> None:
    if not token:
        return
    db.execute("DELETE FROM sessions WHERE id = ?", (_hash_token(token),))


def delete_user_sessions(user_id: str, *, keep_session_id: str | None = None) -> int:
    if keep_session_id:
        return db.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, keep_session_id))
    return db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions() -> int:
    return db.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.iso(),))


def verify_csrf(session: SessionRecord | None, provided: str | None) -> bool:
    if not session or not provided:
        return False
    return hmac.compare_digest(session.csrf_token, provided)


def rotate_csrf(session_id: str) -> str:
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE sessions SET csrf_token = ? WHERE id = ?", (token, session_id))
    return token
