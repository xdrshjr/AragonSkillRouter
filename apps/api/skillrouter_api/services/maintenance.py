"""维护模式（F-25 / §A.3.13）。

恢复备份期间数据库处于**半恢复**状态：读可能读到旧数据但能自洽，写会污染
刚放回去的库。因此判据不是「服务停了没有」，而是「恢复流程有没有留下标记」：

- `settings.data_dir/.restore-in-progress` 存在 → 维护中（写操作 503，读放行）。
- 标记的 mtime 超过 24h → 视为**陈旧标记**（恢复进程被杀、容器重启等），
  记 `stale_maintenance_marker` 告警但**仍然返回 True**：宁可 503 也不让
  半恢复的库被写。
- 这个检查在 `deps.maintenance_block()` 里被每个写请求调用，因此只做一次
  `stat()`，不查库、不加锁。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..logging import get_logger
from ..settings import settings

log = get_logger("skillrouter.run")

#: 恢复流程的标记文件名（`scripts/restore.py` 创建/删除）
MARKER_NAME = ".restore-in-progress"

#: 超过这个秒数的标记视为陈旧（恢复被中断），只告警不自动清除
STALE_AFTER_S = 24 * 3600

#: 陈旧告警的抑制窗口：每个进程每小时最多一条，避免每请求刷日志
_QUIET_S = 3600
_last_stale_warn: dict[str, float] = {"at": 0.0}


def marker_path() -> Path:
    """标记文件路径（`data_dir` 的绝对解析，与恢复脚本一致）。"""
    return settings.data_path / MARKER_NAME


def is_maintenance() -> bool:
    """当前是否处于维护模式。"""
    path = marker_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    age_s = time.time() - mtime
    if age_s > STALE_AFTER_S:
        now = time.time()
        if now - _last_stale_warn["at"] > _QUIET_S:
            _last_stale_warn["at"] = now
            log.error(
                "stale_maintenance_marker",
                extra={
                    "object": str(path),
                    "detail": {
                        "age_hours": round(age_s / 3600, 1),
                        "note": "标记超过 24 小时未清理；仍按维护中处理，请确认恢复是否已完成",
                    },
                },
            )
    return True


__all__ = ["MARKER_NAME", "STALE_AFTER_S", "is_maintenance", "marker_path"]
