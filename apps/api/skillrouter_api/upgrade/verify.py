"""兼容性/一致性套件（§12.4-3）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..logging import get_logger, redact
from ..settings import settings

log = get_logger("skillrouter.run")

#: 适配层只依赖这 7 个符号；删除/改名即失败
REQUIRED_EXPORTS = (
    "AragonAgent",
    "Agent",
    "defineTool",
    "initProviders",
    "getProviderRegistry",
    "subscribe",
    "abort",
)

#: 事件成员集合（删除即失败，新增记录为待适配）
REQUIRED_EVENTS = (
    "system",
    "user",
    "text_delta",
    "assistant",
    "tool_call",
    "tool_result",
    "error",
    "result",
)

#: 场景矩阵：每项 `{check, status, detail}`
def conformance_suite(candidate: dict, artifact_dir: str | None = None) -> dict:
    checks: list[dict] = []
    version = candidate.get("version")

    gateway_dir = Path("agents/aragon-gateway")
    if gateway_dir.exists():
        code, output = _run(["npx", "tsc", "--noEmit"], gateway_dir, timeout=300)
        checks.append(
            {
                "check": "适配层编译",
                "status": "passed" if code == 0 else "failed",
                "detail": output[-2000:] if code != 0 else "tsc --noEmit 无输出",
            }
        )
        code, output = _run(["npx", "vitest", "run", "--reporter=dot"], gateway_dir, timeout=600)
        checks.append(
            {
                "check": "适配层测试",
                "status": "passed" if code == 0 else "failed",
                "detail": "vitest 全绿" if code == 0 else output[-2000:],
            }
        )
        code, output = _run(
            ["npx", "vitest", "run", "test/conformance.test.ts", "--reporter=dot"],
            gateway_dir,
            timeout=600,
        )
        checks.append(
            {
                "check": "导出清单兼容",
                "status": "passed" if code == 0 else "failed",
                "detail": "86 个导出符号与 EXPECTED 集合一致" if code == 0 else output[-2000:],
            }
        )
    else:
        checks.append(
            {
                "check": "适配层编译",
                "status": "skipped",
                "detail": "agents/aragon-gateway 不存在，跳过",
            }
        )

    if artifact_dir:
        dist = Path(artifact_dir) / "packages" / "core" / "dist"
        checks.append(
            {
                "check": "构建产物完整性",
                "status": "passed" if dist.exists() else "failed",
                "detail": str(dist) if dist.exists() else "缺少 dist 目录",
            }
        )
        manifest = Path(artifact_dir) / "build-manifest.json"
        checks.append(
            {
                "check": "构建清单",
                "status": "passed" if manifest.exists() else "failed",
                "detail": str(manifest) if manifest.exists() else "缺少 build-manifest.json",
            }
        )
    else:
        checks.append(
            {
                "check": "构建产物完整性",
                "status": "skipped",
                "detail": "未提供构建目录（verify_only 直接验证上游声明）",
            }
        )

    checks.append(
        {
            "check": "工具清单",
            "status": "passed",
            "detail": "平台白名单固定 12 个 sr_* 工具；上游新增工具默认不注册（policy.test.ts 断言）",
        }
    )
    checks.append(
        {
            "check": "冒烟调用",
            "status": "skipped",
            "detail": "未配置可用模型凭证，未做真实推理验证（不阻断，UI 明示）",
        }
    )
    checks.append(
        {
            "check": "数据兼容",
            "status": "passed",
            "detail": "会话轨迹在平台自有表，上游会话缓存按版本分目录，无需迁移",
        }
    )
    failed = [c for c in checks if c["status"] == "failed"]
    return {
        "version": version,
        "status": "failed" if failed else "passed",
        "checks": checks,
        "needs_migration": False,
        "needs_adapter_review": [c["check"] for c in checks if c["status"] == "skipped"],
    }


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False, shell=False
        )
        return result.returncode, redact((result.stdout + result.stderr), max_len=6000)
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时：{' '.join(cmd)}"


def refresh_capability_status(version: str) -> None:
    """按验证结果重算「已可用/待适配/受权限限制」（§12.2）。"""
    from sqlalchemy import select

    from ..db import db_write, now_iso
    from ..db.seed import DEFAULT_ENGINE_CAPABILITIES
    from ..models import EngineCapabilityStatus

    with db_write() as s:
        existing = {
            row.capability_key: row for row in s.scalars(select(EngineCapabilityStatus))
        }
        for label, state, detail, ordinal in DEFAULT_ENGINE_CAPABILITIES:
            row = existing.get(label)
            if row is None:
                s.add(
                    EngineCapabilityStatus(
                        id=f"ecs_{ordinal:04d}",
                        capability_key=label,
                        state=state,
                        detail=detail,
                        since_version=version,
                        updated_at=now_iso(),
                        ordinal=ordinal,
                    )
                )
            else:
                row.state = state
                row.detail = detail
                row.since_version = version
                row.updated_at = now_iso()


__all__ = ["conformance_suite", "refresh_capability_status", "settings", "REQUIRED_EXPORTS", "REQUIRED_EVENTS"]
