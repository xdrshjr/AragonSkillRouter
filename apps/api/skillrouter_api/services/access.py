"""外部 Agent 与平台令牌管理（§7.10 / §9.2）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..models import ExternalAgent, PlatformToken, QuotaCounter, QuotaLimit
from ..schemas.common import paginate
from ..security.ratelimit import quota_snapshot
from ..security.tokens import create_token_material
from ..settings import settings
from . import audit


def _agent(row: ExternalAgent, tokens: list[PlatformToken]) -> dict:
    active = [t for t in tokens if not t.revoked_at]
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "contact": row.contact,
        "status": row.status,
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "token_count": len(tokens),
        "active_token_count": len(active),
        "tokens": [_token(t) for t in tokens],
    }


def _token(row: PlatformToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "scopes": row.scopes or [],
        "allowed_modes": row.allowed_modes or [],
        "expires_at": row.expires_at,
        "rpm": row.rpm,
        "concurrency": row.concurrency,
        "daily_calls": row.daily_calls,
        "budget_usd": row.budget_usd,
        "revoked_at": row.revoked_at,
        "last_used_at": row.last_used_at,
        "created_at": row.created_at,
        "usage": quota_snapshot("token", row.id),
    }


def list_agents(*, q: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    with db_session() as s:
        agents = list(s.scalars(select(ExternalAgent).order_by(ExternalAgent.created_at)))
        tokens = list(s.scalars(select(PlatformToken)))
    by_agent: dict[str, list[PlatformToken]] = {}
    for token in tokens:
        by_agent.setdefault(token.agent_id, []).append(token)
    items = [_agent(a, by_agent.get(a.id, [])) for a in agents]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["name"].lower()]
    return paginate(items, page, page_size)


def get_agent(agent_id: str) -> dict:
    with db_session() as s:
        row = s.get(ExternalAgent, agent_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "外部 Agent 不存在")
        tokens = list(s.scalars(select(PlatformToken).where(PlatformToken.agent_id == agent_id)))
        return _agent(row, tokens)


def create_agent(data: dict, *, actor: str | None = None) -> dict:
    agent_id = uuid_pk("agt")
    with db_write() as s:
        if s.scalar(select(ExternalAgent).where(ExternalAgent.name == data["name"])):
            raise ApiError(Code.CONFLICT, f"名称已存在：{data['name']}")
        s.add(
            ExternalAgent(
                id=agent_id,
                name=data["name"],
                description=data.get("description"),
                contact=data.get("contact"),
                status="active",
                created_at=now_iso(),
            )
        )
    return get_agent(agent_id)


def update_agent(agent_id: str, patch: dict, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(ExternalAgent, agent_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "外部 Agent 不存在")
        for key in ("name", "description", "contact", "status"):
            if patch.get(key) is not None:
                setattr(row, key, patch[key])
    return get_agent(agent_id)


def delete_agent(agent_id: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(ExternalAgent, agent_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "外部 Agent 不存在")
        row.status = "revoked"
        for token in s.scalars(select(PlatformToken).where(PlatformToken.agent_id == agent_id)):
            if not token.revoked_at:
                token.revoked_at = now_iso()
    audit.write("token.revoked", object_type="external_agent", object_id=agent_id, actor_id=actor)
    return {"ok": True, "id": agent_id}


def create_token(agent_id: str, data: dict, *, actor: str | None = None) -> dict:
    full, digest, prefix = create_token_material()
    token_id = uuid_pk("tok")
    expires_at = data.get("expires_at")
    if not expires_at and data.get("expires_in_days"):
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=int(data["expires_in_days"]))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    scopes = data.get("scopes")
    if scopes is None:
        scopes = ["*"]
    if not scopes:
        # 空作用域集合 = 什么都不允许；不能让它在校验时被当成 "*"（fail-closed，§11）
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="scopes 不能为空；如需全量能力请显式传 [\"*\"]",
            details={"fields": [{"path": "scopes", "message": "至少一个作用域"}]},
        )
    allowed_modes = data.get("allowed_modes") or ["sync", "async", "stream"]
    with db_write() as s:
        agent = s.get(ExternalAgent, agent_id)
        if agent is None:
            raise ApiError(Code.NOT_FOUND, "外部 Agent 不存在")
        s.add(
            PlatformToken(
                id=token_id,
                agent_id=agent_id,
                name=data.get("name") or f"{agent.name} 令牌",
                token_hash=digest,
                prefix=prefix,
                scopes=scopes,
                allowed_modes=allowed_modes,
                expires_at=expires_at,
                rpm=data.get("rpm"),
                concurrency=data.get("concurrency"),
                daily_calls=data.get("daily_calls"),
                budget_usd=data.get("budget_usd"),
                created_by=actor,
                created_at=now_iso(),
            )
        )
        for scope_type, scope_id, payload in (
            ("token", token_id, {
                "rpm": data.get("rpm"),
                "concurrency": data.get("concurrency"),
                "daily_calls": data.get("daily_calls"),
                "monthly_budget_usd": data.get("budget_usd"),
            }),
        ):
            if any(v is not None for v in payload.values()):
                s.add(
                    QuotaLimit(
                        id=uuid_pk("qlt"),
                        scope_type=scope_type,
                        scope_id=scope_id,
                        rpm=payload["rpm"],
                        concurrency=payload["concurrency"],
                        daily_calls=payload["daily_calls"],
                        monthly_budget_usd=payload["monthly_budget_usd"],
                        updated_at=now_iso(),
                    )
                )
    audit.write(
        "token.created",
        object_type="platform_token",
        object_id=token_id,
        summary={"agent_id": agent_id, "scopes": scopes, "prefix": prefix},
        actor_id=actor,
    )
    # 不要把真实令牌拼进命令串：`--token <srk_...>` 会留在 shell 历史与进程列表里
    # （CLI 自己也会打一条同样的警告），与 README「令牌不出现在 argv 里」的承诺矛盾。
    # 令牌本身已经在响应体的 `token` 字段里单独返回，命令统一走 stdin。
    install_command = settings.install_command(token_stdin=True)
    return {
        "token_id": token_id,
        "token": full,
        "prefix": prefix,
        "scopes": scopes,
        "allowed_modes": allowed_modes,
        "expires_at": expires_at,
        "install_command": install_command,
        "warning": "令牌只在此处完整展示一次，请立即保存；平台只保留 sha256 校验值",
    }


def revoke_token(token_id: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(PlatformToken, token_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "令牌不存在")
        row.revoked_at = now_iso()
    audit.write("token.revoked", object_type="platform_token", object_id=token_id, actor_id=actor)
    return {"ok": True, "id": token_id, "revoked_at": now_iso()}


def list_tokens(*, agent_id: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    with db_session() as s:
        stmt = select(PlatformToken).order_by(PlatformToken.created_at)
        if agent_id:
            stmt = stmt.where(PlatformToken.agent_id == agent_id)
        rows = list(s.scalars(stmt))
        names = {a.id: a.name for a in s.scalars(select(ExternalAgent)).all()}
    items = [{**_token(r), "agent_id": r.agent_id, "agent_name": names.get(r.agent_id, "")} for r in rows]
    return paginate(items, page, page_size)


def quota_overview() -> dict:
    with db_session() as s:
        limits = list(s.scalars(select(QuotaLimit)))
        counters = list(s.scalars(select(QuotaCounter)))
    return {
        "limits": [
            {
                "scope_type": l.scope_type,
                "scope_id": l.scope_id,
                "rpm": l.rpm,
                "concurrency": l.concurrency,
                "daily_calls": l.daily_calls,
                "monthly_budget_usd": l.monthly_budget_usd,
            }
            for l in limits
        ],
        "counters": [
            {
                "scope_type": c.scope_type,
                "scope_id": c.scope_id,
                "period": c.period,
                "calls": c.calls,
                "cost_usd": c.cost_usd,
            }
            for c in counters
        ],
    }


def set_quota(scope_type: str, scope_id: str, payload: dict, *, actor: str | None = None) -> dict:
    if scope_type not in ("global", "provider", "capability", "token"):
        raise ApiError(Code.VALIDATION_FAILED, "scope_type 必须是 global/provider/capability/token")
    with db_write() as s:
        row = s.scalar(
            select(QuotaLimit).where(QuotaLimit.scope_type == scope_type, QuotaLimit.scope_id == scope_id)
        )
        if row is None:
            row = QuotaLimit(
                id=uuid_pk("qlt"),
                scope_type=scope_type,
                scope_id=scope_id,
                updated_at=now_iso(),
            )
            s.add(row)
        for key in ("rpm", "concurrency", "daily_calls", "monthly_budget_usd"):
            if key in payload:
                setattr(row, key, payload[key])
        row.updated_at = now_iso()
    return {"ok": True, "scope_type": scope_type, "scope_id": scope_id}
