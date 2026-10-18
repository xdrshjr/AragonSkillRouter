"""ORM 基类与公共列（§10.1）。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def uuid_pk(prefix: str = "") -> str:
    raw = uuid4().hex[:24]
    return f"{prefix}_{raw}" if prefix else raw


def short_id(prefix: str, n: int = 12) -> str:
    return f"{prefix}_{uuid4().hex[:n]}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso, nullable=False)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=now_iso, onupdate=now_iso, nullable=False
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid_pk())

    @classmethod
    def new_id(cls, prefix: str) -> str:
        return uuid_pk(prefix)


JSONText = Text
