"""把候选产物装进引擎 home（§A.3.7-3 第 2 步）。

安装目标：`settings.engine_home_root/<version>/node_modules/@aragon-agent/core`
（与网关 `AGENT_ENGINE_HOME_ROOT/<version>/node_modules` 必须同值，否则「装了但没生效」）。

为什么是物理目录而不是 symlink：Windows 上建 symlink 需要额外特权，junction 又只对目录
有效且跨卷行为不一致（§12.4-5 的同一条理由）。重复安装是幂等的。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..logging import get_logger
from ..settings import settings

log = get_logger("skillrouter.run")

PACKAGE = "@aragon-agent/core"


class InstallError(RuntimeError):
    """产物不完整，无法安装（调用方据此中止切换，不写指针）。"""


def target(version: str) -> Path:
    """安装后的包目录：`<engine_home_root>/<version>/node_modules/@aragon-agent/core`。"""
    return settings.engine_home_root / version / "node_modules" / "@aragon-agent" / "core"


def resolve(version: str) -> Path | None:
    """已安装则返回包目录（`package.json` 可读），否则 `None`。"""
    path = target(version)
    return path if (path / "package.json").exists() else None


def resolved_version(version: str) -> str | None:
    """读已安装包的 `package.json.version`（T-20 的「解析到候选版本」以此断言）。"""
    path = resolve(version)
    if path is None:
        return None
    try:
        payload = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - 损坏的安装
        return None
    value = payload.get("version")
    return str(value) if value else None


def install(artifact_dir: str | Path, version: str) -> dict:
    """把 `artifact_dir`（`engine-builds/…/runtime`）装到 home；返回安装结果。

    返回 `{path, home, version, installed, action, source_version}`；重复安装返回
    `installed=False, action='reused'`，不重复拷贝。
    """
    artifact = Path(artifact_dir)
    source = artifact / "packages" / "core"
    dist = source / "dist"
    if not dist.exists():
        raise InstallError(f"产物缺少 packages/core/dist：{dist} 不存在，无法安装到 home")

    destination = target(version)
    if resolved_version(version) == version and (destination / "dist").exists():
        return {
            "path": str(destination),
            "home": str(destination.parents[2]),
            "version": version,
            "installed": False,
            "action": "reused",
            "source_version": version,
        }

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist, destination / "dist", dirs_exist_ok=True)

    package = _package_json(source, artifact, version)
    (destination / "package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    home = destination.parents[2]
    (home / "installed.json").write_text(
        json.dumps(
            {
                "version": version,
                "package": PACKAGE,
                "artifact": str(artifact),
                "path": str(destination),
                "manifest": _manifest(artifact),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info(
        "engine_home_installed",
        extra={"detail": {"version": version, "path": str(destination), "artifact": str(artifact)}},
    )
    return {
        "path": str(destination),
        "home": str(home),
        "version": version,
        "installed": True,
        "action": "installed",
        "source_version": package.get("_source_version"),
    }


def _package_json(source: Path, artifact: Path, version: str) -> dict:
    """优先沿用产物自带的 `package.json`，并把版本**对齐到候选版本**。

    为什么必须覆写版本：安装目录名、指针与 `/healthz.engine.version` 都以候选版本为准；
    若产物里的版本与候选不一致还照抄，`pin` 与实际运行的包就会静默分叉。
    """
    payload: dict = {}
    for candidate in (source / "package.json", artifact / "package.json"):
        if candidate.exists():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(loaded, dict):
                payload = loaded
                break
    source_version = payload.get("version")
    payload["name"] = PACKAGE
    payload["version"] = version
    payload.setdefault("type", "module")
    payload.setdefault("main", "dist/index.js")
    payload.setdefault(
        "exports",
        {".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"}},
    )
    if source_version is not None and str(source_version) != version:
        payload["_source_version"] = source_version
    return payload


def _manifest(artifact: Path) -> dict:
    path = artifact / "build-manifest.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["PACKAGE", "InstallError", "install", "resolve", "resolved_version", "target"]
