"""SKILL 分发（§9.2.1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ...deps import require_token
from ...security.tokens import TokenContext
from ...services import skills

router = APIRouter(prefix="/skills", tags=["v1-skills"])


@router.get("")
async def list_skills(ctx: TokenContext = Depends(require_token)) -> dict:
    items = skills.catalog()
    return {"items": items, "total": len(items)}


@router.get("/{name}")
async def get_skill(name: str, ctx: TokenContext = Depends(require_token)) -> dict:
    return skills.public_skill(name)


@router.get("/{name}/bundle")
async def bundle(name: str, ctx: TokenContext = Depends(require_token)) -> Response:
    from sqlalchemy import select

    from ...db import db_session
    from ...errors import ApiError, Code
    from ...models import Skill

    with db_session() as s:
        row = s.scalar(select(Skill).where(Skill.name == name, Skill.deleted_at.is_(None)))
        if row is None:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        skill_id = row.id
    payload, filename, _mime = skills.build_bundle(skill_id, "zip")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
