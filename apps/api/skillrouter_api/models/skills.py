"""SKILL 域模型（§10.1）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin, TimestampMixin


class Skill(Base, IdMixin, TimestampMixin):
    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    draft_definition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    latest_version: Mapped[str | None] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128))
    source_commit: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(512))
    deleted_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_skills_status", "status", "enabled"),)


class SkillVersion(Base, IdMixin):
    __tablename__ = "skill_versions"

    skill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_md: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    execution_plan: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    dependencies: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    examples: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    published_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_skill_versions_ver", "skill_id", "version", unique=True),
        Index("uq_skill_versions_hash", "skill_id", "content_hash", unique=True),
    )


class SkillFile(Base, IdMixin):
    __tablename__ = "skill_files"

    skill_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    mime: Mapped[str | None] = mapped_column(String(96))
    executable: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (Index("uq_skill_files_path", "skill_version_id", "path", unique=True),)


class SkillImport(Base, IdMixin):
    __tablename__ = "skill_imports"

    skill_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="url", nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_ref: Mapped[str | None] = mapped_column(String(128))
    resolved_commit: Mapped[str | None] = mapped_column(String(64))
    preview_token: Mapped[str | None] = mapped_column(String(64))
    files: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    requires_sandbox_review: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="previewed", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class SkillExport(Base, IdMixin):
    __tablename__ = "skill_exports"

    skill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str | None] = mapped_column(String(32))
    format: Mapped[str] = mapped_column(String(16), default="zip", nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
