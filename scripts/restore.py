#!/usr/bin/env python3
"""Skill Router 恢复（spec §7.4 / §10.3，v2 修正 R09）。

流程（顺序不可调换）
--------------------
1. 读 `manifest.json`（zip 内嵌的权威副本）并校验格式；
2. **主密钥指纹**必须与当前主密钥一致 —— 不一致立即拒绝（否则恢复出来的凭证永远无法解密）；
3. 备份 schema **不得新于**当前程序（禁止「用新库跑旧代码」）；
4. 逐个校验 entries 的 sha256（先验证、后落盘，避免半损坏的包把生产库写坏）；
5. 把当前 DB 再备一份（`--keep-current` 默认开启），然后替换 DB 与 `var/files`；
6. 若可用，`alembic upgrade head`（允许从更旧 schema 升级）；
7. `verify_restore()` 自检：与 manifest 的 `row_counts` 比对行数，并打印差异。

仅标准库；Windows 与 `python:3.11-slim` 均可直接运行（无 sqlite3 CLI / tar 依赖）。

用法
----
    python scripts/restore.py var/backup/skillrouter-20261018T054125Z.zip --dry-run
    python scripts/restore.py var/backup/skillrouter-20261018T054125Z.zip --yes

退出码：0 成功；1 自检失败；2 参数错误；3 主密钥指纹不匹配；
        4 schema 比当前程序新/未知；5 备份包完整性校验失败。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_FORMAT = "skillrouter-backup/1"
CHUNK = 1024 * 1024
MIGRATION_DIR_CANDIDATES = (
    "apps/api/migrations/versions",
    "apps/api/alembic/versions",
    "migrations/versions",
    "db/migrations/versions",
)
ALEMBIC_INI_CANDIDATES = (
    "apps/api/migrations/alembic.ini",
    "apps/api/alembic.ini",
    "alembic.ini",
)


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


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _or_dash(value: object) -> str:
    """None 显示成 `—`，避免把「未知」打印成字面量 None 被误读为真值。"""
    return "—" if value is None or value == "" else str(value)


def fingerprint_key(material: str) -> str:
    return hashlib.sha256(material.strip().encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── 配置解析（与 backup.py 保持一致）──────────────────────────────────────


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


def resolve_master_key(data_dir: Path) -> tuple[str | None, str]:
    env_value = (os.environ.get("SKILLROUTER_MASTER_KEY") or "").strip()
    if env_value:
        return env_value, "env:SKILLROUTER_MASTER_KEY"
    key_file = data_dir / "credentials-master.key"
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value, f"file:{key_file}"
    return None, "missing"


# ── schema 版本判定 ────────────────────────────────────────────────────────


def known_revisions(root: Path | None = None) -> set[str]:
    """扫描本地 Alembic revision 文件，得到「当前程序认识」的 revision 集合。

    只做正则提取（不 import alembic），以便在依赖未安装时也能判定。
    """
    base = root or Path.cwd()
    found: set[str] = set()
    for rel in MIGRATION_DIR_CANDIDATES:
        folder = base / rel
        if not folder.is_dir():
            continue
        for path in folder.glob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.finditer(
                r"^[ \t]*revision(?::\s*[A-Za-z_\[\]\.]+)?\s*[:=]\s*[\"']([^\"']+)[\"']",
                text,
                re.MULTILINE,
            ):
                found.add(match.group(1))
    return found


def head_revision(revisions: set[str], root: Path | None = None) -> str | None:
    """粗判 head：存在但没有任何 revision 被当作 down_revision 引用的那个。"""
    base = root or Path.cwd()
    referenced: set[str] = set()
    for rel in MIGRATION_DIR_CANDIDATES:
        folder = base / rel
        if not folder.is_dir():
            continue
        for path in folder.glob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.finditer(
                r"^[ \t]*down_revision(?::\s*[A-Za-z_\[\]\.]+)?\s*[:=]\s*(.+)$",
                text,
                re.MULTILINE,
            ):
                referenced.update(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))
    heads = sorted(revisions - referenced)
    return heads[-1] if heads else None


def find_alembic_ini(root: Path) -> Path | None:
    for rel in ALEMBIC_INI_CANDIDATES:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


# ── 包校验与解包 ───────────────────────────────────────────────────────────


def read_manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as zf:
        try:
            raw = zf.read("manifest.json")
        except KeyError as exc:
            raise ValueError(
                "备份包内没有 manifest.json（不是本平台生成的包？）"
            ) from exc
    return json.loads(raw.decode("utf-8"))


def verify_entries(archive: Path, manifest: dict) -> list[str]:
    """逐条校验 sha256；返回问题列表（空 = 全部通过）。"""
    problems: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        for entry in manifest.get("entries", []):
            name = entry.get("path")
            if not name:
                problems.append("manifest 中存在缺少 path 的条目")
                continue
            if name not in names:
                problems.append(f"缺少条目：{name}")
                continue
            digest = hashlib.sha256()
            size = 0
            with zf.open(name) as handle:
                for chunk in iter(lambda: handle.read(CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != entry.get("sha256"):
                problems.append(f"sha256 不匹配：{name}")
            elif entry.get("size") is not None and size != entry["size"]:
                problems.append(f"大小不匹配：{name}（{size} != {entry['size']}）")
    return problems


def existing_db_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        con = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.DatabaseError:
        return {}
    try:
        tables = [
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        ]
        for table in tables:
            try:
                counts[table] = int(
                    con.execute(f'select count(*) from "{table}"').fetchone()[0]
                )
            except sqlite3.DatabaseError:
                counts[table] = -1
    finally:
        con.close()
    return counts


def run_alembic_upgrade(ini: Path) -> tuple[bool, str]:
    if importlib.util.find_spec("alembic") is None:
        return (
            False,
            "未安装 alembic（跳过 upgrade head；装好后手工执行 `alembic upgrade head`）",
        )
    project = ini.parent.parent if ini.parent.name == "migrations" else ini.parent
    env = dict(os.environ)
    python_path = str(project)
    env["PYTHONPATH"] = python_path + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"alembic 调用失败：{exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, f"alembic upgrade head 退出码 {proc.returncode}：{output[-2000:]}"
    return True, output[-2000:] or "(无输出)"


def verify_restore(db_path: Path, manifest: dict) -> tuple[bool, list[str]]:
    """与 manifest 的 row_counts 比对；返回 (是否一致, 差异说明)。"""
    problems: list[str] = []
    expected = (manifest.get("db") or {}).get("row_counts") or {}
    actual = existing_db_counts(db_path)
    for table, want in expected.items():
        if want is None or want < 0:
            continue
        have = actual.get(table)
        if have is None:
            problems.append(f"表缺失：{table}（备份 {want} 行）")
        elif have != want:
            problems.append(f"行数不一致：{table} 备份 {want} 行 / 实际 {have} 行")
    return (not problems), problems


# ── 主流程 ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restore.py",
        description="Skill Router 恢复（校验 manifest + 主密钥指纹 + schema，然后恢复 DB 与文件）",
    )
    parser.add_argument("archive", help="backup.py 生成的 .zip")
    parser.add_argument(
        "--data-dir", help="持久化根目录（默认 $SKILLROUTER_DATA_DIR 或 var）"
    )
    parser.add_argument("--db", help="目标 SQLite 路径（默认按 settings.py 语义解析）")
    parser.add_argument("--dry-run", action="store_true", help="只校验、不写盘")
    parser.add_argument(
        "--yes", action="store_true", help="跳过交互确认（自动化/容器内使用）"
    )
    parser.add_argument(
        "--allow-unknown-schema",
        action="store_true",
        help="允许恢复「本地无法识别」的 schema revision（默认拒绝，禁止用新库跑旧代码）",
    )
    parser.add_argument(
        "--no-keep-current",
        action="store_true",
        help="不把当前 DB 另存一份（默认会先备份到 <data-dir>/backup/pre-restore-*.db）",
    )
    parser.add_argument(
        "--fresh-files",
        action="store_true",
        help="恢复文件前清空 <data-dir>/files（默认覆盖同名文件、保留多余文件）",
    )
    parser.add_argument(
        "--no-migrate", action="store_true", help="不执行 alembic upgrade head"
    )
    parser.add_argument(
        "--no-marker",
        action="store_true",
        help="不写 <data-dir>/.restore-in-progress 维护标记",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)

    archive = Path(args.archive).expanduser()
    if not archive.is_absolute():
        archive = (Path.cwd() / archive).resolve()
    if not archive.is_file():
        log(f"[restore] [FATAL] 备份包不存在：{archive}")
        return 2

    data_dir = resolve_data_dir(args.data_dir)
    db_path = resolve_db_path(data_dir, args.db)
    marker = data_dir / ".restore-in-progress"

    log("[restore] Skill Router 恢复 —— 校验优先，先验证后落盘")
    log(f"[restore] 备份包　：{archive} ({human_bytes(archive.stat().st_size)})")
    log(f"[restore] 数据目录：{data_dir}")
    log(f"[restore] 目标 DB ：{db_path}")

    try:
        manifest = read_manifest(archive)
    except (ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        log(f"[restore] [FATAL] 无法读取 manifest：{exc}")
        return 5

    if manifest.get("format") != MANIFEST_FORMAT:
        log(
            f"[restore] [FATAL] manifest 格式不支持：{manifest.get('format')!r}"
            f"（期望 {MANIFEST_FORMAT}）"
        )
        return 5

    log("")
    log(f"[restore] 备份时间　　：{manifest.get('created_at')}")
    log(f"[restore] 备份模式　　：{manifest.get('mode')}")
    log(f"[restore] schema_version：{_or_dash(manifest.get('schema_version'))}")
    log(f"[restore] migration　　：{_or_dash(manifest.get('migration_revision'))}")
    log(
        f"[restore] engine_pin　：{_or_dash((manifest.get('app') or {}).get('engine_pin'))}"
    )

    # ── 1. 主密钥指纹 ──
    key_material, key_source = resolve_master_key(data_dir)
    expected_fp = (manifest.get("master_key") or {}).get("fingerprint")
    if not key_material:
        log(
            "[restore] [FATAL] 当前环境没有主密钥（SKILLROUTER_MASTER_KEY 或 "
            f"{data_dir / 'credentials-master.key'}）。"
        )
        log("[restore]         凭证在恢复后将永远无法解密，已拒绝执行。")
        return 3
    current_fp = fingerprint_key(key_material)
    if expected_fp and current_fp != expected_fp:
        log("[restore] [FATAL] 主密钥与备份不匹配，已拒绝恢复。")
        log(
            f"[restore]         备份指纹 {expected_fp} / 当前指纹 {current_fp}（来源 {key_source}）"
        )
        log(
            "[restore]         处理：把备份时使用的主密钥恢复到本机（或从 PREVIOUS 恢复），再重试。"
        )
        return 3
    if not expected_fp:
        log(
            "[restore] [WARN] 备份 manifest 没有主密钥指纹（可能是旧格式备份），已跳过该检查。"
        )
    else:
        log(f"[restore] 主密钥指纹　：{current_fp} ✓ 匹配")

    # ── 2. schema 新于当前程序？──
    backup_rev = manifest.get("migration_revision")
    revisions = known_revisions()
    if not revisions:
        log(
            "[restore] [WARN] 本地未找到 Alembic revision 文件，无法判定「备份是否比程序新」；"
            "已继续（--allow-unknown-schema 可显式承认该状态）。"
        )
    else:
        head = head_revision(revisions)
        log(f"[restore] 本地 revision ：{len(revisions)} 个，head≈{head}")
        if backup_rev and backup_rev not in revisions:
            if not args.allow_unknown_schema:
                log(
                    f"[restore] [FATAL] 备份 revision {backup_rev} 不在本地 revision 集合中 —— "
                    "该备份来自更新的程序版本，禁止用新库跑旧代码。"
                )
                log(
                    "[restore]         处理：升级到对应程序版本后再恢复，或确认后加 --allow-unknown-schema。"
                )
                return 4
            log(
                f"[restore] [WARN] 备份 revision {backup_rev} 本地不认识，"
                "因 --allow-unknown-schema 已继续。"
            )

    # ── 3. 完整性 ──
    problems = verify_entries(archive, manifest)
    if problems:
        log("[restore] [FATAL] 备份包完整性校验失败：")
        for item in problems[:20]:
            log(f"[restore]         - {item}")
        if len(problems) > 20:
            log(f"[restore]         ... 共 {len(problems)} 项")
        return 5
    log(
        f"[restore] 完整性校验　：{manifest.get('entry_count', 0)} 个条目 sha256 全部通过 ✓"
    )

    # ── 4. 摘要与确认 ──
    files_meta = manifest.get("files") or {}
    extra_meta = manifest.get("extra") or {}
    log("")
    log("[restore] 将要执行：")
    log(f"[restore]   1) 用备份 DB 替换 {db_path}")
    if files_meta.get("included"):
        log(
            f"[restore]   2) 恢复 {files_meta.get('count')} 个文件到 {data_dir / 'files'}"
            f"（{human_bytes(int(files_meta.get('bytes') or 0))}）"
            + ("，先清空目标目录" if args.fresh_files else "，覆盖同名文件")
        )
    else:
        log("[restore]   2) 该备份为 quick 模式，不含 var/files —— 文件不会被改动")
    if extra_meta.get("count"):
        log(
            f"[restore]   3) 恢复 {extra_meta.get('count')} 个额外文件（data/extra/* → {data_dir}）"
        )
    log("[restore]   4) 运行 alembic upgrade head（若可用）并做行数自检")

    if args.dry_run:
        log("")
        log("[restore] --dry-run：校验全部通过，未写入任何文件。")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            log("[restore] [FATAL] 非交互环境必须显式传 --yes。")
            return 2
        answer = input(
            "[restore] 确认恢复？请确认 api/worker 已停止（输入 restore 继续）："
        ).strip()
        if answer != "restore":
            log("[restore] 已取消。")
            return 0

    # ── 5. 维护标记 + 另存当前 DB ──
    if not args.no_marker:
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "started_at": utc_stamp(),
                    "archive": str(archive),
                    "pid": os.getpid(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log(f"[restore] 维护标记　：{marker}（恢复成功后自动删除）")

    if db_path.exists() and not args.no_keep_current:
        pre = data_dir / "backup" / f"pre-restore-{utc_stamp()}.db"
        pre.parent.mkdir(parents=True, exist_ok=True)
        try:
            src = sqlite3.connect(str(db_path), timeout=30)
            try:
                dst = sqlite3.connect(str(pre))
                try:
                    src.backup(dst)
                    dst.commit()
                finally:
                    dst.close()
            finally:
                src.close()
            log(f"[restore] 当前 DB 已另存：{pre}")
        except sqlite3.DatabaseError as exc:
            log(f"[restore] [WARN] 另存当前 DB 失败（继续）：{exc}")

    # ── 6. 落盘 ──
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (
        db_path.with_suffix(db_path.suffix + "-wal"),
        db_path.with_suffix(db_path.suffix + "-shm"),
    ):
        if stale.exists():
            stale.unlink()

    with zipfile.ZipFile(archive) as zf:
        tmp_db = db_path.with_suffix(db_path.suffix + ".restoring")
        with zf.open("db/skillrouter.db") as src, tmp_db.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        os.replace(tmp_db, db_path)
        log(f"[restore] DB 已替换：{db_path}")

        if files_meta.get("included"):
            files_root = data_dir / "files"
            if args.fresh_files and files_root.exists():
                shutil.rmtree(files_root)
            files_root.mkdir(parents=True, exist_ok=True)
            restored = 0
            for name in zf.namelist():
                if not name.startswith("data/files/") or name.endswith("/"):
                    continue
                relative = name[len("data/files/") :]
                target = files_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                restored += 1
            log(f"[restore] 文件已恢复：{restored} 个 → {files_root}")

        restored_extra = 0
        for name in zf.namelist():
            if not name.startswith("data/extra/") or name.endswith("/"):
                continue
            relative = name[len("data/extra/") :]
            target = data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            restored_extra += 1
        if restored_extra:
            log(f"[restore] 额外文件已恢复：{restored_extra} 个")

    # ── 7. 迁移 + 自检 ──
    if not args.no_migrate:
        ini = find_alembic_ini(Path.cwd())
        if ini is None:
            log("[restore] [WARN] 未找到 alembic.ini，跳过 upgrade head。")
        else:
            ok, detail = run_alembic_upgrade(ini)
            if ok:
                log(f"[restore] alembic upgrade head ✓（{ini}）")
                if detail and detail != "(无输出)":
                    log(f"[restore] {detail.splitlines()[-1]}")
            else:
                log(f"[restore] [WARN] {detail}")

    consistent, diffs = verify_restore(db_path, manifest)
    log("")
    log("[restore] verify_restore() 行数自检：")
    expected_counts = (manifest.get("db") or {}).get("row_counts") or {}
    actual_counts = existing_db_counts(db_path)
    shown = 0
    for table in sorted(expected_counts):
        want = expected_counts[table]
        if want is None or want < 0:
            continue
        have = actual_counts.get(table)
        mark = "✓" if have == want else "✗"
        if have != want or want > 0:
            log(
                f"[restore]   {mark} {table:<28} 备份 {want:>8} / 实际 {have if have is not None else '—':>8}"
            )
            shown += 1
        if shown >= 40:
            log("[restore]   ... 其余表省略")
            break
    if consistent:
        log("[restore] 自检通过：所有表行数与备份一致 ✓")
    else:
        for item in diffs[:20]:
            log(f"[restore]   ✗ {item}")
        log("[restore] 自检未通过：请勿直接对外提供服务，先排查上面的差异。")

    if marker.exists() and consistent:
        marker.unlink()
        log(f"[restore] 维护标记已删除：{marker}")

    log("")
    log(
        "[restore] 下一步：重启 api/worker → 用原密码登录 → 抽查一个凭证能否解密、一个能力能否调用。"
    )
    if not consistent:
        log("[restore] 注意：自检未通过时维护标记保留在 " + str(marker))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
