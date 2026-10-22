"""The one-line "connect an agent" command.

The panel used to print a three-step procedure (create a token, keep it in a shell
variable, install, verify). That is four chances to get it wrong, and an agent cannot be
handed a procedure anyway — it needs one command it can run.

This endpoint mints everything the command needs in one round trip:

  * an agent record (reused when one already exists),
  * a token scoped to the published capabilities, expiring by default,
  * the command string with the platform URL and an absolute path to the bundled CLI.

The secret exists only in this response, exactly like a token shown once on the Access
page; it is never logged and never returned again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core import db
from app.core.config import get_settings
from app.core.errors import conflict
from app.deps import AuthUser, client_ip, require_admin
from app.security import tokens as token_security
from app.services import audit
from app.services import bootstrap as bootstrap_service
from app.services.installer import installer_meta

router = APIRouter(prefix="/api/admin/connect", tags=["admin-connect"])

DEFAULT_AGENT_NAME = "Connected agent"


class CommandRequest(BaseModel):
    """Options for the generated command."""

    agent_id: str | None = Field(default=None, description="Reuse this agent instead of the default one.")
    token_id: str | None = Field(
        default=None,
        description=(
            "Build the command for this existing token (its secret is re-issued so it can be placed in the "
            "command). Omit to create a new token."
        ),
    )
    expires_in_days: int = Field(default=30, ge=1, le=365)
    scopes: list[str] | None = Field(default=None, description="Defaults to every published capability.")
    rotate: bool = Field(default=True, description="Revoke the previous command token for this agent.")


def _rotate_existing_token(token_id: str, actor_id: str, ip: str | None) -> dict[str, Any]:
    """Re-issue the secret of an existing token, keeping its agent, scopes and expiry.

    A token's secret is stored as a hash, so an existing token cannot be embedded in a
    command — only a fresh secret can. Rotating keeps the *choice* the user made (this
    agent, this scope) while producing a secret that can actually be copied. Anything
    still using the old secret stops working, which the caller is told explicitly.
    """
    row = db.query_one(
        """SELECT t.*, a.name AS agent_name, a.slug AS agent_slug, a.status AS agent_status
             FROM agent_tokens t JOIN agents a ON a.id = t.agent_id
            WHERE t.id = ?""",
        (token_id,),
    )
    if not row:
        raise conflict("token_missing", "That token no longer exists.", actions=["Pick another token, or create one."])
    if row["status"] != "active":
        raise conflict(
            "token_inactive",
            f"That token is {row['status']} and cannot be used.",
            actions=["Pick an active token, or create a new one."],
        )
    if row["agent_status"] != "active":
        raise conflict(
            "agent_disabled",
            f"The agent behind that token ({row['agent_name']}) is disabled.",
            actions=["Re-enable the agent, then try again."],
        )
    issued = token_security.issue_agent_token()
    db.execute(
        "UPDATE agent_tokens SET token_hash = ?, token_prefix = ? WHERE id = ?",
        (issued.token_hash, issued.prefix, token_id),
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="token.rotate",
        object_type="agent_token",
        object_id=token_id,
        summary=f"secret re-issued to build a connect command ({row['agent_name']} / {row['name']})",
        metadata={"agent_id": row["agent_id"], "scopes": db.loads(row["scopes"], [])},
        ip=ip,
    )
    return {
        "token": issued.token,
        "prefix": issued.prefix,
        "expires_at": row["expires_at"],
        "agent": {"id": row["agent_id"], "name": row["agent_name"], "slug": row["agent_slug"]},
        "scopes": db.loads(row["scopes"], []),
        "token_name": row["name"],
        "rotated": True,
    }


def _slug_for(name: str) -> str:
    base = token_security.slugify(name) or "agent"
    candidate = base
    index = 2
    while db.query_one("SELECT 1 FROM agents WHERE slug = ?", (candidate,)):
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _ensure_agent(agent_id: str | None, actor_id: str, ip: str | None) -> dict[str, Any]:
    if agent_id:
        row = db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not row:
            raise conflict("agent_missing", "That agent no longer exists.", actions=["Pick another agent, or leave it empty."])
        return row
    existing = db.query_one(
        "SELECT * FROM agents WHERE name = ? AND status = 'active' ORDER BY created_at LIMIT 1",
        (DEFAULT_AGENT_NAME,),
    )
    if existing:
        return existing
    now = db.iso()
    new_id = db.new_id("agent")
    db.insert(
        "agents",
        {
            "id": new_id,
            "name": DEFAULT_AGENT_NAME,
            "slug": _slug_for(DEFAULT_AGENT_NAME),
            "description": "Created by the Overview connect command.",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        },
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="agent.created",
        object_type="agent",
        object_id=new_id,
        summary=f"{DEFAULT_AGENT_NAME} created from the Overview connect command",
        ip=ip,
    )
    return db.query_one("SELECT * FROM agents WHERE id = ?", (new_id,)) or {}


def _published_slugs() -> list[str]:
    rows = db.query_all("SELECT slug FROM capabilities WHERE status = 'published' ORDER BY slug")
    return [row["slug"] for row in rows]


def _quote(value: str) -> str:
    """Quote a path for PowerShell, which is where the command is pasted."""
    return '"' + value.replace('"', '`"') + '"'


@router.post("/command")
def create_connect_command(
    payload: CommandRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    meta = installer_meta()
    if not meta.get("available"):
        raise conflict(
            "installer_unavailable",
            "This deployment has no bundled connection CLI, so a command cannot be generated.",
            details={"note": meta.get("note")},
            actions=["Run the desktop app, or install the CLI from packages/installer on the agent's machine."],
        )

    settings = get_settings()
    slugs = _published_slugs()

    # Two paths, both ending in a command that carries a real secret:
    #   * a chosen token  -> its secret is re-issued (keeps agent, scopes, expiry)
    #   * no token chosen -> an agent + token are created
    # A token's secret is only stored as a hash, so "reuse the token" can only mean
    # "re-issue its secret"; the caller is told that in the response.
    if payload.token_id:
        rotated = _rotate_existing_token(payload.token_id, user.id, client_ip(request))
        base_url = settings.public_base_url.rstrip("/")
        command = f'node {_quote(str(meta["cli_path"]))} install-self --url {base_url} --token {rotated["token"]}'
        return {
            "command": command,
            "token": rotated["token"],
            "token_prefix": rotated["prefix"],
            "expires_at": rotated["expires_at"],
            "agent": rotated["agent"],
            "token_id": payload.token_id,
            "token_name": rotated["token_name"],
            "rotated_existing": True,
            "revoked_previous": 0,
            "scopes": rotated["scopes"],
            "platform": base_url,
            "capabilities": {"published": len(slugs), "granted": len(rotated["scopes"])},
            "warning": (
                "This command contains the token's new secret, shown once. The token's previous secret "
                "stopped working the moment this command was created — re-run the command in the agent "
                "that was using it."
            ),
        }

    agent = _ensure_agent(payload.agent_id, user.id, client_ip(request))
    scopes = payload.scopes if payload.scopes is not None else (slugs or ["*"])

    if payload.rotate:
        revoked = db.execute(
            "UPDATE agent_tokens SET status = 'revoked', revoked_at = ? WHERE agent_id = ? AND status = 'active' AND name = ?",
            (db.iso(), agent["id"], "connect-command"),
        )
    else:
        revoked = 0

    issued = token_security.issue_agent_token()
    token_id = db.new_id("tok")
    now = db.iso()
    expires_at = db.iso(db.utcnow() + timedelta(days=payload.expires_in_days))
    db.insert(
        "agent_tokens",
        {
            "id": token_id,
            "agent_id": agent["id"],
            "name": "connect-command",
            "token_prefix": issued.prefix,
            "token_hash": issued.token_hash,
            "scopes": db.dumps(scopes),
            "expires_at": expires_at,
            "rate_limit_per_min": None,
            "max_concurrency": None,
            "budget_usd": None,
            "status": "active",
            "created_at": now,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="token.create",
        object_type="agent_token",
        object_id=token_id,
        summary=f"connect command token for {agent['name']} ({len(scopes)} scope(s), {payload.expires_in_days}d)",
        metadata={"scopes": scopes, "rotated": revoked},
        ip=client_ip(request),
    )

    base_url = settings.public_base_url.rstrip("/")
    command = (
        f'node {_quote(str(meta["cli_path"]))} install-self --url {base_url} --token {issued.token}'
    )
    return {
        "command": command,
        "token": issued.token,
        "token_prefix": issued.prefix,
        "expires_at": expires_at,
        "agent": {"id": agent["id"], "name": agent["name"], "slug": agent["slug"]},
        "token_id": token_id,
        "token_name": "connect-command",
        "rotated_existing": False,
        "scopes": scopes,
        "revoked_previous": revoked,
        "platform": base_url,
        "capabilities": {"published": len(slugs), "granted": len(scopes)},
        "onboarding": bootstrap_service.onboarding_summary()["steps"],
        "warning": (
            "This command contains an agent token. It is shown once and works until it expires; "
            "generate a new one if it leaks."
        ),
    }


def _diagnose_capability(row: dict[str, Any]) -> dict[str, Any]:
    """Why a capability is not visible to an agent, in the terms the fix needs.

    "I added a capability but my agent sees nothing" is the most confusing state in the
    product: the record exists, yet it is invisible. The reasons are precise — the version
    is still a draft, or it has no passing test — so the diagnosis names them and points at
    the one screen that fixes it.
    """
    from app.services.capability_config import merge_defaults, validate_config

    version = db.query_one(
        "SELECT id, version, status, config FROM capability_versions WHERE id = ?",
        (row["current_version_id"],),
    ) if row.get("current_version_id") else None
    if version is None:
        return {
            "slug": row["slug"],
            "name": row["name"],
            "status": row["status"],
            "blockers": ["has no version yet"],
            "next_step": "Open the capability and save a draft version.",
        }

    config = merge_defaults(db.loads(version["config"], {}), row.get("type") or "generic")
    issues = validate_config(config, capability_type=row.get("type") or "generic")
    errors = [issue for issue in issues if issue["severity"] == "error"]
    passing_test = db.query_one(
        "SELECT 1 FROM capability_tests WHERE version_id = ? AND status = 'passed' LIMIT 1",
        (version["id"],),
    )
    blockers: list[str] = []
    if row["status"] != "published" or version["status"] != "published":
        blockers.append(f"version {version['version']} is still {version['status']}")
    if errors:
        fields = ", ".join(sorted({issue["field"] for issue in errors})[:4])
        blockers.append(f"the request/response config is incomplete ({fields})")
    if not passing_test:
        blockers.append("no passing test has been recorded for this version")

    if errors:
        next_step = "Fill the fields the validator lists, then run Test."
    elif not passing_test:
        next_step = "Run Test with a sample input; a passing call is the evidence publish requires."
    else:
        next_step = "Press Publish."

    return {
        "slug": row["slug"],
        "name": row["name"],
        "status": row["status"],
        "version": version["version"],
        "blockers": blockers,
        "next_step": next_step,
        "validation_errors": errors[:8],
    }


@router.get("/status")
def connect_status(user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    """What is still missing before the command can produce a usable skill."""
    published = _published_slugs()
    active_tokens = db.query_scalar("SELECT COUNT(*) FROM agent_tokens WHERE status = 'active'") or 0
    token_rows = db.query_all(
        "SELECT t.*, a.name AS agent_name FROM agent_tokens t JOIN agents a ON a.id = t.agent_id "
        "WHERE t.status = 'active' ORDER BY t.created_at DESC LIMIT 10"
    )
    meta = installer_meta()
    # Capabilities the user has added but that no agent can see yet. Without this the UI can
    # only say "nothing is published", which is true but not actionable.
    drafts = db.query_all(
        "SELECT id, slug, name, type, status, current_version_id FROM capabilities WHERE status != 'published' ORDER BY created_at DESC LIMIT 10"
    )
    return {
        "installer": meta,
        "published_capabilities": published,
        "unpublished": [_diagnose_capability(row) for row in drafts],
        "active_tokens": int(active_tokens),
        "tokens": [
            {
                "id": row["id"],
                "name": row["name"],
                "agent": row["agent_name"],
                "agent_id": row["agent_id"],
                "prefix": row["token_prefix"],
                "scopes": db.loads(row["scopes"], []),
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
            }
            for row in token_rows
        ],
        # The picker preselects this: the token a user most likely means. `None` means
        # "there is none yet, offer to create one".
        "default_token_id": token_rows[0]["id"] if token_rows else None,
        # Blocking is limited to what actually cannot work. Having nothing published is a
        # *warning*, not a blocker: installing the skill first and publishing afterwards is
        # a legitimate order, and the skill can be refreshed with `update` later. Blocking
        # it left users with a grey button and no way forward.
        "ready": bool(meta.get("available")),
        "blockers": [] if meta.get("available") else ["The connection CLI is not available on this machine."],
        "warnings": (
            []
            if published
            else [
                "No capability is published yet, so the installed skill will list nothing to call. "
                "Publish a capability, then run `skill-router-connect update` in the agent to refresh it."
            ]
        ),
    }
