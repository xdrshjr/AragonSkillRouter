"""初始化脚本（§7.1 / §14.1）：建库 → 迁移 → 种子 → 首次管理员 → 打印摘要。

用法：
    python scripts/bootstrap.py
    python scripts/bootstrap.py reset-admin-password --user admin
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from skillrouter_api.errors import ApiError  # noqa: E402
from skillrouter_api.services import bootstrap as bootstrap_service  # noqa: E402
from skillrouter_api.settings import settings  # noqa: E402


def _print(summary: dict) -> None:
    print("─" * 68)
    print(" Skill Router 初始化摘要")
    print("─" * 68)
    for key, value in summary.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        print(f" {key:<26} {value}")
    print("─" * 68)
    admin = summary.get("admin") or {}
    if admin.get("created"):
        print(f" 初始账号：{admin.get('username')} / {settings.admin_password}")
        print(" 首次登录必须修改密码（默认密码仅此时生效一次）。")
    elif admin.get("reason") == "already_initialized":
        print(" 管理员已初始化，本次未创建/重置任何账号（重启与升级不会复活默认密码）。")
    print(" 启动：uvicorn skillrouter_api.main:app --port 8000  (cwd=apps/api)")
    print(" 前端：http://localhost:3000/login")


def cmd_init() -> int:
    try:
        summary = bootstrap_service.bootstrap(reason="cli")
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] BOOTSTRAP_FAILED {exc}", file=sys.stderr)
        return 2
    _print(summary)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    password = args.password
    if not password:
        password = getpass.getpass("新密码（≥12 字符，含字母与数字）：")
        confirm = getpass.getpass("再次输入：")
        if password != confirm:
            print("[FATAL] PASSWORD_MISMATCH 两次输入不一致", file=sys.stderr)
            return 2
    try:
        result = bootstrap_service.reset_admin_password(args.user, password)
    except ApiError as exc:
        print(f"[FATAL] {exc.code} {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("下次登录必须修改该密码。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Skill Router 初始化与运维脚本")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="建库、迁移、种子、首次管理员（默认命令）")
    reset = sub.add_parser("reset-admin-password", help="重置管理员密码（需停机或确认无写入）")
    reset.add_argument("--user", default=settings.admin_user)
    reset.add_argument("--password", default=None)
    args = parser.parse_args()
    if args.command == "reset-admin-password":
        return cmd_reset(args)
    return cmd_init()


if __name__ == "__main__":
    sys.exit(main())
