"""SKILL 路由（§9.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...deps import page_params, require_password_changed
from ...errors import ApiError, Code
from ...schemas.skills import (
    ExportRequest,
    ImportCommitRequest,
    ImportPreviewRequest,
    PublishSkillRequest,
    SkillCreate,
    SkillTestRequest,
    SkillUpdate,
)
from ...security.sessions import SessionContext
from ...services import skillimport, skills

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills(
    status: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return skills.list_skills(
        q=paging["q"], status=status, category=category, enabled=enabled,
        page=paging["page"], page_size=paging["page_size"], sort=paging["sort"],
    )


@router.post("", status_code=201)
async def create_skill(payload: SkillCreate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.create_skill(payload.model_dump(), actor=ctx.admin_id)


@router.post("/import/preview")
async def import_preview(
    payload: ImportPreviewRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return await skillimport.preview(payload.model_dump())


@router.post("/import/commit", status_code=201)
async def import_commit(
    payload: ImportCommitRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return skillimport.commit(payload.model_dump(), actor=ctx.admin_id)


@router.get("/{skill_id}")
async def get_skill(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.get_skill(skill_id)


@router.patch("/{skill_id}")
async def update_skill(
    skill_id: str, payload: SkillUpdate, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return skills.update_skill(skill_id, payload.model_dump(exclude_none=True), actor=ctx.admin_id)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.delete(skill_id, actor=ctx.admin_id)


@router.post("/{skill_id}/test")
async def test_skill(
    skill_id: str, payload: SkillTestRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return await skills.test_skill(skill_id, payload.model_dump(), actor=ctx.admin_id)


@router.post("/{skill_id}/publish")
async def publish_skill(
    skill_id: str, payload: PublishSkillRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return skills.publish(skill_id, version=payload.version, changelog=payload.changelog, actor=ctx.admin_id)


@router.post("/{skill_id}/deprecate")
async def deprecate_skill(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.deprecate(skill_id, actor=ctx.admin_id)


@router.post("/{skill_id}/enable")
async def enable_skill(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.set_enabled(skill_id, True, actor=ctx.admin_id)


@router.post("/{skill_id}/disable")
async def disable_skill(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.set_enabled(skill_id, False, actor=ctx.admin_id)


@router.get("/{skill_id}/versions")
async def list_versions(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": skills.list_versions(skill_id)}


@router.get("/{skill_id}/diff")
async def diff(
    skill_id: str, from_version: str, to_version: str, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    return skills.diff(skill_id, from_version, to_version)


@router.get("/{skill_id}/dependencies")
async def dependencies(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return skills.dependencies_of(skill_id)


@router.get("/{skill_id}/preview")
async def preview(skill_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    data = skills.get_skill(skill_id)
    return {"skill_md": data["skill_md"], "name": data["name"], "version": data["latest_version"]}


@router.post("/{skill_id}/export")
async def export_skill(
    skill_id: str, payload: ExportRequest, ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    data, filename, mime = skills.build_bundle(skill_id, payload.format)
    saved = skills.record_export(skill_id, payload.format, data, actor=ctx.admin_id)
    return {
        "file_id": saved["file_id"],
        "filename": filename,
        "mime": mime,
        "size": len(data),
        "sha256": saved["sha256"],
        "download_url": f"/api/admin/files/{saved['file_id']}",
        "install_command": skills.get_skill(skill_id)["install_command"],
    }
