"""执行域模型：调用记录、路由决策、任务与队列（§10.1）。"""

from __future__ import annotations

from sqlalchemy import JSON, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin


class CallLog(Base, IdMixin):
    __tablename__ = "call_logs"

    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    caller_type: Mapped[str] = mapped_column(String(16), default="admin", nullable=False)
    caller_id: Mapped[str | None] = mapped_column(String(64))
    capability_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version_id: Mapped[str | None] = mapped_column(String(64))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    credential_id: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16), default="sync", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    queue_ms: Mapped[int | None] = mapped_column(Integer)
    upstream_ms: Mapped[int | None] = mapped_column(Integer)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    payload_policy: Mapped[str] = mapped_column(String(24), default="redacted_preview")
    request_preview: Mapped[str | None] = mapped_column(Text)
    response_preview: Mapped[str | None] = mapped_column(Text)
    agent_session_id: Mapped[str | None] = mapped_column(String(64))
    agent_turn_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_call_logs_created", "created_at"),
        Index("ix_call_logs_caller", "caller_id", "created_at"),
        Index("ix_call_logs_cap", "capability_key", "created_at"),
        Index("ix_call_logs_status", "status"),
        Index("ix_call_logs_task", "task_id"),
    )


class RouteDecision(Base, IdMixin):
    __tablename__ = "route_decisions"

    call_id: Mapped[str | None] = mapped_column(String(64))
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    dropped: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    chosen_route_id: Mapped[str | None] = mapped_column(String(64))
    chosen_capability_version_id: Mapped[str | None] = mapped_column(String(64))
    chosen_provider_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    failover_from: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class UsageRecord(Base, IdMixin):
    __tablename__ = "usage_records"

    call_id: Mapped[str | None] = mapped_column(String(64))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    capability_key: Mapped[str | None] = mapped_column(String(128))
    token_id: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(16), default="tokens", nullable=False)
    qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="estimated", nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_usage_provider", "provider_id", "at"),
        Index("ix_usage_cap", "capability_key", "at"),
    )


class PriceBook(Base, IdMixin):
    __tablename__ = "price_book"

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_key: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(16), default="tokens", nullable=False)
    unit_price_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    effective_from: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index(
            "uq_price_book",
            "provider_id",
            "capability_key",
            "unit",
            "effective_from",
            unique=True,
        ),
    )


class IdempotencyKey(Base, IdMixin):
    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_idem_scope_key", "scope", "idem_key", unique=True),
        Index("ix_idem_expires", "expires_at"),
    )


class Task(Base, IdMixin):
    __tablename__ = "tasks"

    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version_id: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16), default="async", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_stage: Mapped[str | None] = mapped_column(String(64))
    upstream_task_id: Mapped[str | None] = mapped_column(String(128))
    upstream_payload_redacted: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    cancel_capability: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_files: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(64))
    agent_session_id: Mapped[str | None] = mapped_column(String(64))
    resume_policy: Mapped[str] = mapped_column(String(16), default="check", nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    route_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_tasks_status", "status", "updated_at"),
        Index("ix_tasks_upstream", "upstream_task_id"),
        Index("ix_tasks_request", "request_id"),
    )


class TaskQueue(Base, IdMixin):
    __tablename__ = "task_queue"

    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    available_at: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_task_queue_pick", "status", "available_at"),)


class CallbackEvent(Base, IdMixin):
    __tablename__ = "callback_events"

    provider_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload_redacted: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_callback_event", "provider_slug", "event_id", unique=True),
    )
