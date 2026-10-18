"""引擎升级：检测 → 隔离构建 → 验证 → 切换 → 回滚（§12.4）。"""

from . import build, detect, rollback, switch, verify

__all__ = ["detect", "build", "verify", "switch", "rollback"]
