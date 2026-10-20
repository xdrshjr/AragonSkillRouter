"""Skill Router 桌面端 sidecar：bootstrap / api / worker 三种角色（spec §2.7）。

约束（改这个文件前先读 spec §2.7 与 §4.5）：

- 必须在 import skillrouter_api 之前注入环境（settings 在 import 时就固化数据目录与密钥）；
- 迁移目录布局要同时满足 bootstrap.MIGRATIONS_DIR 与 adopt_alembic.MIGRATIONS_DIR
  （两条推导路径不同，靠 PyInstaller 的 datas 双布局，不靠环境变量）；
- `--role api` 必须传 **ASGI 对象** 而不是 "module:attr" 字符串：PyInstaller 的静态分析不把
  字符串当导入，冻结后必 `ModuleNotFoundError`（P0-2）；
- 迁移不一致时**必须**非 0 退出（退出码 21 → 主进程映射 E-D03），与 SKILLROUTER_ENV 无关（P0-3）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

#: P0-3：桌面端专用退出码 —— 迁移不一致时**必须**非 0 退出。
EXIT_MIGRATION_MISMATCH = 21
#: 与 spec §4.5 的错误码对齐：主进程把这个退出码映射成 E-D03。
FATAL_MIGRATION = "E-D03 DESKTOP_BOOTSTRAP_FAILED"

VERSION = "0.1.0"


def app_root() -> Path:
    """冻结后是 `...\\resources\\python`；开发态是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _migration_problem() -> str | None:
    """迁移一致性硬校验：三条断言（user_version / alembic head / 当前 revision）。

    **为什么必须自己复核（P0-3）**：`bootstrap.assert_migrated()` 只在
    `settings.env == "production"` 时抛错，其余环境仅 `log.warning` 后返回 None。而桌面端
    按 D7 必须是 `development`，于是「库 schema 不对」时 bootstrap 仍然退出码 0 → api 照常
    起来 → 控制台带着错版库运行、坏数据无告警。这里的判定**与 env 无关，永远阻断**。

    不改 `apps/api` 一行代码（INV-1）。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from skillrouter_api.db.engine import schema_version
    from skillrouter_api.services import bootstrap as svc
    from skillrouter_api.settings import settings

    expected = svc.SCHEMA_VERSION  # 单一真相源：不在这里硬编码 2
    pragma = schema_version()
    if pragma != expected:
        return f"PRAGMA user_version={pragma}，期望 {expected}"

    cfg = Config(str(svc.MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(svc.MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        # 最常见的成因：PyInstaller 没把 migrations 铺进 _internal（见 spec §2.7 双布局）
        return "无法读取 alembic head（迁移目录缺失？检查 _internal\\migrations）"

    with sqlite3.connect(str(settings.db_file)) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = row[0] if row else None
    if revision != head:
        return f"alembic_version={revision}，期望 {head}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="skillrouter-server")
    ap.add_argument("--role", choices=("bootstrap", "api", "worker"))
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--json", action="store_true")  # bootstrap 输出机器可读摘要
    ap.add_argument("--version", action="store_true")  # P1-5：verify-artifact 需要它
    args = ap.parse_args()

    if args.version:
        # 不起服务、不建目录、不 require --role；退出码恒 0，供 scripts/verify-artifact.mjs 断言
        try:
            from skillrouter_api import __version__ as api_version
        except Exception:  # pragma: no cover - 打包残缺时仍要能打印
            api_version = "unknown"
        print(
            f"skillrouter-server {VERSION} "
            f"(skillrouter_api {api_version}, py{sys.version_info.major}.{sys.version_info.minor})",
            flush=True,
        )
        return 0

    if not args.role:
        ap.error("--role 必填（bootstrap / api / worker）")
        return 2

    # 数据目录优先级（P1-13）：--data-dir（命令行） > SKILLROUTER_DATA_DIR（环境） > 默认。
    # 不能用 setdefault()：那会让环境变量悄悄赢，--data-dir 变成静默无效参数。
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
        os.environ["SKILLROUTER_DATA_DIR"] = str(data_dir)
        os.environ["SKILLROUTER_DB_PATH"] = str(data_dir / "skillrouter.db")
    elif os.environ.get("SKILLROUTER_DATA_DIR"):
        data_dir = Path(os.environ["SKILLROUTER_DATA_DIR"]).resolve()
        os.environ.setdefault("SKILLROUTER_DB_PATH", str(data_dir / "skillrouter.db"))
    else:
        data_dir = (app_root() / "data").resolve()
        os.environ["SKILLROUTER_DATA_DIR"] = str(data_dir)
        os.environ["SKILLROUTER_DB_PATH"] = str(data_dir / "skillrouter.db")
    data_dir.mkdir(parents=True, exist_ok=True)

    # frozen：模块都在 PYZ 里，不用驻留 sys.path；开发态要让 skillrouter_api 可导入。
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(app_root() / "apps" / "api"))
    os.environ.setdefault("PYTHONUTF8", "1")
    # 注意：迁移目录**不走环境变量**。bootstrap.MIGRATIONS_DIR 与 adopt_alembic.MIGRATIONS_DIR
    # 都由 __file__ 推导，必须靠 PyInstaller 的 datas 同时铺两条路径（spec §2.7 表格）。

    if args.role == "bootstrap":
        from skillrouter_api.services import bootstrap as svc

        try:
            summary = svc.bootstrap(reason="desktop")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可判定的退出码
            import traceback

            traceback.print_exc()
            print(f"[FATAL] {FATAL_MIGRATION} bootstrap 抛异常：{exc}", file=sys.stderr, flush=True)
            return 1
        problem = _migration_problem()
        if problem is not None:
            print(f"[FATAL] {FATAL_MIGRATION} 迁移未收敛：{problem}", file=sys.stderr, flush=True)
            return EXIT_MIGRATION_MISMATCH
        print(json.dumps(summary, ensure_ascii=False) if args.json else summary, flush=True)
        return 0

    if args.role == "api":
        import uvicorn

        # P0-2：**直接传 ASGI 对象**，不传 "module:attr" 字符串。字符串形式在 PyInstaller
        # 冻结后要求该模块已在依赖图里，而静态分析看不到字符串 → ModuleNotFoundError。
        from skillrouter_api.main import app as asgi_app

        uvicorn.run(
            asgi_app,
            host="127.0.0.1",
            port=args.port,
            log_level="info",
            access_log=False,
            http="h11",
            ws="none",
            loop="asyncio",
            lifespan="on",
        )
        return 0

    import asyncio

    from skillrouter_api.worker.runner import main as worker_main  # main() 是无参协程

    try:
        asyncio.run(worker_main())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
