"""提供商服务（§7.5）。"""

from __future__ import annotations

from sqlalchemy import func, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..models import Capability, CapabilityRoute, Credential, Provider
from ..schemas.common import paginate, sort_items
from . import audit, credentials

CATEGORIES = ("llm", "image", "video", "speech", "asr", "generic")


def _brief(row: Provider) -> dict:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "category": row.category,
        "base_url": row.base_url,
        "docs_url": row.docs_url,
        "tags": row.tags or [],
        "environment": row.environment,
        "notes": row.notes,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_providers(
    *,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = None,
) -> dict:
    with db_session() as s:
        rows = list(s.scalars(select(Provider).where(Provider.deleted_at.is_(None))))
        cred_counts = dict(
            s.execute(
                select(Credential.provider_id, func.count())
                .where(Credential.deleted_at.is_(None))
                .group_by(Credential.provider_id)
            ).all()
        )
        route_counts = dict(
            s.execute(
                select(CapabilityRoute.provider_id, func.count()).group_by(CapabilityRoute.provider_id)
            ).all()
        )
        active_creds = {
            row.provider_id
            for row in s.scalars(
                select(Credential).where(
                    Credential.deleted_at.is_(None), Credential.status == "active"
                )
            )
        }
    items = []
    for row in rows:
        data = _brief(row)
        data["credential_count"] = int(cred_counts.get(row.id, 0))
        data["capability_count"] = int(route_counts.get(row.id, 0))
        data["has_active_credential"] = row.id in active_creds
        data["route_count"] = int(route_counts.get(row.id, 0))
        items.append(data)
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["name"].lower() or needle in i["slug"].lower()]
    if status:
        items = [i for i in items if i["status"] == status]
    if category:
        items = [i for i in items if i["category"] == category]
    if tag:
        items = [i for i in items if tag in (i["tags"] or [])]
    items = sort_items(items, sort, default="name")
    return paginate(items, page, page_size)


def get_provider(provider_id: str) -> dict:
    with db_session() as s:
        row = s.get(Provider, provider_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "提供商不存在")
        data = _brief(row)
        creds = list(
            s.scalars(
                select(Credential).where(
                    Credential.provider_id == provider_id, Credential.deleted_at.is_(None)
                )
            )
        )
        routes = list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.provider_id == provider_id)))
        caps = {c.id: c for c in s.scalars(select(Capability)).all()}
        # 凭证摘要复用 credentials 的同一投影（字段名一致，不留两套；§A.3.2 / §A.5.2）
        data["credentials"] = [credentials.summarize(c) for c in creds]
        data["capabilities"] = [
            {
                "id": r.capability_id,
                "key": caps[r.capability_id].key if r.capability_id in caps else r.capability_id,
                "name": caps[r.capability_id].name if r.capability_id in caps else "",
                "status": caps[r.capability_id].status if r.capability_id in caps else "unknown",
                "route_id": r.route_key,
                "credential_id": r.credential_id,
                "priority": r.priority,
                "enabled": bool(r.enabled),
            }
            for r in routes
        ]
    return data


def create_provider(data: dict, *, actor: str | None = None) -> dict:
    if data.get("category") not in CATEGORIES:
        raise ApiError(Code.VALIDATION_FAILED, f"category 必须是 {', '.join(CATEGORIES)}")
    with db_write() as s:
        if s.scalar(select(Provider).where(Provider.slug == data["slug"])):
            raise ApiError(Code.CONFLICT, f"slug 已存在：{data['slug']}")
        row = Provider(
            id=uuid_pk("prv"),
            slug=data["slug"],
            name=data["name"],
            category=data.get("category", "llm"),
            base_url=data.get("base_url"),
            docs_url=data.get("docs_url"),
            tags=data.get("tags") or [],
            environment=data.get("environment", "prod"),
            notes=data.get("notes"),
            status=data.get("status", "active"),
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        s.add(row)
        provider_id = row.id
    audit.write(
        "provider.created",
        object_type="provider",
        object_id=provider_id,
        summary={"slug": data["slug"]},
        actor_id=actor,
    )
    return get_provider(provider_id)


def update_provider(provider_id: str, patch: dict, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Provider, provider_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "提供商不存在")
        for key, value in patch.items():
            if value is not None and hasattr(row, key):
                setattr(row, key, value)
        row.updated_at = now_iso()
    return get_provider(provider_id)


def delete_provider(provider_id: str, *, force: bool = False, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Provider, provider_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "提供商不存在")
        creds = list(
            s.scalars(
                select(Credential).where(
                    Credential.provider_id == provider_id, Credential.deleted_at.is_(None)
                )
            )
        )
        routes = list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.provider_id == provider_id)))
        if (creds or routes) and not force:
            raise ApiError(
                Code.PROVIDER_IN_USE,
                message="提供商仍有关联凭证或能力路由",
                hint="先查看影响面，或使用 ?force=true 软删",
                details={
                    "credentials": [{"id": c.id, "name": c.name} for c in creds],
                    "capabilities": [{"id": r.capability_id, "route": r.route_key} for r in routes],
                },
            )
        row.deleted_at = now_iso()
        row.status = "disabled"
    audit.write("provider.deleted", object_type="provider", object_id=provider_id, actor_id=actor)
    return {"ok": True, "id": provider_id, "forced": force}
