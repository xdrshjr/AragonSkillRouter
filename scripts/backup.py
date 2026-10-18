#!/usr/bin/env python3
"""Skill Router 备份（spec §7.4 / §10.3 / §14.2，v2 修正 R09）。

设计约束
--------
* **仅标准库**：`sqlite3.Connection.backup()` 做一致性热备（WAL 下安全）、`zipfile` 打包、
  `json` 写 manifest。**不调用** `sqlite3` CLI，**不用** `tar` —— 运行镜像是
  `python:3.11-slim`（无 sqlite3 CLI），Windows 无 tar。
* 主密钥**本体永不进备份**，只导出指纹 `sha256(key_material)[:16]`（16 位十六进制）。
  `scripts/restore.py` 用它做「主密钥与备份是否匹配」的前置校验。
* 不依赖 pydantic-settings / SQLAlchemy：脚本可以在依赖未安装、服务未启动的情况下运行
  （entrypoint 在启动前就要调用它，见 `deploy/entrypoint.api.sh`）。

用法
----
    python scripts/backup.py                     # 全量：DB + var/files + manifest
    python scripts/backup.py --quick             # 快速：仅 DB + manifest（启动/升级前）
    python scripts/backup.py --include var/aragon-home --label pre-upgrade
    python scripts/backup.py --out var/backup --keep 14

产物
----
    <out>/skillrouter-<UTC时间戳>[-label].zip          备份包（内嵌 manifest.json）
    <out>/skillrouter-<...>.zip.manifest.json          manifest 副本（不解压即可核对）
    <out>/skillrouter-<...>.zip.sha256                 包文件 sha256（`sha256sum` 可直接校验）

退出码：0 成功；1 运行失败；2 参数/配置错误（例如数据库不存在）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_FORMAT = "skillrouter-backup/1"
DEFAULT_BACKUP_DIRNAME = "backup"
CHUNK = 1024 * 1024


# ── 基础工具 ────────────────────────────────────────────────────────────────


def _utf8_stdout() -> None:
    """cmd.exe 默认 GBK，中文输出会抛 UnicodeEncodeError；显式切到 UTF-8。"""
    try:  # pragma: no cover - 取决于宿主终端
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _or_dash(value: object) -> str:
    """None 显示成 `—`，避免把「未知」打印成字面量 None 被误读为真值。"""
    return "—" if value is None or value == "" else str(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_key(material: str) -> str:
    """`sha256(master_key)[:16]` —— 只做匹配校验，不可反推密钥本体。"""
    return hashlib.sha256(material.strip().encode("utf-8")).hexdigest()[:16]


# ── 配置解析（不依赖 pydantic-settings）────────────────────────────────────


def resolve_data_dir(raw: str | None) -> Path:
    candidate = Path(
        raw or os.environ.get("SKILLROUTER_DATA_DIR") or "var"
    ).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def resolve_db_path(data_dir: Path, raw: str | None) -> Path:
    """对齐 `settings.py` 的语义，同时容忍「DB 直接在 cwd 下」的历史布局。

    `settings.py::db_file` 的默认值（`SKILLROUTER_DB_PATH=var/skillrouter.db` 且
    `SKILLROUTER_DATA_DIR=var`）会解析成 `var/var/skillrouter.db`；生产环境按 §14.2 用
    绝对路径（`/data/skillrouter.db`）规避。这里按「绝对路径 > 已存在的候选 > cwd 相对」
    的顺序解析并打印结果，避免脚本和服务指向不同文件。
    """
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
    """返回 (密钥材料, 来源说明)。来源只用于日志，不打印密钥。"""
    env_value = (os.environ.get("SKILLROUTER_MASTER_KEY") or "").strip()
    if env_value:
        return env_value, "env:SKILLROUTER_MASTER_KEY"
    key_file = data_dir / "credentials-master.key"
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value, f"file:{key_file}"
    return None, "missing"


# ── SQLite 一致性热备 ──────────────────────────────────────────────────────


def snapshot_db(source: Path, target: Path) -> None:
    """用 `sqlite3.Connection.backup()` 生成一致快照（WAL 模式下安全）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def inspect_db(path: Path) -> dict:
    """统计行数与版本信息（在快照副本上读，保证自洽）。"""
    info: dict = {
        "tables": {},
        "row_counts": {},
        "schema_version": None,
        "migration_revision": None,
        "engine_pin": None,
    }
    con = sqlite3.connect(str(path), timeout=30)
    try:
        tables = [
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%' order by name"
            )
        ]
        info["tables"] = tables
        counts: dict[str, int] = {}
        for table in tables:
            try:
                counts[table] = int(
                    con.execute(f'select count(*) from "{table}"').fetchone()[0]
                )
            except sqlite3.DatabaseError:
                counts[table] = -1
        info["row_counts"] = counts

        if "system_state" in tables:
            for key, field in (
                ("schema_version", "schema_version"),
                ("engine_pin", "engine_pin"),
            ):
                try:
                    row = con.execute(
                        "select value from system_state where key = ?", (key,)
                    ).fetchone()
                except sqlite3.DatabaseError:
                    row = None
                if row and row[0] is not None:
                    info[field] = str(row[0])
        if info["schema_version"] is None:
            try:
                info["schema_version"] = str(
                    con.execute("pragma user_version").fetchone()[0]
                )
                info["schema_version_source"] = "pragma_user_version"
            except sqlite3.DatabaseError:
                pass
        if "alembic_version" in tables:
            try:
                row = con.execute("select version_num from alembic_version").fetchone()
            except sqlite3.DatabaseError:
                row = None
            if row:
                info["migration_revision"] = str(row[0])
    finally:
        con.close()
    return info


# ── 文件树 ─────────────────────────────────────────────────────────────────


def iter_tree(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _add_stream(zf: zipfile.ZipFile, data: bytes, arcname: str) -> None:
    zf.writestr(zipfile.ZipInfo(arcname, date_time=utc_now().timetuple()[:6]), data)


def _add_file(zf: zipfile.ZipFile, source: Path, arcname: str) -> tuple[str, int]:
    """流式写入 zip 并同时算 sha256（避免把 20MB 文件整体读进内存）。"""
    digest = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo(arcname, date_time=utc_now().timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as src, zf.open(info, "w") as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
    return digest.hexdigest(), size


# ── 主流程 ─────────────────────────────────────────────────────────────────


def create_backup(
    *,
    data_dir: Path,
    db_path: Path,
    out_dir: Path,
    quick: bool = False,
    label: str | None = None,
    includes: list[str] | None = None,
) -> dict:
    """生成一个备份包并返回 manifest 摘要（供 CLI 与测试复用）。"""
    if not db_path.exists():
        raise FileNotFoundError(
            f"数据库不存在：{db_path}（先运行 scripts/bootstrap.py）"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    created = utc_now()
    suffix = f"-{label}" if label else ""
    archive = out_dir / f"skillrouter-{stamp(created)}{suffix}.zip"

    key_material, key_source = resolve_master_key(data_dir)
    files_root = data_dir / "files"
    extra_roots = [Path(p) for p in (includes or [])]

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        db_info: dict = {}
        db_entry: dict = {}
        with _tempdir() as tmp:
            snapshot = tmp / "snapshot.db"
            snapshot_db(db_path, snapshot)
            db_info = inspect_db(snapshot)
            db_sha, db_size = _add_file(zf, snapshot, "db/skillrouter.db")
            db_entry = {
                "path": "db/skillrouter.db",
                "source": str(db_path),
                "sha256": db_sha,
                "size": db_size,
            }

        file_entries: list[dict] = []
        files_meta: dict = {
            "included": not quick,
            "root": str(files_root),
            "count": 0,
            "bytes": 0,
        }
        if not quick:
            for path in iter_tree(files_root):
                arcname = "data/files/" + path.relative_to(files_root).as_posix()
                sha, size = _add_file(zf, path, arcname)
                file_entries.append({"path": arcname, "sha256": sha, "size": size})
            files_meta["count"] = len(file_entries)
            files_meta["bytes"] = sum(e["size"] for e in file_entries)
            files_meta["sha256"] = hashlib.sha256(
                json.dumps(
                    [[e["path"], e["sha256"]] for e in file_entries], sort_keys=True
                ).encode("utf-8")
            ).hexdigest()

        extra_entries: list[dict] = []
        for root in extra_roots:
            root_resolved = root if root.is_absolute() else (Path.cwd() / root)
            root_resolved = root_resolved.resolve()
            if not root_resolved.exists():
                log(f"[backup] [WARN] --include 路径不存在，已跳过：{root_resolved}")
                continue
            for path in iter_tree(root_resolved):
                arcname = (
                    f"data/extra/{root_resolved.name}/"
                    + path.relative_to(root_resolved).as_posix()
                )
                sha, size = _add_file(zf, path, arcname)
                extra_entries.append({"path": arcname, "sha256": sha, "size": size})

        manifest = {
            "format": MANIFEST_FORMAT,
            "mode": "quick" if quick else "full",
            "created_at": iso(created),
            "generator": "scripts/backup.py",
            "app": {
                "name": "Skill Router",
                "version": os.environ.get("SKILLROUTER_APP_VERSION", "0.1.0"),
                "engine_pin": db_info.get("engine_pin")
                or os.environ.get("SKILLROUTER_ENGINE_PIN"),
            },
            "schema_version": db_info.get("schema_version"),
            "schema_version_source": db_info.get(
                "schema_version_source", "system_state"
            ),
            "migration_revision": db_info.get("migration_revision"),
            "master_key": {
                "fingerprint": fingerprint_key(key_material) if key_material else None,
                "algorithm": "sha256(key_material)[:16]",
                "source": key_source,
                "key_exported": False,
            },
            "db": {
                **db_entry,
                "tables": db_info.get("tables", []),
                "row_counts": db_info.get("row_counts", {}),
            },
            "files": files_meta,
            "extra": {
                "count": len(extra_entries),
                "bytes": sum(e["size"] for e in extra_entries),
            },
            "entries": [db_entry, *file_entries, *extra_entries],
            "entry_count": 1 + len(file_entries) + len(extra_entries),
            "notes": [
                "zip 内嵌 manifest.json 是权威清单；同名 .manifest.json 是副本。",
                "manifest 不含主密钥本体，只含 sha256(key_material)[:16] 指纹。",
                "恢复前必须用 scripts/restore.py 校验指纹与 schema。",
            ],
        }
        _add_stream(
            zf,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            "manifest.json",
        )

    sidecar = archive.with_suffix(archive.suffix + ".manifest.json")
    sidecar.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    archive_sha = sha256_file(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{archive_sha}  {archive.name}\n", encoding="utf-8"
    )

    summary = {
        "archive": str(archive),
        "sha256": archive_sha,
        "size": archive.stat().st_size,
        "manifest": manifest,
        "manifest_sidecar": str(sidecar),
        "master_key_source": key_source,
        "files_included": not quick,
    }
    return summary


class _tempdir:
    def __enter__(self) -> Path:
        import tempfile

        self._ctx = tempfile.TemporaryDirectory(prefix="skillrouter-backup-")
        return Path(self._ctx.__enter__())

    def __exit__(self, *exc: object) -> None:
        self._ctx.__exit__(*exc)  # type: ignore[arg-type]


def prune(out_dir: Path, keep: int) -> list[Path]:
    if keep <= 0:
        return []
    archives = sorted(out_dir.glob("skillrouter-*.zip"), reverse=True)
    removed: list[Path] = []
    for archive in archives[keep:]:
        for candidate in (
            archive,
            archive.with_suffix(archive.suffix + ".manifest.json"),
            archive.with_suffix(archive.suffix + ".sha256"),
        ):
            if candidate.exists():
                candidate.unlink()
                removed.append(candidate)
    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="Skill Router 备份（纯标准库：sqlite3 backup API + zipfile + manifest.json）",
    )
    parser.add_argument(
        "--data-dir", help="持久化根目录（默认 $SKILLROUTER_DATA_DIR 或 var）"
    )
    parser.add_argument("--db", help="SQLite 文件路径（默认按 settings.py 语义解析）")
    parser.add_argument("--out", help="产物目录（默认 <data-dir>/backup）")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="快速模式：只备份 DB + manifest，跳过 var/files（启动/升级前使用）",
    )
    parser.add_argument("--label", help="产物文件名后缀，例如 pre-upgrade")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="DIR",
        help="额外打包的目录（可重复），例如 --include var/aragon-home（§12.4 切换前备份）",
    )
    parser.add_argument(
        "--keep", type=int, default=0, help="只保留最近 N 份备份（0 = 不清理）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    db_path = resolve_db_path(data_dir, args.db)
    out_dir = (
        Path(args.out).expanduser() if args.out else data_dir / DEFAULT_BACKUP_DIRNAME
    )
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()

    log("[backup] Skill Router 备份 —— 纯标准库实现（无 sqlite3 CLI / tar 依赖）")
    log(f"[backup] 数据目录：{data_dir}")
    log(f"[backup] 数据库　：{db_path}")
    log(f"[backup] 输出目录：{out_dir}")
    log(
        f"[backup] 模式　　：{'quick（仅 DB）' if args.quick else 'full（DB + files）'}"
    )

    try:
        summary = create_backup(
            data_dir=data_dir,
            db_path=db_path,
            out_dir=out_dir,
            quick=args.quick,
            label=args.label,
            includes=args.include,
        )
    except FileNotFoundError as exc:
        log(f"[backup] [FATAL] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 边界，任何异常都要可读
        log(f"[backup] [FATAL] 备份失败：{type(exc).__name__}: {exc}")
        return 1

    manifest = summary["manifest"]
    db_meta = manifest["db"]
    files_meta = manifest["files"]

    log("")
    log(f"[backup] db            : {db_meta['path']} ({human_bytes(db_meta['size'])})")
    log(f"[backup] db.sha256     : {db_meta['sha256']}")
    log(
        f"[backup] tables        : {len(manifest['db']['tables'])} 张表，"
        f"行数合计 {sum(v for v in manifest['db']['row_counts'].values() if v > 0)}"
    )
    log(f"[backup] schema_version: {_or_dash(manifest['schema_version'])}")
    log(f"[backup] migration     : {_or_dash(manifest['migration_revision'])}")
    log(f"[backup] engine_pin    : {_or_dash(manifest['app']['engine_pin'])}")
    log(
        "[backup] master-key fp : "
        f"{manifest['master_key']['fingerprint']}（来源 {manifest['master_key']['source']}，密钥本体未导出）"
    )
    if files_meta["included"]:
        log(
            f"[backup] files         : {files_meta['count']} 个文件 / "
            f"{human_bytes(files_meta['bytes'])}（清单 sha256 {files_meta['sha256'][:16]}…）"
        )
    else:
        log("[backup] files         : 已跳过（quick 模式不打包 var/files）")
    log(f"[backup] 产物          : {summary['archive']}")
    log(f"[backup] 产物 sha256   : {summary['sha256']}")

    removed = prune(out_dir, args.keep)
    if removed:
        log(f"[backup] 清理旧备份    : {len(removed)} 个文件（--keep {args.keep}）")

    if manifest["master_key"]["fingerprint"] is None:
        log(
            "[backup] [WARN] 未找到主密钥（SKILLROUTER_MASTER_KEY 或 "
            f"{data_dir / 'credentials-master.key'}）；该备份无法通过 restore.py 的指纹校验。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
