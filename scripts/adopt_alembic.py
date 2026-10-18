"""存量库接入 Alembic 单轨（§A.3.8-3 / §A.6.3）。

背景：F-11 之前 `alembic_version` 恒空、`create_all` 才是真相。已部署的存量库
没有任何版本记录，`alembic upgrade head` 无法判断该从哪一版继续，因此必须先
「认领」（stamp）到某个 revision。认领是**破坏性**判断：stamp 低了会把已存在的
迁移重跑一遍，stamp 高了会让后续迁移永远不执行。所以本脚本只做一件事：
比对「业务表名集合指纹」与 `PRAGMA user_version`，一致才 stamp，不一致就打印
差异表并以退出码 3 退出（**绝不强制 stamp**）。

指纹 = `sqlite_master` 中 `type='table' and name not like 'sqlite_%'`（排除
Alembic 自己的 `alembic_version`）排序后用 `\n` 连接再 sha256，与
`Base.metadata` 生成的表名集合用**同一算法**计算，因此两边口径必然一致。

认领目标固定为 `0002_engine_upgrade`（不是 head）：存量库缺的是本轮新增的 6 个
列，stamp 到 head 会让幂等的 `0003_iteration2` 永远不执行；stamp 到 0002 后由
`0003` 的条件加列补齐，若该库恰好已由新元数据 `create_all` 建过（列已存在），
条件加列会自动跳过。

用法（仓库根目录或 `apps/api` 下均可）：:

    python scripts/adopt_alembic.py --db var/skillrouter.db
    python scripts/adopt_alembic.py --db var/skillrouter.db --dry-run

退出码：``0`` = 一致（已 stamp 或 dry-run 通过）· ``3`` = 指纹不一致（未写任何版本）
· ``2`` = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
MIGRATIONS_DIR = API_DIR / "migrations"
if str(API_DIR) not in sys.path:  # 以脚本方式运行时仍能 import skillrouter_api
    sys.path.insert(0, str(API_DIR))

#: 存量库认领到哪一版：0002（此后由幂等的 0003 补齐本轮新增列）
STAMP_REVISION = "0002_engine_upgrade"
#: 存量库应处于的 PRAGMA user_version（0001 + 0002 时代由 create_schema 写入 1）
LEGACY_USER_VERSION = 1
#: 不计入业务表指纹：Alembic 自己的版本表 + SQLite 内部表（`sqlite_%` 前缀已排除）
IGNORED_TABLES = frozenset({"alembic_version", "sqlite_sequence", "sqlite_stat1", "sqlite_stat4"})
#: 指纹不一致的退出码（spec 固定为 3）
EXIT_MISMATCH = 3


def fingerprint(names: Iterable[str]) -> str:
    """表名集合指纹：排序 → `\\n` 连接 → sha256（库与元数据两侧同一算法）。"""
    joined = "\n".join(sorted(names))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def metadata_tables() -> set[str]:
    """当前模型元数据里的业务表名（与迁移里 `create_all` 用的是同一份 Base）。"""
    from skillrouter_api import models as _models  # noqa: F401  确保全部表注册
    from skillrouter_api.db.base import Base

    return {name for name in Base.metadata.tables if name not in IGNORED_TABLES}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开，绝不因为「看错库」而新建一个空 DB 文件。"""
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def db_tables(db_path: str | Path) -> set[str]:
    """库里已有的业务表名；文件不存在时返回空集合。"""
    path = Path(db_path)
    if not path.is_file():
        return set()
    connection = _connect_ro(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        connection.close()
    return {
        str(row[0])
        for row in rows
        if not str(row[0]).startswith("sqlite_") and str(row[0]) not in IGNORED_TABLES
    }


def read_user_version(db_path: str | Path) -> int:
    """读 `PRAGMA user_version`；文件不存在时视为 0（= 无版本记录）。"""
    path = Path(db_path)
    if not path.is_file():
        return 0
    connection = _connect_ro(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


@dataclass(frozen=True)
class Report:
    """一次指纹比对的结果（只读，不含任何写操作）。"""

    db_path: Path
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    user_version: int

    @property
    def missing(self) -> list[str]:
        """元数据有、库里没有（存量库偏旧）。"""
        return [name for name in self.expected if name not in set(self.actual)]

    @property
    def extra(self) -> list[str]:
        """库里有、元数据没有（多出来的表，认领不安全）。"""
        return [name for name in self.actual if name not in set(self.expected)]

    @property
    def expected_fingerprint(self) -> str:
        return fingerprint(self.expected)

    @property
    def actual_fingerprint(self) -> str:
        return fingerprint(self.actual)

    @property
    def version_ok(self) -> bool:
        return self.user_version == LEGACY_USER_VERSION

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra and self.version_ok

    def render(self) -> str:
        """人读的差异表（启动日志与 CLI 共用）。"""
        lines = [
            f"[ADOPT] db={self.db_path}",
            f"  指纹（业务表名集合，排序后 sha256）expected={self.expected_fingerprint[:16]} "
            f"actual={self.actual_fingerprint[:16]}",
            f"  {'状态':<8}{'表名':<40}说明",
        ]
        for name in self.missing:
            lines.append(f"  {'MISSING':<8}{name:<40}元数据有、库里没有")
        for name in self.extra:
            lines.append(f"  {'EXTRA':<8}{name:<40}库里有、元数据没有，需人工确认来源")
        if not self.missing and not self.extra:
            lines.append(f"  {'TABLES':<8}{'<全部一致>':<40}{len(self.expected)} 张业务表")
        lines.append(
            f"  {'VERSION':<8}{'PRAGMA user_version':<40}"
            f"实际 {self.user_version} / 期望 {LEGACY_USER_VERSION} "
            f"[{'OK' if self.version_ok else '不一致'}]"
        )
        if self.ok:
            lines.append(
                f"  结论：一致 → 可 stamp {STAMP_REVISION}（随后 upgrade head 由 0003 补齐新列）"
            )
        else:
            lines.append(
                f"  结论：不一致 → 拒绝 stamp（退出码 {EXIT_MISMATCH}）。"
                "请先备份并核对 db/migrations/0001_init.sql 快照与残表来源；本脚本绝不强制 stamp"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "db": str(self.db_path),
            "ok": self.ok,
            "missing": self.missing,
            "extra": self.extra,
            "user_version": self.user_version,
            "expected_user_version": LEGACY_USER_VERSION,
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
            "stamp_revision": STAMP_REVISION,
        }


def inspect_db(db_path: str | Path) -> Report:
    """只读比对（不写库、不 import alembic 运行时）。"""
    path = Path(db_path)
    return Report(
        db_path=path,
        expected=tuple(sorted(metadata_tables())),
        actual=tuple(sorted(db_tables(path))),
        user_version=read_user_version(path),
    )


def alembic_config(db_path: str | Path | None = None) -> Config:
    """构造 Alembic Config：`script_location` 用绝对路径，因此不依赖 cwd。

    `db_path` 给出时通过 `sqlalchemy.url` 显式指定目标库（`migrations/env.py`
    会优先尊重这个显式值），避免误连到环境变量指向的另一个 DB。
    """
    from alembic.config import Config

    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if db_path is not None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    return config


def stamp(db_path: str | Path, revision: str = STAMP_REVISION) -> None:
    """把存量库认领到 `revision`（默认 0002）；**只应在 Report.ok 为真时调用**。"""
    from alembic import command

    command.stamp(alembic_config(db_path), revision)


def upgrade(db_path: str | Path, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(alembic_config(db_path), revision)


def default_db() -> Path:
    from skillrouter_api.settings import settings

    return settings.db_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="存量库接入 Alembic：指纹一致才 stamp 0002_engine_upgrade（不一致退出 3）"
    )
    parser.add_argument("--db", default=None, help="目标 SQLite 文件（默认取 SKILLROUTER_DB_PATH）")
    parser.add_argument("--dry-run", action="store_true", help="只打印比对结果，不写任何版本信息")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db()
    tables = db_tables(db_path)
    if not tables:
        # 空库不需要认领：直接 alembic upgrade head 即可（bootstrap 也走这条路）
        print(
            f"[ADOPT] db={db_path} 没有业务表（空库/新库）→ 无需 adopt，请直接 "
            f"python -m alembic -c migrations/alembic.ini upgrade head"
        )
        return 0

    report = inspect_db(db_path)
    print(report.render())
    if not report.ok:
        return EXIT_MISMATCH
    if args.dry_run:
        print(f"[ADOPT] --dry-run：未写入版本；将执行 alembic stamp {STAMP_REVISION}")
        return 0

    stamp(db_path)
    print(
        f"[ADOPT] 已 stamp {STAMP_REVISION}；接着执行 "
        f"python -m alembic -c migrations/alembic.ini upgrade head（由 0003 补齐新增列）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
