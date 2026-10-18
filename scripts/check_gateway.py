"""校验 Python ↔ AragonAgent 网关的真实联通（§7.12 / §9.5）。

前置：`uvicorn` 与 `node agents/aragon-gateway/dist/src/main.js` 都在运行，
且两侧的共享密钥一致（`SKILLROUTER_AGENT_GATEWAY_SECRET` == `AGENT_GATEWAY_SHARED_SECRET`）。

    python scripts/check_gateway.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

from skillrouter_api.agent.client import AragonAgentAdapter  # noqa: E402
from skillrouter_api.settings import settings  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label, detail))
    print(f" {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail and not ok else ''}")


async def main() -> int:
    adapter = AragonAgentAdapter()

    health = await adapter.health()
    engine = health.get("engine") or {}
    check(True, "Python → 网关 /healthz 可达", adapter.base_url)
    check(engine.get("version") == settings.gateway_pin_version,
          f"引擎版本与 pin 一致（{settings.gateway_pin_version}）", str(engine))
    check(engine.get("status") == "available", "引擎状态 available", str(engine.get("status")))
    tools = health.get("tools") or []
    names = [t["name"] if isinstance(t, dict) else t for t in tools]
    check(len(names) == 12, "网关注册 12 个平台工具", str(len(names)))
    check("sr_capabilities_list" in names and "sr_request_approval" in names,
          "关键工具在位", str(names)[:200])
    forbidden = {"bash", "write_file", "read_file", "web", "skill_install"}
    check(not (forbidden & set(names)), "未注册任何上游内置工具（权限边界）", str(forbidden & set(names)))

    caps = await adapter.capabilities()
    states = {c["state"] for c in caps}
    check(bool(caps), "能力状态表可读", f"{len(caps)} 项")
    check({"available", "needs_adapter", "permission_limited"} <= states,
          "三态能力状态齐备", str(sorted(states)))

    connectivity = await adapter.connectivity()
    check(connectivity["gateway"]["ok"] is True, "适配层 connectivity 通过")
    check(connectivity["engine"].get("schema_version") == 1, "上游 schemaVersion=1",
          str(connectivity["engine"].get("schema_version")))

    # 网关 → Python 内部接口：通过网关的热切换接口触发一次真实 reload（不改 pin）
    reload_result = await adapter.reload_engine(settings.gateway_pin_version)
    check(bool(reload_result.get("ok")), "网关热切换（reload 到同一版本）成功",
          json.dumps(reload_result, ensure_ascii=False)[:300])
    health_after = await adapter.health()
    check((health_after.get("engine") or {}).get("version") == settings.gateway_pin_version,
          "reload 后版本未漂移", str((health_after.get("engine") or {}).get("version")))

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n 网关联通性：{passed}/{len(results)} 项通过")
    if passed != len(results):
        for ok, label, detail in results:
            if not ok:
                print(f"   ✗ {label} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
