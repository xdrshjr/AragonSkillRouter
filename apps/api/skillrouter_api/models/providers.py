"""提供商与凭证域模型（§10.1）。"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin, TimestampMixin


class Provider(Base, IdMixin, TimestampMixin):
    __tablename__ = "providers"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="llm", nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    docs_url: Mapped[str | None] = mapped_column(String(512))
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), default="prod", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    deleted_at: Mapped[str | None] = mapped_column(String(32))


class Credential(Base, IdMixin, TimestampMixin):
    __tablename__ = "credentials"

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="llm", nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), default="api_key", nullable=False)
    header_name: Mapped[str | None] = mapped_column(String(64))
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    secret_mask: Mapped[str] = mapped_column(String(64), default="***", nullable=False)
    secret_fingerprint: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    #: 敏感 meta 的信封加密列（AAD `record_type="credential_meta"`），不出现在任何投影里
    meta_sealed: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String(32))
    last_verified_at: Mapped[str | None] = mapped_column(String(32))
    last_verify_ok: Mapped[int | None] = mapped_column(Integer)
    rotation_of: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_credentials_provider_status", "provider_id", "status"),)


class CredentialVerification(Base, IdMixin):
    __tablename__ = "credential_verifications"

    credential_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    detail_redacted: Mapped[str | None] = mapped_column(Text)
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_cred_verif_cred", "credential_id", "at"),)
