"""凭证服务：加密落库、掩码、影响面、替换/轮换、凭证代理（§7.5 / §11.2 / §A.3.2）。

投影纪律（F-03 / §A.3.2）：`GET /credentials*`、`GET /providers/{id}` 的凭证摘要、
`/impact` 三个投影**只允许**出现 `_PUBLIC_META_KEYS` 里的键；`meta` 原样返回 JSON 列
视为缺陷。命中 `_SENSITIVE_META` 的 meta 键在**写路径**被摘出来信封加密进
`meta_sealed`（AAD `record_type="credential_meta"`），因此库里不再有敏感键
（不变式 I4）。服务端真正要读原始 meta 的地方（`materialize()`、`executors/auth.py`、
`services/credentials_verify.py`）不走投影。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

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

#: `status=expiring` 的窗口：`expires_at` 非空且 ≤7 天（§A.3.11）
EXPIRING_DAYS = 7

log = get_logger("skillrouter.run")

KINDS = ("llm", "image", "video", "speech", "asr", "generic")
AUTH_TYPES = ("api_key", "bearer", "basic", "header")

#: 白名单 = 仓库里真实写入/读取的键 + 明确预留的键；新增键必须同时改 spec §A.3.2
_PUBLIC_META_KEYS = (
    "provider_slug",     # api/v1/callbacks.py 按它找凭证
    "probe_path",        # services/credentials_verify.py
    "probe_method",      # services/credentials_verify.py
    "header_name",       # executors/auth.py
    "username",          # executors/auth.py（basic 认证用户名，非密钥）
    "mock",              # scripts/seed_demo.py（演示数据标记）
    "llm_protocol",      # iter3：LLM 协议规范（openai/anthropic/google/custom），非密钥
    "api_version",       # iter3：Azure 类端点的 api-version，非密钥
    "model", "region", "notes", "base_url_hint",   # 预留：UI 未来可填，当前无人写
)

#: 命中即视为敏感键：写路径密封进 `meta_sealed`，投影里不出现
_SENSITIVE_META = re.compile(r"(?i)(secret|token|passw|signature|webhook|api[_-]?key|private)")
#: 公开别名（`scripts/migrate_seal_meta.py` 与测试复用同一份判据，避免出现第二套正则）
SENSITIVE_META = _SENSITIVE_META

#: **无论名字是否命中正则**都必须密封的键（iter3 / spec §3.3）
#: `extra_headers` 的值可能是第二种密钥，而上面的正则匹配不到它；
#: 不显式登记就会以明文进 `row.meta` 并随 `_brief()` 出到列表响应里。
_ALWAYS_SEALED = frozenset({"extra_headers", "proxy_authorization"})

#: `meta_sealed` 的 AAD 固定值（与 `crypto/secretbox.py` 顶部登记表一致）
_META_RECORD_TYPE = "credential_meta"


def _partition_meta(meta: dict | None) -> tuple[dict, dict]:
    """按 `_SENSITIVE_META` + `_ALWAYS_SEALED` 把 meta 分成（可明文存储, 需密封）两份。

    `probe_path` / `probe_method` / `header_name` / `username` / `mock` 都不命中
    正则，因此不会被密封（密封它们会让探测与认证直接失效）。
    `extra_headers` / `proxy_authorization` 名字本身不敏感，但值可能是密钥，
    因此由 `_ALWAYS_SEALED` 强制密封（spec §3.3）。
    """
    public: dict = {}
    sensitive: dict = {}
    for key, value in (meta or {}).items():
        name = str(key)
        if name in _ALWAYS_SEALED or _SENSITIVE_META.search(name):
            sensitive[name] = value
        else:
            public[name] = value
    return public, sensitive


def _seal_meta(credential_id: str, mapping: dict) -> str:
    payload = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    return secretbox.seal(payload, record_type=_META_RECORD_TYPE, record_id=credential_id)


def _merge_sealed_meta(existing: str | None, credential_id: str, incoming: dict) -> str | None:
    """把新的敏感键并进已有密文（值为 `None` 视为删除）。"""
    merged: dict = {}
    if existing:
        try:
            loaded = json.loads(
                secretbox.open_sealed(existing, record_type=_META_RECORD_TYPE, record_id=credential_id)
            )
            if isinstance(loaded, dict):
                merged.update(loaded)
        except Exception as exc:  # noqa: BLE001 - 打不开就只保留新值，不阻断写入
            log.warning("meta_sealed_unreadable", extra={"object": credential_id, "detail": redact(str(exc))})
    merged.update(incoming)
    kept = {k: v for k, v in merged.items() if v is not None}
    return _seal_meta(credential_id, kept) if kept else None


def _sealed_meta(row: Credential) -> dict:
    """解封 `meta_sealed`（仅回调验签/轮换使用；投影只取键名）。"""
    sealed = getattr(row, "meta_sealed", None)
    if not sealed:
        return {}
    try:
        loaded = json.loads(
            secretbox.open_sealed(sealed, record_type=_META_RECORD_TYPE, record_id=row.id)
        )
    except Exception as exc:  # noqa: BLE001 - 旧密钥缺失时不能拖垮整个列表
        log.warning("meta_sealed_unreadable", extra={"object": row.id, "detail": redact(str(exc))})
        return {}
    return loaded if isinstance(loaded, dict) else {}


def sealed_meta_of(row: Credential) -> dict:
    """对外暴露的解封入口（`api/v1/callbacks.py` 的验签回落用）。"""
    return _sealed_meta(row)


def migrate_meta(credential_id: str, meta: dict, existing_sealed: str | None) -> tuple[dict, str | None]:
    """回填用入口（幂等）：把 meta 里的敏感键密封，返回（可明文的 meta, 新密文）。

    `scripts/migrate_seal_meta.py` 与写路径共用同一实现，避免出现第二套密封逻辑。
    """
    public_meta, sensitive_meta = _partition_meta(meta)
    if not sensitive_meta:
        return public_meta, existing_sealed
    return public_meta, _merge_sealed_meta(existing_sealed, credential_id, sensitive_meta)


def _public_meta(meta: dict | None) -> dict:
    """白名单投影：只回 `_PUBLIC_META_KEYS` 里的键；被剔除的键名交给 `meta_redacted_keys`。"""
    data = meta or {}
    return {key: value for key, value in data.items() if key in _PUBLIC_META_KEYS}


def _meta_redacted_keys(row: Credential) -> list[str]:
    """被投影剔除的键名（白名单外的现存键 + 已密封的键）。"""
    meta = row.meta or {}
    keys = {key for key in meta if key not in _PUBLIC_META_KEYS}
    keys |= set(_sealed_meta(row))
    return sorted(keys)


def _brief(row: Credential) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "name": row.name,
        "kind": row.kind,
        "auth_type": row.auth_type,
        "header_name": row.header_name,
        "secret_mask": row.secret_mask,
        "secret_fingerprint": row.secret_fingerprint,
        # 兼容旧响应形状（前端已迁移到 secret_mask，保留一版以免连接包/脚本报错）
        "secret": {"masked": row.secret_mask},
        "meta": _public_meta(row.meta),
        "meta_redacted_keys": _meta_redacted_keys(row),
        "meta_sealed": bool(row.meta_sealed),
        "status": row.status,
        "expires_at": row.expires_at,
        "last_verified_at": row.last_verified_at,
        "last_verify_ok": None if row.last_verify_ok is None else bool(row.last_verify_ok),
        "rotation_of": row.rotation_of,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def summarize(row: Credential) -> dict:
    """凭证摘要投影（`services/providers.py` 详情复用同一字段集，不留两套字段名）。"""
    return _brief(row)


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
        # `q` 覆盖 name / kind / provider_name（§A.3.11）；`provider_name` 来自上一步的 JOIN 结果
        needle = q.lower()
        items = [
            i
            for i in items
            if needle in str(i.get("name") or "").lower()
            or needle in str(i.get("kind") or "").lower()
            or needle in str(i.get("provider_name") or "").lower()
        ]
    if status == "expiring":
        # 「即将过期」= `expires_at` 非空且 ≤ EXPIRING_DAYS 天（§A.3.11）
        horizon = (
            datetime.now(UTC) + timedelta(days=EXPIRING_DAYS)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        items = [i for i in items if i.get("expires_at") and i["expires_at"] <= horizon]
    elif status:
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
    public_meta, sensitive_meta = _partition_meta(data.get("meta"))
    meta_sealed = _seal_meta(credential_id, sensitive_meta) if sensitive_meta else None
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
                meta=public_meta,
                meta_sealed=meta_sealed,
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
        for key in ("name", "kind", "auth_type", "header_name", "expires_at", "status"):
            if key in patch and patch[key] is not None:
                setattr(row, key, patch[key])
        if patch.get("meta") is not None:
            # 写路径即收口：敏感键进 meta_sealed，meta 只留白名单可见的明文键（I4）
            public_meta, sensitive_meta = _partition_meta(patch["meta"])
            row.meta = {**(row.meta or {}), **public_meta}
            if sensitive_meta:
                row.meta_sealed = _merge_sealed_meta(row.meta_sealed, row.id, sensitive_meta)
        row.updated_at = now_iso()
    # 目录失效必须覆盖「凭证写路径」（spec §3.5 / R-11）：改了协议/地址/认证方式后，
    # 旧清单就是别人的清单，不能等前端/脚本自己叫 refresh。
    if _catalog_affected_by_patch(patch):
        invalidate_catalog(credential_id)
    return get_credential(credential_id)


#: 一次 `PATCH /credentials/{id}` 里会改变「模型清单归属」的键
_CATALOG_AFFECTING_KEYS = ("auth_type", "header_name", "kind")


def _catalog_affected_by_patch(patch: dict) -> bool:
    if any(key in patch and patch[key] is not None for key in _CATALOG_AFFECTING_KEYS):
        return True
    meta = patch.get("meta") or {}
    return any(key in meta for key in ("llm_protocol", "llm_paths", "api_version", "extra_headers"))


def invalidate_catalog(credential_id: str) -> None:
    """清掉该凭证的模型清单缓存（失败不影响主流程；缓存只是加速）。"""
    from .llm import catalog as llm_catalog

    try:
        llm_catalog.invalidate(credential_id)
    except Exception as exc:  # noqa: BLE001 - 缓存清理失败不能把写路径拖红
        log.warning(
            "llm_catalog_invalidate_failed",
            extra={"object": credential_id, "detail": redact(str(exc))},
        )


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
    invalidate_catalog(credential_id)
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
    invalidate_catalog(credential_id)
    return get_credential(credential_id)


def rotate(credential_id: str, secret: str, *, name: str | None = None, actor: str | None = None) -> dict:
    """生成新凭证行并原子切换所有 route 引用。"""
    new_id = uuid_pk("crd")
    ciphertext = secretbox.seal(secret, record_type="credential", record_id=new_id)
    with db_write() as s:
        old = s.get(Credential, credential_id)
        if old is None or old.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        # 敏感 meta 的 AAD 里带 record_id：换行必须用新 id 重新密封（否则新行打不开）
        public_meta, legacy_sensitive = _partition_meta(old.meta)
        sealed_source = old.meta_sealed
        if legacy_sensitive:
            sealed_source = _merge_sealed_meta(sealed_source, old.id, legacy_sensitive)
        sealed_for_new = (
            secretbox.seal(
                secretbox.open_sealed(sealed_source, record_type=_META_RECORD_TYPE, record_id=old.id),
                record_type=_META_RECORD_TYPE,
                record_id=new_id,
            )
            if sealed_source
            else None
        )
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
                meta=public_meta,
                meta_sealed=sealed_for_new,
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
    # 旧行被停用、新行是新身份：两条的清单缓存都必须清（spec §3.5）
    invalidate_catalog(credential_id)
    invalidate_catalog(new_id)
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


def materialize(credential_id: str, *, include_sealed: bool = False) -> dict:
    """凭证代理：只在请求发出的瞬间解封，作用域仅本次调用（D5 / §11.2）。

    `include_sealed=True` 时把 `meta_sealed` 里的敏感键解封并合进 `meta`
    （LLM 探测与轮次注入需要 `extra_headers` / `api_version`）。
    默认 `False` 保持既有调用点（`credentials_verify` / `executors` / `api/v1/callbacks`）
    的行为**完全不变**（spec §3.3）。
    """
    with db_session() as s:
        row = s.get(Credential, credential_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "凭证不存在")
        if row.status != "active":
            raise ApiError(
                Code.CREDENTIAL_DISABLED,
                message=f"凭证状态为 {row.status}，无法调用",
                hint="在凭证页启用它，或替换/轮换一把新密钥",
                details={"credential_id": credential_id, "status": row.status},
            )
        provider = s.get(Provider, row.provider_id)
        secret = secretbox.open_sealed(
            row.secret_ciphertext, record_type="credential", record_id=row.id
        )
        meta = dict(row.meta or {})
        if include_sealed:
            meta.update(_sealed_meta(row))
        return {
            "credential_id": row.id,
            "credential_name": row.name,
            "secret_mask": row.secret_mask,
            "provider_id": row.provider_id,
            "provider_slug": provider.slug if provider else None,
            "provider_name": provider.name if provider else None,
            "base_url": provider.base_url if provider else None,
            "kind": row.kind,
            "auth_type": row.auth_type,
            "header_name": row.header_name,
            "meta": meta,
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
    from datetime import datetime, timedelta

    limit = (
        datetime.now(UTC) + timedelta(days=days)
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
    "summarize",
    "sealed_meta_of",
    "migrate_meta",
    "invalidate_catalog",
    "SENSITIVE_META",
    "log",
]
