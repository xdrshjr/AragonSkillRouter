#!/usr/bin/env python3
"""主密钥轮换 CLI 包装（spec §7.4 keyring / §9.2 `POST /settings/rotate-master-key`）。

它做什么 / 不做什么
-------------------
* **做**：以管理员会话调用平台的轮换端点，打印任务编号，可选 `--wait` 轮询到结束。
* **不做**：自己在本地重加密数据库。轮换涉及 `master_key_rotations` 记录、分批事务、
  AAD 里的 `key_id` 重算、`SKILLROUTER_MASTER_KEY_PREVIOUS` 双读窗口 —— 这些必须由平台
  服务层完成，脚本绕过服务层只会制造一份「看起来加密了但打不开」的数据。
  因此 `--offline` 只打印步骤清单并以退出码 2 结束，不提供本地实现。

轮换的正确顺序（脚本会打印，`docs/runbook/init.md` 同步）
---------------------------------------------------------
1. 生成新密钥：`python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`
2. 把**旧**密钥写入 `SKILLROUTER_MASTER_KEY_PREVIOUS`，把**新**密钥写入 `SKILLROUTER_MASTER_KEY`，
   并把 `SKILLROUTER_MASTER_KEY_ID` 从 `mk1` 改成新标识（如 `mk2`）。重启 api/worker。
3. 运行本脚本触发全表重加密（幂等、可重试），等 `master_key_rotations.status=succeeded`。
4. 抽查一个凭证能解密、一个能力能调用后，再移除 `SKILLROUTER_MASTER_KEY_PREVIOUS`。

用法
----
    python scripts/rotate_master_key.py --base-url http://127.0.0.1:8000 ^
        --cookie-stdin --csrf <X-CSRF-Token> --wait

    set SKILLROUTER_SESSION_COOKIE=sr_session=...
    set SKILLROUTER_CSRF_TOKEN=...
    python scripts/rotate_master_key.py --base-url https://router.example.com --wait

退出码：0 成功；1 任务失败；2 参数错误/离线模式；3 连不上平台；4 认证失败（401/403）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROTATE_PATH = "/api/admin/settings/rotate-master-key"
TERMINAL = {"succeeded", "success", "failed", "canceled", "canceled_local"}
STEPS = (
    """
主密钥轮换步骤（§7.4 / §14.3）：
  1) 生成新密钥（32 字节 base64）：
     python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
  2) 更新环境变量并重启 api/worker：
     SKILLROUTER_MASTER_KEY=<新密钥>
     SKILLROUTER_MASTER_KEY_PREVIOUS=<旧密钥>     # 轮换期间双读
     SKILLROUTER_MASTER_KEY_ID=mk2                # 新标识，随密文保存
  3) 触发轮换（本脚本）：POST %s
  4) 等待 master_key_rotations.status=succeeded，并抽查：1 个凭证可解密 + 1 个能力可调用
  5) 确认无误后移除 SKILLROUTER_MASTER_KEY_PREVIOUS 并再次重启（双读窗口关闭）
"""
    % ROTATE_PATH
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


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def http_json(
    method: str,
    url: str,
    *,
    cookie: str | None,
    csrf: str | None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict | str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 用户指定基址
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, f"{type(exc.reason).__name__}: {exc.reason}"


def error_hint(payload: dict | str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        if isinstance(error, dict) and error.get("code"):
            return f"{error['code']} — {error.get('message', '')}（request_id={error.get('request_id')}）"
    return str(payload)[:300]


def read_cookie(args: argparse.Namespace) -> str | None:
    if args.cookie:
        value = args.cookie.strip()
        return value if "=" in value else f"sr_session={value}"
    if args.cookie_stdin:
        value = sys.stdin.readline().strip()
        if value:
            return value if "=" in value else f"sr_session={value}"
    env_value = (os.environ.get("SKILLROUTER_SESSION_COOKIE") or "").strip()
    if env_value:
        return env_value if "=" in env_value else f"sr_session={env_value}"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rotate_master_key.py",
        description="触发主密钥轮换（包装 POST /api/admin/settings/rotate-master-key）",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="平台基址")
    parser.add_argument(
        "--cookie",
        help="sr_session Cookie（推荐用 --cookie-stdin 或环境变量，避免留痕）",
    )
    parser.add_argument(
        "--cookie-stdin", action="store_true", help="从 stdin 读 Cookie"
    )
    parser.add_argument("--csrf", help="X-CSRF-Token（也可用 $SKILLROUTER_CSRF_TOKEN）")
    parser.add_argument(
        "--to-key-id", dest="to_key_id", help="新 key_id（默认由平台按 settings 决定）"
    )
    parser.add_argument("--wait", action="store_true", help="轮询任务直到终态")
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="--wait 的最长等待秒数（默认 900）"
    )
    parser.add_argument(
        "--interval", type=float, default=3.0, help="轮询间隔秒（默认 3）"
    )
    parser.add_argument(
        "--offline", action="store_true", help="不联网；只打印正确的操作顺序"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")

    if args.offline:
        log(STEPS)
        log(
            "[rotate] --offline：本脚本不实现本地重加密（会把数据写成「打不开的密文」），已退出。"
        )
        return 2

    cookie = read_cookie(args)
    csrf = (args.csrf or os.environ.get("SKILLROUTER_CSRF_TOKEN") or "").strip() or None

    log("[rotate] Skill Router 主密钥轮换（CLI 包装）")
    log(f"[rotate] 目标：{base_url}{ROTATE_PATH}")
    if not cookie:
        log(
            "[rotate] [FATAL] 缺少管理员会话 Cookie：用 --cookie / --cookie-stdin / $SKILLROUTER_SESSION_COOKIE"
        )
        return 2
    if not csrf:
        log(
            "[rotate] [FATAL] 缺少 CSRF：用 --csrf 或 $SKILLROUTER_CSRF_TOKEN（写接口必填，§7.3）"
        )
        return 2

    status, payload = http_json(
        "GET",
        f"{base_url}/api/admin/overview",
        cookie=cookie,
        csrf=None,
        timeout=20.0,
    )
    if status == 0:
        log(f"[rotate] [FATAL] 连不上平台：{payload}")
        return 3
    if status in (401, 403):
        log(f"[rotate] [FATAL] 会话无效或未完成改密（{status}）：{error_hint(payload)}")
        return 4
    if status >= 500:
        log(f"[rotate] [FATAL] 平台未就绪（{status}）：{error_hint(payload)}")
        return 1
    log(f"[rotate] 会话可用：GET /api/admin/overview → {status}")

    body: dict = {}
    if args.to_key_id:
        body["to_key_id"] = args.to_key_id
    log("[rotate] 触发全表重加密（后台任务，分批 50 行 + master_key_rotations 记录）…")
    status, payload = http_json(
        "POST",
        f"{base_url}{ROTATE_PATH}",
        cookie=cookie,
        csrf=csrf,
        body=body,
        timeout=60.0,
    )

    if status == 0:
        log(f"[rotate] [FATAL] 连不上平台：{payload}")
        return 3
    if status in (401, 403):
        log(f"[rotate] [FATAL] 认证失败（{status}）：{error_hint(payload)}")
        return 4
    if status >= 400:
        log(f"[rotate] [FATAL] 触发失败（{status}）：{error_hint(payload)}")
        return 1

    pretty = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        if isinstance(payload, dict)
        else str(payload)
    )
    log(f"[rotate] 已受理（{status}）：{pretty}")
    task_id = None
    if isinstance(payload, dict):
        task_id = (
            payload.get("task_id") or payload.get("rotation_id") or payload.get("id")
        )

    log("")
    log(STEPS)

    if not args.wait:
        if task_id:
            log(
                f"[rotate] 后续：`python scripts/rotate_master_key.py --wait` 或在概览页查看任务 {task_id}"
            )
        return 0

    if not task_id:
        log("[rotate] [WARN] 响应里没有 task_id，无法轮询；请到设置页确认轮换状态。")
        return 0

    log(
        f"[rotate] 轮询任务 {task_id}（最长 {args.timeout:.0f}s，间隔 {args.interval:.0f}s）…"
    )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        status, detail = http_json(
            "GET",
            f"{base_url}/api/admin/tasks/{task_id}",
            cookie=cookie,
            csrf=None,
            timeout=20.0,
        )
        if status == 0:
            log(f"[rotate] {now()} 轮询失败：{detail}")
            continue
        if status >= 400:
            log(f"[rotate] [FATAL] 查询任务失败（{status}）：{error_hint(detail)}")
            return 1
        state = ""
        progress = ""
        if isinstance(detail, dict):
            state = str(detail.get("status") or detail.get("result") or "").lower()
            prog = detail.get("progress")
            if isinstance(prog, dict):
                progress = (
                    f" {prog.get('percent', '?')}% {prog.get('stage', '')}".rstrip()
                )
        log(f"[rotate] {now()} status={state or 'unknown'}{progress}")
        if state in TERMINAL:
            if state in ("succeeded", "success"):
                log(
                    "[rotate] ✓ 轮换完成。下一步：抽查凭证解密/能力调用，然后移除 SKILLROUTER_MASTER_KEY_PREVIOUS。"
                )
                return 0
            log(
                f"[rotate] ✗ 轮换结束状态为 {state}：{json.dumps(detail, ensure_ascii=False)[:500]}"
            )
            log(
                "[rotate] 旧密钥仍在 SKILLROUTER_MASTER_KEY_PREVIOUS 中可用，可修复后重试（幂等）。"
            )
            return 1

    log(
        f"[rotate] [WARN] 等待超时（{args.timeout:.0f}s）；任务仍在后台运行，可稍后用 --wait 或到设置页查看。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
