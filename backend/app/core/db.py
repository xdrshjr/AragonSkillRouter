"""SQLite access layer.

Design notes (see docs/ARCHITECTURE.md):
  * One short-lived connection per unit of work. FastAPI runs our `def`
    endpoints in a thread pool, so a connection is never shared across threads.
  * WAL + `busy_timeout` keep concurrent readers from blocking the writer and
    bound how long a writer waits for the lock.
  * Writes take `BEGIN IMMEDIATE` explicitly so two workers cannot both think
    they own a row during a select-then-update.
  * All SQL lives here or in a repository module — the rest of the app never
    touches `sqlite3`, which keeps a Postgres/other backend portable later.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.core.config import get_settings
from app.core.paths import migrations_dir

MIGRATIONS_DIR = migrations_dir()

_init_lock = threading.Lock()
_initialised = False

# One connection per (thread, database path). Opening a SQLite connection and
# setting six pragmas costs far more than a simple query, and FastAPI's thread
# pool reuses threads, so caching here removes that cost from every request
# without ever sharing a connection across threads.
_local = threading.local()


def _thread_connections() -> dict[str, sqlite3.Connection]:
    store = getattr(_local, "connections", None)
    if store is None:
        store = {}
        _local.connections = store
    return store


_all_connections: list[sqlite3.Connection] = []
_all_lock = threading.Lock()


def close_all_connections() -> None:
    """Close every pooled connection (settings reload, tests, shutdown)."""
    with _all_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        _all_connections.clear()
    _local.connections = {}


def close_thread_connections() -> None:
    """Close connections owned by the current thread (tests, shutdown)."""
    store = getattr(_local, "connections", None) or {}
    for conn in store.values():
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing twice is not an error we care about
            pass
    _local.connections = {}


# --------------------------------------------------------------------- time --
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def minutes_from_now(minutes: int) -> str:
    return iso(utcnow() + timedelta(minutes=minutes))


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}_{raw}" if prefix else raw


# ------------------------------------------------------------------ connect --
def _connect(path: Path | None = None, *, fresh: bool = False) -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(path or settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(db_path.resolve())
    store = _thread_connections()
    if not fresh and key in store:
        return store[key]
    conn = sqlite3.connect(
        db_path,
        timeout=10.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # journal_mode is persistent for a file database: it is set once by
    # `apply_migrations`, never per connection. Re-applying it on every write
    # transaction was the dominant cost of the async submit path.
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()
    if not fresh:
        store[key] = conn
        with _all_lock:
            _all_connections.append(conn)
    return conn


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A thread-owned connection; do not close it (cursors are short-lived)."""
    yield _connect(path)


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Write transaction on its own connection, so a reader cursor in the same
    thread cannot be disturbed by BEGIN IMMEDIATE. Commits on success, rolls
    back on any exception, and always closes the dedicated connection."""
    conn = _connect(path, fresh=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


# --------------------------------------------------------------- helpers ----
def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def query_all(sql: str, params: Sequence[Any] = (), *, path: Path | None = None) -> list[dict[str, Any]]:
    with connection(path) as conn:
        cur = conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Sequence[Any] = (), *, path: Path | None = None) -> dict[str, Any] | None:
    with connection(path) as conn:
        cur = conn.execute(sql, tuple(params))
        return _row_to_dict(cur.fetchone())


def query_scalar(sql: str, params: Sequence[Any] = (), *, path: Path | None = None) -> Any:
    with connection(path) as conn:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return row[0] if row else None


def execute(sql: str, params: Sequence[Any] = (), *, path: Path | None = None) -> int:
    with transaction(path) as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.rowcount


def execute_many(statements: Iterable[tuple[str, Sequence[Any]]], *, path: Path | None = None) -> None:
    with transaction(path) as conn:
        for sql, params in statements:
            conn.execute(sql, tuple(params))


def insert(table: str, values: dict[str, Any], *, path: Path | None = None) -> None:
    cols = ", ".join(values.keys())
    marks = ", ".join("?" for _ in values)
    execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()), path=path)


def update(table: str, row_id: str, values: dict[str, Any], *, id_col: str = "id", path: Path | None = None) -> int:
    if not values:
        return 0
    assignments = ", ".join(f"{k} = ?" for k in values)
    params = list(values.values()) + [row_id]
    return execute(f"UPDATE {table} SET {assignments} WHERE {id_col} = ?", params, path=path)


# ------------------------------------------------------------------- JSON ----
def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


# -------------------------------------------------------------- migrations --
def _split_statements(sql: str) -> list[str]:
    """Split a migration file on top-level semicolons (no triggers used)."""
    return [stmt.strip() for stmt in re.split(r";\s*(?:\r?\n|$)", sql) if stmt.strip()]


def apply_migrations(db_path: Path | None = None) -> list[str]:
    """Apply pending .sql migrations in filename order. Returns applied names."""
    settings = get_settings()
    path = Path(db_path or settings.db_path)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied: list[str] = []
    with connection(path) as conn:
        # WAL is a persistent property of the database file, so it belongs here
        # (once) rather than on every connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=512")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for file in files:
        match = re.match(r"(\d+)", file.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in done:
            continue
        sql = file.read_text(encoding="utf-8")
        statements = _split_statements(sql)
        with transaction(path) as conn:
            for stmt in statements:
                if not stmt:
                    continue
                if stmt.upper().startswith("PRAGMA"):
                    conn.execute(stmt)
                    continue
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, file.name, iso()),
            )
        applied.append(file.name)
    return applied


def init_db(db_path: Path | None = None) -> None:
    """Idempotent initialisation: migrate, then seed default settings."""
    global _initialised
    with _init_lock:
        path = Path(db_path or get_settings().db_path)
        apply_migrations(path)
        _seed_settings(path)
        _initialised = True


DEFAULT_SETTINGS: dict[str, Any] = {
    "security.session_ttl_minutes": 480,
    "security.idle_timeout_minutes": 120,
    "security.login_max_attempts": 5,
    "logging.store_request_bodies": False,
    "logging.store_response_bodies": False,
    "files.retention_days": 30,
    "agent.upgrade_channel": "stable",
    "agent.upgrade_policy": "manual",
    "agent.track_branch": "main",
    "agent.auto_execute": {
        "enabled": False,
        "allowed_providers": [],
        "max_calls": 10,
        "max_minutes": 30,
        "max_cost_usd": 2.0,
    },
    "onboarding.steps": {
        "password_changed": False,
        "agent_model_configured": False,
        "first_capability_published": False,
        "first_agent_token_created": False,
    },
}


def _seed_settings(path: Path) -> None:
    now = iso()
    with transaction(path) as conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, json.dumps(value), now),
            )


def get_setting(key: str, default: Any = None, *, path: Path | None = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,), path=path)
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key: str, value: Any, *, path: Path | None = None) -> None:
    execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), iso()),
        path=path,
    )


def merge_setting(key: str, patch: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    current = get_setting(key, {}, path=path)
    if not isinstance(current, dict):
        current = {}
    current.update(patch)
    set_setting(key, current, path=path)
    return current


def sql_placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def reset_db_for_tests(db_path: Path) -> None:
    """Drop and recreate everything (tests only)."""
    with connection(db_path) as conn:
        conn.execute("PRAGMA writable_schema=ON")
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        for table in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute("PRAGMA writable_schema=OFF")
