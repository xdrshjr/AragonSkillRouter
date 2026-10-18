"""0003_iteration2：迭代 2 增量列（幂等加列，§A.6.1）。

为什么必须幂等：`0001_init` / `0002_engine_upgrade` 不是字面 DDL，它们调用
`Base.metadata.create_all(checkfirst=True)`——即按**当前模型元数据**建表。模型已含本轮
6 个新列时，`alembic upgrade head` 会先由 0001 建出带新列的表，随后本文件的
`ALTER TABLE … ADD COLUMN` 就会以 `duplicate column name` 失败，升级即不可启动（R2-02）。
因此每一列、每个索引都先探测再创建，且 `downgrade()` 与 `upgrade()` 对称。

改动共 6 列 + 2 个索引：
  idempotency_keys.state            幂等三态（F-12）
  tasks.cancel_requested_at         取消意图（F-07）
  credentials.meta_sealed           敏感 meta 密封（F-03）
  call_logs.needs_reconcile         待对账标记（F-06）
  engine_upgrade_runs.verify_status 验证三态（F-26）
  engine_upgrade_runs.blocked_reasons 阻断原因（F-23 / F-26）
"""

from __future__ import annotations

from alembic import op

revision = "0003_iteration2"
down_revision = "0002_engine_upgrade"
branch_labels = None
depends_on = None

#: (表, 列, DDL 片段)——顺序即执行顺序
COLUMNS: list[tuple[str, str, str]] = [
    ("idempotency_keys", "state", "state TEXT NOT NULL DEFAULT 'done'"),
    ("tasks", "cancel_requested_at", "cancel_requested_at TEXT NULL"),
    ("credentials", "meta_sealed", "meta_sealed TEXT NULL"),
    ("call_logs", "needs_reconcile", "needs_reconcile INTEGER NOT NULL DEFAULT 0"),
    ("engine_upgrade_runs", "verify_status", "verify_status TEXT NULL"),
    ("engine_upgrade_runs", "blocked_reasons", "blocked_reasons TEXT NULL"),
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_idem_state ON idempotency_keys(state, expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_cancel_req ON tasks(status, cancel_requested_at)",
]


def _columns(bind, table: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _table_exists(bind, table: str) -> bool:
    row = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _has_column(bind, table: str, column: str) -> bool:
    if not _table_exists(bind, table):
        return False
    return column in _columns(bind, table)


def _add(bind, table: str, column: str, ddl: str) -> None:
    if not _table_exists(bind, table):
        # 表还没建（模型的 create_all 尚未执行）→ 由后续 create_all 直接建出带列的版本
        return
    if column not in _columns(bind, table):
        bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _drop(bind, table: str, column: str) -> None:
    if _has_column(bind, table, column):
        bind.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")


def upgrade() -> None:
    bind = op.get_bind()
    for table, column, ddl in COLUMNS:
        _add(bind, table, column, ddl)
    for statement in INDEXES:
        bind.exec_driver_sql(statement)
    bind.exec_driver_sql("PRAGMA user_version=2")


def downgrade() -> None:
    bind = op.get_bind()

    # 前置校验：已密封的敏感 meta 一旦丢列就**永久无法解密**，必须先导出（R10）
    if _has_column(bind, "credentials", "meta_sealed"):
        sealed = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM credentials WHERE meta_sealed IS NOT NULL AND meta_sealed != ''"
        ).scalar()
        if sealed:
            raise RuntimeError(
                f"[ABORT] 仍有 {sealed} 条凭证的 meta_sealed 非空；"
                "先运行 python scripts/migrate_seal_meta.py --unseal --out <file> 导出后再回滚"
            )

    for statement in (
        "DROP INDEX IF EXISTS ix_tasks_cancel_req",
        "DROP INDEX IF EXISTS ix_idem_state",
    ):
        bind.exec_driver_sql(statement)
    for table, column, _ddl in reversed(COLUMNS):
        _drop(bind, table, column)
    bind.exec_driver_sql("PRAGMA user_version=1")
