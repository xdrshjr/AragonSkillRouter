"""Route selection.

A route is one concrete way to satisfy a standard capability: a published
version, a credential and a provider. Selection is explicit — input/output
compatibility first, then health, then priority/weight — and the reason is
recorded on every invocation so a surprising result can be explained later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core import db
from app.services.capability_config import merge_defaults

HEALTH_RANK = {"healthy": 0, "unknown": 1, "degraded": 2, "unhealthy": 3}


@dataclass
class RouteCandidate:
    route_id: str
    route: dict[str, Any]
    version: dict[str, Any]
    config: dict[str, Any]
    provider: dict[str, Any] | None
    credential: dict[str, Any] | None
    capability_id: str
    capability_slug: str
    capability_type: str


@dataclass
class RouteSelection:
    candidates: list[RouteCandidate] = field(default_factory=list)
    reason: str = ""
    rejected: list[dict[str, Any]] = field(default_factory=list)

    @property
    def chosen(self) -> RouteCandidate | None:
        return self.candidates[0] if self.candidates else None


def _schema_accepts(schema: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Cheap compatibility gate: the declared required fields must be present.

    Deliberately not a full jsonschema validation — the executor validates the
    chosen route's schema properly. This only decides whether a route is even a
    candidate, so a route that demands a field the caller never sent is skipped
    with a reason instead of failing the whole call.
    """
    if not isinstance(schema, dict):
        return True
    required = schema.get("required") or []
    return all(name in payload for name in required)


def candidates_for(
    *,
    capability: dict[str, Any],
    version: dict[str, Any],
    input_payload: dict[str, Any],
    mode: str,
    preferred_provider_id: str | None = None,
    allow_unhealthy: bool = False,
    allow_draft: bool = False,
) -> RouteSelection:
    rows = db.query_all(
        """SELECT r.*, p.name AS provider_name, p.status AS provider_status, p.category AS provider_category,
                  p.base_url AS provider_base_url,
                  c.status AS credential_status, c.auth_type, c.name AS credential_name,
                  v.status AS version_status, v.config AS version_config, v.version AS version_number
             FROM routes r
             LEFT JOIN providers p ON p.id = r.provider_id
             LEFT JOIN credentials c ON c.id = r.credential_id
             JOIN capability_versions v ON v.id = r.capability_version_id
            WHERE r.capability_id = ? AND r.enabled = 1 AND v.status = 'published'
            ORDER BY r.priority ASC, r.weight DESC, r.created_at ASC""",
        (capability["id"],),
    )
    selection = RouteSelection()
    # A capability with no explicit route is still callable through its own
    # provider/credential pair — that keeps manual configuration usable before
    # the administrator defines routing policies. Draft versions qualify only
    # for the admin surfaces that explicitly allow them (test/invoke previews).
    if not rows and (version.get("status") == "published" or allow_draft):
        credential_row = (
            db.query_one("SELECT id, name, status, auth_type FROM credentials WHERE id = ?", (capability.get("credential_id"),))
            if capability.get("credential_id")
            else None
        )
        provider_row = (
            db.query_one("SELECT id, name, status, base_url FROM providers WHERE id = ?", (capability.get("provider_id"),))
            if capability.get("provider_id")
            else None
        )
        pseudo = {
            "id": None,
            "provider_id": capability.get("provider_id"),
            "credential_id": capability.get("credential_id"),
            "strategy": "fixed",
            "priority": 0,
            "weight": 1,
            "health_status": "unknown",
            # Carry the real statuses so the compatibility gates below judge the
            # implicit route exactly like an explicit one.
            "credential_status": (credential_row or {}).get("status"),
            "credential_name": (credential_row or {}).get("name"),
            "auth_type": (credential_row or {}).get("auth_type"),
            "provider_status": (provider_row or {}).get("status"),
            "provider_name": (provider_row or {}).get("name"),
            "provider_base_url": (provider_row or {}).get("base_url"),
            "version_status": version.get("status"),
            # Raw JSON text: `db.loads` parses it exactly once below.
            "version_config": version.get("config"),
            "version_number": version.get("version"),
        }
        rows = [pseudo]

    for row in rows:
        route_id = row.get("id") or "implicit"
        # Version configs arrive as JSON text from SQLite; parse exactly once.
        raw_config: Any = row.get("version_config")
        if raw_config is None:
            raw_config = version.get("config")
        if isinstance(raw_config, str):
            raw_config = db.loads(raw_config, {})
        elif not isinstance(raw_config, dict):
            raw_config = {}
        config = merge_defaults(raw_config, capability.get("type", "generic"))
        version_row = {
            "id": version["id"],
            "version": row.get("version_number") or version.get("version"),
            "status": "published",
            "config": config,
        }
        modes = config.get("modes") or {}
        if not modes.get(mode):
            selection.rejected.append({"route": route_id, "reason": f"mode '{mode}' not supported"})
            continue
        if row.get("credential_id"):
            if row.get("credential_status") != "active":
                selection.rejected.append({"route": route_id, "reason": "credential is not active"})
                continue
        if row.get("provider_id"):
            if row.get("provider_status") not in (None, "active"):
                selection.rejected.append({"route": route_id, "reason": "provider is disabled"})
                continue
        if preferred_provider_id and row.get("provider_id") != preferred_provider_id:
            selection.rejected.append({"route": route_id, "reason": "not the requested provider"})
            continue
        health = row.get("health_status") or "unknown"
        if health == "unhealthy" and not allow_unhealthy:
            selection.rejected.append({"route": route_id, "reason": "health check failing"})
            continue
        if not _schema_accepts(config.get("input_schema") or {}, input_payload):
            selection.rejected.append({"route": route_id, "reason": "input does not satisfy this version's schema"})
            continue
        selection.candidates.append(
            RouteCandidate(
                route_id=route_id,
                route=row,
                version=version_row,
                config=config,
                provider={
                    "id": row.get("provider_id"),
                    "name": row.get("provider_name"),
                    "status": row.get("provider_status"),
                    "base_url": row.get("provider_base_url"),
                }
                if row.get("provider_id")
                else None,
                credential={
                    "id": row.get("credential_id"),
                    "name": row.get("credential_name"),
                    "status": row.get("credential_status"),
                    "auth_type": row.get("auth_type"),
                }
                if row.get("credential_id")
                else None,
                capability_id=capability["id"],
                capability_slug=capability["slug"],
                capability_type=capability.get("type", "generic"),
            )
        )

    strategy = selection.candidates[0].route.get("strategy", "priority") if selection.candidates else "priority"
    if strategy == "health_first":
        selection.candidates.sort(
            key=lambda cand: (HEALTH_RANK.get(cand.route.get("health_status") or "unknown", 1), cand.route.get("priority", 100))
        )
    if selection.candidates:
        first = selection.candidates[0]
        provider_name = (first.provider or {}).get("name") or "the capability's own endpoint"
        selection.reason = (
            f"chose {provider_name} via route {first.route_id} "
            f"(strategy={strategy}, health={first.route.get('health_status') or 'unknown'}, "
            f"priority={first.route.get('priority')}); {len(selection.candidates) - 1} fallback(s) available"
        )
    else:
        selection.reason = "no compatible route: " + (
            "; ".join(f"{r['route']}: {r['reason']}" for r in selection.rejected) or "no routes are configured"
        )
    return selection


def update_health(route_id: str, status: str) -> None:
    if not route_id or route_id == "implicit":
        return
    db.execute(
        "UPDATE routes SET health_status = ?, last_health_at = ?, updated_at = ? WHERE id = ?",
        (status, db.iso(), db.iso(), route_id),
    )


def mark_failure(route_id: str, *, degraded: bool = True) -> None:
    if not route_id or route_id == "implicit":
        return
    current = db.query_one("SELECT health_status FROM routes WHERE id = ?", (route_id,))
    if not current:
        return
    if current["health_status"] in {"unhealthy", "degraded"}:
        return
    update_health(route_id, "degraded" if degraded else "unhealthy")


def mark_success(route_id: str) -> None:
    if not route_id or route_id == "implicit":
        return
    current = db.query_one("SELECT health_status FROM routes WHERE id = ?", (route_id,))
    if current and current["health_status"] != "healthy":
        update_health(route_id, "healthy")


def explain_selection(selection: RouteSelection) -> dict[str, Any]:
    return {
        "reason": selection.reason,
        "chosen_route": selection.chosen.route_id if selection.chosen else None,
        "candidates": [
            {
                "route_id": cand.route_id,
                "provider": (cand.provider or {}).get("name"),
                "version": cand.version["version"],
                "health": cand.route.get("health_status"),
                "priority": cand.route.get("priority"),
            }
            for cand in selection.candidates
        ],
        "rejected": selection.rejected,
    }
