"""初始化与生命周期：迁移 → 种子 → 首次管理员 → 清理 → 目录（§7.1 / §4.4(a)）。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, select

from ..db import Base, db_session, db_write, get_engine, now_iso
from ..db.seed import get_state, seed_system_state, set_state
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import AdminSession, AdminUser, SystemState
from ..security import passwords
from ..security.sessions import cleanup_expired
from ..settings import settings
from .. import models as _models  # noqa: F401  确保全部表注册

log = get_logger("skillrouter.run")

SCHEMA_VERSION = 1


def create_schema() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")


def create_triggers() -> None:
    engine = get_engine()
    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS capability_versions_immutable
        BEFORE UPDATE ON capability_versions
        WHEN OLD.status = 'published' AND NEW.status = 'published'
             AND OLD.definition != NEW.definition
        BEGIN
            SELECT RAISE(ABORT, 'published_version_immutable');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS skill_versions_immutable
        BEFORE UPDATE ON skill_versions
        WHEN OLD.status = 'published' AND NEW.status = 'published'
             AND OLD.skill_md != NEW.skill_md
        BEGIN
            SELECT RAISE(ABORT, 'published_version_immutable');
        END;
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


def tables_ok() -> bool:
    return inspect(get_engine()).has_table("admin_users")


def initialize_admin() -> dict:
    """首次初始化管理员；已置位则绝不重建（需求硬约束）。"""
    with db_session() as s:
        initialized = get_state(s, "admin_initialized", "0") == "1"
        existing = s.query(AdminUser).first()
    if initialized and existing is not None:
        return {"created": False, "username": existing.username, "reason": "already_initialized"}
    with db_write() as s:
        # 双重检查（并发启动保护）
        if get_state(s, "admin_initialized", "0") == "1":
            return {"created": False, "reason": "already_initialized"}
        user = s.query(AdminUser).filter(AdminUser.username == settings.admin_user).one_or_none()
        if user is None:
            user = AdminUser(
                id="adm_default",
                username=settings.admin_user,
                password_hash=passwords.hash_password(settings.admin_password),
                must_change_password=1,
                created_at=now_iso(),
            )
            s.add(user)
        set_state(s, "admin_initialized", "1")
        set_state(s, "initialized_at", now_iso())
        s.flush()
        username = user.username
    log.info("admin_initialized", extra={"actor": username})
    return {
        "created": True,
        "username": username,
        "must_change_password": True,
        "note": "首次登录必须修改默认密码；该初始化不会在重启或升级后重复执行",
    }


def bootstrap(reason: str = "startup") -> dict:
    """幂等启动流程：失败即抛错，由调用方打印 [FATAL] 并退出。"""
    settings.ensure_dirs()
    from ..crypto.masterkey import fingerprint, master_key
    from ..crypto.masterkey import file_signing_key

    # 主密钥与文件签名密钥必须在此步可用（无「先启动后补密钥」的降级路径）
    master_key()
    file_signing_key()
    create_schema()
    create_triggers()
    with db_write() as s:
        seed_system_state(s)
    admin = initialize_admin()
    expired = cleanup_expired()
    from ..services import settings as settings_service
    from ..services.files import ensure_dirs

    settings_service.write_agent_policy()
    ensure_dirs()
    with db_session() as s:
        sessions = len(list(s.scalars(select(AdminSession))))
        initialized = get_state(s, "admin_initialized", "0")
        pin = get_state(s, "engine_pin", settings.engine_pin)
    summary = {
        "reason": reason,
        "version": SCHEMA_VERSION,
        "db": str(settings.db_file),
        "data_dir": str(settings.data_path),
        "admin": admin,
        "initialized": initialized == "1",
        "expired_sessions_revoked": expired,
        "session_rows": sessions,
        "engine_pin": pin,
        "adapter_version": "1",
        "master_key_fingerprint": fingerprint(),
        "env": settings.env,
        "public_base_url": settings.public_base_url,
    }
    log.info("bootstrap_done", extra={"detail": redact(summary)})
    return summary


def reset_admin_password(username: str, new_password: str) -> dict:
    issues = passwords.validate_strength(new_password)
    if issues:
        raise ApiError(Code.VALIDATION_FAILED, message="密码不符合要求", details={"fields": issues})
    with db_write() as s:
        user = s.query(AdminUser).filter(AdminUser.username == username).one_or_none()
        if user is None:
            raise ApiError(Code.NOT_FOUND, f"账号不存在：{username}")
        user.password_hash = passwords.hash_password(new_password)
        user.must_change_password = 1
        user.password_changed_at = now_iso()
    return {"ok": True, "username": username, "must_change_password": True}


def assert_migrated() -> None:
    """Alembic 版本与 PRAGMA user_version 双向校验（不一致即启动失败，§10.3）。"""
    from ..db.engine import schema_version

    actual = schema_version()
    if actual != SCHEMA_VERSION:
        log.error(
            "schema_version_mismatch",
            extra={"detail": {"pragma": actual, "expected": SCHEMA_VERSION}},
        )
        raise RuntimeError(
            f"[FATAL] SCHEMA_VERSION_MISMATCH user_version={actual} expected={SCHEMA_VERSION}"
        )
    alembic_rev = _alembic_revision()
    if alembic_rev is None:
        return
    head = _alembic_head()
    if not head:
        return
    if alembic_rev != head:
        raise RuntimeError(
            f"[FATAL] MIGRATION_OUT_OF_DATE alembic={alembic_rev} head={head}；请运行 alembic upgrade head"
        )


def _alembic_head() -> str | None:
    """读取迁移 head；alembic 或迁移目录不可用时返回 None 并告警（不静默降级）。"""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.util import CommandError
    except ImportError as exc:  # pragma: no cover - alembic 未安装
        log.warning("alembic_unavailable", extra={"detail": redact(str(exc))})
        return None
    try:
        config = Config(str(Path(__file__).resolve().parents[2] / "migrations" / "alembic.ini"))
        return ScriptDirectory.from_config(config).get_current_head()
    except (OSError, KeyError, ValueError, CommandError) as exc:
        log.warning("alembic_head_unavailable", extra={"detail": redact(str(exc))})
        return None


def _alembic_revision() -> str | None:
    import sqlite3

    try:
        connection = sqlite3.connect(str(settings.db_file))
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


__all__ = [
    "bootstrap",
    "create_schema",
    "create_triggers",
    "initialize_admin",
    "reset_admin_password",
    "assert_migrated",
    "SystemState",
]
