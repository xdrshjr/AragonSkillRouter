"""Routing: which concrete provider serves a capability, and is it healthy?

Owner: backend-capabilities (docs/CONTRACT.md §5.5).

The health check is a real call. It never writes a status it did not observe:
  * an async version with an upstream task id is polled through
    `execution.fetch_status` (the same code the worker uses);
  * otherwise a GET probe goes through `network.guarded_request` against the
    capability version's request URL with the route's credential applied
    (send-time only, via `credentials.build_auth_headers`);
  * 401/403 → unhealthy, other 2xx/4xx → healthy/degraded, transport failure →
    unhealthy. The outcome is stored with `routing.update_health`.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.security.redact import Redactor
from app.services import (
    audit,
    capabilities as capability_service,
    credentials as credential_service,
    execution,
    routing,
)
from app.services.capability_config import merge_defaults, render

router = APIRouter(tags=["admin-routing"])

MAX_LIMIT = 200
DEFAULT_PROBE_TIMEOUT_S = 15.0

RouteStrategy = Literal["priority", "fixed", "health_first"]
RouteHealth = Literal["unknown", "healthy", "degraded", "unhealthy"]


# ------------------------------------------------------------------- models ---
class RouteCreate(BaseModel):
    """Body of `POST /api/admin/capabilities/{id}/routes`."""

    capability_version_id: str = Field(min_length=1)
    provider_id: str | None = None
    credential_id: str | None = None
    strategy: RouteStrategy = "priority"
    priority: int = Field(default=100, ge=0, le=10000)
    weight: int = Field(default=1, ge=1, le=1000)
    enabled: bool = True


class RouteUpdate(BaseModel):
    """Body of `PATCH /api/admin/routes/{route_id}` — partial."""

    model_config = ConfigDict(extra="forbid")

    capability_version_id: str | None = None
    provider_id: str | None = None
    credential_id: str | None = None
    strategy: RouteStrategy | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    weight: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None


class HealthCheckRequest(BaseModel):
    upstream_task_id: str | None = Field(
        default=None,
        description="Polls the async status endpoint of the version instead of probing the request URL.",
    )
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=DEFAULT_PROBE_TIMEOUT_S, gt=0, le=60)


# ----------------------------------------------------------------- helpers ----
def _route_row(route_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT r.*, v.version AS version, v.status AS version_status, v.config AS version_config,
                  c.slug AS capability_slug, c.type AS capability_type
             FROM routes r
             JOIN capability_versions v ON v.id = r.capability_version_id
             JOIN capabilities c ON c.id = r.capability_id
            WHERE r.id = ?""",
        (route_id,),
    )
    if not row:
        raise not_found("Route")
    return row


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    provider = db.query_one("SELECT name, status FROM providers WHERE id = ?", (row["provider_id"],)) if row.get("provider_id") else None
    credential = (
        db.query_one("SELECT name, status FROM credentials WHERE id = ?", (row["credential_id"],))
        if row.get("credential_id")
        else None
    )
    return {
        "id": row["id"],
        "capability_id": row["capability_id"],
        "capability_version_id": row["capability_version_id"],
        "version": row.get("version"),
        "version_status": row.get("version_status"),
        "provider_id": row.get("provider_id"),
        "provider_name": (provider or {}).get("name"),
        "provider_status": (provider or {}).get("status"),
        "credential_id": row.get("credential_id"),
        "credential_name": (credential or {}).get("name"),
        "credential_status": (credential or {}).get("status"),
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


def _check_references(capability_id: str, version_id: str | None, provider_id: str | None, credential_id: str | None) -> None:
    if version_id:
        row = db.query_one(
            "SELECT id, capability_id, version, status FROM capability_versions WHERE id = ?", (version_id,)
        )
        if not row:
            raise not_found("Capability version")
        if row["capability_id"] != capability_id:
            raise unprocessable(
                "version_capability_mismatch",
                "That version belongs to a different capability.",
                details={"version_id": version_id, "version_capability_id": row["capability_id"]},
                actions=["Pick a version of this capability."],
            )
    if provider_id:
        if not db.query_one("SELECT id FROM providers WHERE id = ?", (provider_id,)):
            raise not_found("Provider")
    if credential_id:
        credential = db.query_one("SELECT id, provider_id, status FROM credentials WHERE id = ?", (credential_id,))
        if not credential:
            raise not_found("Credential")
        if credential["status"] != "active":
            raise conflict(
                "credential_inactive",
                "That credential is not active, so a route using it would never be selected.",
                details={"credential_id": credential_id, "status": credential["status"]},
                actions=["Enable the credential first, or pick another one."],
            )
        if provider_id and credential["provider_id"] != provider_id:
            raise unprocessable(
                "credential_provider_mismatch",
                "That credential belongs to a different provider.",
                details={"credential_id": credential_id, "credential_provider_id": credential["provider_id"]},
            )


# ------------------------------------------------------------ capabilities ----
@router.get("/api/admin/capabilities/{capability_id}/routes")
def list_routes(capability_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    capability_service.get_row(capability_id)  # 404s for an unknown id
    items = capability_service.routes_for(capability_id)
    return {"items": items, "total": len(items), "limit": MAX_LIMIT, "offset": 0}


@router.post("/api/admin/capabilities/{capability_id}/routes", status_code=201)
def create_route(
    capability_id: str,
    payload: RouteCreate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    capability = capability_service.get_row(capability_id)
    _check_references(
        capability_id, payload.capability_version_id, payload.provider_id, payload.credential_id
    )
    provider_id = payload.provider_id or capability.get("provider_id")
    credential_id = payload.credential_id or (
        capability.get("credential_id") if provider_id == capability.get("provider_id") else None
    )
    now = db.iso()
    route_id = db.new_id("route")
    db.insert(
        "routes",
        {
            "id": route_id,
            "capability_id": capability_id,
            "capability_version_id": payload.capability_version_id,
            "provider_id": provider_id,
            "credential_id": credential_id,
            "strategy": payload.strategy,
            "priority": payload.priority,
            "weight": payload.weight,
            "enabled": 1 if payload.enabled else 0,
            "health_status": "unknown",
            "conditions": db.dumps({}),
            "created_at": now,
            "updated_at": now,
        },
    )
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="route.created",
        object_type="route",
        object_id=route_id,
        summary=f"{capability['slug']} → provider {provider_id or 'none'} (strategy {payload.strategy})",
        metadata={
            "capability_id": capability_id,
            "capability_version_id": payload.capability_version_id,
            "provider_id": provider_id,
            "credential_id": credential_id,
            "priority": payload.priority,
            "weight": payload.weight,
        },
        ip=client_ip(request),
    )
    return _serialize(_route_row(route_id))


# ------------------------------------------------------------------- routes ---
@router.get("/api/admin/routes")
def list_all_routes(
    capability_id: str | None = None,
    provider_id: str | None = None,
    health_status: RouteHealth | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    if capability_id:
        where.append("r.capability_id = ?")
        params.append(capability_id)
    if provider_id:
        where.append("r.provider_id = ?")
        params.append(provider_id)
    if health_status:
        where.append("r.health_status = ?")
        params.append(health_status)
    if enabled is not None:
        where.append("r.enabled = ?")
        params.append(1 if enabled else 0)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM routes r WHERE {clause}", params) or 0
    ids = db.query_all(
        f"SELECT r.id FROM routes r WHERE {clause} ORDER BY r.priority ASC, r.created_at ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    items = [_serialize(_route_row(row["id"])) for row in ids]
    return {"items": items, "total": int(total), "limit": limit, "offset": offset}


@router.patch("/api/admin/routes/{route_id}")
def update_route(
    route_id: str,
    payload: RouteUpdate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = _route_row(route_id)
    changes = payload.model_dump(exclude_unset=True)
    if "capability_version_id" in changes and changes["capability_version_id"]:
        _check_references(
            row["capability_id"],
            changes["capability_version_id"],
            changes.get("provider_id", row.get("provider_id")),
            changes.get("credential_id", row.get("credential_id")),
        )
    elif "provider_id" in changes or "credential_id" in changes:
        _check_references(
            row["capability_id"],
            None,
            changes.get("provider_id", row.get("provider_id")),
            changes.get("credential_id", row.get("credential_id")),
        )
    values: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "enabled":
            values["enabled"] = 1 if value else 0
        elif value is not None or field in {"provider_id", "credential_id"}:
            values[field] = value
    if values:
        values["updated_at"] = db.iso()
        db.update("routes", route_id, values)
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="route.updated",
        object_type="route",
        object_id=route_id,
        summary=f"route for {row['capability_slug']} updated ({', '.join(sorted(values)) or 'no changes'})",
        metadata={"fields_changed": sorted(values.keys()), "capability_id": row["capability_id"]},
        ip=client_ip(request),
    )
    return _serialize(_route_row(route_id))


@router.delete("/api/admin/routes/{route_id}")
def delete_route(
    route_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = _route_row(route_id)
    db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="route.deleted",
        object_type="route",
        object_id=route_id,
        summary=f"route for {row['capability_slug']} deleted",
        metadata={"capability_id": row["capability_id"], "provider_id": row.get("provider_id")},
        ip=client_ip(request),
    )
    return {"deleted": True, "id": route_id, "capability_id": row["capability_id"]}


# ------------------------------------------------------------- health check ---
def _probe_headers(route: dict[str, Any]) -> tuple[dict[str, str], Redactor]:
    credential_id = route.get("credential_id")
    if not credential_id:
        return {}, Redactor()
    try:
        credential_row = credential_service.get_row(credential_id)
        secrets = credential_service.get_secret_map(credential_id)
    except ApiError as exc:
        raise conflict(
            "credential_unusable",
            "The route's credential cannot be used for a probe.",
            details={"credential_id": credential_id, "code": exc.code},
            actions=["Verify or re-create the credential.", "Or remove the credential from the route."],
        ) from None
    return credential_service.build_auth_headers(credential_row, secrets), secrets.redactor()


@router.post("/api/admin/routes/{route_id}/health-check")
async def health_check(
    route_id: str,
    request: Request,
    payload: HealthCheckRequest | None = None,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """A real probe. The stored status is only what this call observed."""
    from app.services.network import NetworkPolicyError, classify_network_error, guarded_request

    payload = payload or HealthCheckRequest()
    route = _route_row(route_id)
    capability = capability_service.get_row(route["capability_id"])
    config = db.loads(route["version_config"], {})
    merged = merge_defaults(config, capability["type"])
    redactor = Redactor()

    # -- async versions: poll the same status endpoint the worker uses ---------
    if payload.upstream_task_id:
        if not (merged.get("async") or {}).get("enabled"):
            raise unprocessable(
                "not_async_capability",
                "This version is not configured for asynchronous calls, so there is no status endpoint to poll.",
                details={"capability_id": capability["id"], "version": route.get("version")},
                actions=["Drop upstream_task_id to probe the request URL instead."],
            )
        try:
            status = await execution.fetch_status(
                config=merged,
                upstream_task_id=payload.upstream_task_id,
                input_payload=payload.input or {},
                credential_id=route.get("credential_id"),
            )
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are expected
            code, message = classify_network_error(exc)
            routing.update_health(route_id, "unhealthy")
            audit.record(
                actor_type="user",
                actor_id=user.id,
                action="route.health_checked",
                object_type="route",
                object_id=route_id,
                summary=f"async poll failed ({code})",
                metadata={"code": code, "capability_id": capability["id"]},
                ip=client_ip(request),
            )
            return {
                "route_id": route_id,
                "status": "unhealthy",
                "latency_ms": None,
                "message": message,
                "method": "async_status_poll",
                "http_status": None,
                "checked_at": db.iso(),
                "probe_persisted": True,
            }
        if not status.get("ok"):
            health = "unhealthy"
            message = (status.get("error") or {}).get("message") or "The upstream status endpoint refused the poll."
        else:
            upstream = status.get("status")
            health = {"succeeded": "healthy", "failed": "unhealthy", "cancelled": "degraded"}.get(upstream, "healthy")
            message = f"Upstream task status is '{upstream}'."
        routing.update_health(route_id, health)
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="route.health_checked",
            object_type="route",
            object_id=route_id,
            summary=f"async poll → {health}",
            metadata={"health": health, "capability_id": capability["id"]},
            ip=client_ip(request),
        )
        return {
            "route_id": route_id,
            "status": health,
            "latency_ms": None,
            "message": message,
            "method": "async_status_poll",
            "http_status": status.get("http_status"),
            "upstream_status": status.get("upstream_status"),
            "checked_at": db.iso(),
            "probe_persisted": True,
        }

    # -- synchronous probe of the capability's request URL ---------------------
    request_cfg = merged.get("request") or {}
    url = str(request_cfg.get("url") or "").strip()
    if not url:
        raise unprocessable(
            "missing_probe_url",
            "This capability version has no request URL to probe.",
            details={"capability_id": capability["id"], "version": route.get("version")},
            actions=["Set request.url on the version, then run the health check again."],
        )
    ctx = {"input": payload.input or {}, "model": merged.get("model"), "mode": "sync", "task_id": None}
    url = str(render(url, ctx)).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise unprocessable(
            "invalid_probe_url",
            "The rendered request URL is not an absolute http(s) URL.",
            details={"url": redactor.text(url)},
            actions=["Fix request.url (or the template placeholders) on the version."],
        )

    try:
        headers, redactor = _probe_headers(route)
    except ApiError:
        raise
    if (merged.get("auth") or {}).get("mode") == "none" or not route.get("credential_id"):
        headers = {}
    headers.setdefault("accept", "application/json")
    for name, value in (request_cfg.get("headers") or {}).items():
        rendered = render(value, ctx)
        if rendered is not None and str(name).lower() not in {"authorization", "x-api-key", "api-key", "cookie"}:
            headers.setdefault(str(name), str(rendered))

    import time

    started = time.perf_counter()
    try:
        response = await guarded_request("GET", url, headers=headers, timeout_s=float(payload.timeout_s))
    except NetworkPolicyError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        routing.update_health(route_id, "unhealthy")
        message = redactor.text(str(exc))
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="route.health_checked",
            object_type="route",
            object_id=route_id,
            summary=f"probe blocked ({exc.code})",
            metadata={"code": exc.code, "capability_id": capability["id"]},
            ip=client_ip(request),
        )
        return {
            "route_id": route_id,
            "status": "unhealthy",
            "latency_ms": latency_ms,
            "message": f"The outbound policy refused the probe: {message}",
            "method": "GET",
            "http_status": None,
            "error_code": exc.code,
            "checked_at": db.iso(),
            "probe_persisted": True,
        }
    except Exception as exc:  # noqa: BLE001 - transport failure is a real answer here
        latency_ms = int((time.perf_counter() - started) * 1000)
        code, message = classify_network_error(exc)
        routing.update_health(route_id, "unhealthy")
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="route.health_checked",
            object_type="route",
            object_id=route_id,
            summary=f"probe failed ({code})",
            metadata={"code": code, "capability_id": capability["id"]},
            ip=client_ip(request),
        )
        return {
            "route_id": route_id,
            "status": "unhealthy",
            "latency_ms": latency_ms,
            "message": redactor.text(message),
            "method": "GET",
            "http_status": None,
            "error_code": code,
            "checked_at": db.iso(),
            "probe_persisted": True,
        }

    http_status = response.status_code
    if http_status in (401, 403):
        health = "unhealthy"
        message = f"HTTP {http_status}: the provider rejected the route's credential."
    elif 200 <= http_status < 300:
        health = "healthy"
        message = f"HTTP {http_status}: the endpoint answered."
    elif 400 <= http_status < 500:
        health = "healthy"
        message = (
            f"HTTP {http_status}: the endpoint is reachable (the bare GET probe was rejected, "
            "which is expected for POST-only APIs)."
        )
    elif http_status == 429:
        health = "degraded"
        message = "HTTP 429: the endpoint is reachable but rate limiting."
    elif http_status >= 500:
        health = "degraded"
        message = f"HTTP {http_status}: the provider is failing to serve requests."
    else:
        health = "degraded"
        message = f"HTTP {http_status}: unexpected status from the probe."

    routing.update_health(route_id, health)
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="route.health_checked",
        object_type="route",
        object_id=route_id,
        summary=f"probe → {health} (HTTP {http_status})",
        metadata={"health": health, "http_status": http_status, "capability_id": capability["id"]},
        ip=client_ip(request),
    )
    return {
        "route_id": route_id,
        "status": health,
        "latency_ms": response.elapsed_ms,
        "message": message,
        "method": "GET",
        "http_status": http_status,
        "url": redactor.text(response.url),
        "checked_at": db.iso(),
        "probe_persisted": True,
        "real_call": True,
    }


__all__ = ["router"]
