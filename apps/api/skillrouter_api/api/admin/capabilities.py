"""能力路由：CRUD / 校验 / 测试 / 发布 / 版本 / 影响面（§9.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import page_params, require_password_changed
from ...schemas.capabilities import (
    CapabilityCreate,
    CapabilityTestRequest,
    CapabilityUpdate,
    CompatibilityRequest,
    PublishRequest,
    RollbackRequest,
    RouteUpsert,
)
from ...security.sessions import SessionContext
from ...services import capabilities

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("")
async def list_capabilities(
    kind: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return capabilities.list_capabilities(
        q=paging["q"], kind=kind, status=status, provider=provider, mode=mode,
        page=paging["page"], page_size=paging["page_size"], sort=paging["sort"],
    )


@router.post("", status_code=201)
async def create_capability(
    payload: CapabilityCreate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.create_capability(payload.model_dump(), actor=ctx.admin_id)


@router.get("/{capability_id}")
async def get_capability(capability_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return capabilities.get_capability(capability_id)


@router.patch("/{capability_id}")
async def update_capability(
    capability_id: str, payload: CapabilityUpdate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.update_capability(
        capability_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id
    )


@router.delete("/{capability_id}")
async def delete_capability(capability_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return capabilities.delete(capability_id, actor=ctx.admin_id)


@router.post("/{capability_id}/validate")
async def validate_capability(
    capability_id: str, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.validate(capability_id)


@router.post("/{capability_id}/test")
async def test_capability(
    capability_id: str, payload: CapabilityTestRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return await capabilities.test_capability(capability_id, payload.model_dump(), actor=ctx.admin_id)


@router.post("/{capability_id}/publish")
async def publish_capability(
    capability_id: str, payload: PublishRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.publish(
        capability_id, version=payload.version, changelog=payload.changelog, actor=ctx.admin_id
    )


@router.post("/{capability_id}/deprecate")
async def deprecate_capability(
    capability_id: str, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.deprecate(capability_id, actor=ctx.admin_id)


@router.get("/{capability_id}/versions")
async def list_versions(capability_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": capabilities.list_versions(capability_id)}


@router.get("/{capability_id}/versions/{version}")
async def version_detail(
    capability_id: str, version: str, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.version_detail(capability_id, version)


@router.get("/{capability_id}/diff")
async def diff_versions(
    capability_id: str,
    from_version: str,
    to_version: str,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return capabilities.diff_versions(capability_id, from_version, to_version)


@router.post("/{capability_id}/rollback")
async def rollback_capability(
    capability_id: str, payload: RollbackRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.rollback(capability_id, payload.to_version, actor=ctx.admin_id)


@router.post("/{capability_id}/compatibility")
async def confirm_compatibility(
    capability_id: str, payload: CompatibilityRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.confirm_compatibility(
        capability_id, payload.group, notes=payload.notes, actor=ctx.admin_id
    )


@router.get("/{capability_id}/impact")
async def capability_impact(capability_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return capabilities.impact(capability_id)


@router.get("/{capability_id}/routes")
async def list_routes(capability_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": capabilities.list_routes(capability_id)}


@router.post("/{capability_id}/routes", status_code=201)
async def upsert_route(
    capability_id: str, payload: RouteUpsert, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.upsert_route(capability_id, payload.model_dump(by_alias=True), actor=ctx.admin_id)


@router.patch("/{capability_id}/routes/{route_id}")
async def patch_route(
    capability_id: str,
    route_id: str,
    payload: RouteUpsert,
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    body = payload.model_dump(by_alias=True)
    body["route_id"] = route_id
    return capabilities.upsert_route(capability_id, body, actor=ctx.admin_id)


@router.delete("/{capability_id}/routes/{route_id}")
async def delete_route(
    capability_id: str, route_id: str, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return capabilities.delete_route(capability_id, route_id, actor=ctx.admin_id)
