"""设置项运行时生效（F-13 / §A.3.9 / T-26、T-27）。

`services/settings_runtime.py` 是唯一允许把设置表投影到运行时的入口：
`update()` 立即 `invalidate()` → 同进程下一次读取即用新值；白名单外的键
（部署事实）直接 `KeyError`，永不从 DB 读。
"""

from __future__ import annotations

import pytest

from skillrouter_api.services import settings_runtime


def test_update_takes_effect_immediately(client, admin) -> None:
    """T-26：PATCH 后同进程立即生效（TTL 内也生效），返回体带生效范围说明。"""
    original = settings_runtime.get("retention_days")
    try:
        # 先读一次把缓存填上，确保「立即生效」不是靠缓存恰好为空
        assert settings_runtime.get("retention_days") == original
        response = client.patch(
            "/api/admin/settings",
            headers=admin["headers"],
            json={"retention_days": 45},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["saved_at"], "缺少 saved_at（前端要展示『已保存于…』）"
        assert body["effective"] == {"api": "immediate", "worker": "≤60s"}
        # 部署事实键不可热更，必须出现在 readonly 里
        for key in ("data_dir", "db_path", "agent_engine_home_root", "engine_toolchain"):
            assert key in body["readonly"]
        assert settings_runtime.get("retention_days") == 45
    finally:
        client.patch(
            "/api/admin/settings",
            headers=admin["headers"],
            json={"retention_days": original},
        )
        settings_runtime.invalidate()


def test_unknown_key_raises() -> None:
    """T-27：白名单外的键 `get()` 抛 KeyError，部署事实键不在白名单内。"""
    for key in ("agent_engine_home_root", "engine_toolchain", "cancel_reconcile_minutes"):
        assert key not in settings_runtime.KEYS
        with pytest.raises(KeyError):
            settings_runtime.get(key)
    # 白名单内的键照常可读
    assert settings_runtime.get("retention_days") is not None
