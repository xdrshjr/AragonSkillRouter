"""Capability lifecycle: create, version, validate, prove with a test, publish.

This module is the only place the admin API learns *what is true* about a
capability. The contract (docs/CONTRACT.md §4) forbids conflating four facts, so
every serialised capability carries them separately:

    saved       a version row with a config exists in the database;
    last test   the outcome of the most recent real call (capability_tests);
    published   which version is immutable and live (capability.current_version_id);
    callable    an external caller would succeed right now — published version,
                active credential and at least one usable route (or the
                capability's own implicit provider/credential pair).

`callable` is computed from the database only. It never performs a network call,
so it can never turn into a false claim of connectivity.

Published versions are immutable: editing, testing-config or republishing them is
refused with `409 version_immutable`. The way forward is always a new version
(`new_version`) or `rollback`, which copies an old config into a fresh version.
"""

from __future__ import annotations

import json
from typing import Any

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.deps import AgentPrincipal
from app.security.redact import preview
from app.security.tokens import slugify
from app.services import audit
from app.services.capability_config import (
    CAPABILITY_TYPES,
    config_checksum,
    default_config,
    has_blocking_issues,
    merge_defaults,
    validate_config,
)

CAPABILITY_STATUSES = {"draft", "testing", "published", "disabled"}
MUTABLE_VERSION_STATUSES = {"draft", "testing"}
TEST_KIND = "sample_call"


# --------------------------------------------------------------------- slugs --
def _slug_taken(slug: str, *, exclude_id: str | None = None) -> bool:
    if exclude_id:
        return bool(
            db.query_scalar("SELECT 1 FROM capabilities WHERE slug = ? AND id != ?", (slug, exclude_id))
        )
    return bool(db.query_scalar("SELECT 1 FROM capabilities WHERE slug = ?", (slug,)))


def unique_slug(name: str, requested: str | None = None, *, exclude_id: str | None = None) -> str:
    """Slug from the name, `-2`, `-3`, … on collision."""
    base = slugify(requested or name)
    if not _slug_taken(base, exclude_id=exclude_id):
        return base
    counter = 2
    while _slug_taken(f"{base}-{counter}", exclude_id=exclude_id):
        counter += 1
    return f"{base}-{counter}"


# -------------------------------------------------------------------- lookup --
def get_row(capability_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT c.*, p.name AS provider_name, p.status AS provider_status,
                  cr.name AS credential_name, cr.status AS credential_status
             FROM capabilities c
             LEFT JOIN providers p ON p.id = c.provider_id
             LEFT JOIN credentials cr ON cr.id = c.credential_id
            WHERE c.id = ?""",
        (capability_id,),
    )
    if not row:
        raise not_found("Capability")
    return row


def get_version_row(capability_id: str, version_id: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT * FROM capability_versions WHERE id = ? AND capability_id = ?", (version_id, capability_id)
    )
    if not row:
        raise not_found("Capability version")
    return row


def list_versions(capability_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM capability_versions WHERE capability_id = ? ORDER BY version DESC", (capability_id,)
    )
    return [serialize_version(row) for row in rows]


def current_version_row(capability: dict[str, Any]) -> dict[str, Any] | None:
    if capability.get("current_version_id"):
        row = db.query_one("SELECT * FROM capability_versions WHERE id = ?", (capability["current_version_id"],))
        if row:
            return row
    return db.query_one(
        "SELECT * FROM capability_versions WHERE capability_id = ? ORDER BY version DESC LIMIT 1",
        (capability["id"],),
    )


def published_version_row(capability_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM capability_versions WHERE capability_id = ? AND status = 'published' "
        "ORDER BY version DESC LIMIT 1",
        (capability_id,),
    )


def config_of(version: dict[str, Any] | None, capability_type: str) -> dict[str, Any]:
    if not version:
        return default_config(capability_type)
    return merge_defaults(db.loads(version.get("config"), {}), capability_type)


def list_capabilities(
    *,
    type: str | None = None,  # noqa: A002 - matches the query parameter name
    status: str | None = None,
    provider_id: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if type:
        where.append("c.type = ?")
        params.append(type)
    if status:
        where.append("c.status = ?")
        params.append(status)
    if provider_id:
        where.append("c.provider_id = ?")
        params.append(provider_id)
    if search:
        where.append("(c.name LIKE ? OR c.slug LIKE ? OR c.description LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM capabilities c WHERE {clause}", params) or 0
    rows = db.query_all(
        f"""SELECT c.*, p.name AS provider_name, p.status AS provider_status,
                   cr.name AS credential_name, cr.status AS credential_status
              FROM capabilities c
              LEFT JOIN providers p ON p.id = c.provider_id
              LEFT JOIN credentials cr ON cr.id = c.credential_id
             WHERE {clause}
             ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    return [serialize_capability(row) for row in rows], int(total)


# ----------------------------------------------------------------- redaction --
_SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}


def redacted_config(config: Any) -> Any:
    """Copy a config with credential-carrying header values blanked out."""
    clone = json.loads(json.dumps(config or {}, default=str))
    headers = ((clone.get("request") or {}).get("headers") or {})
    if isinstance(headers, dict):
        for name in list(headers.keys()):
            if str(name).lower() in _SENSITIVE_HEADERS:
                headers[name] = "[REDACTED]"
    return clone


# ------------------------------------------------------------------ counts ---
def counts_for(capability: dict[str, Any]) -> dict[str, int]:
    capability_id = capability["id"]
    versions = db.query_scalar("SELECT COUNT(*) FROM capability_versions WHERE capability_id = ?", (capability_id,)) or 0
    routes = db.query_scalar("SELECT COUNT(*) FROM routes WHERE capability_id = ?", (capability_id,)) or 0
    tests = db.query_scalar("SELECT COUNT(*) FROM capability_tests WHERE capability_id = ?", (capability_id,)) or 0
    skills = db.query_scalar(
        "SELECT COUNT(*) FROM skill_versions WHERE manifest LIKE ?", (f'%"{capability["slug"]}"%',)
    ) or 0
    return {
        "versions": int(versions),
        "routes": int(routes),
        "tests": int(tests),
        "skill_versions": int(skills),
    }


# ----------------------------------------------------------------- evidence ---
def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def serialize_test(row: dict[str, Any], *, include_bodies: bool = False) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "capability_id": row["capability_id"],
        "version_id": row.get("version_id"),
        "version": row.get("version"),
        "kind": row["kind"],
        "status": row["status"],
        "http_status": row.get("http_status"),
        "latency_ms": row.get("latency_ms"),
        "error": _maybe_json(row.get("error")),
        "actor": row.get("actor"),
        "created_at": row["created_at"],
    }
    if include_bodies:
        payload["request"] = _maybe_json(row.get("request"))
        payload["response"] = _maybe_json(row.get("response"))
    return payload


def latest_test(*, capability_id: str | None = None, version_id: str | None = None) -> dict[str, Any] | None:
    if version_id:
        return db.query_one(
            "SELECT * FROM capability_tests WHERE version_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (version_id,),
        )
    if capability_id:
        return db.query_one(
            "SELECT * FROM capability_tests WHERE capability_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (capability_id,),
        )
    return None


def list_tests(capability_id: str, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    total = db.query_scalar("SELECT COUNT(*) FROM capability_tests WHERE capability_id = ?", (capability_id,)) or 0
    rows = db.query_all(
        """SELECT t.*, v.version AS version FROM capability_tests t
             LEFT JOIN capability_versions v ON v.id = t.version_id
            WHERE t.capability_id = ?
            ORDER BY t.created_at DESC, t.rowid DESC LIMIT ? OFFSET ?""",
        (capability_id, limit, offset),
    )
    return [serialize_test(row, include_bodies=False) for row in rows], int(total)


def evidence_for_publish(version_id: str, *, inherited_from: str | None = None) -> dict[str, Any]:
    """The newest test for the version must be `passed`. Raises otherwise."""
    evidence_version = inherited_from or version_id
    row = latest_test(version_id=evidence_version)
    if row and row["status"] == "passed":
        return {
            "test_id": row["id"],
            "status": row["status"],
            "tested_at": row["created_at"],
            "version_id": row["version_id"],
            "inherited_from_version_id": inherited_from if inherited_from else None,
            "http_status": row.get("http_status"),
            "latency_ms": row.get("latency_ms"),
        }
    last_status = row["status"] if row else "none"
    raise conflict(
        "test_evidence_missing",
        "This version has no passing test, so it cannot be published.",
        details={
            "version_id": version_id,
            "last_test_status": last_status,
            "last_test_at": row["created_at"] if row else None,
            "required_status": "passed",
        },
        actions=[
            "Run a real call: POST /api/admin/capabilities/{capability_id}/versions/{version_id}/test "
            "with a sample input.",
            "Fix the endpoint or credential if the test failed, then test again.",
            "Only a successful real call is accepted as publish evidence.",
        ],
    )


def record_test(
    *,
    capability_id: str,
    version_id: str,
    result: dict[str, Any],
    actor_id: str | None,
) -> dict[str, Any]:
    """Persist test evidence when the executor did not already do it.

    `execution.test_version` inserts a row itself for a draft/testing version (it
    returns `test_id`). For an already-published version it routes through the
    real executor, which only writes an invocation — so we record the evidence
    here from the truthful result instead of inventing a status.
    """
    status_map = {"succeeded": "passed", "failed": "failed", "uncertain": "blocked", "preview": "blocked"}
    if result.get("test_id"):
        row = db.query_one("SELECT * FROM capability_tests WHERE id = ?", (result["test_id"],))
        if row:
            _sync_capability_after_test(capability_id, status=row["status"])
            return serialize_test(row, include_bodies=True)

    raw_status = str(result.get("status") or "failed")
    status = status_map.get(raw_status, "failed")
    record_id = db.new_id("captest")
    request_snapshot = result.get("request") or {}
    response_snapshot = {
        "status": result.get("http_status"),
        "result": result.get("result"),
        "error": result.get("error"),
    }
    db.insert(
        "capability_tests",
        {
            "id": record_id,
            "capability_id": capability_id,
            "version_id": version_id,
            "kind": TEST_KIND,
            "status": status,
            "request": preview(request_snapshot, limit=4000),
            "response": preview(response_snapshot, limit=6000),
            "latency_ms": result.get("latency_ms"),
            "http_status": result.get("http_status"),
            "error": preview(result.get("error"), limit=800) if result.get("error") else None,
            "actor": actor_id,
            "created_at": db.iso(),
        },
    )
    _sync_capability_after_test(capability_id, status=status)
    row = db.query_one("SELECT * FROM capability_tests WHERE id = ?", (record_id,))
    return serialize_test(row or {}, include_bodies=True)


def _sync_capability_after_test(capability_id: str, *, status: str) -> None:
    """A passed test moves a draft capability to `testing` — still not published."""
    capability = db.query_one("SELECT id, status FROM capabilities WHERE id = ?", (capability_id,))
    if not capability:
        return
    if status == "passed" and capability["status"] == "draft":
        db.execute(
            "UPDATE capabilities SET status = 'testing', updated_at = ? WHERE id = ?", (db.iso(), capability_id)
        )


# --------------------------------------------------------------- callable ----
def _route_rows(capability_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        """SELECT r.*, v.version AS version, v.status AS version_status,
                  p.name AS provider_name, p.status AS provider_status,
                  c.name AS credential_name, c.status AS credential_status
             FROM routes r
             JOIN capability_versions v ON v.id = r.capability_version_id
             LEFT JOIN providers p ON p.id = r.provider_id
             LEFT JOIN credentials c ON c.id = r.credential_id
            WHERE r.capability_id = ?
            ORDER BY r.priority ASC, r.weight DESC, r.created_at ASC""",
        (capability_id,),
    )


def _probe_payload(config: dict[str, Any]) -> dict[str, Any]:
    """A minimal input that satisfies the declared *required* fields.

    Used only to get past routing's required-field gate; it is never sent
    anywhere. Without it a capability with required inputs would look
    uncallable even when a route is perfectly healthy.
    """
    schema = config.get("input_schema") or {}
    properties = schema.get("properties") or {}
    placeholders = {
        "string": "probe",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "object": {},
        "array": [],
    }
    payload: dict[str, Any] = {}
    for name in schema.get("required") or []:
        spec = properties.get(name) or {}
        payload[name] = placeholders.get(spec.get("type") if isinstance(spec, dict) else None, "probe")
    return payload


def _routing_check(
    capability: dict[str, Any], version: dict[str, Any], config: dict[str, Any]
) -> tuple[bool, str, str | None, list[str]]:
    """Ask the real selector, so `callable` can never disagree with invoke.

    `routing.candidates_for` is pure database work (it only reads routes,
    versions, providers and credentials), so this stays network-free.
    """
    from app.services import routing  # local import keeps the module graph flat

    modes = config.get("modes") or {}
    payload = _probe_payload(config)
    tried: list[str] = []
    for mode in ("sync", "async", "stream"):
        if not modes.get(mode):
            continue
        tried.append(mode)
        try:
            selection = routing.candidates_for(
                capability=capability, version=version, input_payload=payload, mode=mode
            )
        except Exception:  # noqa: BLE001 - a broken config must not break serialisation
            continue
        if selection.candidates:
            chosen = selection.chosen
            return True, selection.reason, (chosen.route_id if chosen else None), tried
        if selection.rejected:
            return (
                False,
                "; ".join(f"{item.get('route')}: {item.get('reason')}" for item in selection.rejected),
                None,
                tried,
            )
    return False, "no supported mode and no route could be selected", None, tried


def callable_state(capability: dict[str, Any], version: dict[str, Any] | None = None) -> dict[str, Any]:
    """Would an external call succeed? Database facts only — no network."""
    version = version if version is not None else current_version_row(capability)
    config = config_of(version, capability.get("type", "generic"))
    auth_mode = (config.get("auth") or {}).get("mode", "credential")
    credential_required = auth_mode != "none"

    wants_credential = bool(capability.get("credential_id"))
    wants_provider = bool(capability.get("provider_id"))
    provider_ok = (not wants_provider) or capability.get("provider_status") in (None, "active")
    credential_ok = (not credential_required) or (
        wants_credential and capability.get("credential_status") == "active"
    )

    published = version is not None and version.get("status") == "published"
    if published:
        route_ok, route_reason, route_id, modes_tried = _routing_check(capability, version, config)
    else:
        enabled = [row for row in _route_rows(capability["id"]) if row["enabled"]]
        route_ok, route_reason, route_id = False, "no published version to select a route for", None
        modes_tried = []

    callable_now = published and route_ok and provider_ok and credential_ok

    if not version:
        reason = "No version exists yet."
    elif not published:
        reason = f"Version {version.get('version')} is {version.get('status')}, so it is not published."
    elif not credential_ok:
        reason = (
            f"Published version {version.get('version')}, but the capability has no active credential, "
            "so an authenticated call would fail."
        )
    elif not provider_ok:
        reason = f"Published version {version.get('version')}, but the provider is disabled."
    elif not route_ok:
        reason = f"Published version {version.get('version')}, but no usable route: {route_reason}."
    else:
        reason = f"Published version {version.get('version')} is callable ({route_reason})."

    return {
        "callable": callable_now,
        "reason": reason,
        "checks": {
            "published": published,
            "credential_required": credential_required,
            "credential_active": credential_ok,
            "provider_active": provider_ok,
            "route_usable": route_ok,
            "route_detail": route_reason,
            "route_id": route_id,
            "modes_checked": modes_tried,
        },
    }


# ------------------------------------------------------------- serialisation --
def verification_summary(capability: dict[str, Any]) -> dict[str, Any]:
    current = current_version_row(capability)
    published = published_version_row(capability["id"])
    last_test = latest_test(capability_id=capability["id"])
    state = callable_state(capability, published or current)
    return {
        "saved": current is not None,
        "saved_version": current["version"] if current else None,
        "saved_version_status": current["status"] if current else None,
        "last_test": serialize_test(last_test) if last_test else None,
        "last_test_status": last_test["status"] if last_test else None,
        "test_passed": bool(last_test and last_test["status"] == "passed"),
        "published_version": published["version"] if published else None,
        "published_version_id": published["id"] if published else None,
        "published_at": published["published_at"] if published else None,
        "callable": state["callable"],
        "callable_reason": state["reason"],
        "checks": state["checks"],
    }


def serialize_capability(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """Public capability shape. Carries the four distinct facts (§4) explicitly."""
    current = current_version_row(row)
    published = published_version_row(row["id"])
    verification = verification_summary(row)
    payload: dict[str, Any] = {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "type": row["type"],
        "description": row.get("description") or "",
        "provider_id": row.get("provider_id"),
        "provider_name": row.get("provider_name"),
        "credential_id": row.get("credential_id"),
        "credential_name": row.get("credential_name"),
        "status": row["status"],
        "needs_live_agent": bool(row.get("needs_live_agent")),
        "current_version_id": row.get("current_version_id"),
        "current_version": current["version"] if current else None,
        "current_version_status": current["status"] if current else None,
        "needs_live_agent_note": (
            "Steps/invocations of this capability need the live agent engine."
            if row.get("needs_live_agent")
            else None
        ),
        # the four facts, flat, so no client can conflate them
        "saved": verification["saved"],
        "last_test_status": verification["last_test_status"],
        "last_test": verification["last_test"],
        "published_version": verification["published_version"],
        "published_version_id": verification["published_version_id"],
        "callable": verification["callable"],
        "verification": verification,
        "counts": counts_for(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if detail:
        payload["versions"] = list_versions(row["id"])
        payload["routes"] = [serialize_route(item) for item in _route_rows(row["id"])]
        payload["issues"] = (
            validate_config(db.loads(current["config"]), capability_type=row["type"]) if current else []
        )
    return payload


def serialize_version(row: dict[str, Any], *, include_config: bool = False) -> dict[str, Any]:
    if not row:
        return {}
    test = latest_test(version_id=row["id"])
    payload: dict[str, Any] = {
        "id": row["id"],
        "capability_id": row["capability_id"],
        "version": row["version"],
        "status": row["status"],
        "notes": row.get("notes"),
        "checksum": row.get("checksum") or "",
        "created_by": row.get("created_by"),
        "created_at": row["created_at"],
        "published_at": row.get("published_at"),
        "editable": row["status"] in MUTABLE_VERSION_STATUSES,
        "last_test_status": test["status"] if test else None,
        "last_test_at": test["created_at"] if test else None,
    }
    if include_config:
        payload["config"] = redacted_config(db.loads(row.get("config"), {}))
    return payload


def serialize_route(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "capability_id": row["capability_id"],
        "capability_version_id": row["capability_version_id"],
        "version": row.get("version"),
        "version_status": row.get("version_status"),
        "provider_id": row.get("provider_id"),
        "provider_name": row.get("provider_name"),
        "provider_status": row.get("provider_status"),
        "credential_id": row.get("credential_id"),
        "credential_name": row.get("credential_name"),
        "credential_status": row.get("credential_status"),
        "strategy": row["strategy"],
        "priority": row["priority"],
        "weight": row["weight"],
        "enabled": bool(row["enabled"]),
        "health_status": row.get("health_status") or "unknown",
        "last_health_at": row.get("last_health_at"),
        "conditions": db.loads(row.get("conditions"), {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def routes_for(capability_id: str) -> list[dict[str, Any]]:
    return [serialize_route(row) for row in _route_rows(capability_id)]


# ------------------------------------------------------------------ mutation --
def _resolve_provider_credential(
    provider_id: str | None, credential_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    provider = None
    credential = None
    if provider_id:
        provider = db.query_one("SELECT id, name, status FROM providers WHERE id = ?", (provider_id,))
        if not provider:
            raise not_found("Provider")
    if credential_id:
        credential = db.query_one(
            "SELECT id, name, provider_id, status FROM credentials WHERE id = ?", (credential_id,)
        )
        if not credential:
            raise not_found("Credential")
        if provider_id and credential["provider_id"] != provider_id:
            raise unprocessable(
                "credential_provider_mismatch",
                "That credential belongs to a different provider.",
                details={"credential_id": credential_id, "credential_provider_id": credential["provider_id"]},
                actions=["Pick a credential created for the selected provider."],
            )
    return provider, credential


def _check_type(capability_type: str) -> str:
    if capability_type not in CAPABILITY_TYPES:
        raise unprocessable(
            "invalid_capability_type",
            f"{capability_type!r} is not a supported capability type.",
            details={"supported": sorted(CAPABILITY_TYPES)},
            actions=[f"Use one of: {', '.join(sorted(CAPABILITY_TYPES))}."],
        )
    return capability_type


def create_capability(
    *,
    name: str,
    type: str = "generic",  # noqa: A002 - contract field name
    description: str = "",
    slug: str | None = None,
    provider_id: str | None = None,
    credential_id: str | None = None,
    needs_live_agent: bool = False,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    _check_type(type)
    _resolve_provider_credential(provider_id, credential_id)
    now = db.iso()
    capability_id = db.new_id("cap")
    final_slug = unique_slug(name, slug)
    db.insert(
        "capabilities",
        {
            "id": capability_id,
            "slug": final_slug,
            "name": name.strip(),
            "type": type,
            "description": (description or "").strip(),
            "provider_id": provider_id,
            "credential_id": credential_id,
            "status": "draft",
            "current_version_id": None,
            "needs_live_agent": 1 if needs_live_agent else 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    config = default_config(type)
    version_id = db.new_id("capver")
    db.insert(
        "capability_versions",
        {
            "id": version_id,
            "capability_id": capability_id,
            "version": 1,
            "status": "draft",
            "config": db.dumps(config),
            "notes": "Initial draft created with the type defaults.",
            "checksum": config_checksum(config),
            "created_by": actor_id,
            "created_at": now,
        },
    )
    db.execute(
        "UPDATE capabilities SET current_version_id = ?, updated_at = ? WHERE id = ?",
        (version_id, now, capability_id),
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.created",
        object_type="capability",
        object_id=capability_id,
        summary=f"{final_slug} created with draft v1 ({type})",
        metadata={"type": type, "slug": final_slug, "version_id": version_id, "provider_id": provider_id},
        ip=ip,
    )
    return serialize_capability(get_row(capability_id), detail=True)


def update_capability(
    capability_id: str,
    changes: dict[str, Any],
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    row = get_row(capability_id)
    values: dict[str, Any] = {}
    if "name" in changes and changes["name"] is not None:
        values["name"] = str(changes["name"]).strip()
    if "description" in changes and changes["description"] is not None:
        values["description"] = str(changes["description"]).strip()
    if "type" in changes and changes["type"] is not None:
        _check_type(str(changes["type"]))
        values["type"] = str(changes["type"])
    if "needs_live_agent" in changes and changes["needs_live_agent"] is not None:
        values["needs_live_agent"] = 1 if changes["needs_live_agent"] else 0
    if "provider_id" in changes or "credential_id" in changes:
        provider_id = changes.get("provider_id", row.get("provider_id"))
        credential_id = changes.get("credential_id", row.get("credential_id"))
        _resolve_provider_credential(provider_id, credential_id)
        values["provider_id"] = provider_id
        values["credential_id"] = credential_id
    if values:
        values["updated_at"] = db.iso()
        db.update("capabilities", capability_id, values)
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.updated",
        object_type="capability",
        object_id=capability_id,
        summary=f"{row['slug']} updated ({', '.join(sorted(values)) or 'no changes'})",
        metadata={"fields_changed": sorted(values.keys())},
        ip=ip,
    )
    return serialize_capability(get_row(capability_id), detail=True)


def _immutable_error(version: dict[str, Any], capability_slug: str) -> ApiError:
    return conflict(
        "version_immutable",
        f"Version {version['version']} is {version['status']} and can no longer be edited.",
        details={
            "version_id": version["id"],
            "version": version["version"],
            "status": version["status"],
            "editable_statuses": sorted(MUTABLE_VERSION_STATUSES),
        },
        actions=[
            f"Create a new draft version: POST /api/admin/capabilities/{{capability_id}}/versions with "
            f"from_version_id={version['id']}.",
            f"Or roll back to an older version for {capability_slug}.",
        ],
    )


def new_version(
    capability_id: str,
    *,
    from_version_id: str | None = None,
    config: dict[str, Any] | None = None,
    notes: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    capability = get_row(capability_id)
    source: dict[str, Any] | None = None
    if from_version_id:
        source = get_version_row(capability_id, from_version_id)
    else:
        source = current_version_row(capability)
    if config is not None and not isinstance(config, dict):
        raise unprocessable("invalid_config", "The version config must be a JSON object.")
    if config is None:
        next_config = db.loads(source["config"], {}) if source else default_config(capability["type"])
    else:
        next_config = config
    next_number = int(
        db.query_scalar("SELECT COALESCE(MAX(version), 0) FROM capability_versions WHERE capability_id = ?", (capability_id,))
        or 0
    ) + 1
    version_id = db.new_id("capver")
    now = db.iso()
    db.insert(
        "capability_versions",
        {
            "id": version_id,
            "capability_id": capability_id,
            "version": next_number,
            "status": "draft",
            "config": db.dumps(next_config),
            "notes": notes or (f"Copied from v{source['version']}." if source and config is None else "New draft."),
            "checksum": config_checksum(next_config),
            "created_by": actor_id,
            "created_at": now,
        },
    )
    if not capability.get("current_version_id") or capability["status"] != "published":
        db.execute(
            "UPDATE capabilities SET current_version_id = COALESCE(current_version_id, ?), updated_at = ? WHERE id = ?",
            (version_id, now, capability_id),
        )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.version_created",
        object_type="capability_version",
        object_id=version_id,
        summary=f"{capability['slug']} v{next_number} created",
        metadata={
            "capability_id": capability_id,
            "from_version_id": source["id"] if source else None,
            "checksum": config_checksum(next_config),
        },
        ip=ip,
    )
    return serialize_version(get_version_row(capability_id, version_id), include_config=True)


def update_version(
    capability_id: str,
    version_id: str,
    *,
    config: dict[str, Any] | None = None,
    notes: str | None = None,
    status: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    capability = get_row(capability_id)
    version = get_version_row(capability_id, version_id)
    if version["status"] not in MUTABLE_VERSION_STATUSES:
        raise _immutable_error(version, capability["slug"])
    values: dict[str, Any] = {}
    if config is not None:
        if not isinstance(config, dict):
            raise unprocessable("invalid_config", "The version config must be a JSON object.")
        values["config"] = db.dumps(config)
        values["checksum"] = config_checksum(config)
    if notes is not None:
        values["notes"] = notes
    if status is not None:
        if status not in {"draft", "testing"}:
            raise unprocessable(
                "invalid_version_status",
                "A version can only be moved to 'testing' by hand; publishing has its own endpoint.",
                details={"allowed": ["draft", "testing"]},
            )
        values["status"] = status
    if values:
        db.update("capability_versions", version_id, values)
        if status == "testing" and capability["status"] == "draft":
            db.execute(
                "UPDATE capabilities SET status = 'testing', updated_at = ? WHERE id = ?",
                (db.iso(), capability_id),
            )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.version_updated",
        object_type="capability_version",
        object_id=version_id,
        summary=f"{capability['slug']} v{version['version']} updated",
        metadata={"fields_changed": sorted(values.keys()), "capability_id": capability_id},
        ip=ip,
    )
    return serialize_version(get_version_row(capability_id, version_id), include_config=True)


def validate(capability_id: str, version_id: str) -> dict[str, Any]:
    capability = get_row(capability_id)
    version = get_version_row(capability_id, version_id)
    config = db.loads(version["config"], {})
    issues = validate_config(config, capability_type=capability["type"])
    return {
        "capability_id": capability_id,
        "version_id": version_id,
        "version": version["version"],
        "status": version["status"],
        "checksum": version.get("checksum") or config_checksum(config),
        "issues": issues,
        "blocking": has_blocking_issues(issues),
        "errors": sum(1 for issue in issues if issue["severity"] == "error"),
        "warnings": sum(1 for issue in issues if issue["severity"] == "warning"),
    }


def publish(
    capability_id: str,
    version_id: str,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
    inherited_evidence_from: str | None = None,
) -> dict[str, Any]:
    capability = get_row(capability_id)
    version = get_version_row(capability_id, version_id)
    if version["status"] == "published" and capability.get("current_version_id") == version_id:
        raise conflict(
            "already_published",
            f"Version {version['version']} is already the published version.",
            details={"version_id": version_id, "version": version["version"]},
            actions=["Create a new version if you need to change anything."],
        )
    if version["status"] == "deprecated":
        raise conflict(
            "version_deprecated",
            f"Version {version['version']} was deprecated; publish a new version instead.",
            details={"version_id": version_id},
            actions=["Use POST /api/admin/capabilities/{capability_id}/rollback to re-publish that config."],
        )
    config = db.loads(version["config"], {})
    issues = validate_config(config, capability_type=capability["type"])
    if has_blocking_issues(issues):
        raise unprocessable(
            "validation_failed",
            "The version has blocking validation issues and cannot be published.",
            details={"version_id": version_id, "issues": issues},
            actions=[
                "Fix the reported fields on the version config.",
                "POST .../versions/{version_id}/validate to re-check before publishing.",
            ],
        )
    evidence = evidence_for_publish(version_id, inherited_from=inherited_evidence_from)
    now = db.iso()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE capability_versions SET status = 'published', published_at = COALESCE(published_at, ?) WHERE id = ?",
            (now, version_id),
        )
        conn.execute(
            "UPDATE capabilities SET status = 'published', current_version_id = ?, updated_at = ? WHERE id = ?",
            (version_id, now, capability_id),
        )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.published",
        object_type="capability",
        object_id=capability_id,
        summary=f"{capability['slug']} v{version['version']} published",
        metadata={
            "version_id": version_id,
            "version": version["version"],
            "test_id": evidence["test_id"],
            "inherited_evidence_from": evidence.get("inherited_from_version_id"),
            "checksum": version.get("checksum"),
        },
        ip=ip,
    )
    payload = serialize_capability(get_row(capability_id), detail=True)
    payload["published_version"] = version["version"]
    payload["evidence"] = evidence
    if not payload["callable"]:
        payload["actions"] = [
            "Publish is done, but calls will fail until routing is usable: "
            + payload["verification"]["callable_reason"],
            "Add a route: POST /api/admin/capabilities/{capability_id}/routes with this version, "
            "a provider and an active credential.",
            "Check the provider is enabled and the credential is active.",
        ]
    return payload


def deprecate(
    capability_id: str,
    version_id: str,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    capability = get_row(capability_id)
    version = get_version_row(capability_id, version_id)
    if version["status"] == "deprecated":
        raise conflict(
            "already_deprecated",
            f"Version {version['version']} is already deprecated.",
            details={"version_id": version_id},
        )
    now = db.iso()
    with db.transaction() as conn:
        conn.execute("UPDATE capability_versions SET status = 'deprecated' WHERE id = ?", (version_id,))
        if capability.get("current_version_id") == version_id:
            conn.execute(
                "UPDATE capabilities SET current_version_id = NULL, status = 'disabled', updated_at = ? WHERE id = ?",
                (now, capability_id),
            )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.deprecated",
        object_type="capability_version",
        object_id=version_id,
        summary=f"{capability['slug']} v{version['version']} deprecated",
        metadata={"capability_id": capability_id, "version": version["version"]},
        ip=ip,
    )
    payload = serialize_capability(get_row(capability_id), detail=True)
    payload["deprecated"] = {"version_id": version_id, "version": version["version"]}
    if capability.get("current_version_id") == version_id:
        payload["actions"] = [
            "Publish another version to make the capability callable again.",
            "Routes pointing at this version will no longer be selected.",
        ]
    return payload


def rollback(
    capability_id: str,
    version_id: str,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Re-publish an older config as a *new* version.

    The config is identical to the source version, so the source's passing test
    evidence is inherited (the checksum is the same document). This is the only
    place a publish is accepted without a fresh test, and it is recorded on the
    audit row and in the response.
    """
    capability = get_row(capability_id)
    source = get_version_row(capability_id, version_id)
    if source["status"] not in {"published", "deprecated"}:
        raise conflict(
            "rollback_source_invalid",
            f"Version {source['version']} was never published, so there is nothing to roll back to.",
            details={"version_id": version_id, "status": source["status"]},
            actions=["Pick a version with status 'published' or 'deprecated'."],
        )
    created = new_version(
        capability_id,
        from_version_id=version_id,
        config=db.loads(source["config"], {}),
        notes=f"Rollback of {capability['slug']} to v{source['version']}.",
        actor_id=actor_id,
        ip=ip,
    )
    published = publish(
        capability_id,
        created["id"],
        actor_id=actor_id,
        ip=ip,
        inherited_evidence_from=version_id,
    )
    published["rolled_back_from"] = {"version_id": version_id, "version": source["version"]}
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.rolled_back",
        object_type="capability",
        object_id=capability_id,
        summary=f"{capability['slug']} rolled back to v{source['version']} as v{created['version']}",
        metadata={"from_version_id": version_id, "new_version_id": created["id"]},
        ip=ip,
    )
    return published


# --------------------------------------------------------------------- diff --
def _diff_node(before: Any, after: Any, path: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}.{key}"
            if key not in before:
                changes.append({"path": child, "kind": "added", "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child, "kind": "removed", "before": before[key], "after": None})
            else:
                changes.extend(_diff_node(before[key], after[key], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before):
                changes.append({"path": child, "kind": "added", "before": None, "after": after[index]})
            elif index >= len(after):
                changes.append({"path": child, "kind": "removed", "before": before[index], "after": None})
            else:
                changes.extend(_diff_node(before[index], after[index], child))
        return changes
    if before != after:
        changes.append(
            {
                "path": path,
                "kind": "changed",
                "before": before,
                "after": after,
                "type_before": type(before).__name__,
                "type_after": type(after).__name__,
            }
        )
    return changes


def diff_versions(
    capability_id: str,
    from_version_id: str,
    to_version_id: str,
    *,
    capability_type: str | None = None,
) -> dict[str, Any]:
    capability = get_row(capability_id)
    source = get_version_row(capability_id, from_version_id)
    target = get_version_row(capability_id, to_version_id)
    kind = capability_type or capability["type"]
    before = merge_defaults(db.loads(source["config"], {}), kind)
    after = merge_defaults(db.loads(target["config"], {}), kind)
    changes = _diff_node(before, after, "$")
    summary = {
        "added": sum(1 for change in changes if change["kind"] == "added"),
        "removed": sum(1 for change in changes if change["kind"] == "removed"),
        "changed": sum(1 for change in changes if change["kind"] == "changed"),
    }
    return {
        "capability_id": capability_id,
        "from": {"version_id": source["id"], "version": source["version"], "status": source["status"]},
        "to": {"version_id": target["id"], "version": target["version"], "status": target["status"]},
        "identical": not changes,
        "summary": summary,
        "changes": changes,
    }


# ------------------------------------------------------------------ deletion --
def delete_guard(capability_id: str) -> dict[str, Any]:
    """Routes and SKILL manifests referencing a published version block deletion.

    SKILL dependencies live inside `skill_versions.manifest`, a JSON text column,
    so this scan is deliberately coarse: every manifest that mentions the
    capability slug as a quoted string is reported. That can over-report (a slug
    mentioned in prose), which blocks a delete instead of silently breaking a
    published SKILL — the safe direction.
    """
    published_ids = [
        row["id"]
        for row in db.query_all(
            "SELECT id FROM capability_versions WHERE capability_id = ? AND status = 'published'", (capability_id,)
        )
    ]
    capability = db.query_one("SELECT slug FROM capabilities WHERE id = ?", (capability_id,)) or {}
    routes = db.query_all(
        """SELECT r.id, r.capability_version_id, v.version
             FROM routes r JOIN capability_versions v ON v.id = r.capability_version_id
            WHERE r.capability_id = ? AND v.status = 'published'""",
        (capability_id,),
    )
    skill_dependencies = db.query_all(
        """SELECT sv.id, sv.skill_id, sv.version, s.slug AS skill_slug, s.name AS skill_name
             FROM skill_versions sv JOIN skills s ON s.id = sv.skill_id
            WHERE sv.manifest LIKE ? AND sv.status = 'published'""",
        (f'%"{capability.get("slug") or ""}"%',),
    )
    in_use = bool(routes) or bool(skill_dependencies)
    details = {
        "published_version_ids": published_ids,
        "routes": routes,
        "skill_versions": skill_dependencies,
    }
    if in_use:
        raise conflict(
            "capability_in_use",
            "This capability is referenced by routes or published SKILLs and cannot be deleted.",
            details=details,
            actions=[
                "Delete or re-point the routes that reference the published version.",
                "Update or unpublish the SKILLs that depend on it.",
                "Or disable the capability instead of deleting it.",
            ],
        )
    return details


def delete_capability(capability_id: str, *, actor_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
    capability = get_row(capability_id)
    delete_guard(capability_id)
    impact = {
        "versions": db.query_scalar("SELECT COUNT(*) FROM capability_versions WHERE capability_id = ?", (capability_id,)),
        "routes": db.query_scalar("SELECT COUNT(*) FROM routes WHERE capability_id = ?", (capability_id,)),
        "tests": db.query_scalar("SELECT COUNT(*) FROM capability_tests WHERE capability_id = ?", (capability_id,)),
    }
    db.execute("DELETE FROM capabilities WHERE id = ?", (capability_id,))
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="capability.deleted",
        object_type="capability",
        object_id=capability_id,
        summary=f"{capability['slug']} deleted",
        metadata={"impact": impact},
        ip=ip,
    )
    return {"deleted": True, "id": capability_id, "slug": capability["slug"], "impact": impact}


# ------------------------------------------------------------------- invoke ---
def admin_principal(user: Any) -> AgentPrincipal:
    """The principal an admin manual call runs as (mirrors execution.test_version)."""
    return AgentPrincipal(
        agent_id="",
        agent_name=getattr(user, "username", "admin"),
        token_id="",
        scopes=["*"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )
