"""设置项运行时生效（F-13 / §A.3.9）。

问题背景：设置页把策略写进 `settings` 表，但读取点仍然直接读 `settings.*`（进程启动时的
环境变量快照），于是「保存成功但策略不生效」。本模块是**唯一**允许把 DB 值投影到运行时
的入口，用 60s TTL 内存缓存 + 显式 `invalidate()` 控制一致性：

- 同进程（api 内）：`services.settings.update()` 调用 `invalidate()` → 下一次调用立即生效。
- 跨进程（api ↔ worker）：最长 60s 延迟，设置页文案必须写「worker 最多 60 秒内生效」。

安全底线：只允许白名单键。主密钥路径、`SKILLROUTER_AGENT_GATEWAY_SECRET`、`db_path`、
`data_dir`，以及 `agent_engine_home_root` / `engine_toolchain` / `cancel_reconcile_minutes`
都是**部署事实**而不是可调策略，永不从 DB 读；未列入白名单的键 `get()` 直接抛 `KeyError`
（防止未来有人顺手加键）。
"""

from __future__ import annotations

import time
from typing import Any

from ..settings import settings

#: 允许从 DB 覆盖的键（唯一白名单）
KEYS = frozenset(
    {
        "payload_policy",
        "retention_days",
        "fetch_allowlist",
        "ssrf_deny",
        "ssrf_deny_extra",
        "engine_policy",
        "default_budget_usd",
        "agent_model",
    }
)

#: 缓存 TTL（秒）——同一数字必须出现在设置页文案里
TTL_SECONDS = 60

_cache: dict[str, tuple[float, Any]] = {}


def _env_default(key: str):  # noqa: ANN202
    if key == "payload_policy":
        return settings.payload_policy
    if key == "retention_days":
        return settings.retention_days
    if key == "fetch_allowlist":
        return settings.allowlist
    if key == "ssrf_deny_extra":
        return list(settings.ssrf_deny_list)
    if key == "ssrf_deny":
        # DB 侧的键名是 `ssrf_deny`（见 services/settings.EDITABLE 与 db/seed.py）；
        # 未配置时回落到部署基线，`deny_extra()` 会自动减掉基线（§A.3.9）
        return list(settings.ssrf_deny_list)
    if key == "engine_policy":
        return settings.engine_policy
    if key == "default_budget_usd":
        return settings.default_budget_usd
    if key == "agent_model":
        return None
    raise KeyError(key)


def get(key: str, default=None):  # noqa: ANN001, ANN201
    """读取运行时生效值：内存缓存 → `settings` 表 → environment。"""
    if key not in KEYS:
        raise KeyError(f"{key} 不在 settings_runtime 白名单内（部署事实不入 DB）")
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    from . import settings as settings_service  # 延迟导入：避免与 services.settings 循环

    try:
        value = settings_service.get_value(key, None)
    except Exception:  # pragma: no cover - DB 尚未就绪时退回环境值
        value = None
    if value is None:
        value = _env_default(key) if default is None else default
    _cache[key] = (now + TTL_SECONDS, value)
    return value


def invalidate(key: str | None = None) -> None:
    """清空缓存；`services.settings.update()` 与 `update_agent_config()` 后必须调用。"""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def allowlist() -> list[str]:
    """抓取白名单：DB 值为字符串或列表都接受。"""
    value = get("fetch_allowlist", [])
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def deny_extra() -> list[str]:
    """设置页可追加的黑名单（读 `ssrf_deny` 与 `ssrf_deny_extra` 两个键，去重）。

    历史实现只认 `ssrf_deny_extra`，而 `services/settings.EDITABLE` 与 `db/seed.py`
    写的是 `ssrf_deny` —— 于是设置页改了黑名单永远不生效（F-13）。两个键都读即可闭环，
    基线由 `fetch_guard.deny_list()` 合并去重。
    """
    baseline = set(settings.ssrf_deny_list)
    merged: list[str] = []
    for key in ("ssrf_deny_extra", "ssrf_deny"):
        value = get(key, [])
        if isinstance(value, str):
            items = [x.strip() for x in value.split(",")]
        else:
            items = [str(x).strip() for x in (value or [])]
        for item in items:
            if item and item not in baseline and item not in merged:
                merged.append(item)
    return merged


def get_int(key: str, default: int | None = None) -> int:
    """`get()` 的整数投影：DB 里存成字符串（如 `"45"`）也能用，非法值回落默认。"""
    value = get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default or 0)


def effective_scopes() -> dict[str, str]:
    """设置页文案用的生效范围说明（`services.settings.update()` 的 `effective` 复用）。"""
    return {"api": "immediate", "worker": f"≤{TTL_SECONDS}s"}


__all__ = [
    "KEYS",
    "TTL_SECONDS",
    "get",
    "get_int",
    "invalidate",
    "allowlist",
    "deny_extra",
    "effective_scopes",
]
