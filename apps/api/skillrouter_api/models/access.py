"""外部 Agent、平台令牌与配额模型（§10.1）。"""

from __future__ import annotations

from sqlalchemy import JSON, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin, TimestampMixin

#: 配额与记账的作用域（唯一真相来源，§A.3.4）：令牌 → Agent → 能力 → 提供商 → 全局
SCOPE_TYPES = ("token", "agent", "capability", "provider", "global")


class ExternalAgent(Base, IdMixin):
    __tablename__ = "external_agents"

    name: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_seen_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class PlatformToken(Base, IdMixin):
    __tablename__ = "platform_tokens"

    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=lambda: ["*"], nullable=False)
    allowed_modes: Mapped[list] = mapped_column(JSON, default=lambda: ["sync"], nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String(32))
    rpm: Mapped[int | None] = mapped_column(Integer)
    concurrency: Mapped[int | None] = mapped_column(Integer)
    daily_calls: Mapped[int | None] = mapped_column(Integer)
    budget_usd: Mapped[float | None] = mapped_column(Float)
    revoked_at: Mapped[str | None] = mapped_column(String(32))
    last_used_at: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_tokens_agent", "agent_id", "revoked_at"),)


class RateCounter(Base):
    __tablename__ = "rate_counters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), default="*", nullable=False)
    window_start: Mapped[str] = mapped_column(String(32), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    concurrent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index(
            "uq_rate_counters",
            "scope_type",
            "scope_id",
            "window_start",
            "window_seconds",
            unique=True,
        ),
    )


class QuotaLimit(Base, IdMixin):
    __tablename__ = "quota_limits"

    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), default="*", nullable=False)
    rpm: Mapped[int | None] = mapped_column(Integer)
    concurrency: Mapped[int | None] = mapped_column(Integer)
    daily_calls: Mapped[int | None] = mapped_column(Integer)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_quota_limits", "scope_type", "scope_id", unique=True),
    )


class QuotaCounter(Base, IdMixin):
    __tablename__ = "quota_counters"

    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), default="*", nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("uq_quota_counters", "scope_type", "scope_id", "period", unique=True),
    )


__all__ = [
    "SCOPE_TYPES",
    "ExternalAgent",
    "PlatformToken",
    "RateCounter",
    "QuotaLimit",
    "QuotaCounter",
    "TimestampMixin",
]
