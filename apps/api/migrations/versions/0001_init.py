"""0001_init：全量初始 schema（§10 全部表 + 触发器 + 索引）。

本 revision 与 `db/migrations/0001_init.sql` 快照一一对应；首次交付时
`0002_engine_upgrade` 已并入本文件（§0 P07 / §10.3 允许的合并）。
"""

from __future__ import annotations

from alembic import op

from skillrouter_api.db.base import Base
from skillrouter_api.models import *  # noqa: F401,F403

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS capability_versions_immutable
    BEFORE UPDATE ON capability_versions
    WHEN OLD.status = 'published' AND NEW.status = 'published'
         AND OLD.definition != NEW.definition
    BEGIN
        SELECT RAISE(ABORT, 'published_version_immutable');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skill_versions_immutable
    BEFORE UPDATE ON skill_versions
    WHEN OLD.status = 'published' AND NEW.status = 'published'
         AND OLD.skill_md != NEW.skill_md
    BEGIN
        SELECT RAISE(ABORT, 'published_version_immutable');
    END;
    """,
]

SEED_STATE = [
    ("admin_initialized", "0"),
    ("schema_version", "1"),
    ("engine_previous_pin", ""),
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    for statement in TRIGGERS:
        bind.exec_driver_sql(statement)
    for key, value in SEED_STATE:
        bind.exec_driver_sql(
            "INSERT OR IGNORE INTO system_state (key, value, updated_at) "
            f"VALUES ('{key}', '{value}', datetime('now'))"
        )
    bind.exec_driver_sql("PRAGMA user_version=1")


def downgrade() -> None:
    bind = op.get_bind()
    for statement in ("DROP TRIGGER IF EXISTS skill_versions_immutable", "DROP TRIGGER IF EXISTS capability_versions_immutable"):
        bind.exec_driver_sql(statement)
    Base.metadata.drop_all(bind=bind)
    bind.exec_driver_sql("PRAGMA user_version=0")
