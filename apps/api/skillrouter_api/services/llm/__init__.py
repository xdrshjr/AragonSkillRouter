"""LLM 服务配置子包（spec §4.1）：`protocols` / `identity` / `probe` / `catalog`。

**为什么用惰性导出**：`schemas/llm.py` 需要 `protocols` 里的协议键（INV-1：协议知识只能有
一份），而 `identity` / `probe` / `catalog` 又反过来依赖 `schemas` 与彼此。若在本文件里
`import` 全部子模块，`import skillrouter_api.schemas.llm` 会在半初始化的包上炸
`ImportError`。因此这里只做「按需加载」的导出，`from .llm import protocols` 与
`from .llm import catalog` 两种写法都能用，且没有导入顺序陷阱。
"""

from __future__ import annotations

from importlib import import_module

#: 子模块名 → 本包对外暴露的名字（同一份）
_SUBMODULES = ("protocols", "identity", "probe", "catalog")


def __getattr__(name: str):  # noqa: ANN202
    if name in _SUBMODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *_SUBMODULES])


__all__ = list(_SUBMODULES)
