"""LLM 服务配置域模型（spec §6.1 / §6.2）。

两张表都是**独立生命周期**（ADR-7），不给 `credentials` 加列：

- `llm_model_catalog`：模型清单缓存。`dedupe_key` 是
  `sha256(protocol|wire|base_url|credential_id)[:32]` —— 用哈希串而不是复合唯一索引，
  是因为 SQLite 的 UNIQUE 把 NULL 视为互异（草稿探测的 `credential_id` 是 NULL）。
- `llm_connection_probes`：探测历史。**永不包含密钥**：`stages` 里的 `detail`
  在写库前已经过 `redact()` + 显式 `secret → ***` 替换（spec §3.4）。
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, IdMixin, TimestampMixin


class LlmModelCatalog(Base, IdMixin, TimestampMixin):
    __tablename__ = "llm_model_catalog"

    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    credential_id: Mapped[str | None] = mapped_column(String(64))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(String(24), nullable=False)
    wire: Mapped[str] = mapped_column(String(24), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    #: `ModelEntry[]` 的 JSON（已归一化、已截断到上限）
    models: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    model_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: upstream | cache | cache-stale
    source: Mapped[str] = mapped_column(String(24), default="upstream", nullable=False)
    fetched_at: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error_redacted: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_llm_catalog_exp", "expires_at"),
        Index("ix_llm_catalog_cred", "credential_id"),
    )


class LlmConnectionProbe(Base, IdMixin):
    __tablename__ = "llm_connection_probes"

    #: 草稿测试为 NULL（密钥从未落库，也没有凭证行可引用）
    credential_id: Mapped[str | None] = mapped_column(String(64))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(String(24), nullable=False)
    wire: Mapped[str] = mapped_column(String(24), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(200))
    #: auth | completion | both
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: JSON: [{stage, ok, http_status, latency_ms, detail, model_count, text_chars}]
    stages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    detail_redacted: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_llm_probe_cred", "credential_id", "at"),
        Index("ix_llm_probe_at", "at"),
    )


__all__ = ["LlmConnectionProbe", "LlmModelCatalog"]
