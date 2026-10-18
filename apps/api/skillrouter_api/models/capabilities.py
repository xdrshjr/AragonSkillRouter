"""能力域模型（§10.1，v2：标准能力 + routes[]）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin, TimestampMixin


class Capability(Base, IdMixin, TimestampMixin):
    __tablename__ = "capabilities"

    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), default="generic.http", nullable=False)
    modes: Mapped[list] = mapped_column(JSON, default=lambda: ["sync"], nullable=False)
    file_io: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    compatibility_group: Mapped[str | None] = mapped_column(String(96))
    failover: Mapped[dict] = mapped_column(JSON, default=lambda: {"enabled": True, "max_switches": 2})
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    draft_definition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    latest_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    requires_agent_reasoning: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    deleted_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_capabilities_status", "status", "enabled"),)


class CapabilityRoute(Base, IdMixin):
    __tablename__ = "capability_routes"

    capability_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(64), nullable=False)
    route_key: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    model_hint: Mapped[str | None] = mapped_column(String(128))
    request: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    param_map: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_map: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    stream: Mapped[dict | None] = mapped_column(JSON)
    async_: Mapped[dict | None] = mapped_column("async", JSON)
    limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=60000, nullable=False)
    retry: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    health: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_cap_routes_key", "capability_id", "route_key", unique=True),
        Index("ix_cap_routes_cap", "capability_id", "enabled", "priority"),
        Index("ix_cap_routes_cred", "credential_id"),
        Index("ix_cap_routes_provider", "provider_id"),
    )


class CapabilityVersion(Base, IdMixin):
    __tablename__ = "capability_versions"

    capability_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    published_at: Mapped[str | None] = mapped_column(String(32))
    published_by: Mapped[str | None] = mapped_column(String(64))
    changelog: Mapped[str | None] = mapped_column(Text)
    test_run_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_cap_versions_ver", "capability_id", "version", unique=True),
        Index("uq_cap_versions_hash", "capability_id", "content_hash", unique=True),
    )


class CapabilityTestRun(Base, IdMixin):
    __tablename__ = "capability_test_runs"

    capability_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_version_id: Mapped[str | None] = mapped_column(String(64))
    route_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16), default="sync", nullable=False)
    ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_preview_redacted: Mapped[str | None] = mapped_column(Text)
    response_preview_redacted: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    credential_id: Mapped[str | None] = mapped_column(String(64))
    route: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_cap_tests_cap", "capability_id", "at"),)


class CompatibilityGroup(Base, IdMixin):
    __tablename__ = "compatibility_groups"

    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[str | None] = mapped_column(String(32))
