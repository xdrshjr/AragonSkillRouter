"""路由选择：候选、过滤、排序、故障切换依据（§7.7）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiError, Code
from ..models import Credential, Provider
from ..security.tokens import TokenContext, scope_allows
from .health import route_health


@dataclass
class RouteSelection:
    chosen: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    reason: str = ""
    available_providers: list[str] = field(default_factory=list)


def _credential_ok(cred: Credential | None) -> tuple[bool, str]:
    if cred is None:
        return False, "凭证不存在"
    if cred.deleted_at:
        return False, "凭证已删除"
    if cred.status != "active":
        return False, f"凭证状态为 {cred.status}"
    return True, ""


def select(
    session: Session,
    *,
    capability: dict,
    routes: list[dict],
    mode: str,
    options: dict | None = None,
    token_ctx: TokenContext | None = None,
    tags: list[str] | None = None,
    exclude_route_ids: set[str] | None = None,
) -> RouteSelection:
    options = options or {}
    exclude = exclude_route_ids or set()
    preferred = options.get("preferred_provider")
    supported_modes = capability.get("modes") or ["sync"]
    if mode not in supported_modes:
        raise ApiError(
            Code.UNSUPPORTED_MODE,
            message=f"该能力不支持 {mode} 模式",
            details={"supported": supported_modes},
        )
    file_io = (capability.get("file_io") or {})
    result = RouteSelection()
    providers_seen: set[str] = set()

    for route in routes:
        route_id = route.get("id") or route.get("route_key") or ""
        provider_id = route.get("provider_id")
        if provider_id:
            providers_seen.add(provider_id)
        if not route.get("enabled", True):
            result.dropped.append({"route_id": route_id, "reason": "route 已停用"})
            continue
        if route_id in exclude:
            result.dropped.append({"route_id": route_id, "reason": "已尝试过该 route（故障切换）"})
            continue
        if mode == "stream" and not (route.get("stream") or {}).get("protocol"):
            result.dropped.append({"route_id": route_id, "reason": "该 route 不提供流式能力"})
            continue
        if mode == "async" and not ((route.get("async") or {}).get("submit") or {}).get("task_id_path"):
            result.dropped.append({"route_id": route_id, "reason": "该 route 不提供异步提交"})
            continue
        if mode == "async" and not ((route.get("async") or {}).get("query") or {}).get("url_template"):
            result.dropped.append({"route_id": route_id, "reason": "该 route 缺少异步查询配置"})
            continue
        if preferred and provider_id != preferred:
            result.dropped.append(
                {"route_id": route_id, "reason": f"preferred_provider={preferred} 指定了其它提供商"}
            )
            continue
        cred = session.get(Credential, route.get("credential_id")) if route.get("credential_id") else None
        ok, why = _credential_ok(cred)
        if not ok:
            result.dropped.append({"route_id": route_id, "reason": why})
            continue
        provider = session.get(Provider, provider_id) if provider_id else None
        if provider is None or provider.deleted_at or provider.status != "active":
            result.dropped.append({"route_id": route_id, "reason": "提供商不存在或已停用"})
            continue
        if token_ctx is not None and not scope_allows(token_ctx, capability.get("key", ""), tags):
            raise ApiError(
                Code.TOKEN_SCOPE_DENIED,
                message="令牌没有该能力的权限",
                details={"capability": capability.get("key")},
            )
        if token_ctx is not None and token_ctx.allowed_modes and mode not in token_ctx.allowed_modes:
            raise ApiError(
                Code.UNSUPPORTED_MODE,
                message=f"令牌不允许 {mode} 模式",
                details={"allowed_modes": token_ctx.allowed_modes},
            )
        health = route.get("health") or route_health(session, route_id)
        price = float((route.get("price") or {}).get("unit_price_usd") or 0.0)
        result.candidates.append(
            {
                **route,
                "route_id": route_id,
                "health": health,
                "price_usd": price,
                "credential_status": cred.status if cred else "missing",
                "provider_name": provider.name if provider else "",
                "provider_priority": provider.status == "active",
            }
        )

    if not result.candidates:
        if preferred:
            raise ApiError(
                Code.PROVIDER_NOT_AVAILABLE,
                message=f"指定的提供商 {preferred} 没有可用路由",
                hint="检查该提供商的凭证状态与 route 是否启用",
                details={"available": sorted(providers_seen)},
            )
        raise ApiError(
            Code.CAPABILITY_NOT_CALLABLE,
            message="能力当前没有可调用的路由",
            hint="检查凭证状态、route 启用状态与模式配置",
            details={"dropped": result.dropped},
        )

    def sort_key(route: dict) -> tuple:
        return (
            int(route.get("priority", 0)),
            -float((route.get("health") or {}).get("score", 1.0)),
            float(route.get("price_usd") or 0.0),
            float((route.get("health") or {}).get("p95_ms") or 0),
            str(route.get("route_id")),
        )

    result.candidates.sort(key=sort_key)
    result.chosen = result.candidates[0]
    result.reason = (
        f"按 priority={result.chosen.get('priority', 0)}、健康分 "
        f"{(result.chosen.get('health') or {}).get('score', 1.0)}、单价与 P95 排序后选中 "
        f"{result.chosen.get('route_id')}（provider={result.chosen.get('provider_id')}）"
    )
    result.available_providers = sorted(providers_seen)
    return result
