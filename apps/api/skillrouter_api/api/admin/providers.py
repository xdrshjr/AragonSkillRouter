"""提供商与凭证路由（§9.2 / §A.3.11）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import page_params, require_password_changed
from ...errors import ApiError, Code
from ...schemas.providers import (
    CredentialCreate,
    CredentialRotate,
    CredentialUpdate,
    ProviderCreate,
    ProviderUpdate,
    SecretReplace,
)
from ...security.sessions import SessionContext
from ...services import credentials, credentials_verify, providers

router = APIRouter(tags=["providers"])

#: `GET /credentials` 的 `sort` 白名单（§A.3.11）
CREDENTIAL_SORT_FIELDS = ("name", "created_at", "expires_at", "last_verified_at")


# ── 提供商 ───────────────────────────────────────────────────


@router.get("/providers")
async def list_providers(
    status: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return providers.list_providers(
        q=q, status=status, category=category, tag=tag,
        page=paging["page"], page_size=paging["page_size"], sort=paging["sort"],
    )


@router.post("/providers", status_code=201)
async def create_provider(payload: ProviderCreate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return providers.create_provider(payload.model_dump(), actor=ctx.admin_id)


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return providers.get_provider(provider_id)


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str, payload: ProviderUpdate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return providers.update_provider(provider_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str, force: bool = False, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return providers.delete_provider(provider_id, force=force, actor=ctx.admin_id)


# ── 凭证 ─────────────────────────────────────────────────────


@router.get("/providers/{provider_id}/credentials")
async def list_provider_credentials(
    provider_id: str,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return credentials.list_credentials(
        provider_id=provider_id, page=paging["page"], page_size=paging["page_size"], sort=paging["sort"], q=paging["q"]
    )


@router.post("/providers/{provider_id}/credentials", status_code=201)
async def create_credential(
    provider_id: str, payload: CredentialCreate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return credentials.create_credential(provider_id, payload.model_dump(), actor=ctx.admin_id)


@router.get("/credentials")
async def list_credentials(
    status: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """分页 + `sort`（白名单）+ `status=expiring`（≤7 天）+ `q`（name/kind/provider_name）。

    字段投影由 `services/credentials.py` 单点负责（`secret_mask`/`secret_fingerprint`/…），
    这里只做查询参数、白名单校验与「服务层不支持的那两维」的本地筛选。
    """
    sort = paging["sort"]
    if sort:
        for token in [part.strip() for part in str(sort).split(",") if part.strip()]:
            field = token.lstrip("-+")
            if field not in CREDENTIAL_SORT_FIELDS:
                raise ApiError(
                    Code.VALIDATION_FAILED,
                    message=f"不支持的排序字段：{field}",
                    details={"allowed": list(CREDENTIAL_SORT_FIELDS)},
                )
    # q / status=expiring / provider_name 全部在服务层做（含 JOIN providers）：
    # 曾经在这里只取前 100 条再本地筛选，超过 100 条凭证时「即将过期」会漏、搜索会漏、total 也会错。
    return credentials.list_credentials(
        q=q,
        status=status,
        kind=kind,
        page=paging["page"],
        page_size=paging["page_size"],
        sort=sort,
    )


@router.get("/credentials/{credential_id}")
async def get_credential(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return credentials.get_credential(credential_id)


@router.patch("/credentials/{credential_id}")
async def update_credential(
    credential_id: str, payload: CredentialUpdate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return credentials.update_credential(credential_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id)


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return credentials.delete(credential_id, actor=ctx.admin_id)


@router.post("/credentials/{credential_id}/verify")
async def verify_credential(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return await credentials_verify.verify(credential_id, actor=ctx.admin_id)


@router.get("/credentials/{credential_id}/verifications")
async def verification_history(
    credential_id: str,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return credentials.verification_history(credential_id, paging["page"], paging["page_size"])


@router.get("/credentials/{credential_id}/models")
async def credential_models(
    credential_id: str,
    protocol: str | None = None,
    refresh: bool = False,
    include_non_llm: bool = False,
    limit: int = 500,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    """`GET /api/admin/credentials/{id}/models`：选了已有凭证 → 立刻有模型清单（spec §5.4）。"""
    from .llm import credential_models as list_credential_models

    return await list_credential_models(
        credential_id,
        protocol=protocol,
        refresh=refresh,
        include_non_llm=include_non_llm,
        limit=max(1, min(int(limit or 500), 500)),
        actor=ctx.admin_id,
    )


@router.post("/credentials/{credential_id}/disable")
async def disable_credential(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return credentials.set_status(credential_id, "disabled", actor=ctx.admin_id)


@router.post("/credentials/{credential_id}/enable")
async def enable_credential(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return credentials.set_status(credential_id, "active", actor=ctx.admin_id)


@router.post("/credentials/{credential_id}/replace")
async def replace_secret(
    credential_id: str, payload: SecretReplace, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return credentials.replace_secret(credential_id, payload.secret, actor=ctx.admin_id)


@router.post("/credentials/{credential_id}/rotate")
async def rotate_credential(
    credential_id: str, payload: CredentialRotate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return credentials.rotate(credential_id, payload.secret, name=payload.name, actor=ctx.admin_id)


@router.get("/credentials/{credential_id}/impact")
async def credential_impact(credential_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return credentials.impact(credential_id)
