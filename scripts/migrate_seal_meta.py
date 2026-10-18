#!/usr/bin/env python3
"""`credentials.meta_sealed` 回填（spec §A.3.2-6 / §A.6.3 / F-03）。

它做什么
--------
扫描 `credentials.meta` 里仍含**敏感键**的行（判据复用
`services/credentials.py::SENSITIVE_META`，不另写第二套正则），把命中键的值
用 `secretbox.seal(record_type="credential_meta", record_id=<凭证 id>)` 密封进
`credentials.meta_sealed`，并从 `meta` 删掉这些键（不变式 I4：`meta` 不含敏感键）。

幂等：已回填过的行 `meta` 里没有敏感键，再跑一次只会报告 `pending=0`。

用法
----
    python scripts/migrate_seal_meta.py                          # 默认 dry-run，只报告
    python scripts/migrate_seal_meta.py --apply                  # 真正回填
    python scripts/migrate_seal_meta.py --db var/skillrouter.db  # 指定库（默认取 settings）
    python scripts/migrate_seal_meta.py --unseal --out var/meta-plaintext.json
                                                                # 导出明文，供回滚/核对

`--unseal` 只读不写库：它把每个 `meta_sealed` 解密成 JSON 导出。`0003` 的
`downgrade()` 在 `meta_sealed` 非空时会中止，**先导出再降级**（`docs/runbook/backup-restore.md`）。

退出码：0 成功；2 参数错误；3 数据库不存在/打不开；4 解密失败（主密钥不匹配）。
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from skillrouter_api.crypto import secretbox  # noqa: E402
from skillrouter_api.services.credentials import (  # noqa: E402
    SENSITIVE_META,
    migrate_meta,
)
from skillrouter_api.settings import settings  # noqa: E402

META_RECORD_TYPE = "credential_meta"
EXPORT_FORMAT = "skillrouter-credential-meta/1"


def _db_path(raw: str | None) -> Path:
    return Path(raw).resolve() if raw else settings.db_file


def _load_rows(path: Path) -> list[tuple[str, dict, str | None]]:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT id, meta, meta_sealed FROM credentials WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out: list[tuple[str, dict, str | None]] = []
    for credential_id, meta_raw, sealed in rows:
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except json.JSONDecodeError:
            meta = {}
        out.append((credential_id, meta if isinstance(meta, dict) else {}, sealed))
    return out


def _sensitive_keys(meta: dict) -> list[str]:
    return sorted(key for key in meta if SENSITIVE_META.search(str(key)))


def run_apply(path: Path, rows: list[tuple[str, dict, str | None]]) -> dict:
    sealed_now = 0
    failed: list[str] = []
    conn = sqlite3.connect(str(path))
    try:
        for credential_id, meta, existing in rows:
            if not _sensitive_keys(meta):
                continue
            try:
                public_meta, new_sealed = migrate_meta(credential_id, meta, existing)
            except Exception as exc:  # noqa: BLE001 - 单行失败不阻断整批
                failed.append(f"{credential_id}: {exc}")
                continue
            conn.execute(
                "UPDATE credentials SET meta = ?, meta_sealed = ? WHERE id = ?",
                (json.dumps(public_meta, ensure_ascii=False), new_sealed, credential_id),
            )
            sealed_now += 1
        conn.commit()
    finally:
        conn.close()
    return {"sealed": sealed_now, "failed": failed}


def run_unseal(path: Path, rows, out: Path) -> dict:
    payload = {
        "format": EXPORT_FORMAT,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "db_path": str(path),
        "credentials": {},
    }
    for credential_id, meta, sealed in rows:
        if not sealed:
            continue
        plain = secretbox.open_sealed(sealed, record_type=META_RECORD_TYPE, record_id=credential_id)
        try:
            sealed_meta = json.loads(plain)
        except json.JSONDecodeError:
            sealed_meta = {"_raw": plain}
        payload["credentials"][credential_id] = {"meta": meta, "sealed_meta": sealed_meta}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"exported": len(payload["credentials"]), "out": str(out)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回填 credentials.meta_sealed（幂等；默认 dry-run 只报告）",
    )
    parser.add_argument("--db", default=None, help="SQLite 文件路径（默认取 settings.db_path）")
    parser.add_argument("--apply", action="store_true", help="真正写库；不加即为 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    parser.add_argument("--unseal", action="store_true", help="导出已密封的 meta 明文（只读）")
    parser.add_argument("--out", default=None, help="--unseal 的导出文件（JSON）")
    args = parser.parse_args(argv)

    if args.unseal and not args.out:
        print("[ERROR] --unseal 需要 --out <file>（明文导出必须落盘到受控路径）")
        return 2
    if args.unseal and args.apply:
        print("[ERROR] --unseal 只读，不能与 --apply 同时使用")
        return 2

    path = _db_path(args.db)
    if not path.exists():
        print(f"[ERROR] 数据库不存在：{path}")
        return 3

    rows = _load_rows(path)
    pending = [row for row in rows if _sensitive_keys(row[1])]
    sealed_rows = [row for row in rows if row[2]]

    print(f"db            : {path}")
    print(f"scanned       : {len(rows)} 行")
    print(f"sealed_rows   : {len(sealed_rows)} 行已有 meta_sealed")
    print(f"pending       : {len(pending)} 行 meta 仍含敏感键")
    for credential_id, meta, _ in pending[:10]:
        print(f"  - {credential_id}: {', '.join(_sensitive_keys(meta))}")

    if args.unseal:
        try:
            result = run_unseal(path, rows, Path(args.out))
        except Exception as exc:  # noqa: BLE001 - 解密失败要给出可读原因
            print(f"[ERROR] 导出失败（主密钥不匹配？）：{exc}")
            return 4
        print(f"unsealed      : {result['exported']} 行 -> {result['out']}")
        print("提示：该文件含凭据明文，仅用于回滚核对，用完即删。")
        return 0

    if not args.apply:
        print("dry-run：未写库。加 --apply 执行回填。")
        return 0
    if not pending:
        print("已是最新：没有需要回填的行（幂等）。")
        return 0

    result = run_apply(path, pending)
    print(f"sealed        : {result['sealed']} 行")
    if result["failed"]:
        for line in result["failed"]:
            print(f"  [FAILED] {line}")
        return 4
    print("完成。可再次运行确认 pending=0。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, io.UnsupportedOperation):  # pragma: no cover - 老终端
            pass
    sys.exit(main())
