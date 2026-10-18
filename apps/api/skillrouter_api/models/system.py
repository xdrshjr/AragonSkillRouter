"""系统域模型：状态、设置、审计、文件、轮换、心跳、SSE（§10.1）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="global", nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AuditLog(Base, IdMixin):
    __tablename__ = "audit_logs"

    at: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), default="admin", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(48))
    object_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_audit_at", "at"),
        Index("ix_audit_action", "action", "at"),
        Index("ix_audit_object", "object_type", "object_id"),
    )


class FileObject(Base, IdMixin):
    __tablename__ = "file_objects"

    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(96), default="application/octet-stream", nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="upload", nullable=False)
    owner_type: Mapped[str | None] = mapped_column(String(24))
    owner_id: Mapped[str | None] = mapped_column(String(64))
    filename: Mapped[str | None] = mapped_column(String(256))
    expires_at: Mapped[str | None] = mapped_column(String(32))
    ref_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    deleted_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_files_sha", "sha256"),
        Index("ix_files_expires", "expires_at"),
        Index("ix_files_owner", "owner_type", "owner_id"),
    )


class MasterKeyRotation(Base, IdMixin):
    __tablename__ = "master_key_rotations"

    from_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    to_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    leases_held: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(32))


class SseEvent(Base):
    __tablename__ = "sse_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_key: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_sse_stream", "stream_key", "id"),)
