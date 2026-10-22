"""Login throttling.

Counting login attempts in the database (rather than in memory) means a restart
does not reset an attacker's budget. The window is per (username, ip) pair and
a successful login clears the pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.core import db
from app.core.config import get_settings


@dataclass(frozen=True)
class ThrottleState:
    blocked: bool
    failures: int
    max_attempts: int
    retry_after_seconds: int
    message: str = ""


def _window_start(minutes: int) -> str:
    return db.iso(db.utcnow() - timedelta(minutes=minutes))


def check_login_allowed(username: str, ip: str | None) -> ThrottleState:
    settings = get_settings()
    window = _window_start(settings.login_window_minutes)
    row = db.query_one(
        """SELECT COUNT(*) AS failures, MAX(created_at) AS last_at
             FROM login_attempts
            WHERE username = ? AND COALESCE(ip, '') = COALESCE(?, '')
              AND success = 0 AND created_at >= ?""",
        (username, ip, window),
    )
    failures = int(row["failures"]) if row else 0
    if failures < settings.login_max_attempts:
        return ThrottleState(False, failures, settings.login_max_attempts, 0)
    last_at = db.parse_iso(row["last_at"]) if row and row["last_at"] else None
    if last_at is None:
        return ThrottleState(False, failures, settings.login_max_attempts, 0)
    unlock = last_at + timedelta(minutes=settings.login_lockout_minutes)
    remaining = int((unlock - db.utcnow()).total_seconds())
    if remaining <= 0:
        return ThrottleState(False, failures, settings.login_max_attempts, 0)
    return ThrottleState(
        blocked=True,
        failures=failures,
        max_attempts=settings.login_max_attempts,
        retry_after_seconds=remaining,
        message=(
            f"Too many failed sign-in attempts. Try again in {max(1, remaining // 60 + 1)} minute(s), "
            "or reset the admin password from the server CLI."
        ),
    )


def record_attempt(username: str, ip: str | None, *, success: bool) -> None:
    db.execute(
        "INSERT INTO login_attempts (username, ip, success, created_at) VALUES (?, ?, ?, ?)",
        (username, ip, 1 if success else 0, db.iso()),
    )
    if success:
        db.execute(
            "DELETE FROM login_attempts WHERE username = ? AND COALESCE(ip, '') = COALESCE(?, '') AND success = 0",
            (username, ip),
        )


def purge_old_attempts(days: int = 2) -> int:
    return db.execute(
        "DELETE FROM login_attempts WHERE created_at < ?",
        (db.iso(db.utcnow() - timedelta(days=days)),),
    )
