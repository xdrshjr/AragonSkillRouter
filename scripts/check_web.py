"""前端与后端联通性检查（真实 Next.js + 真实 FastAPI）。

用法（两个进程都已在运行）：
    python scripts/check_web.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

WEB = "http://127.0.0.1:3000"
API = "http://127.0.0.1:8000"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label, detail))
    print(f" {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail and not ok else ''}")


def fetch(url: str, timeout: float = 30.0) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, response.read(4000).decode("utf-8", "replace"), headers
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, exc.read(4000).decode("utf-8", "replace"), headers
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:300], {}


def main() -> int:
    status, body, _ = fetch(f"{WEB}/api/health")
    check(status == 200 and '"ok"' in body, "web /api/health", f"{status} {body[:120]}")

    status, body, _ = fetch(f"{WEB}/login")
    check(status == 200 and 'lang="zh-CN"' in body, "登录页渲染", f"{status} {body[:120]}")
    status, body, _ = fetch(f"{WEB}/change-password")
    check(status == 200 and 'lang="zh-CN"' in body, "强制改密页渲染", f"{status} {body[:120]}")

    status, body, _ = fetch(f"{WEB}/api/admin/auth/bootstrap")
    check(status == 200, "Next.js /api 反代到 FastAPI（bootstrap）", f"{status} {body[:160]}")
    try:
        data = json.loads(body)
        check("initialized" in data, "bootstrap 返回契约字段", body[:160])
    except json.JSONDecodeError:
        check(False, "bootstrap 返回 JSON", body[:160])

    status, body, headers = fetch(f"{WEB}/api/v1/health")
    check(status == 200 and '"ok"' in body, "/api/v1/health 经反代可用", f"{status} {body[:120]}")
    check(bool(headers.get("x-request-id")), "反代透传 X-Request-Id", str(list(headers))[:160])
    check(headers.get("x-frame-options") == "DENY", "反代透传 X-Frame-Options", str(headers.get("x-frame-options")))

    status, body, _ = fetch(f"{WEB}/api/admin/overview")
    check(status == 401, "未登录访问管理接口经反代返回 401", f"{status} {body[:160]}")

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n 前端联通性：{passed}/{len(results)} 项通过")
    if passed != len(results):
        for ok, label, detail in results:
            if not ok:
                print(f"   ✗ {label} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
