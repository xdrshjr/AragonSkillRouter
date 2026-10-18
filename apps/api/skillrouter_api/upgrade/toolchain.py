"""工具链归属（§A.3.7-0）：构建与验证在网关容器执行，Python 只做编排。

为什么必须这样分：`deploy/Dockerfile.api` 里只有 `ca-certificates`（无 node/npm/git），
而隔离构建要 `git clone` / `npm ci` / `npm run build`、导出清单校验要跑 node 脚本。
若 Python 侧自己 spawn，则**任何部署形态下**验证都只能是 `blocked`，`policy=auto` 永不生效。

本轮固定：

| 项 | 决策 |
|---|---|
| 构建与验证在哪跑 | 网关容器（`node:22-slim` + `git`），与 api/worker 共享 `/data` 卷 |
| Python 侧角色 | 只做编排：状态机、审计、并发限制、结果落库 |
| 本地开发 | `SKILLROUTER_ENGINE_TOOLCHAIN=local` 且 `SKILLROUTER_ENV=development` 才生效，走原 subprocess 路径 |
| 工具链不可用 | 返回 `status='blocked'`（`detail` 说明缺什么），**不是** `failed`：那是环境的问题，不是候选的问题 |

两个网关端点：`POST /internal/admin/build`、`POST /internal/admin/verify-artifact`
（均要求 `X-Internal-Token`，均只接受 `settings.data_path` 下的绝对路径）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..logging import get_logger, redact
from ..security.netpolicy import client_kwargs
from ..settings import settings

log = get_logger("skillrouter.run")

GATEWAY = "gateway"
LOCAL = "local"
USABLE = (GATEWAY, LOCAL)

#: 与 `build.BUILD_TIMEOUT_S` 同值：构建在网关容器里跑，可能很慢，但必须有个上限
TIMEOUT_S = 15 * 60

BUILD_PATH = "/internal/admin/build"
VERIFY_PATH = "/internal/admin/verify-artifact"


def resolve() -> str:
    """当前**实际生效**的工具链（`local` 在非 development 环境被忽略并记警告）。"""
    name = (settings.engine_toolchain or GATEWAY).strip().lower()
    if name == LOCAL:
        if settings.env != "development":
            log.warning(
                "toolchain_local_ignored",
                extra={
                    "detail": {
                        "configured": LOCAL,
                        "env": settings.env,
                        "effective": GATEWAY,
                        "why": "api/worker 镜像没有 node/npm/git，生产必须由网关容器构建",
                    }
                },
            )
            return GATEWAY
        return LOCAL
    if name not in USABLE:
        log.warning(
            "toolchain_unknown",
            extra={"detail": {"configured": name, "effective": GATEWAY}},
        )
        return GATEWAY
    return GATEWAY


def script_path() -> Path:
    """`scripts/verify_artifact.mjs`（本地工具链用；网关容器内有自己的副本）。"""
    return Path(__file__).resolve().parents[3] / "scripts" / "verify_artifact.mjs"


def inside_data_dir(path: str | Path) -> bool:
    """产物路径必须落在 `settings.data_path` 下（否则拒收网关回传的路径）。"""
    try:
        candidate = Path(path).resolve()
        root = settings.data_path
        return candidate == root or root in candidate.parents
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - 非法路径
        return False


def blocked(detail: str, *, stage: str = "toolchain", steps: list[dict] | None = None) -> dict:
    """构建侧「不能执行」的统一返回：`blocked` 而不是 `failed`。"""
    return {
        "ok": False,
        "status": "blocked",
        "stage": stage,
        "detail": detail,
        "artifact_dir": None,
        "manifest": {},
        "steps": steps or [],
    }


def run_build(candidate: dict) -> dict:
    """构建候选产物，返回 `{ok, status, stage, detail, artifact_dir, manifest, steps}`。

    `status ∈ {built, failed, blocked}`：`blocked` 只用于「这台机器跑不了构建」，
    `failed` 才表示候选本身构建失败。
    """
    toolchain = resolve()
    if toolchain == LOCAL:
        from . import build as build_module  # 延迟导入：build 反向依赖本模块

        return build_module.local_build(candidate)

    body = {
        "version": candidate.get("version"),
        "provenance": candidate.get("provenance") or "tagged",
        "commit_sha": candidate.get("commit_sha"),
    }
    if not body["version"]:
        return blocked("候选缺少 version，无法提交构建", stage="validate")

    response, error = _post(BUILD_PATH, body)
    if response is None:
        return blocked(
            f"网关不可达：{error}（构建工具链 git/npm/node 只在网关容器内，"
            "api/worker 镜像不含；检查 gateway 是否运行与 SKILLROUTER_AGENT_GATEWAY_SECRET）",
            stage="toolchain",
        )
    if response.status_code in (404, 405):
        return blocked(
            f"网关未实现 {BUILD_PATH}（HTTP {response.status_code}）：网关版本过旧，"
            "需与 api 同版本发布",
            stage="toolchain",
        )
    payload = _json(response)
    if payload is None:
        return blocked(
            f"网关返回非 JSON（HTTP {response.status_code}）：{redact(response.text, max_len=300)}",
            stage="toolchain",
        )
    status = str(payload.get("status") or ("built" if payload.get("ok") else "failed"))
    if status not in ("built", "failed", "blocked"):
        status = "failed" if payload.get("ok") is False else "built"
    artifact_dir = payload.get("artifact_dir")
    if artifact_dir and not inside_data_dir(artifact_dir):
        # 路径前缀校验：网关回传的产物必须是共享卷上的路径，否则后续 install/gc 会读错地方
        return blocked(
            f"网关回传的 artifact_dir 不在数据目录下：{artifact_dir}（期望在 {settings.data_path} 下）",
            stage="toolchain",
        )
    return {
        "ok": bool(payload.get("ok")) and status == "built",
        "status": status,
        "stage": str(payload.get("stage") or ("built" if payload.get("ok") else "build")),
        "detail": redact(str(payload.get("detail") or ""), max_len=4000),
        "artifact_dir": str(artifact_dir) if artifact_dir else None,
        "manifest": payload.get("manifest") or {},
        "steps": payload.get("steps") or [],
    }


def run_verify(artifact_dir: str | Path, expected: dict | list | None = None) -> dict:
    """在网关容器里跑 node 侧能做的检查（导出符号、事件名）。

    返回 `{ok, status, checks[], detail}`；`status ∈ {passed, failed, blocked}`。
    `checks[]` 每项形如 `{check, status, detail}`，由 `verify.py` 合并进 7 项总表。
    """
    artifact = str(artifact_dir)
    if not inside_data_dir(artifact):
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"artifact_dir 不在数据目录下：{artifact}",
            "checks": [],
        }
    expected_symbols = _expected_symbols(expected)
    toolchain = resolve()
    if toolchain == LOCAL:
        return _local_verify(artifact, expected_symbols)

    response, error = _post(
        VERIFY_PATH,
        {"artifact_dir": artifact, "expected_symbols": expected_symbols},
    )
    if response is None:
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"网关不可达：{error}（导出清单/事件映射只能在网关容器内校验）",
            "checks": [],
        }
    if response.status_code in (404, 405):
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"网关未实现 {VERIFY_PATH}（HTTP {response.status_code}）：网关版本过旧",
            "checks": [],
        }
    payload = _json(response)
    if payload is None:
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"网关返回非 JSON（HTTP {response.status_code}）",
            "checks": [],
        }
    status = str(payload.get("status") or "blocked")
    if status not in ("passed", "failed", "blocked"):
        status = "blocked"
    checks = [
        {
            "check": str(item.get("check") or "未知检查"),
            "status": str(item.get("status") or "blocked"),
            "detail": str(item.get("detail") or ""),
        }
        for item in (payload.get("checks") or [])
        if isinstance(item, dict)
    ]
    return {
        "ok": status == "passed",
        "status": status,
        "detail": str(payload.get("detail") or ""),
        "checks": checks,
    }


def _local_verify(artifact_dir: str, expected_symbols: list[str]) -> dict:
    """开发机上的等价检查：直接跑 `scripts/verify_artifact.mjs`（需要本机有 node）。"""
    from . import build as build_module

    script = script_path()
    if not script.exists():
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"本地缺少校验脚本：{script}",
            "checks": [],
        }
    code, output = build_module._run(
        ["node", str(script), artifact_dir, "--expected", ",".join(expected_symbols)],
        Path(artifact_dir),
        timeout=300,
    )
    if code == 127:
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"本机缺少 node：{output}",
            "checks": [],
        }
    payload = _json_text(output)
    if payload is None:
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"校验脚本输出无法解析：{output[-600:]}",
            "checks": [],
        }
    checks = [
        {
            "check": str(item.get("check") or ""),
            "status": str(item.get("status") or "blocked"),
            "detail": str(item.get("detail") or ""),
        }
        for item in (payload.get("checks") or [])
        if isinstance(item, dict)
    ]
    status = str(payload.get("status") or "blocked")
    return {
        "ok": status == "passed",
        "status": status if status in ("passed", "failed", "blocked") else "blocked",
        "detail": str(payload.get("detail") or ""),
        "checks": checks,
    }


def _expected_symbols(expected: dict | list | None) -> list[str]:
    if isinstance(expected, dict):
        raw = expected.get("symbols") or []
    else:
        raw = expected or []
    return [str(item) for item in raw]


def _post(path: str, body: dict[str, Any]) -> tuple[httpx.Response | None, str]:
    """调用网关内部端点；网络层面的失败返回 `(None, 原因)`，不抛异常。"""
    url = f"{settings.agent_gateway_url.rstrip('/')}{path}"
    headers = {"X-Internal-Token": settings.agent_gateway_secret}
    try:
        with httpx.Client(**client_kwargs(url, timeout=TIMEOUT_S)) as client:
            return client.post(url, json=body, headers=headers), ""
    except httpx.HTTPError as exc:
        return None, redact(str(exc), max_len=300)


def _json(response: httpx.Response) -> dict | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _json_text(text: str) -> dict | None:
    """从命令输出里挑出最后一段 JSON（脚本可能先打印进度日志）。"""
    stripped = text.strip()
    if not stripped:
        return None
    for candidate in (stripped, *reversed(stripped.splitlines())):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


__all__ = [
    "BUILD_PATH",
    "GATEWAY",
    "LOCAL",
    "TIMEOUT_S",
    "blocked",
    "inside_data_dir",
    "resolve",
    "run_build",
    "run_verify",
    "script_path",
]
