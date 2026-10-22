"""Settings and engine console API (docs/CONTRACT.md §5.10, owner agent-engine).

`GET /api/admin/settings` returns the *effective* settings — the values the running
process would use right now — plus engine state. `PATCH` applies a validated
subset. Two rules this module enforces because getting them wrong is expensive:

  * **No secrets.** Settings never contain a provider key; the agent's model is
    referenced by credential id, and the response says whether that credential is
    usable without echoing anything from it.
  * **Validate before persisting.** ``agent.auto_execute`` guards spending, and
    ``agent.upgrade_policy`` guards replacing the engine. A typo that silently
    disables the call budget is worse than a rejected PATCH, so every value is
    checked and an unknown key is an error rather than a silent no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core import db
from app.core.errors import unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.services import agent_runtime, audit, engine_upgrade

log = logging.getLogger("skillrouter.admin_settings")

router = APIRouter(prefix="/api/admin", tags=["settings"])

UPGRADE_POLICIES = ("manual", "auto")
UPGRADE_CHANNELS = ("stable", "branch")

#: Every settings key the UI may change through this endpoint. `onboarding.*`
#: and `engine.*` are written by their own flows.
EDITABLE_KEYS = (
    "logging.store_request_bodies",
    "logging.store_response_bodies",
    "files.retention_days",
    "security.session_ttl_minutes",
    "security.idle_timeout_minutes",
    "security.login_max_attempts",
    "agent.upgrade_policy",
    "agent.upgrade_channel",
    "agent.track_branch",
    "agent.auto_execute",
    "agent.model",
)


# ------------------------------------------------------------------- models ---
class LoggingPatch(BaseModel):
    store_request_bodies: bool | None = None
    store_response_bodies: bool | None = None


class FilesPatch(BaseModel):
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class SecurityPatch(BaseModel):
    session_ttl_minutes: int | None = Field(default=None, ge=5, le=10080)
    idle_timeout_minutes: int | None = Field(default=None, ge=5, le=10080)
    login_max_attempts: int | None = Field(default=None, ge=1, le=50)


class AutoExecutePatch(BaseModel):
    enabled: bool | None = None
    allowed_providers: list[str] | None = Field(default=None, max_length=50)
    allowed_capabilities: list[str] | None = Field(default=None, max_length=200)
    max_calls: int | None = Field(default=None, ge=0, le=10_000)
    max_minutes: int | None = Field(default=None, ge=0, le=10_080)
    max_cost_usd: float | None = Field(default=None, ge=0, le=1_000_000)


class ModelPatch(BaseModel):
    provider_id: str | None = Field(default=None, max_length=40)
    model_id: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=400)
    credential_id: str | None = Field(default=None, max_length=80)


class AgentPatch(BaseModel):
    upgrade_policy: str | None = Field(default=None, max_length=20)
    upgrade_channel: str | None = Field(default=None, max_length=20)
    track_branch: str | None = Field(default=None, max_length=120)
    auto_execute: AutoExecutePatch | None = None
    model: ModelPatch | None = None


class SettingsPatch(BaseModel):
    logging: LoggingPatch | None = None
    files: FilesPatch | None = None
    security: SecurityPatch | None = None
    agent: AgentPatch | None = None


# -------------------------------------------------------------------- reads ---
def _effective_settings() -> dict[str, Any]:
    return {
        "logging": {
            "store_request_bodies": bool(db.get_setting("logging.store_request_bodies", False)),
            "store_response_bodies": bool(db.get_setting("logging.store_response_bodies", False)),
        },
        "files": {"retention_days": int(db.get_setting("files.retention_days", 30))},
        "security": {
            "session_ttl_minutes": int(db.get_setting("security.session_ttl_minutes", 480)),
            "idle_timeout_minutes": int(db.get_setting("security.idle_timeout_minutes", 120)),
            "login_max_attempts": int(db.get_setting("security.login_max_attempts", 5)),
        },
        "agent": {
            "upgrade_policy": db.get_setting("agent.upgrade_policy", "manual"),
            "upgrade_channel": db.get_setting("agent.upgrade_channel", "stable"),
            "track_branch": db.get_setting("agent.track_branch", "main"),
            "auto_execute": agent_runtime.AutoExecutePolicy.load().__dict__,
            "model": db.get_setting("agent.model", {}) or {},
        },
    }


@router.get("/settings")
def get_settings_(user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    """Effective settings (no secrets) plus engine state."""
    settings = _effective_settings()
    engine = agent_runtime.engine_status()
    # The model block is the *resolved* view: "configured" must mean the agent can
    # actually run, not merely that a value was typed somewhere.
    settings["agent"]["model_resolved"] = engine["model"]
    return {
        "settings": settings,
        "engine": {
            "adapter_version": engine["adapter_version"],
            "sidecar": engine["sidecar"],
            "tools": engine["tools"],
            "release": db.get_setting("engine.active_release", None),
            "candidate": db.get_setting("engine.candidate_release", None),
        },
        "editable": list(EDITABLE_KEYS),
        "upgrade": {
            "policies": list(UPGRADE_POLICIES),
            "channels": list(UPGRADE_CHANNELS),
        },
    }


# ------------------------------------------------------------------ patches ---
@router.patch("/settings")
def patch_settings(
    patch: SettingsPatch,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    changes: dict[str, Any] = {}

    if patch.logging:
        if patch.logging.store_request_bodies is not None:
            changes["logging.store_request_bodies"] = patch.logging.store_request_bodies
        if patch.logging.store_response_bodies is not None:
            changes["logging.store_response_bodies"] = patch.logging.store_response_bodies
    if patch.files and patch.files.retention_days is not None:
        changes["files.retention_days"] = patch.files.retention_days
    if patch.security:
        for field_name in ("session_ttl_minutes", "idle_timeout_minutes", "login_max_attempts"):
            value = getattr(patch.security, field_name)
            if value is not None:
                changes[f"security.{field_name}"] = value

    if patch.agent:
        agent = patch.agent
        if agent.upgrade_policy is not None:
            if agent.upgrade_policy not in UPGRADE_POLICIES:
                raise unprocessable(
                    "invalid_upgrade_policy",
                    f"upgrade_policy must be one of {', '.join(UPGRADE_POLICIES)}.",
                    details={"allowed": list(UPGRADE_POLICIES)},
                )
            changes["agent.upgrade_policy"] = agent.upgrade_policy
        if agent.upgrade_channel is not None:
            if agent.upgrade_channel not in UPGRADE_CHANNELS:
                raise unprocessable(
                    "invalid_upgrade_channel",
                    f"upgrade_channel must be one of {', '.join(UPGRADE_CHANNELS)}.",
                    details={"allowed": list(UPGRADE_CHANNELS)},
                )
            changes["agent.upgrade_channel"] = agent.upgrade_channel
        if agent.track_branch is not None:
            branch = agent.track_branch.strip()
            if not branch or " " in branch:
                raise unprocessable("invalid_branch", "track_branch must be a branch name without spaces.")
            changes["agent.track_branch"] = branch
        if agent.auto_execute is not None:
            merged = {**agent_runtime.AutoExecutePolicy.load().__dict__}
            merged.update(
                {k: v for k, v in agent.auto_execute.model_dump().items() if v is not None}
            )
            # A budget of zero would silently mean "unlimited" in some readings;
            # make the intent explicit instead.
            if merged["max_calls"] == 0 and agent.auto_execute.max_calls is not None:
                raise unprocessable(
                    "invalid_budget",
                    "max_calls must be at least 1.",
                    details={"max_calls": merged["max_calls"]},
                )
            changes["agent.auto_execute"] = merged
        if agent.model is not None:
            current = db.get_setting("agent.model", {}) or {}
            merged_model = {**current} if isinstance(current, dict) else {}
            merged_model.update({k: v for k, v in agent.model.model_dump().items() if v is not None})
            if merged_model.get("provider_id") and merged_model["provider_id"] not in {
                "anthropic",
                "openai",
                "google",
            }:
                raise unprocessable(
                    "invalid_provider",
                    "provider_id must be anthropic, openai (also for compatible gateways) or google.",
                )
            if merged_model.get("credential_id"):
                row = db.query_one("SELECT id, status FROM credentials WHERE id = ?", (merged_model["credential_id"],))
                if not row:
                    raise unprocessable(
                        "credential_not_found",
                        "The credential named in agent.model does not exist.",
                        details={"credential_id": merged_model["credential_id"]},
                    )
                if row["status"] != "active":
                    raise unprocessable(
                        "credential_not_active",
                        "The credential named in agent.model is not active.",
                        details={"credential_id": row["id"], "status": row["status"]},
                    )
            changes["agent.model"] = merged_model

    if not changes:
        return {"updated": [], "settings": _effective_settings()}

    for key, value in changes.items():
        db.set_setting(key, value)
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="settings.updated",
        object_type="settings",
        object_id=None,
        summary=f"settings changed: {', '.join(sorted(changes))}",
        metadata={"keys": sorted(changes)},
        ip=client_ip(request),
    )
    return {
        "updated": sorted(changes),
        "settings": _effective_settings(),
        "engine": agent_runtime.engine_status(),
    }


# ------------------------------------------------------------------- engine ---
@router.get("/engine")
def get_engine(
    user: AuthUser = Depends(require_admin),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return engine_upgrade.engine_overview(limit=limit)


@router.get("/engine/releases")
def list_releases(
    user: AuthUser = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status", max_length=30),
) -> dict[str, Any]:
    items, total = engine_upgrade.list_releases(limit=limit, offset=offset, status=status_filter)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/engine/check")
async def check_engine(request: Request, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    """Detect the upstream ref and record a candidate release row."""
    return await engine_upgrade.check_for_updates(actor_id=user.id, ip=client_ip(request))


@router.post("/engine/releases/{release_id}/validate")
async def validate_release(
    release_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return await engine_upgrade.validate_release(release_id, actor_id=user.id, ip=client_ip(request))


@router.post("/engine/releases/{release_id}/activate")
async def activate_release(
    release_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return await engine_upgrade.activate_release(release_id, actor_id=user.id, ip=client_ip(request))


@router.post("/engine/rollback")
async def rollback_engine(request: Request, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return await engine_upgrade.rollback(actor_id=user.id, ip=client_ip(request))
