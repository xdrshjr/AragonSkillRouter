#!/usr/bin/env python3
"""写入 **MOCK 演示数据**（不是联调数据，不得用于验收）。

为什么单独一个脚本
------------------
§15.2/§15.5 明确禁止用 mock 结果冒充真实联调。因此演示数据必须：

* 一眼能看出来是假的 —— 每一行 `name` 都带 `[MOCK]` 前缀，id 带 `mock` 段；
* 不可被当成真实能力调用 —— 能力与凭证都落在 `draft` / `disabled`，不参与路由与执行；
* 不会被误认为已联调 —— 凭证密文是**结构性占位**（`v1.mk1.MOCK.MOCK`），解密必然失败；
* 生产环境默认拒绝 —— `SKILLROUTER_ENV=production` 时必须显式 `--force`。

做法
----
直接写 SQLite（不走 HTTP，不依赖 ORM），但对 schema 做**内省**：只写目标表里真实存在的列，
缺表/缺列时明确报错或跳过，绝不「猜着写」。因此它在本仓库 schema 演进后仍可安全使用。

用法
----
    python scripts/seed_demo.py                     # 写入演示数据（development/staging）
    python scripts/seed_demo.py --clean             # 只删除本脚本写入的数据
    python scripts/seed_demo.py --force             # 生产环境强制写入（会打大号警告）

退出码：0 成功；1 运行失败；2 参数/前置条件不满足（含生产环境拒绝）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MOCK_PREFIX = "[MOCK]"
MOCK_ID_SEGMENT = "mock"
REQUIRED_TABLES = ("providers", "credentials", "capabilities")

BANNER = """
================================================================================
  MOCK 演示数据 —— 不是真实联调数据
  --------------------------------------------------------------------------
  * 这里的所有行都不是真实上游调用结果，禁止写入验收报告 / 性能报告 / DoD 勾选。
  * 凭证密文是结构性占位值，解密必然失败；能力与 SKILL 均落在 draft 状态。
  * 真实联调请按 docs/runbook/verification-checklist.md 执行，
    没跑通的项在文档里保持「待验证」。
================================================================================
"""


# ── 基础工具 ────────────────────────────────────────────────────────────────


def _utf8_stdout() -> None:
    try:  # pragma: no cover - 取决于宿主终端
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass


def log(message: str = "") -> None:
    print(message, flush=True)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_data_dir(raw: str | None) -> Path:
    candidate = Path(
        raw or os.environ.get("SKILLROUTER_DATA_DIR") or "var"
    ).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def resolve_db_path(data_dir: Path, raw: str | None) -> Path:
    if raw:
        path = Path(raw).expanduser()
        return path.resolve() if path.is_absolute() else (data_dir / path).resolve()
    env_raw = os.environ.get("SKILLROUTER_DB_PATH")
    if env_raw:
        env_path = Path(env_raw).expanduser()
        if env_path.is_absolute():
            return env_path.resolve()
        for candidate in (data_dir / env_path, Path.cwd() / env_path):
            if candidate.exists():
                return candidate.resolve()
        return (Path.cwd() / env_path).resolve()
    for candidate in (
        data_dir / "skillrouter.db",
        Path.cwd() / "var" / "skillrouter.db",
    ):
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / "var" / "skillrouter.db").resolve()


# ── schema 内省 ────────────────────────────────────────────────────────────


def table_columns(con: sqlite3.Connection, table: str) -> dict[str, dict]:
    rows = con.execute(f'pragma table_info("{table}")').fetchall()
    return {
        row[1]: {"type": row[2], "notnull": bool(row[3]), "default": row[4]}
        for row in rows
    }


def insert_row(
    con: sqlite3.Connection, table: str, values: dict, *, dry_run: bool = False
) -> tuple[bool, str]:
    """只写目标表真实存在的列；缺少必填无默认值时直接放弃该行并说明原因。"""
    columns = table_columns(con, table)
    if not columns:
        return False, f"表不存在：{table}"
    usable = {k: v for k, v in values.items() if k in columns}
    missing = [
        name
        for name, meta in columns.items()
        if meta["notnull"] and meta["default"] is None and name not in usable
    ]
    if missing:
        return False, f"{table} 缺少必填列 {missing}，已跳过该行（请先补全脚本）"
    placeholders = ", ".join("?" for _ in usable)
    columns_sql = ", ".join(f'"{c}"' for c in usable)
    if dry_run:
        return True, f"[dry-run] INSERT {table} ({columns_sql})"
    con.execute(
        f'INSERT INTO "{table}" ({columns_sql}) VALUES ({placeholders})',
        tuple(usable.values()),
    )
    return True, f"INSERT {table}: {usable.get('id')}"


# ── 演示数据（全部带 [MOCK] 标记）─────────────────────────────────────────


def mock_rows() -> list[tuple[str, dict]]:
    now = iso_now()
    provider = {
        "id": "prv_mock_openai",
        "slug": "mock-openai",
        "name": f"{MOCK_PREFIX} OpenAI 兼容端点（演示）",
        "category": "llm",
        "base_url": "https://mock.invalid/v1",
        "docs_url": "https://example.invalid/mock-docs",
        "tags": json.dumps(["mock", "demo"], ensure_ascii=False),
        "environment": "dev",
        "notes": "MOCK 演示数据：base_url 不可达，不存在真实凭证。",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    provider2 = {
        "id": "prv_mock_image",
        "slug": "mock-image",
        "name": f"{MOCK_PREFIX} 图像生成端点（演示）",
        "category": "image",
        "base_url": "https://mock-image.invalid/v1",
        "tags": json.dumps(["mock", "demo"], ensure_ascii=False),
        "environment": "dev",
        "notes": "MOCK 演示数据：用于展示「同一能力多条 route」的界面形态。",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    credential = {
        "id": "crd_mock_openai_main",
        "provider_id": provider["id"],
        "name": f"{MOCK_PREFIX} 演示凭证（占位密文，不可解密）",
        "kind": "llm",
        "auth_type": "bearer",
        "header_name": "Authorization",
        "secret_ciphertext": "v1.mk1.MOCK.MOCK",  # 结构性占位，解密必然失败
        "meta": json.dumps({"mock": True}, ensure_ascii=False),
        "status": "disabled",
        "created_at": now,
        "updated_at": now,
    }
    credential2 = {
        "id": "crd_mock_image_main",
        "provider_id": provider2["id"],
        "name": f"{MOCK_PREFIX} 演示凭证（占位密文，不可解密）",
        "kind": "image",
        "auth_type": "api_key",
        "header_name": "X-API-Key",
        "secret_ciphertext": "v1.mk1.MOCK.MOCK",
        "meta": json.dumps({"mock": True}, ensure_ascii=False),
        "status": "disabled",
        "created_at": now,
        "updated_at": now,
    }
    capability = {
        "id": "cap_mock_text_echo",
        "key": "demo.text.echo",
        "name": f"{MOCK_PREFIX} 文本回显",
        "kind": "text.generate",
        "status": "draft",
        "description": "MOCK 演示能力：只用于展示表单/列表形态，永远不会被真实路由。",
        "compatibility_group": "mock.text.v1",
        "draft_definition": json.dumps(
            {
                "key": "demo.text.echo",
                "name": f"{MOCK_PREFIX} 文本回显",
                "kind": "text.generate",
                "modes": ["sync"],
                "file_io": {"input": [], "output": []},
                "input_schema": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string", "maxLength": 2000}},
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "examples": [{"title": "MOCK 示例", "input": {"prompt": "你好"}}],
                "compatibility_group": "mock.text.v1",
                "failover": {"enabled": False, "max_switches": 0},
                "requires_agent_reasoning": False,
                "tags": ["mock"],
                "routes": [
                    {
                        "id": "rt_mock_primary",
                        "provider_id": provider["id"],
                        "credential_id": credential["id"],
                        "priority": 0,
                        "enabled": False,
                        "model_hint": "mock-model",
                        "request": {
                            "method": "POST",
                            "url_template": "{base_url}/chat/completions",
                            "headers": {"Content-Type": "application/json"},
                            "body_template": {
                                "model": "${model}",
                                "prompt": "${prompt}",
                            },
                            "encoding": "json",
                        },
                        "param_map": {
                            "prompt": {
                                "from": "input.prompt",
                                "transform": "passthrough",
                            }
                        },
                        "response_map": {"format": "json", "jsonpath": "$.text"},
                        "timeout_ms": 15000,
                        "retry": {"max_attempts": 1, "base_ms": 500, "max_ms": 2000},
                    }
                ],
                "mock": True,
            },
            ensure_ascii=False,
        ),
        "created_at": now,
        "updated_at": now,
    }
    skill = {
        "id": "skl_mock_demo",
        "name": "demo-echo-skill",
        "title": f"{MOCK_PREFIX} 文本回显 SKILL（演示）",
        "description": "MOCK 演示 SKILL：依赖 demo.text.echo，永不发布。",
        "instructions": "这是 MOCK 演示内容，不代表任何真实能力。",
        "status": "draft",
        "version": "0.0.0-mock",
        "enabled": 0,
        "activation": "manual",
        "tags": json.dumps(["mock"], ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }
    return [
        ("providers", provider),
        ("providers", provider2),
        ("credentials", credential),
        ("credentials", credential2),
        ("capabilities", capability),
        ("skills", skill),
    ]


def clean(con: sqlite3.Connection, dry_run: bool) -> int:
    """只删本脚本写入的行（id 含 `mock` 或名称带 `[MOCK]` 前缀）。"""
    removed = 0
    candidates = (
        ("skills", "id like ? or name like ? or title like ?"),
        ("capabilities", "id like ? or key like ? or name like ?"),
        ("credentials", "id like ? or name like ?"),
        ("providers", "id like ? or slug like ? or name like ?"),
    )
    for table, where in candidates:
        if not table_columns(con, table):
            continue
        params = [f"%{MOCK_ID_SEGMENT}%"] + [f"{MOCK_PREFIX}%"] * (where.count("?") - 1)
        rows = con.execute(f'select id from "{table}" where {where}', params).fetchall()
        for (row_id,) in rows:
            log(f"[seed] {'[dry-run] ' if dry_run else ''}DELETE {table}: {row_id}")
            if not dry_run:
                try:
                    con.execute(f'delete from "{table}" where id = ?', (row_id,))
                except sqlite3.DatabaseError as exc:
                    log(
                        f"[seed] [WARN] 删除 {table}/{row_id} 失败（可能被外键引用）：{exc}"
                    )
                    continue
            removed += 1
    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_demo.py",
        description="写入 Skill Router 的 MOCK 演示数据（禁止用于验收）",
    )
    parser.add_argument(
        "--data-dir", help="持久化根目录（默认 $SKILLROUTER_DATA_DIR 或 var）"
    )
    parser.add_argument("--db", help="SQLite 路径（默认按 settings.py 语义解析）")
    parser.add_argument("--clean", action="store_true", help="只删除演示数据，不写入")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 SQL")
    parser.add_argument(
        "--force",
        action="store_true",
        help="在 SKILLROUTER_ENV=production 下强制执行（不推荐）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)

    env = (os.environ.get("SKILLROUTER_ENV") or "development").strip().lower()
    data_dir = resolve_data_dir(args.data_dir)
    db_path = resolve_db_path(data_dir, args.db)

    log(BANNER)
    log(f"[seed] 环境　：SKILLROUTER_ENV={env}")
    log(f"[seed] 数据库：{db_path}")

    if env == "production" and not args.force:
        log(
            "[seed] [FATAL] SKILLROUTER_ENV=production：拒绝写入 MOCK 数据。\n"
            "[seed]         演示数据一旦进入生产库，会被误当成真实能力出现在列表里。\n"
            "[seed]         确需演示请用 staging/development 环境；或用 --force 并自行承担后果。"
        )
        return 2
    if env == "production" and args.force:
        log(
            "[seed] [WARN] 生产环境 + --force：正在往生产库写入 MOCK 数据，请立即记入运维日志。"
        )

    if not db_path.exists():
        log(
            f"[seed] [FATAL] 数据库不存在：{db_path}（先运行 `python scripts/bootstrap.py`）"
        )
        return 2

    try:
        con = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.DatabaseError as exc:
        log(f"[seed] [FATAL] 打不开数据库：{exc}")
        return 1

    try:
        con.execute("pragma busy_timeout=5000")
        missing = [t for t in REQUIRED_TABLES if not table_columns(con, t)]
        if missing:
            log(f"[seed] [FATAL] schema 未就绪，缺少表：{missing}")
            log(
                "[seed]         先运行 `python scripts/bootstrap.py`（迁移 + 种子）后重试。"
            )
            return 2

        if args.clean:
            removed = clean(con, args.dry_run)
            if not args.dry_run:
                con.commit()
            log(
                f"[seed] 删除完成：{removed} 行（{'dry-run 未提交' if args.dry_run else '已提交'}）"
            )
            return 0

        inserted = 0
        for table, values in mock_rows():
            ok, detail = insert_row(con, table, values, dry_run=args.dry_run)
            log(f"[seed] {'✓' if ok else '✗'} {detail}")
            if ok:
                inserted += 1
            elif table in REQUIRED_TABLES:
                con.rollback()
                log(f"[seed] [FATAL] 关键表写入失败，已回滚：{detail}")
                return 1

        if args.dry_run:
            con.rollback()
            log(f"[seed] dry-run：{inserted} 行将被写入，未提交。")
            return 0
        con.commit()
        log(
            f"[seed] 写入完成：{inserted} 行（id 含 `mock`，名称带 `{MOCK_PREFIX}` 前缀；"
            "能力/凭证处于 draft/disabled，不会被路由或执行）。"
        )
        log(
            "[seed] 清理方式：`python scripts/seed_demo.py --clean`。\n"
            "[seed] 提醒：这些行**不能**用于 §15.2 的真实联调验收，验收请按 "
            "docs/runbook/verification-checklist.md 走。"
        )
        return 0
    except sqlite3.DatabaseError as exc:
        con.rollback()
        log(f"[seed] [FATAL] 数据库错误：{exc}")
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
