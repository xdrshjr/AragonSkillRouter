"""备份作业（§7.4 / §10.3）：纯 Python，无 sqlite3/tar 依赖。"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime

from ..crypto.masterkey import fingerprint
from ..db.engine import schema_version
from ..logging import get_logger, redact
from ..services import audit, files
from ..settings import settings

log = get_logger("skillrouter.run")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_backup(*, quick: bool = False) -> dict:
    settings.ensure_dirs()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    db_snapshot = settings.backup_dir / f"db-{stamp}.sqlite"

    source = sqlite3.connect(str(settings.db_file))
    try:
        target = sqlite3.connect(str(db_snapshot))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    entries: list[dict] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_snapshot, arcname="db/skillrouter.sqlite")
        entries.append({"path": "db/skillrouter.sqlite", "size": db_snapshot.stat().st_size})
        if not quick and settings.files_dir.exists():
            for path in sorted(settings.files_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(settings.data_path).as_posix()
                zf.write(path, arcname=f"data/{relative}")
                entries.append({"path": f"data/{relative}", "size": path.stat().st_size})
    payload = buffer.getvalue()
    manifest = {
        "schema_version": schema_version(),
        "migration_revision": _migration_revision(),
        "engine_pin": _engine_pin(),
        "master_key_fingerprint": fingerprint(),
        "created_at": _now(),
        "app_version": "0.1.0",
        "quick": quick,
        "entries": entries[:5000],
        "entry_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    archive = settings.backup_dir / f"backup-{stamp}.zip"
    archive.write_bytes(payload)
    (settings.backup_dir / f"manifest-{stamp}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        db_snapshot.unlink()
    except OSError:
        pass
    saved = files.save_bytes(
        payload,
        mime="application/zip",
        filename=archive.name,
        kind="artifact",
        owner_type="backup",
        owner_id=manifest["sha256"][:16],
    )
    return {"file": str(archive), "manifest": manifest, "file_id": saved["file_id"], "sha256": manifest["sha256"]}


def quick_backup(*, label: str = "pre-upgrade") -> dict:
    """升级/回滚前的快速备份（只导 DB，不带 files/）。

    返回 `{file_id, sha256, file, label, created_at}`；失败直接抛异常，
    由调用方（`upgrade/switch.py`）转成「不切换」——**先备份后切换**是硬顺序。
    """
    result = build_backup(quick=True)
    return {
        "file_id": result.get("file_id"),
        "sha256": result.get("sha256"),
        "file": result.get("file"),
        "label": label,
        "created_at": (result.get("manifest") or {}).get("created_at"),
    }


def _migration_revision() -> str | None:
    try:
        connection = sqlite3.connect(str(settings.db_file))
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _engine_pin() -> str:
    from ..db import db_session
    from ..db.seed import get_state

    with db_session() as s:
        return get_state(s, "engine_pin", settings.engine_pin)


async def handle(payload: dict) -> None:
    quick = bool(payload.get("quick"))
    result = build_backup(quick=quick)
    audit.write(
        "backup.created",
        object_type="backup",
        object_id=result["file_id"],
        summary={"sha256": result["sha256"], "quick": quick},
    )
    log.info("backup_created", extra={"detail": {"path": result["file"], "quick": quick}})
    return redact(result)
