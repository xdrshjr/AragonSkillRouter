"""迁移往返与存量库认领（T-23 / T-24 / T-42，§A.3.8 / §A.6.1 / §A.6.3）。

三个用例都在**临时目录 + 子进程**里跑真实 Alembic：`python -m alembic -c
migrations/alembic.ini ...`，工作目录固定为 `apps/api`，`SKILLROUTER_ENV=development`，
`SKILLROUTER_DATA_DIR` / `SKILLROUTER_DB_PATH` 指向 tmp_path —— 因此**不会**碰
会话级测试 DB（conftest 的 `sr-test-*`）。Windows 上子进程用 `sys.executable`
显式指定解释器，`cwd` 用绝对路径。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
ADOPT_SCRIPT = REPO_ROOT / "scripts" / "adopt_alembic.py"
ALEMBIC_INI = "migrations/alembic.ini"
HEAD_REVISION = "0004_llm_config"
ADOPT_REVISION = "0002_engine_upgrade"
#: 0004 之后的 schema 版本（`PRAGMA user_version`）
SCHEMA_VERSION = 3

#: 本轮新增的 6 列（§A.6.1）：T-23/T-24/T-42 逐列断言「各在」
NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("idempotency_keys", "state"),
    ("tasks", "cancel_requested_at"),
    ("credentials", "meta_sealed"),
    ("call_logs", "needs_reconcile"),
    ("engine_upgrade_runs", "verify_status"),
    ("engine_upgrade_runs", "blocked_reasons"),
)

#: 不是业务表（Alembic 自己的版本表 / SQLite 内部表）
NON_BUSINESS = frozenset({"alembic_version"})

#: 0004_llm_config 新增的两张表（spec §6.1 / §6.2 / T-29）
NEW_TABLES: tuple[str, ...] = ("llm_model_catalog", "llm_connection_probes")


# ── 子进程跑 alembic / adopt 脚本 ─────────────────────────────
def _env(tmp_path: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "SKILLROUTER_ENV": "development",
        "SKILLROUTER_DATA_DIR": str(tmp_path),
        "SKILLROUTER_DB_PATH": str(tmp_path / "migrations.db"),
        "PYTHONIOENCODING": "utf-8",
    }
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(API_DIR) + (os.pathsep + existing if existing else "")
    return env


def run_alembic(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在子进程里跑 alembic（cwd=apps/api，DB 指向 tmp_path）。"""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI, *args],
        cwd=str(API_DIR),
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def run_adopt(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在子进程里跑 scripts/adopt_alembic.py（cwd=仓库根）。"""
    db = tmp_path / "migrations.db"
    return subprocess.run(
        [sys.executable, str(ADOPT_SCRIPT), "--db", str(db), *args],
        cwd=str(REPO_ROOT),
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


# ── 直接读库（测试侧断言用，不经过被测代码） ──────────────────
def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


def db_tables(db: Path) -> set[str]:
    connection = _connect(db)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        connection.close()
    return {
        str(row[0])
        for row in rows
        if not str(row[0]).startswith("sqlite_") and str(row[0]) not in NON_BUSINESS
    }


def db_columns(db: Path, table: str) -> set[str]:
    connection = _connect(db)
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def db_user_version(db: Path) -> int:
    connection = _connect(db)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def db_revision(db: Path) -> str | None:
    connection = _connect(db)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:  # 表不存在 = 从未 stamp
        return None
    finally:
        connection.close()
    return None if row is None else str(row[0])


# ── 造「存量库」与重置 ────────────────────────────────────────
def reset_db(db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)


def _drop_column(connection: sqlite3.Connection, table: str, column: str) -> None:
    """删除列（先把引用该列的索引删掉，否则 SQLite 拒绝 DROP COLUMN）。"""
    indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
    for row in indexes:
        name = str(row[1])
        if name.startswith("sqlite_autoindex"):
            continue
        columns = {str(item[2]) for item in connection.execute(f"PRAGMA index_info({name})")}
        if column in columns:
            connection.execute(f'DROP INDEX IF EXISTS "{name}"')
    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def make_legacy_db(db: Path, *, drop_new_columns: bool) -> None:
    """造一个「有业务表但没有 alembic_version」的存量库。

    `drop_new_columns=True` 模拟 pre-iteration-2 的库（缺本轮 6 列，`user_version=1`）；
    `False` 模拟「已由新元数据 create_all 建过含新列、但还没有版本记录」的库。
    """
    from sqlalchemy import create_engine

    import skillrouter_api.models  # noqa: F401  确保全部表注册
    from skillrouter_api.db.base import Base

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    connection = _connect(db)
    try:
        if drop_new_columns:
            for table, column in NEW_COLUMNS:
                _drop_column(connection, table, column)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()


def assert_new_columns_present(db: Path) -> None:
    for table, column in NEW_COLUMNS:
        assert column in db_columns(db, table), f"{table}.{column} 缺失"


def assert_llm_tables_present(db: Path) -> None:
    """T-29：0004 的两张新表必须真的被建出来（不是只有版本号前进）。"""
    tables = db_tables(db)
    for table in NEW_TABLES:
        assert table in tables, f"{table} 缺失（0004_llm_config 未生效）"
    # 关键索引也在（清单命中与探测历史查询都依赖它们）
    columns = db_columns(db, "llm_model_catalog")
    assert {"dedupe_key", "protocol", "wire", "base_url", "models", "expires_at"} <= columns
    probe_columns = db_columns(db, "llm_connection_probes")
    assert {"protocol", "mode", "ok", "stages", "at"} <= probe_columns


# ── T-23 ─────────────────────────────────────────────────────
def test_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    """空库 → head（6 列各在）→ base（表清空）→ head 再成功。"""
    db = tmp_path / "migrations.db"
    assert not db.exists()

    up = run_alembic(tmp_path, "upgrade", "head")
    assert up.returncode == 0, _output(up)
    assert db_revision(db) == HEAD_REVISION
    assert db_user_version(db) == SCHEMA_VERSION
    assert len(db_tables(db)) > 40, f"表数异常：{len(db_tables(db))}"
    assert_new_columns_present(db)
    assert_llm_tables_present(db)

    down = run_alembic(tmp_path, "downgrade", "base")
    assert down.returncode == 0, _output(down)
    assert db_tables(db) == set(), f"downgrade base 后仍有业务表：{sorted(db_tables(db))}"
    assert db_revision(db) is None
    assert db_user_version(db) == 0

    again = run_alembic(tmp_path, "upgrade", "head")
    assert again.returncode == 0, _output(again)
    assert db_revision(db) == HEAD_REVISION
    assert db_user_version(db) == SCHEMA_VERSION
    assert_new_columns_present(db)
    assert_llm_tables_present(db)


# ── T-24 ─────────────────────────────────────────────────────
def test_adopt_alembic_requires_fingerprint_match(tmp_path: Path) -> None:
    """指纹不一致 → 退出 3 且未 stamp；一致 → stamp 0002 后 upgrade head 补齐新列。"""
    db = tmp_path / "migrations.db"

    # 1) 不一致：库里只有一张元数据里没有的表 → 拒绝认领，绝不 stamp
    connection = _connect(db)
    try:
        connection.execute("CREATE TABLE legacy_junk (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()

    mismatch = run_adopt(tmp_path)
    assert mismatch.returncode == 3, _output(mismatch)
    assert "legacy_junk" in mismatch.stdout  # 差异表里点名多余的表
    assert db_revision(db) is None  # 未 stamp

    # --dry-run 一致时也不写版本
    # 2) 一致：模拟 pre-iteration-2 存量库（有全部业务表、缺 6 列、user_version=1）
    reset_db(db)
    make_legacy_db(db, drop_new_columns=True)
    for table, column in NEW_COLUMNS:
        assert column not in db_columns(db, table), f"前置条件错：{table}.{column} 竟然已存在"

    dry = run_adopt(tmp_path, "--dry-run")
    assert dry.returncode == 0, _output(dry)
    assert db_revision(db) is None, "--dry-run 不得写版本"
    for table, column in NEW_COLUMNS:
        assert column not in db_columns(db, table)

    adopt = run_adopt(tmp_path)
    assert adopt.returncode == 0, _output(adopt)
    assert db_revision(db) == ADOPT_REVISION
    # stamp 只写版本，不动列
    for table, column in NEW_COLUMNS:
        assert column not in db_columns(db, table)

    up = run_alembic(tmp_path, "upgrade", "head")
    assert up.returncode == 0, _output(up)
    assert db_revision(db) == HEAD_REVISION
    assert db_user_version(db) == SCHEMA_VERSION
    assert_new_columns_present(db)
    assert_llm_tables_present(db)  # 由幂等的 0003 补齐


# ── T-42 ─────────────────────────────────────────────────────
def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    """连续两次 upgrade head 均 0 退出；在「已由 create_all 建过含新列的库」上也不报错。"""
    db = tmp_path / "migrations.db"

    for attempt in (1, 2):
        result = run_alembic(tmp_path, "upgrade", "head")
        assert result.returncode == 0, f"第 {attempt} 次 upgrade 失败：{_output(result)}"
        assert "duplicate column name" not in _output(result).lower()
        assert db_revision(db) == HEAD_REVISION
        assert_new_columns_present(db)

    # 第三种情形：库里已经有本轮新列（由新元数据 create_all 建出），但没有 alembic_version
    reset_db(db)
    make_legacy_db(db, drop_new_columns=False)
    assert_new_columns_present(db)

    result = run_alembic(tmp_path, "upgrade", "head")
    assert result.returncode == 0, _output(result)
    assert "duplicate column name" not in _output(result).lower()
    assert db_revision(db) == HEAD_REVISION
    assert db_user_version(db) == SCHEMA_VERSION
    assert_new_columns_present(db)
    assert_llm_tables_present(db)


# ── assert_migrated（§A.3.8-4：必须真的被调用，且不得静默 return） ──
def test_assert_migrated_is_strict_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillrouter_api.services import bootstrap as bootstrap_service

    # 会话级 DB 已由 conftest 的 bootstrap() 迁到 head → 本地校验通过
    bootstrap_service.assert_migrated()

    # 版本落后：production 必须抛错（带启动期常量），development 只打印差异
    monkeypatch.setattr(bootstrap_service, "_alembic_revision", lambda: "0001_init")
    monkeypatch.setattr(bootstrap_service.settings, "env", "production")
    with pytest.raises(RuntimeError, match="MIGRATION_NOT_ADOPTED"):
        bootstrap_service.assert_migrated()

    monkeypatch.setattr(bootstrap_service.settings, "env", "development")
    bootstrap_service.assert_migrated()  # 不抛
