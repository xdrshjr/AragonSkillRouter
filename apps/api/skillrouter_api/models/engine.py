"""引擎升级域模型（§10.1 / §12.4）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin


class EngineDeployment(Base, IdMixin):
    __tablename__ = "engine_deployments"

    version: Mapped[str] = mapped_column(String(32), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(24), default="tagged", nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512))
    integrity: Mapped[str | None] = mapped_column(String(256))
    adapter_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    deployed_at: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class EngineCandidate(Base, IdMixin):
    __tablename__ = "engine_candidates"

    version: Mapped[str] = mapped_column(String(32), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(24), default="tagged", nullable=False)
    published_at: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32), default="stable", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    verification_report: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("uq_engine_candidates", "version", "provenance", unique=True),
    )


class EngineUpgradeRun(Base, IdMixin):
    __tablename__ = "engine_upgrade_runs"

    from_version: Mapped[str] = mapped_column(String(32), nullable=False)
    to_version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="verify_only", nullable=False)
    policy: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[str | None] = mapped_column(String(16))
    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    backup_file_id: Mapped[str | None] = mapped_column(String(64))
    rollback_of: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_engine_runs_started", "started_at"),)


class EngineCapabilityStatus(Base, IdMixin):
    __tablename__ = "engine_capability_status"

    capability_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="available", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    since_version: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
