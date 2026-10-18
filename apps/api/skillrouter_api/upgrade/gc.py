"""构建产物与已安装引擎的回收（§A.3.7-5）。

保留策略：**当前指针版本 + 最近 3 个 `passed` 构建**，其余删除。
`switch` 成功后异步触发一次；任何失败只记日志，**不影响切换结果**
（回收是清洁工作，不能把一个已经生效的切换改成失败）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select

from ..db import db_session
from ..logging import get_logger
from ..models import EngineCandidate
from ..settings import settings

log = get_logger("skillrouter.run")

KEEP_PASSED = 3

#: 构建目录命名：`<version>-<provenance>`（见 `build.local_build`）
PROVENANCE_SUFFIXES = ("-branch-commit", "-npm-only", "-tagged")


def collect(*, keep_passed: int = KEEP_PASSED) -> dict:
    """删除多余构建目录与已安装引擎目录；返回 `{kept, removed, errors}`。"""
    result: dict = {"kept": [], "removed": [], "errors": []}
    try:
        keep = _keep_versions(keep_passed)
    except Exception as exc:  # noqa: BLE001 - 回收失败只记日志
        log.error("engine_gc_failed", extra={"detail": {"stage": "select", "error": str(exc)[:300]}})
        result["errors"].append(f"select: {exc}")
        return result

    result["kept"] = sorted(keep)
    _collect_builds(keep, result)
    _collect_homes(keep, result)
    log.info(
        "engine_gc_done",
        extra={"detail": {"kept": result["kept"], "removed": result["removed"], "errors": len(result["errors"])}},
    )
    return result


def _keep_versions(keep_passed: int) -> set[str]:
    from . import switch

    keep: set[str] = set()
    current = (switch.read_pointer() or {}).get("version")
    if not current:
        with db_session() as s:
            from ..db.seed import get_state

            current = get_state(s, "engine_pin", settings.engine_pin).split("@", 1)[0]
    if current:
        keep.add(str(current))
    with db_session() as s:
        rows = list(
            s.scalars(
                select(EngineCandidate)
                .where(EngineCandidate.verification_status == "passed")
                .order_by(EngineCandidate.detected_at.desc())
                .limit(max(0, keep_passed))
            )
        )
    keep.update(str(row.version) for row in rows)
    return keep


def _collect_builds(keep: set[str], result: dict) -> None:
    root = settings.builds_dir
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        version = _version_of(path.name)
        if version is None or version in keep:
            continue
        _remove(path, result)


def _collect_homes(keep: set[str], result: dict) -> None:
    root = settings.engine_home_root
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name in keep:
            continue
        _remove(path, result)


def _remove(path: Path, result: dict) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:  # noqa: BLE001 - 删不掉不算错误结论
        log.warning("engine_gc_remove_failed", extra={"detail": {"path": str(path), "error": str(exc)[:200]}})
        result["errors"].append(f"{path.name}: {exc}")
        return
    result["removed"].append(str(path.name))


def _version_of(directory_name: str) -> str | None:
    for suffix in PROVENANCE_SUFFIXES:
        if directory_name.endswith(suffix):
            return directory_name[: -len(suffix)]
    return None


__all__ = ["KEEP_PASSED", "collect"]
