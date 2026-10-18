"""0002_engine_upgrade：引擎升级相关表。

首次交付时已并入 `0001_init`（§10.3 / P07：无部署实例时允许合并）；
本 revision 保留以示演进，对已有 `0001_init` 的库执行是幂等的空操作。
一旦有已部署实例，任何 schema 变更都必须新增 revision 且 `downgrade` 可用。
"""

from __future__ import annotations

from alembic import op

from skillrouter_api.db.base import Base
from skillrouter_api.models import engine  # noqa: F401
from skillrouter_api.models.engine import (
    EngineCandidate,
    EngineCapabilityStatus,
    EngineDeployment,
    EngineUpgradeRun,
)

revision = "0002_engine_upgrade"
down_revision = "0001_init"
branch_labels = None
depends_on = None

TABLES = [
    EngineDeployment.__table__,
    EngineCandidate.__table__,
    EngineUpgradeRun.__table__,
    EngineCapabilityStatus.__table__,
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=TABLES, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table.name}")
