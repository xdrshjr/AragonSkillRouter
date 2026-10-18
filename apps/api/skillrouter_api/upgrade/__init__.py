"""引擎升级：检测 → 隔离构建 → 验证 → 切换 → 回滚（§12.4 / §A.3.7）。"""

from . import build, compat, detect, gc, install_home, rollback, state, switch, toolchain, verify

__all__ = [
    "build",
    "compat",
    "detect",
    "gc",
    "install_home",
    "rollback",
    "state",
    "switch",
    "toolchain",
    "verify",
]
