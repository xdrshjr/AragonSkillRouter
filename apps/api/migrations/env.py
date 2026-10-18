"""Alembic 环境（SQLite batch 模式，§7.2 / §10.3）。

注意：PRAGMA 通过 Engine 的 `connect` 事件设置，**不要**在连接上手写
`exec_driver_sql("PRAGMA ...")` —— 那会在 SQLAlchemy 2.0 里隐式开启事务，
导致 alembic 自身的 `alembic_version` 写入被回滚（实测：`stamp head` 无效）。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillrouter_api.db.base import Base  # noqa: E402
from skillrouter_api.models import *  # noqa: F401,F403,E402
from skillrouter_api.settings import settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    try:
        # `disable_existing_loggers=False`：bootstrap 会在**进程内**调用 alembic，
        # 默认的 True 会把已经创建的 `skillrouter.*` logger 全部 disable（日志静默）。
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:  # pragma: no cover - 无日志配置时静默
        pass

# 目标库：`alembic.ini` 里的 `sqlalchemy.url` 是占位符（driver://user:pass@localhost/dbname），
# **不能**当真；调用方（scripts/adopt_alembic.py 的 --db、bootstrap 的迁移入口）可以用上面这个
# 选项显式指定目标库，否则回落到 settings.db_url（= SKILLROUTER_DB_PATH）。§A.3.8
_url = (config.get_main_option("sqlalchemy.url") or "").strip()
if not _url or _url.startswith("driver://"):
    _url = settings.db_url
config.set_main_option("sqlalchemy.url", _url)
target_metadata = Base.metadata


def _configure(**kwargs) -> None:  # noqa: ANN003
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings.ensure_dirs()
    engine = create_engine(
        _url,
        poolclass=pool.NullPool,
        connect_args={"timeout": 5, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
