"""路由决策：把 router.selector 与凭证/记录连接起来（§7.7）。"""

from __future__ import annotations

from sqlalchemy import select as sa_select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..models import CapabilityRoute, Credential, Provider, RouteDecision
from ..router.selector import RouteSelection
from ..router.selector import select as select_route
from ..security.tokens import TokenContext
from . import credentials


def routes_from_definition(definition: dict) -> list[dict]:
    return [dict(route) for route in (definition.get("routes") or []) if isinstance(route, dict)]


def _health_from_projection(capability_id: str) -> dict[str, dict]:
    with db_session() as s:
        rows = list(
            s.scalars(sa_select(CapabilityRoute).where(CapabilityRoute.capability_id == capability_id))
        )
        return {row.route_key: (row.health or {}) for row in rows}


def resolve(
    *,
    capability_id: str | None,
    definition: dict,
    mode: str,
    options: dict | None = None,
    token_ctx: TokenContext | None = None,
    tags: list[str] | None = None,
    exclude_route_ids: set[str] | None = None,
) -> tuple[RouteSelection, dict, str]:
    """返回 (选择结果, 凭证 material, provider base_url)。material 仅本次请求有效。"""
    routes = routes_from_definition(definition)
    health = _health_from_projection(capability_id) if capability_id else {}
    for route in routes:
        key = route.get("id") or route.get("route_key")
        if key and key in health:
            route["health"] = health[key]
    with db_session() as s:
        selection = select_route(
            s,
            capability=definition,
            routes=routes,
            mode=mode,
            options=options,
            token_ctx=token_ctx,
            tags=tags,
            exclude_route_ids=exclude_route_ids,
        )
    if selection.chosen is None:  # pragma: no cover - select 已抛错
        raise RuntimeError("route selection produced no chosen route")
    chosen = selection.chosen
    material = credentials.materialize(chosen["credential_id"])
    base_url = material.get("base_url") or ""
    if not base_url:
        with db_session() as s:
            provider = s.get(Provider, chosen.get("provider_id"))
            base_url = (provider.base_url if provider else "") or ""
    return selection, material, base_url


def resolve_pinned(
    *, definition: dict, route_id: str | None
) -> tuple[dict, dict, str]:
    """按任务创建时冻结的 `route_id` 取回同一条 route（异步续跑/取消必须打回同一家）。

    重新选路会让「提交给 A 家的任务」被拿去 B 家查询或取消，也会把 v1 的
    请求结构套到 v2 的定义上 —— 这里不重新决策，只做查找（§7.7 / §9.3）。
    """
    from ..errors import ApiError, Code  # noqa: PLC0415 - 避免与 services 循环导入

    routes = routes_from_definition(definition)
    chosen = None
    if route_id:
        for route in routes:
            if (route.get("id") or route.get("route_key")) == route_id:
                chosen = dict(route)
                break
    if chosen is None:
        raise ApiError(
            Code.PROVIDER_NOT_AVAILABLE,
            message="任务创建时使用的路由已不存在或该版本未包含它",
            hint="该能力已被改版；请按当前版本重新提交任务（勿对旧任务盲目重试）",
            details={"route_id": route_id, "capability_key": definition.get("key")},
        )
    material = credentials.materialize(chosen["credential_id"])
    base_url = material.get("base_url") or ""
    if not base_url:
        with db_session() as s:
            provider = s.get(Provider, chosen.get("provider_id"))
            base_url = (provider.base_url if provider else "") or ""
    return chosen, material, base_url


def alt_route(
    *, definition: dict, mode: str, tried: set[str], options: dict | None = None
) -> dict | None:
    """故障切换：返回下一条 enabled 且未尝试过的 route。"""
    candidates = [
        route
        for route in routes_from_definition(definition)
        if route.get("enabled", True) and (route.get("id") or route.get("route_key")) not in tried
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: int(r.get("priority", 0)))
    for route in candidates:
        if mode == "stream" and not (route.get("stream") or {}).get("protocol"):
            continue
        if mode == "async" and not ((route.get("async") or {}).get("submit") or {}):
            continue
        return route
    return None


def record_decision(
    *,
    call_id: str | None,
    selection: RouteSelection,
    reason: str | None = None,
    failover_from: str | None = None,
) -> str:
    chosen = selection.chosen or {}
    decision_id = uuid_pk("rds")
    candidates = [
        {
            "route_id": c.get("route_id"),
            "provider_id": c.get("provider_id"),
            "credential_id": c.get("credential_id"),
            "priority": c.get("priority"),
            "health": c.get("health"),
            "price_usd": c.get("price_usd"),
        }
        for c in selection.candidates
    ]
    with db_write() as s:
        s.add(
            RouteDecision(
                id=decision_id,
                call_id=call_id,
                candidates=candidates,
                dropped=selection.dropped,
                chosen_route_id=chosen.get("route_id"),
                chosen_capability_version_id=chosen.get("capability_version_id"),
                chosen_provider_id=chosen.get("provider_id"),
                reason=reason or selection.reason,
                failover_from=failover_from,
                created_at=now_iso(),
            )
        )
    return decision_id


def decision_detail(call_id: str) -> dict | None:
    with db_session() as s:
        row = s.scalar(sa_select(RouteDecision).where(RouteDecision.call_id == call_id))
        if row is None:
            return None
        return {
            "id": row.id,
            "candidates": row.candidates,
            "dropped": row.dropped,
            "chosen_route_id": row.chosen_route_id,
            "chosen_provider_id": row.chosen_provider_id,
            "reason": row.reason,
            "failover_from": row.failover_from,
            "created_at": row.created_at,
        }


def credential_status_map(definition: dict) -> dict[str, str]:
    ids = [r.get("credential_id") for r in routes_from_definition(definition) if r.get("credential_id")]
    out: dict[str, str] = {}
    with db_session() as s:
        for cid in ids:
            if not cid:
                continue
            row = s.get(Credential, cid)
            out[cid] = row.status if row else "missing"
    return out
