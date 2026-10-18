"""导出 `db/migrations/0001_init.sql` 可审阅快照（开发期工具，CI 校验用）。

    python apps/api/../..\\scripts\\export_schema_snapshot.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "apps" / "api" / "var" / "skillrouter.db"
OUT = ROOT / "db" / "migrations" / "0001_init.sql"


def main() -> int:
    if not DB.exists():
        print(f"[FATAL] 找不到数据库：{DB}（先运行 alembic upgrade head）")
        return 2
    conn = sqlite3.connect(DB)
    tables = sorted(
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    triggers = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'"))
    indexes = sorted(
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        )
    )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    alembic = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]

    lines = [
        "-- Skill Router 初始 schema 快照（与 Alembic 0001_init 对齐，便于人工审阅）",
        f"-- user_version={version}；alembic_version={alembic}",
        "-- 权威来源：apps/api/migrations/versions/0001_init.py。本文件只读，不参与迁移。",
        "",
    ]
    for name in tables:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row and row[0]:
            lines += [row[0].strip() + ";", ""]
    for name in indexes:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row and row[0]:
            lines.append(row[0].strip() + ";")
    lines.append("")
    for name in triggers:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row and row[0]:
            lines += [row[0].strip() + ";", ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"tables={len(tables)} indexes={len(indexes)} triggers={len(triggers)} -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
