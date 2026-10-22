"""Admin SKILL routes (docs/CONTRACT.md §5.7).

Everything here is admin-only, audited on write, and answers with the uniform
error envelope. Import preview/commit and export are part of the same surface
because that is where a SKILL can enter the platform from outside.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from app.core import db
from app.core.errors import unprocessable
from app.deps import AgentPrincipal, AuthUser, client_ip, require_admin
from app.services import export as export_service
from app.services import skill_import, skills

router = APIRouter(prefix="/api/admin/skills", tags=["skills"])


# ------------------------------------------------------------------- models ---
class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="general", max_length=60)


class SkillUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)
    status: str | None = Field(default=None, max_length=20)


class VersionCreateRequest(BaseModel):
    manifest: dict[str, Any]
    version: str | None = Field(default=None, max_length=40)


class VersionUpdateRequest(BaseModel):
    manifest: dict[str, Any]


class SkillTestRequest(BaseModel):
    version_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class ImportPreviewRequest(BaseModel):
    source_type: str = Field(max_length=10)
    uri: str | None = Field(default=None, max_length=1000)
    file_id: str | None = Field(default=None, max_length=80)


class ImportCommitRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=80)
    confirm_permissions: bool = False


# ------------------------------------------------------------------ helpers ---
def _envelope(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def admin_principal(user: AuthUser) -> AgentPrincipal:
    """The platform itself acting on behalf of the signed-in administrator."""
    return AgentPrincipal(
        agent_id="",
        agent_name=user.username,
        token_id="",
        scopes=["*"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )


def _page(limit: int, offset: int) -> tuple[int, int]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    return limit, offset


# -------------------------------------------------------------------- lists ---
@router.get("")
def list_skills(
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    rows, total = skills.list_skills(
        status=status_filter, category=category, search=search, limit=limit, offset=offset
    )
    items = [
        skills.serialise(
            row, include_versions=False, include_impact=False, include_manifest=False
        )
        for row in rows
    ]
    return _envelope(items, total, limit, offset)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_skill(
    payload: SkillCreateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = skills.create_skill(
        name=payload.name,
        description=payload.description,
        category=payload.category,
        slug=payload.slug,
        actor_id=user.id,
        ip=client_ip(request),
    )
    return skills.serialise(row)


# ------------------------------------------------------------- import flows ---
@router.get("/imports")
def list_imports(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    limit, offset = _page(limit, offset)
    rows, total = skill_import.list_imports(status=status_filter, limit=limit, offset=offset)
    items = [skill_import.serialise_import(row) for row in rows]
    return _envelope(items, total, limit, offset)


@router.post("/import/preview")
async def preview_import(
    payload: ImportPreviewRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return await skill_import.preview_import(
        source_type=payload.source_type,
        uri=payload.uri,
        file_id=payload.file_id,
        actor_id=user.id,
        ip=client_ip(request),
    )


@router.get("/import/{preview_id}")
def get_import(
    preview_id: str,
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = skill_import.get_import(preview_id)
    return skill_import.serialise_import(row, include_payload=True)


@router.post("/import", status_code=status.HTTP_201_CREATED)
def commit_import(
    payload: ImportCommitRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return skill_import.commit_import(
        payload.preview_id,
        payload.confirm_permissions,
        actor_id=user.id,
        ip=client_ip(request),
    )


# ------------------------------------------------------------------- detail ---
@router.get("/{skill_id}")
def get_skill(skill_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return skills.serialise(skills.get_skill(skill_id))


@router.patch("/{skill_id}")
def update_skill(
    skill_id: str,
    payload: SkillUpdateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = skills.update_skill(
        skill_id,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        status=payload.status,
        actor_id=user.id,
        ip=client_ip(request),
    )
    return skills.serialise(row)


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return skills.delete_skill(skill_id, actor_id=user.id, ip=client_ip(request))


@router.get("/{skill_id}/impact")
def dependency_impact(skill_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return skills.dependency_impact(skill_id)


# ----------------------------------------------------------------- versions ---
@router.get("/{skill_id}/versions")
def list_versions(skill_id: str, _user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    rows = skills.list_versions(skill_id)
    items = [skills.serialise_version(row) for row in rows]
    return _envelope(items, len(items), len(items), 0)


@router.post("/{skill_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    skill_id: str,
    payload: VersionCreateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = skills.add_version(
        skill_id, payload.manifest, payload.version, actor_id=user.id, ip=client_ip(request)
    )
    return skills.serialise_version(row)


@router.get("/{skill_id}/versions/{version_id}")
def get_version(
    skill_id: str, version_id: str, _user: AuthUser = Depends(require_admin)
) -> dict[str, Any]:
    return skills.serialise_version(skills.get_version(skill_id, version_id))


@router.patch("/{skill_id}/versions/{version_id}")
def update_version(
    skill_id: str,
    version_id: str,
    payload: VersionUpdateRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = skills.update_version(
        skill_id, version_id, payload.manifest, actor_id=user.id, ip=client_ip(request)
    )
    return skills.serialise_version(row)


@router.post("/{skill_id}/versions/{version_id}/validate")
def validate_version(
    skill_id: str, version_id: str, _user: AuthUser = Depends(require_admin)
) -> dict[str, Any]:
    row = skills.get_version(skill_id, version_id)
    return skills.validate_manifest(db.loads(row["manifest"], {}))


@router.post("/{skill_id}/versions/{version_id}/publish")
def publish_version(
    skill_id: str,
    version_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return skills.publish_version(skill_id, version_id, actor_id=user.id, ip=client_ip(request))


# ------------------------------------------------------------------ execute ---
@router.post("/{skill_id}/test")
async def test_skill(
    skill_id: str,
    payload: SkillTestRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    version_id = payload.version_id
    if version_id is None:
        skill = skills.get_skill(skill_id)
        current = skills.current_version(skill)
        version_id = current["id"] if current else None
    if version_id is None:
        raise unprocessable(
            "no_version",
            "This SKILL has no version to test.",
            actions=["Create a version with a plan first."],
        )
    return await skills.execute_skill(
        skill_id,
        version_id,
        payload.input,
        admin_principal(user),
        surface="admin",
        allow_draft=True,
        actor_id=user.id,
    )


@router.get("/{skill_id}/export")
def export_skill(
    skill_id: str,
    format: str = Query(default="skill"),
    _user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return export_service.export_skill(skill_id, format)
