"""隔离构建（两阶段：含 dev 构建 → 裁剪运行时依赖，§12.4-2 修正）。

本模块只保留**本地工具链**的实现（开发机上有 git/npm 时可用）；生产默认经
`toolchain.run_build()` 走网关容器（§A.3.7-0）。`build()` 是唯一对外的入口。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..logging import get_logger, redact
from ..settings import settings

log = get_logger("skillrouter.run")

BUILD_TIMEOUT_S = 15 * 60
TAIL_LINES = 200


def build(candidate: dict) -> dict:
    """经工具链构建候选产物：返回 `{ok, status, stage, detail, artifact_dir, manifest, steps}`。

    `status='blocked'` 表示工具链不可用（**不产生候选行、也不写 `failed`**）：
    那是环境的问题，不是候选的问题（§A.3.7-0）。
    """
    from . import toolchain

    return toolchain.run_build(candidate)


def _run(cmd: list[str], cwd: Path, timeout: int = BUILD_TIMEOUT_S) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-TAIL_LINES:])
        return result.returncode, redact(tail, max_len=8000)
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout}s）：{' '.join(cmd)}"


def local_build(candidate: dict) -> dict:
    """本地工具链（`SKILLROUTER_ENGINE_TOOLCHAIN=local` 且 development）的原始实现。"""
    version = candidate["version"]
    provenance = candidate.get("provenance", "tagged")
    target = settings.builds_dir / f"{version}-{provenance}"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    steps: list[dict] = []
    if provenance in ("tagged", "branch-commit"):
        ref = candidate.get("commit_sha") or f"core-v{version}"
        code, output = _run(
            ["git", "clone", "--filter=blob:none", "--depth", "1", "--branch", ref,
             "https://github.com/xdrshjr/AragonAgent", "src"],
            target,
        )
        steps.append({"name": "clone", "code": code, "detail": output})
        if code == 127:
            return _blocked("clone", output, steps)
        if code != 0:
            return _failed("clone", output, steps)
        workdir = target / "src"
    else:
        code, output = _run(["npm", "pack", f"@aragon-agent/core@{version}"], target)
        steps.append({"name": "npm_pack", "code": code, "detail": output})
        if code == 127:
            return _blocked("npm_pack", output, steps)
        if code != 0:
            return _failed("npm_pack", output, steps)
        workdir = target

    # 构建阶段：包含 devDependencies（tsc 在其中），--ignore-scripts 禁止 postinstall
    code, output = _run(["npm", "ci", "--ignore-scripts"], workdir)
    steps.append({"name": "npm_ci_build", "code": code, "detail": output})
    if code == 127:
        return _blocked("npm_ci_build", output, steps)
    if code != 0:
        return _failed("npm_ci_build", output, steps)

    code, output = _run(["npm", "run", "build", "-w", "packages/core"], workdir)
    steps.append({"name": "build_core", "code": code, "detail": output})
    if code != 0:
        return _failed("build_core", output, steps)

    # 裁剪阶段：独立目录只装运行时依赖
    pruned = target / "runtime"
    pruned.mkdir(exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        source = workdir / name
        if source.exists():
            shutil.copy2(source, pruned / name)
    code, output = _run(["npm", "ci", "--omit=dev", "--ignore-scripts"], pruned)
    steps.append({"name": "prune_runtime", "code": code, "detail": output})
    if code == 127:
        return _blocked("prune_runtime", output, steps)
    if code != 0:
        return _failed("prune_runtime", output, steps)

    dist = workdir / "packages" / "core" / "dist"
    if not dist.exists():
        return {
            "ok": False,
            "status": "failed",
            "stage": "build_core",
            "detail": "构建完成后未找到 packages/core/dist",
            "artifact_dir": None,
            "manifest": {},
            "steps": steps,
        }
    target_dist = pruned / "packages" / "core" / "dist"
    target_dist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist, target_dist, dirs_exist_ok=True)

    manifest = {
        "version": version,
        "commit": candidate.get("commit_sha"),
        "integrity": candidate.get("integrity"),
        "node": _node_version(),
        "deps_lock_sha256": _sha256(workdir / "package-lock.json"),
        "pruned_lock_sha256": _sha256(pruned / "package-lock.json"),
        "built_at": _now(),
        "adapter_version": "1",
        "provenance": provenance,
    }
    (pruned / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "status": "built",
        "stage": "built",
        "detail": f"构建完成：{pruned}",
        "artifact_dir": str(pruned),
        "manifest": manifest,
        "steps": steps,
    }


def _blocked(stage: str, detail: str, steps: list[dict]) -> dict:
    return {
        "ok": False,
        "status": "blocked",
        "stage": stage,
        "detail": f"工具链缺少命令（本地构建无法执行）：{detail}",
        "artifact_dir": None,
        "manifest": {},
        "steps": steps,
    }


def _failed(stage: str, detail: str, steps: list[dict]) -> dict:
    return {
        "ok": False,
        "status": "failed",
        "stage": stage,
        "detail": detail,
        "artifact_dir": None,
        "manifest": {},
        "steps": steps,
    }


def _node_version() -> str:
    code, output = _run(["node", "--version"], Path.cwd(), timeout=15)
    return output.strip() if code == 0 else "unknown"


def _sha256(path: Path) -> str | None:
    import hashlib

    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    from ..db.base import now_iso

    return now_iso()


def artifacts(version: str, provenance: str = "tagged") -> Path:
    return settings.builds_dir / f"{version}-{provenance}" / "runtime"


__all__ = ["BUILD_TIMEOUT_S", "artifacts", "build", "local_build"]
