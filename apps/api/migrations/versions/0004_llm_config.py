"""0004_llm_config：LLM 服务配置的两张新表（spec §6.3）。

为什么必须幂等：`0001_init` 用 `Base.metadata.create_all(checkfirst=True)` 建表 —— 它按
**当前模型元数据**建，因此模型里已经有 `llm_model_catalog` / `llm_connection_probes` 时，
升级到 0001 就会把两张表建好；本文件再执行一次裸 `CREATE TABLE` 会以
`table already exists` 失败，整个升级不可启动（沿用 0003 的写法）。

改动：2 张表 + 4 个索引，`PRAGMA user_version` 2 → 3。
"""

from __future__ import annotations

from alembic import op

revision = "0004_llm_config"
down_revision = "0003_iteration2"
branch_labels = None
depends_on = None

#: (表名, DDL)——顺序即创建顺序
TABLES: list[tuple[str, str]] = [
    (
        "llm_model_catalog",
        """
        CREATE TABLE IF NOT EXISTS llm_model_catalog (
          id                  TEXT PRIMARY KEY,
          dedupe_key          TEXT NOT NULL UNIQUE,
          credential_id       TEXT NULL,
          provider_id         TEXT NULL,
          protocol            TEXT NOT NULL,
          wire                TEXT NOT NULL,
          base_url            TEXT NOT NULL,
          models              TEXT NOT NULL DEFAULT '[]',
          model_count         INTEGER NOT NULL DEFAULT 0,
          truncated           INTEGER NOT NULL DEFAULT 0,
          source              TEXT NOT NULL DEFAULT 'upstream',
          fetched_at          TEXT NOT NULL,
          expires_at          TEXT NOT NULL,
          last_error_redacted TEXT NULL,
          created_at          TEXT NOT NULL,
          updated_at          TEXT NOT NULL
        )
        """,
    ),
    (
        "llm_connection_probes",
        """
        CREATE TABLE IF NOT EXISTS llm_connection_probes (
          id              TEXT PRIMARY KEY,
          credential_id   TEXT NULL,
          provider_id     TEXT NULL,
          protocol        TEXT NOT NULL,
          wire            TEXT NOT NULL,
          base_url        TEXT NOT NULL,
          model_id        TEXT NULL,
          mode            TEXT NOT NULL,
          ok              INTEGER NOT NULL DEFAULT 0,
          stages          TEXT NOT NULL DEFAULT '[]',
          http_status     INTEGER NULL,
          latency_ms      INTEGER NULL,
          error_code      TEXT NULL,
          detail_redacted TEXT NULL,
          actor_id        TEXT NULL,
          request_id      TEXT NULL,
          at              TEXT NOT NULL
        )
        """,
    ),
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_llm_catalog_exp ON llm_model_catalog(expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_llm_catalog_cred ON llm_model_catalog(credential_id)",
    "CREATE INDEX IF NOT EXISTS ix_llm_probe_cred ON llm_connection_probes(credential_id, at)",
    "CREATE INDEX IF NOT EXISTS ix_llm_probe_at ON llm_connection_probes(at)",
]


def _table_exists(bind, table: str) -> bool:  # noqa: ANN001
    row = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    for name, ddl in TABLES:
        if not _table_exists(bind, name):
            bind.exec_driver_sql(ddl)
    for statement in INDEXES:
        bind.exec_driver_sql(statement)
    bind.exec_driver_sql("PRAGMA user_version=3")


def downgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "DROP INDEX IF EXISTS ix_llm_probe_at",
        "DROP INDEX IF EXISTS ix_llm_probe_cred",
        "DROP INDEX IF EXISTS ix_llm_catalog_cred",
        "DROP INDEX IF EXISTS ix_llm_catalog_exp",
    ):
        bind.exec_driver_sql(statement)
    for name, _ddl in reversed(TABLES):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {name}")
    bind.exec_driver_sql("PRAGMA user_version=2")
