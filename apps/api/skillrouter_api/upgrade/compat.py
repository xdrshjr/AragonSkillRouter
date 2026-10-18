"""会话状态兼容性（§A.3.7-5）：`aragon-home/sessions/*.json` 的结构断言。

为什么需要它：引擎没有持久化，但上游会话缓存按版本分目录写在 `ARAGON_HOME/sessions`。
升级到新版本后，旧会话文件如果结构变了（字段增删、`schemaVersion` 变了），
「升级成功」就会以最难排查的形式变成「历史会话读不出来」。因此切换前必须能回答：
**采样到的会话结构是否与候选声明的 `schemaVersion` 一致？**

采样规则：最多 50 个文件（按文件名排序）+ 1 次全量计数，避免几万个会话文件把验证拖死。
无 home / 无会话 → `blocked`（「不能验证」必须说出来，不能假装通过）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..logging import get_logger, redact
from ..settings import settings

log = get_logger("skillrouter.run")

#: 采样上限（另做 1 次全量计数）
SAMPLE_LIMIT = 50

#: 候选未声明时的机器契约版本（网关 README：`schemaVersion: 1`）
DEFAULT_SCHEMA_VERSION = 1

#: 会话文件里承载结构版本的字段名（历史文件可能没有）
VERSION_KEYS = ("schema_version", "schemaVersion")


def sessions_dir() -> Path:
    """`<data>/aragon-home/sessions`（与网关 `ARAGON_HOME` 同卷）。"""
    return settings.aragon_home / "sessions"


def check(candidate: dict, *, directory: Path | None = None, sample_limit: int = SAMPLE_LIMIT) -> dict:
    """返回 `{status, detail, needs_migration, migration_hint, sampled, total, shapes}`。

    `status ∈ {passed, failed, blocked}`：结构不一致是 `failed`（候选与现状不兼容），
    没有会话文件是 `blocked`（无法断言）。
    """
    root = Path(directory) if directory is not None else sessions_dir()
    schema_version = _schema_version(candidate)
    base = {
        "schema_version": schema_version,
        "sessions_dir": str(root),
        "needs_migration": False,
        "migration_hint": "",
        "sampled": 0,
        "total": 0,
        "shapes": [],
    }
    if not root.exists():
        return {
            **base,
            "status": "blocked",
            "detail": f"无会话采样目录（{root}）：全新部署或引擎尚未写入，无法断言状态兼容",
        }
    files = sorted(root.glob("*.json"))
    total = len(files)
    if total == 0:
        return {
            **base,
            "status": "blocked",
            "detail": f"会话采样目录为空（{root}）：没有任何历史会话，无法断言状态兼容",
        }

    shapes: dict[frozenset[str], int] = {}
    versions: dict[int, int] = {}
    unreadable = 0
    sample = files[: max(1, sample_limit)]
    for path in sample:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unreadable += 1
            log.warning("compat_session_unreadable", extra={"detail": {"file": path.name, "error": redact(str(exc), max_len=200)}})
            continue
        if not isinstance(payload, dict):
            unreadable += 1
            continue
        shapes[frozenset(payload.keys())] = shapes.get(frozenset(payload.keys()), 0) + 1
        version = _sample_version(payload)
        if version is not None:
            versions[version] = versions.get(version, 0) + 1

    if not shapes:
        return {
            **base,
            "status": "blocked",
            "detail": f"采样的 {len(sample)} 个会话文件全部不可解析（共 {total} 个），无法断言状态兼容",
            "sampled": len(sample),
            "total": total,
        }

    problems: list[str] = []
    hints: list[str] = []
    if len(shapes) > 1:
        problems.append(f"会话顶层键集合有 {len(shapes)} 种不一致的结构")
        hints.append(
            "会话文件结构不一致："
            + "；".join(
                f"[{count} 个] {sorted(keys)}" for keys, count in sorted(shapes.items(), key=lambda kv: -kv[1])[:3]
            )
        )
    foreign = sorted(version for version in versions if version != schema_version)
    if foreign:
        problems.append(f"会话 schemaVersion={foreign} 与候选 {schema_version} 不一致")
        hints.append(
            f"先跑迁移脚本把会话升到 schemaVersion={schema_version}（或先用兼容版本启动再导出会话），"
            "迁移前不要切换引擎"
        )

    shape_rows = [
        {"keys": sorted(keys), "count": count}
        for keys, count in sorted(shapes.items(), key=lambda kv: -kv[1])
    ]
    if problems:
        return {
            **base,
            "status": "failed",
            "detail": "；".join(problems) + f"（采样 {len(sample)}/{total} 个会话）",
            "needs_migration": True,
            "migration_hint": " ".join(hints),
            "sampled": len(sample),
            "total": total,
            "shapes": shape_rows,
        }
    return {
        **base,
        "status": "passed",
        "detail": (
            f"采样 {len(sample)}/{total} 个会话：顶层键集合一致（{len(shape_rows[0]['keys'])} 个键），"
            f"schemaVersion={schema_version}"
            + (f"；{unreadable} 个文件不可解析" if unreadable else "")
        ),
        "sampled": len(sample),
        "total": total,
        "shapes": shape_rows,
    }


def _schema_version(candidate: dict) -> int:
    raw = candidate.get("schema_version", DEFAULT_SCHEMA_VERSION)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SCHEMA_VERSION


def _sample_version(payload: dict) -> int | None:
    for key in VERSION_KEYS:
        if key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    return None


__all__ = ["DEFAULT_SCHEMA_VERSION", "SAMPLE_LIMIT", "check", "sessions_dir"]
