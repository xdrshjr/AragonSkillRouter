"""初始化与生命周期：迁移 → 种子 → 首次管理员 → 清理 → 目录（§7.1 / §4.4(a)）。

迁移单轨（§A.3.8 / F-11）：schema 的唯一来源是 Alembic。`create_all` 只在显式要求
（`SKILLROUTER_ENV=test` 或 `SKILLROUTER_CREATE_ALL=1`）时用于单测提速，其余环境一律
`alembic upgrade head`，存量库先由 `scripts/adopt_alembic.py` 校验指纹再认领到 0002。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import inspect, select

from .. import models as _models  # noqa: F401  确保全部表注册
from ..db import Base, db_session, db_write, get_engine, now_iso
from ..db.seed import get_state, seed_system_state, set_state
from ..errors import MIGRATION_NOT_ADOPTED, ApiError, Code
from ..logging import get_logger, redact
from ..models import AdminSession, AdminUser, SystemState
from ..security import passwords
from ..security.sessions import cleanup_expired
from ..settings import settings

log = get_logger("skillrouter.run")

SCHEMA_VERSION = 2

#: 显式启用 `create_all` 的开关（单测提速；生产绝不设置）
CREATE_ALL_ENV = "SKILLROUTER_CREATE_ALL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: Alembic 迁移目录（= apps/api/migrations；容器里是 /app/migrations）
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def create_schema() -> None:
    """`create_all` 建表 + 写 `PRAGMA user_version`（**仅测试/显式开关**用，向后兼容保留）。"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")


def _raw_env() -> str:
    """原始 `SKILLROUTER_ENV`（settings 的校验器不接受 `test`，所以先看环境变量本身）。"""
    return (os.environ.get("SKILLROUTER_ENV") or "").strip().lower()


def use_create_all() -> bool:
    """是否走 `create_all` 快路径：`SKILLROUTER_ENV=test` 或显式开关（§A.3.8-2）。"""
    if _raw_env() == "test" or settings.env == "test":
        return True
    return (os.environ.get(CREATE_ALL_ENV) or "").strip().lower() in _TRUTHY


def db_state() -> tuple[bool, bool]:
    """`(是否有业务表, 是否有 alembic_version)`；只读打开，不会凭空建库。"""
    path = settings.db_file
    if not path.is_file():
        return False, False
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    business = {
        name for name in names if not name.startswith("sqlite_") and name != "alembic_version"
    }
    return bool(business), "alembic_version" in names


def _adopt_module():
    """加载 `scripts/adopt_alembic.py`（仓库布局 `<root>/scripts`、容器布局 `/app/scripts`）。"""
    import importlib.util

    here = Path(__file__).resolve()
    candidates = [here.parents[2] / "scripts" / "adopt_alembic.py"]  # 容器：/app/scripts
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / "scripts" / "adopt_alembic.py")  # 仓库根 scripts/
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("skillrouter_adopt_alembic", candidate)
            if spec is None or spec.loader is None:  # pragma: no cover - 理论不可达
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError(
        "[FATAL] ADOPT_SCRIPT_MISSING 找不到 scripts/adopt_alembic.py；"
        f"已尝试：{[str(path) for path in candidates]}"
    )


def _alembic_run(action: str, revision: str) -> None:
    """在进程内跑 alembic 命令（`upgrade` / `stamp`），目标库固定为 settings.db_file。"""
    from alembic import command

    config = _adopt_module().alembic_config(settings.db_file)
    getattr(command, action)(config, revision)


def migrate_or_adopt() -> dict:
    """迁移单轨入口（§A.3.8-2）：测试快路径 / 空库 / 存量库 / 已接入，四选一。"""
    settings.ensure_dirs()
    if use_create_all():
        create_schema()
        summary = {
            "mode": "explicit_create_all",
            "db": str(settings.db_file),
            "revision": _alembic_revision(),
            "user_version": SCHEMA_VERSION,
        }
        log.info("schema_ready", extra={"detail": summary})
        return summary

    has_tables, has_version = db_state()
    if not has_tables:
        # a) 空库：交给 Alembic 从零建到 head
        _alembic_run("upgrade", "head")
        mode = "empty_db_upgrade"
    elif not has_version:
        # b) 存量库（有业务表、无 alembic_version）：先校验指纹，一致才认领到 0002
        adopt = _adopt_module()
        report = adopt.inspect_db(settings.db_file)
        log.info(
            "alembic_adopt_check",
            extra={"detail": redact(report.to_dict())},
        )
        if not report.ok:
            log.error("alembic_adopt_refused", extra={"detail": redact(report.to_dict())})
            raise RuntimeError(
                f"[FATAL] {MIGRATION_NOT_ADOPTED} 存量库指纹与当前模型元数据不一致，"
                "拒绝 stamp（绝不强制）；核对后手工执行 python scripts/adopt_alembic.py\n"
                + report.render()
            )
        _alembic_run("stamp", adopt.STAMP_REVISION)
        _alembic_run("upgrade", "head")
        mode = "adopted_then_upgrade"
    else:
        # c) 已接入：幂等 upgrade（已是最新时不做事）
        _alembic_run("upgrade", "head")
        mode = "upgrade"

    summary = {
        "mode": mode,
        "db": str(settings.db_file),
        "revision": _alembic_revision(),
        "user_version": SCHEMA_VERSION,
    }
    log.info("schema_ready", extra={"detail": summary})
    return summary


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
    """幂等启动流程：失败即抛错，由调用方打印 [FATAL] 并退出。

    步骤顺序即契约（§A.3.8-1）：
    `ensure_dirs → master_key → file_signing_key → migrate_or_adopt → assert_migrated
    → create_triggers → seed_system_state → initialize_admin → cleanup_expired`。
    迁移必须在种子/管理员之前，否则首次启动会在没有表的库上写种子。
    """
    settings.ensure_dirs()
    from ..crypto.masterkey import file_signing_key, fingerprint, master_key

    # 主密钥与文件签名密钥必须在此步可用（无「先启动后补密钥」的降级路径）
    master_key()
    file_signing_key()
    migration = migrate_or_adopt()
    assert_migrated()
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
        "migration": migration,
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
    """迁移单轨的收尾校验（§A.3.8-4）：**必须**在 `migrate_or_adopt()` 之后被调用。

    三项都要满足：`alembic_version` 存在、等于 head、`PRAGMA user_version == SCHEMA_VERSION`。
    `production` 下任一不满足 → `RuntimeError`（带启动期常量 `MIGRATION_NOT_ADOPTED`，
    调用方据此 `[FATAL]` + 退出码 1）；其他环境打印差异后继续（本地开发不拦死）。
    """
    from ..db.engine import schema_version

    problems: list[dict] = []
    pragma = schema_version()
    if pragma != SCHEMA_VERSION:
        problems.append({"check": "user_version", "actual": pragma, "expected": SCHEMA_VERSION})
    head = _alembic_head()
    revision = _alembic_revision()
    if head is None:
        problems.append({"check": "alembic_head", "actual": None, "expected": "可读取的 head"})
    elif revision is None:
        problems.append({"check": "alembic_version", "actual": None, "expected": head})
    elif revision != head:
        problems.append({"check": "alembic_version", "actual": revision, "expected": head})

    if not problems:
        log.info(
            "migration_ok",
            extra={"detail": {"alembic": revision, "user_version": pragma, "env": settings.env}},
        )
        return

    detail = {
        "env": settings.env,
        "db": str(settings.db_file),
        "problems": problems,
        "hint": "运行 python scripts/bootstrap.py 或 "
        "python -m alembic -c migrations/alembic.ini upgrade head",
    }
    if settings.env == "production":
        log.error("migration_not_adopted", extra={"detail": redact(detail)})
        joined = "; ".join(
            f"{item['check']}: 实际 {item['actual']} / 期望 {item['expected']}" for item in problems
        )
        raise RuntimeError(f"[FATAL] {MIGRATION_NOT_ADOPTED} {joined}；{detail['hint']}")
    log.warning("migration_not_adopted", extra={"detail": redact(detail)})


def _alembic_config():
    """Alembic Config：`script_location` 用绝对路径，因此不依赖当前工作目录。"""
    from alembic.config import Config

    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def _alembic_head() -> str | None:
    """读取迁移 head；alembic 或迁移目录不可用时返回 None 并告警（不静默降级）。"""
    try:
        from alembic.script import ScriptDirectory
        from alembic.util import CommandError
    except ImportError as exc:  # pragma: no cover - alembic 未安装
        log.warning("alembic_unavailable", extra={"detail": redact(str(exc))})
        return None
    try:
        return ScriptDirectory.from_config(_alembic_config()).get_current_head()
    except (OSError, KeyError, ValueError, CommandError) as exc:
        log.warning("alembic_head_unavailable", extra={"detail": redact(str(exc))})
        return None


def _alembic_revision() -> str | None:
    """读库里的 alembic revision；库文件不存在或表缺失 → None（只读打开，不新建库）。"""
    path = settings.db_file
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
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
    "migrate_or_adopt",
    "assert_migrated",
    "use_create_all",
    "db_state",
    "initialize_admin",
    "reset_admin_password",
    "SCHEMA_VERSION",
    "SystemState",
]
