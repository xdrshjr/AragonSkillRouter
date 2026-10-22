"""Operator CLI: init, password recovery, key rotation, backup/restore, doctor.

Run from the backend directory:

    python -m app.cli init
    python -m app.cli reset-admin-password --generate
    python -m app.cli rotate-key
    python -m app.cli backup --out ..\\data\\backups
    python -m app.cli restore ..\\data\\backups\\skillrouter-....db
    python -m app.cli doctor
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from app.core import db
from app.core.config import get_settings
from app.security import crypto, passwords, sessions
from app.services import bootstrap, credentials


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> int:
    bootstrap.ensure_bootstrap()
    status = bootstrap.get_status()
    _print(
        {
            "initialized": status.initialized,
            "admin": status.admin_username,
            "must_change_password": status.must_change_password,
            "database": str(get_settings().db_path),
            "keyring": "environment" if get_settings().master_keys else str(get_settings().keyring_path),
            "next": "Sign in and change the initial password, then configure a provider credential.",
        }
    )
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    db.init_db()
    row = db.query_one("SELECT id, username FROM users ORDER BY created_at LIMIT 1")
    if not row:
        print("No user exists yet. Run `python -m app.cli init` first.", file=sys.stderr)
        return 2
    password = args.password
    if not password:
        if not args.generate:
            print("Pass --password <new> or --generate.", file=sys.stderr)
            return 2
        password = passwords.generate_password()
    try:
        passwords.check_password_policy(password, username=row["username"])
    except passwords.PasswordPolicyError as exc:
        print(f"Password rejected: {exc}", file=sys.stderr)
        return 2
    now = db.iso()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1, password_changed_at = ?, updated_at = ? WHERE id = ?",
        (passwords.hash_password(password), now, now, row["id"]),
    )
    revoked = sessions.delete_user_sessions(row["id"])
    print(f"Password for {row['username']!r} reset. {revoked} session(s) revoked. Change it again at next sign-in.")
    print(f"Temporary password: {password}")
    return 0


def cmd_rotate_key(args: argparse.Namespace) -> int:
    db.init_db()
    ring = crypto.load_keyring()
    if get_settings().master_keys:
        print(
            "This deployment supplies keys through SR_MASTER_KEYS; add the new key there, "
            "set SR_MASTER_KEY_ACTIVE, restart, then run this command again.",
            file=sys.stderr,
        )
        return 2
    existing_ids = set(ring.keys)
    index = 1
    while f"k{index}" in existing_ids:
        index += 1
    new_id = f"k{index}"
    new_ring = crypto.Keyring(active=new_id, keys={**ring.keys, new_id: crypto._generate_key()})  # noqa: SLF001
    crypto.write_keyring(new_ring)
    result = credentials.rotate_master_key(actor_id="cli")
    _print({"new_active_key": new_id, **result, "note": "Old keys stay in the keyring so rollback keeps working."})
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    settings = get_settings()
    db.init_db()
    out_dir = Path(args.out or settings.backups_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = db.utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"skillrouter-{stamp}.db"
    source = sqlite3.connect(settings.db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    manifest = {
        "created_at": db.iso(),
        "database": target.name,
        "schema_migrations": [r["name"] for r in db.query_all("SELECT name FROM schema_migrations ORDER BY version")],
        "keyring_in_env": bool(settings.master_keys),
        "keyring_file": None if settings.master_keys else str(settings.keyring_path),
        "note": "Secrets in this database are unreadable without the matching master keyring. Back it up separately.",
    }
    (target.with_suffix(".manifest.json")).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _print({"backup": str(target), "manifest": str(target.with_suffix(".manifest.json")), "size_bytes": target.stat().st_size})
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    source_path = Path(args.backup)
    if not source_path.exists():
        print(f"Backup not found: {source_path}", file=sys.stderr)
        return 2
    check = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as exc:
        # Defect fixed: a file that is not a SQLite database raised out of the CLI
        # as a traceback (exit 1) instead of the clean refusal this command is
        # documented to give. Refuse, explain, change nothing.
        print(f"Backup is not a readable SQLite database: {exc}", file=sys.stderr)
        return 2
    finally:
        check.close()
    if integrity != "ok":
        print(f"Backup failed the integrity check: {integrity}", file=sys.stderr)
        return 2
    if "schema_migrations" not in tables or "users" not in tables:
        print("Backup does not look like a Skill Router database.", file=sys.stderr)
        return 2
    if not args.force:
        print(
            "Stop the API and worker before restoring. Re-run with --force once they are stopped.",
            file=sys.stderr,
        )
        return 2
    target = settings.db_path
    # Defect fixed: restoring onto a fresh volume failed with
    # "unable to open database file" because the parent directory was never
    # created. The restore target's own directory is ours to create.
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        safety = target.with_suffix(f".pre-restore-{db.utcnow().strftime('%Y%m%dT%H%M%SZ')}.db")
        target.replace(safety)
        print(f"Previous database kept at {safety}")
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    db.apply_migrations(settings.db_path)
    _print({"restored": str(target), "source": str(source_path), "migrations_applied": True})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("data_dir", settings.data_dir.exists(), str(settings.data_dir))
    try:
        db.init_db()
        add("database", True, f"{settings.db_path} (WAL: {db.query_scalar('PRAGMA journal_mode')})")
    except Exception as exc:  # noqa: BLE001
        add("database", False, str(exc))
    try:
        ring = crypto.load_keyring()
        add("master_keyring", True, f"active={ring.active}, keys={sorted(ring.keys)}")
    except Exception as exc:  # noqa: BLE001
        add("master_keyring", False, str(exc))
    status = bootstrap.get_status()
    add("bootstrap", status.initialized, f"admin={status.admin_username}")
    add("initial_password_changed", not status.must_change_password, "first sign-in must change the password" if status.must_change_password else "ok")
    pending = db.query_scalar("SELECT COUNT(*) FROM jobs WHERE status = 'queued'") or 0
    add("queue", True, f"{pending} queued job(s)")
    undecryptable = 0
    for row in db.query_all("SELECT id, secret_ciphertext FROM credentials"):
        try:
            crypto.decrypt_map(row["secret_ciphertext"])
        except crypto.CryptoError:
            undecryptable += 1
    add("credentials_decryptable", undecryptable == 0, f"{undecryptable} credential(s) cannot be decrypted")
    if settings.is_production:
        add("secure_cookies", settings.secure_cookies, "SR_COOKIE_SECURE must be true in production")
    _print({"ok": all(bool(c["ok"]) for c in checks), "checks": checks})
    return 0 if all(bool(c["ok"]) for c in checks) else 1


def cmd_purge(args: argparse.Namespace) -> int:
    db.init_db()
    from app.services import files

    expired_sessions = sessions.purge_expired_sessions()
    expired_files = files.cleanup_expired()
    _print({"sessions_removed": expired_sessions, **expired_files})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skillrouter", description="Skill Router operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="create the database, keyring and admin account")
    init_p.set_defaults(func=cmd_init)

    reset_p = sub.add_parser("reset-admin-password", help="reset the admin password (recovery path)")
    reset_p.add_argument("--password")
    reset_p.add_argument("--generate", action="store_true")
    reset_p.set_defaults(func=cmd_reset_password)

    rotate_p = sub.add_parser("rotate-key", help="add a new master key and re-wrap stored credentials")
    rotate_p.set_defaults(func=cmd_rotate_key)

    backup_p = sub.add_parser("backup", help="consistent online SQLite backup + manifest")
    backup_p.add_argument("--out")
    backup_p.set_defaults(func=cmd_backup)

    restore_p = sub.add_parser("restore", help="restore a backup (requires --force with services stopped)")
    restore_p.add_argument("backup")
    restore_p.add_argument("--force", action="store_true")
    restore_p.set_defaults(func=cmd_restore)

    doctor_p = sub.add_parser("doctor", help="check configuration, keys and credentials")
    doctor_p.set_defaults(func=cmd_doctor)

    purge_p = sub.add_parser("purge", help="remove expired sessions and files")
    purge_p.set_defaults(func=cmd_purge)

    args = parser.parse_args(argv)
    os.environ.setdefault("SR_LOG_LEVEL", "info")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
