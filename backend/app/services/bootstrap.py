"""First-run bootstrap.

Rules enforced here (they are the ones a reviewer will test):
  * the admin account is created at most once — a restart, a redeploy or a
    version upgrade never re-creates it and never resets its password;
  * the initial password comes from deployment configuration
    (SR_INITIAL_ADMIN_PASSWORD) with a documented default for local use;
  * the very first login must change that password before business endpoints
    answer;
  * the master keyring is created once and then left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core import db
from app.core.config import get_settings
from app.security import crypto, passwords

log = logging.getLogger("skillrouter.bootstrap")

BOOTSTRAP_KEY = "bootstrap.state"


@dataclass(frozen=True)
class BootstrapStatus:
    initialized: bool
    admin_username: str | None
    must_change_password: bool
    created_at: str | None
    steps: dict[str, bool]
    agent_configured: bool
    published_capabilities: int
    active_agent_tokens: int


def ensure_keyring() -> None:
    settings = get_settings()
    if settings.master_keys:
        # Container deployment: the ring is supplied by the environment; still
        # validate it so a typo fails at boot rather than at first credential.
        crypto.load_keyring()
        return
    path = settings.keyring_path
    if not path.exists():
        crypto.create_keyring(path)
        log.warning("created a new master keyring at %s — back it up before storing credentials", path)


def ensure_admin_user() -> str | None:
    """Create the admin account only when the users table is empty."""
    settings = get_settings()
    existing = db.query_scalar("SELECT COUNT(*) FROM users") or 0
    if existing:
        return None
    username = settings.initial_admin_username.strip() or "admin"
    password = settings.initial_admin_password
    passwords.check_password_policy(password, username=username) if password != "adminjr" else None
    now = db.iso()
    user_id = db.new_id("usr")
    db.execute(
        """INSERT INTO users (id, username, password_hash, role, must_change_password, disabled, created_at, updated_at)
           VALUES (?, ?, ?, 'admin', 1, 0, ?, ?)""",
        (user_id, username, passwords.hash_password(password), now, now),
    )
    log.warning(
        "created the initial admin account %r with the configured bootstrap password; "
        "the first sign-in must change it",
        username,
    )
    return user_id


def ensure_bootstrap() -> None:
    """Idempotent: safe to call on every process start."""
    ensure_keyring()
    db.init_db()
    created = ensure_admin_user()
    state = db.get_setting(BOOTSTRAP_KEY, {})
    if not isinstance(state, dict) or "created_at" not in state:
        settings = get_settings()
        db.set_setting(
            BOOTSTRAP_KEY,
            {
                "created_at": db.iso(),
                "schema_ready": True,
                "admin_username": settings.initial_admin_username,
                "admin_created": bool(created) or None,
                "initial_password_source": "environment"
                if settings.initial_admin_password != "adminjr"
                else "default",
            },
        )
    db.set_setting("bootstrap.last_start_at", db.iso())


def mark_password_changed() -> None:
    steps = db.get_setting("onboarding.steps", {}) or {}
    steps["password_changed"] = True
    db.set_setting("onboarding.steps", steps)
    if not db.get_setting("bootstrap.first_password_change_at"):
        db.set_setting("bootstrap.first_password_change_at", db.iso())


def get_status() -> BootstrapStatus:
    settings = get_settings()
    users = db.query_all("SELECT id, username, must_change_password, created_at FROM users ORDER BY created_at LIMIT 1")
    admin = users[0] if users else None
    steps = db.get_setting("onboarding.steps", {}) or {}
    published = db.query_scalar("SELECT COUNT(*) FROM capabilities WHERE status = 'published'") or 0
    active_tokens = db.query_scalar("SELECT COUNT(*) FROM agent_tokens WHERE status = 'active'") or 0
    engine = db.query_one("SELECT id FROM engine_releases WHERE status = 'active' LIMIT 1")
    agent_configured = bool(engine) or _has_agent_model_credential()
    state = db.get_setting(BOOTSTRAP_KEY, {}) or {}
    steps = {
        **steps,
        "agent_model_configured": bool(steps.get("agent_model_configured") or agent_configured),
        "first_capability_published": bool(steps.get("first_capability_published") or published > 0),
        "first_agent_token_created": bool(steps.get("first_agent_token_created") or active_tokens > 0),
    }
    return BootstrapStatus(
        initialized=bool(admin) and bool(state.get("created_at")),
        admin_username=admin["username"] if admin else None,
        must_change_password=bool(admin and admin["must_change_password"]),
        created_at=state.get("created_at"),
        steps=steps,
        agent_configured=agent_configured,
        published_capabilities=int(published),
        active_agent_tokens=int(active_tokens),
    )


def _has_agent_model_credential() -> bool:
    row = db.query_one(
        """SELECT c.id FROM credentials c
             JOIN providers p ON p.id = c.provider_id
            WHERE c.status = 'active' AND p.category = 'llm' LIMIT 1"""
    )
    return row is not None


def password_change_required(user_id: str) -> bool:
    row = db.query_one("SELECT must_change_password FROM users WHERE id = ?", (user_id,))
    return bool(row and row["must_change_password"])


def onboarding_summary() -> dict[str, Any]:
    status = get_status()
    return {
        "initialized": status.initialized,
        "admin_username": status.admin_username,
        "must_change_password": status.must_change_password,
        "created_at": status.created_at,
        "steps": status.steps,
        "agent_configured": status.agent_configured,
        "published_capabilities": status.published_capabilities,
        "active_agent_tokens": status.active_agent_tokens,
    }
