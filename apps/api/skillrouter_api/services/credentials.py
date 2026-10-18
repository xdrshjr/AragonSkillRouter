"""凭证服务：加密落库、掩码、影响面、替换/轮换、凭证代理（§7.5 / §11.2）。"""

from __future__ import annotations

import hashlib

from sqlalchemy import desc, select

from ..crypto import secretbox
from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, mask_secret, redact
from ..models import (
    Capability,
    CapabilityRoute,
    Credential,
    CredentialVerification,
    Provider,
    Skill,
)
from ..schemas.common import paginate, sort_items
from . import audit

log = get_logger("skillrouter.run")

KINDS = ("llm", "image", "video", "speech", "asr", "generic")
AUTH_TYPES = ("api_key", "bearer", "basic", "header")


def _brief(row: Credential) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "name": row.name,
        "kind": row.kind,
        "auth_type": row.auth_type,
        "header_name": row.header_name,
        "secret": {"masked": row.secret_mask},
        "meta": row.meta or {},
        "status": row.status,
        "expires_at": row.expires_at,
        "last_verified_at": row.last_verified_at,
        "last_verify_ok": None if row.last_verify_ok is None else bool(row.last_verify_ok),
        "rotation_of": row.rotation_of,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_credentials(
    *,
    provider_id: str | None = None,
    q: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = None,
) -> dict:
    with db_session() as s:
        stmt = select(Credential).where(Credential.deleted_at.is_(None))
        if provider_id:
            stmt = stmt.where(Credential.provider_id == provider_id)
        rows = list(s.scalars(stmt))
        names = {p.id: p.name for p in s.scalars(select(Provider)).all()}
    items = []
    for row in rows:
        data = _brief(row)
        data["provider_name"] = names.get(row.provider_id, "")
        items.append(data)
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["name"].lower()]
    if status:
        items = [i for i in items if i["status"] == status]
    if kind:
        items = [i for i in items if i["kind"] == kind]
    items = sort_items(items, sort, default="-created_at")
    return paginate(items, page, page_size)


def get_credential(credential_id: str) -> dict:
    with db_session() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        data = _brief(row)
        provider = s.get(Provider, row.provider_id)
        data["provider"] = {"id": provider.id, "name": provider.name} if provider else None
        data["verifications"] = [
            {
                "id": v.id,
                "ok": bool(v.ok),
                "status_code": v.status_code,
                "latency_ms": v.latency_ms,
                "error_code": v.error_code,
                "detail_redacted": v.detail_redacted,
                "at": v.at,
            }
            for v in s.scalars(
                select(CredentialVerification)
                .where(CredentialVerification.credential_id == credential_id)
                .order_by(desc(CredentialVerification.at))
                .limit(20)
            )
        ]
        data["ref_count"] = len(
            list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.credential_id == credential_id)))
        )
    return data


def create_credential(provider_id: str, data: dict, *, actor: str | None = None) -> dict:
    if data.get("kind", "llm") not in KINDS:
        raise ApiError(Code.VALIDATION_FAILED, f"kind 必须是 {', '.join(KINDS)}")
    if data.get("auth_type", "api_key") not in AUTH_TYPES:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"auth_type 必须是 {', '.join(AUTH_TYPES)}",
            hint="OAuth / 动态签名属于适配器扩展位，本阶段不支持",
        )
    credential_id = uuid_pk("crd")
    secret = data["secret"]
    ciphertext = secretbox.seal(secret, record_type="credential", record_id=credential_id)
    with db_write() as s:
        provider = s.get(Provider, provider_id)
        if provider is None or provider.deleted_at:
            raise ApiError(Code.NOT_FOUND, "提供商不存在")
        s.add(
            Credential(
                id=credential_id,
                provider_id=provider_id,
                name=data["name"],
                kind=data.get("kind", "llm"),
                auth_type=data.get("auth_type", "api_key"),
                header_name=data.get("header_name"),
                secret_ciphertext=ciphertext,
                secret_mask=mask_secret(secret),
                secret_fingerprint=hashlib.sha256(secret.encode()).hexdigest()[:12],
                meta=data.get("meta") or {},
                status="active",
                expires_at=data.get("expires_at"),
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    audit.write(
        "credential.created",
        object_type="credential",
        object_id=credential_id,
        summary={"provider_id": provider_id, "masked": mask_secret(secret)},
        actor_id=actor,
    )
    return get_credential(credential_id)


def update_credential(credential_id: str, patch: dict, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        for key in ("name", "kind", "auth_type", "header_name", "meta", "expires_at", "status"):
            if key in patch and patch[key] is not None:
                setattr(row, key, patch[key])
        row.updated_at = now_iso()
    return get_credential(credential_id)


def set_status(credential_id: str, status: str, *, actor: str | None = None) -> dict:
    if status not in ("active", "disabled", "expired"):
        raise ApiError(Code.VALIDATION_FAILED, "status 非法")
    with db_write() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        row.status = status
        row.updated_at = now_iso()
    audit.write(
        f"credential.{'disabled' if status == 'disabled' else 'enabled'}",
        object_type="credential",
        object_id=credential_id,
        summary={"status": status},
        actor_id=actor,
    )
    return get_credential(credential_id)


def replace_secret(credential_id: str, secret: str, *, actor: str | None = None) -> dict:
    ciphertext = secretbox.seal(secret, record_type="credential", record_id=credential_id)
    with db_write() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        row.secret_ciphertext = ciphertext
        row.secret_mask = mask_secret(secret)
        row.secret_fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:12]
        row.last_verified_at = None
        row.last_verify_ok = None
        row.updated_at = now_iso()
    audit.write(
        "credential.replaced",
        object_type="credential",
        object_id=credential_id,
        summary={"masked": mask_secret(secret)},
        actor_id=actor,
    )
    return get_credential(credential_id)


def rotate(credential_id: str, secret: str, *, name: str | None = None, actor: str | None = None) -> dict:
    """生成新凭证行并原子切换所有 route 引用。"""
    new_id = uuid_pk("crd")
    ciphertext = secretbox.seal(secret, record_type="credential", record_id=new_id)
    with db_write() as s:
        old = s.get(Credential, credential_id)
        if old is None or old.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        s.add(
            Credential(
                id=new_id,
                provider_id=old.provider_id,
                name=name or f"{old.name}（轮换）",
                kind=old.kind,
                auth_type=old.auth_type,
                header_name=old.header_name,
                secret_ciphertext=ciphertext,
                secret_mask=mask_secret(secret),
                secret_fingerprint=hashlib.sha256(secret.encode()).hexdigest()[:12],
                meta=old.meta or {},
                status="active",
                expires_at=old.expires_at,
                rotation_of=old.id,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        routes = list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.credential_id == credential_id)))
        for route in routes:
            route.credential_id = new_id
            route.updated_at = now_iso()
        old.status = "disabled"
        old.updated_at = now_iso()
    audit.write(
        "credential.rotated",
        object_type="credential",
        object_id=new_id,
        summary={"rotation_of": credential_id, "routes": len(routes)},
        actor_id=actor,
    )
    return get_credential(new_id)


def impact(credential_id: str) -> dict:
    with db_session() as s:
        cred = s.get(Credential, credential_id)
        if cred is None:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        routes = list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.credential_id == credential_id)))
        caps = {c.id: c for c in s.scalars(select(Capability)).all()}
        capabilities = [
            {
                "id": r.capability_id,
                "key": caps[r.capability_id].key if r.capability_id in caps else r.capability_id,
                "name": caps[r.capability_id].name if r.capability_id in caps else "",
                "status": caps[r.capability_id].status if r.capability_id in caps else "unknown",
                "route": r.route_key,
                "enabled": bool(r.enabled),
            }
            for r in routes
        ]
        skills = []
        for skill in s.scalars(select(Skill).where(Skill.deleted_at.is_(None))):
            deps = (skill.draft_definition or {}).get("dependencies") or []
            keys = {c["key"] for c in capabilities}
            if any(dep.get("capability_key") in keys for dep in deps):
                skills.append({"id": skill.id, "name": skill.name, "version": skill.latest_version})
        provider = s.get(Provider, cred.provider_id)
    return {
        "credential": {"id": cred.id, "name": cred.name, "masked": cred.secret_mask, "status": cred.status},
        "provider": {"id": provider.id, "name": provider.name} if provider else None,
        "capabilities": capabilities,
        "skills": skills,
        "external_tokens": [],
        "ref_count": len(routes),
        "blocking": bool(routes),
    }


def delete(credential_id: str, *, actor: str | None = None) -> dict:
    data = impact(credential_id)
    if data["ref_count"] > 0:
        raise ApiError(
            Code.CREDENTIAL_IN_USE,
            message="凭证仍被能力路由引用，无法删除",
            hint="先切换这些 route 的凭证，或改为停用",
            details=data,
        )
    with db_write() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        row.deleted_at = now_iso()
        row.status = "disabled"
    audit.write("credential.deleted", object_type="credential", object_id=credential_id, actor_id=actor)
    return {"ok": True, "id": credential_id}


def materialize(credential_id: str) -> dict:
    """凭证代理：只在请求发出的瞬间解封，作用域仅本次调用（D5 / §11.2）。"""
    with db_session() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        if row.status != "active":
            raise ApiError(
                Code.CREDENTIAL_DISABLED,
                message=f"凭证状态为 {row.status}，无法调用",
                details={"credential_id": credential_id},
            )
        provider = s.get(Provider, row.provider_id)
        secret = secretbox.open_sealed(
            row.secret_ciphertext, record_type="credential", record_id=row.id
        )
        return {
            "credential_id": row.id,
            "provider_id": row.provider_id,
            "provider_slug": provider.slug if provider else None,
            "base_url": provider.base_url if provider else None,
            "kind": row.kind,
            "auth_type": row.auth_type,
            "header_name": row.header_name,
            "meta": row.meta or {},
            "secret": secret,
        }


def verification_history(credential_id: str, page: int = 1, page_size: int = 20) -> dict:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(CredentialVerification)
                .where(CredentialVerification.credential_id == credential_id)
                .order_by(desc(CredentialVerification.at))
                .limit(500)
            )
        )
    items = [
        {
            "id": v.id,
            "ok": bool(v.ok),
            "status_code": v.status_code,
            "latency_ms": v.latency_ms,
            "error_code": v.error_code,
            "detail_redacted": v.detail_redacted,
            "at": v.at,
        }
        for v in rows
    ]
    return paginate(items, page, page_size)


def record_verification(
    credential_id: str,
    *,
    ok: bool,
    status_code: int | None,
    latency_ms: int | None,
    error_code: str | None,
    detail: str | None,
) -> dict:
    detail_redacted = redact(detail or "", max_len=1024)
    with db_write() as s:
        row = s.get(Credential, credential_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        entry_id = uuid_pk("cvr")
        s.add(
            CredentialVerification(
                id=entry_id,
                credential_id=credential_id,
                ok=1 if ok else 0,
                status_code=status_code,
                latency_ms=latency_ms,
                error_code=error_code,
                detail_redacted=detail_redacted,
                at=now_iso(),
            )
        )
        row.last_verified_at = now_iso()
        row.last_verify_ok = 1 if ok else 0
        if not ok and error_code in ("UPSTREAM_AUTH",):
            row.status = "expired"
            row.updated_at = now_iso()
    return {
        "verification_id": entry_id,
        "ok": ok,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error_code": error_code,
        "detail_redacted": detail_redacted,
    }


def expiring_within(days: int = 7) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    limit = (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_session() as s:
        rows = list(
            s.scalars(
                select(Credential).where(
                    Credential.deleted_at.is_(None),
                    Credential.expires_at.is_not(None),
                    Credential.expires_at <= limit,
                )
            )
        )
    return [_brief(r) for r in rows]


__all__ = [
    "list_credentials",
    "get_credential",
    "create_credential",
    "update_credential",
    "set_status",
    "replace_secret",
    "rotate",
    "impact",
    "delete",
    "materialize",
    "record_verification",
    "verification_history",
    "expiring_within",
    "log",
]
