"""兼容性/一致性套件（§12.4-3 / §A.3.7-1）：artifact-first 的三态验证。

核心语义：**「验证通过」必须是候选产物的性质，而不是当前环境的性质；
不能验证时必须说「不能验证」。**

规则（逐条来自 §A.3.7-1）：

1. 唯一输入是 `artifact_dir`（`engine-builds/<version>-<provider>/runtime`），
   不再读 `agents/aragon-gateway` 源码的相对路径（镜像里不存在）。
2. 七项检查，每项 `passed | failed | blocked`，`detail` 必填；`skipped` 取值已删除
   （历史行按 `blocked` 读取）。
3. `verify_status = failed if 任何 failed else (blocked if 任何 blocked else passed)`；
   `needs_adapter_review` = blocked 项列表。
4. `verify_status` 与 `engine_upgrade_runs.result` 是两列：前者是候选结论，后者是
   「这次运行怎么结束的」，不得互相顶替。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..logging import get_logger
from . import compat, toolchain

log = get_logger("skillrouter.run")

PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
STATUSES = (PASSED, FAILED, BLOCKED)

CHECK_ARTIFACT = "构建产物完整性"
CHECK_MANIFEST = "构建清单一致"
CHECK_EXPORTS = "导出清单兼容"
CHECK_EVENTS = "事件映射契约"
CHECK_TOOLS = "工具清单"
CHECK_STATE = "状态兼容"
CHECK_SMOKE = "冒烟调用"

#: 七项检查的固定顺序（UI 表格按此渲染）
CHECK_ORDER = (
    CHECK_ARTIFACT,
    CHECK_MANIFEST,
    CHECK_EXPORTS,
    CHECK_EVENTS,
    CHECK_TOOLS,
    CHECK_STATE,
    CHECK_SMOKE,
)

#: 适配层真正依赖的**运行时导出**（`@aragon-agent/core@0.2.34` 的 `index.d.ts` barrel 实测存在）。
#: 注意：`subscribe`/`abort` 是 `AragonAgent` 实例方法而不是包导出，旧列表照抄会永久 `failed`。
EXPECTED_SYMBOLS: tuple[str, ...] = (
    "AragonAgent",
    "Agent",
    "defineTool",
    "initProviders",
    "getProviderRegistry",
)

#: 旧名保留（外部引用），含义即 `EXPECTED_SYMBOLS`
REQUIRED_EXPORTS = EXPECTED_SYMBOLS

_DIST_INDEX = Path("packages") / "core" / "dist" / "index.js"


def conformance_suite(candidate: dict, artifact_dir: str | Path | None = None) -> dict:
    """跑七项检查，返回 `{version, status, verify_status, checks, needs_migration, …}`。"""
    version = candidate.get("version")
    artifact = Path(artifact_dir) if artifact_dir else None

    checks: list[dict] = []
    checks.append(_artifact_check(artifact))
    checks.append(_manifest_check(artifact, candidate))

    node = _run_node_checks(artifact)
    checks.append(_node_check(node, CHECK_EXPORTS))
    checks.append(_node_check(node, CHECK_EVENTS))
    checks.append(_tool_check(artifact))

    state = compat.check(candidate)
    checks.append(
        _entry(
            CHECK_STATE,
            state["status"],
            state["detail"] + (f"；迁移提示：{state['migration_hint']}" if state.get("migration_hint") else ""),
        )
    )
    checks.append(_smoke_check(candidate))

    checks = sorted(checks, key=lambda item: CHECK_ORDER.index(item["check"]))
    status = _overall(checks)
    return {
        "version": version,
        "status": status,
        "verify_status": status,
        "artifact_dir": str(artifact) if artifact else None,
        "checks": checks,
        "needs_migration": bool(state.get("needs_migration")),
        "migration_hint": state.get("migration_hint") or "",
        "needs_adapter_review": [c["check"] for c in checks if c["status"] == BLOCKED],
    }


def _entry(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status if status in STATUSES else BLOCKED, "detail": detail or "（无 detail）"}


def _overall(checks: list[dict]) -> str:
    if any(item["status"] == FAILED for item in checks):
        return FAILED
    if any(item["status"] == BLOCKED for item in checks):
        return BLOCKED
    return PASSED


def _artifact_check(artifact: Path | None) -> dict:
    if artifact is None:
        return _entry(
            CHECK_ARTIFACT,
            BLOCKED,
            "未提供构建产物目录（artifact_dir 为空）：没有产物就没有可验证的对象",
        )
    index = artifact / _DIST_INDEX
    if not index.exists():
        return _entry(CHECK_ARTIFACT, FAILED, f"缺少构建产物：{index} 不存在")
    size = index.stat().st_size
    if size <= 0:
        return _entry(CHECK_ARTIFACT, FAILED, f"构建产物为空文件：{index}")
    return _entry(CHECK_ARTIFACT, PASSED, f"{index}（{size} 字节）")


def _manifest_check(artifact: Path | None, candidate: dict) -> dict:
    if artifact is None:
        return _entry(CHECK_MANIFEST, BLOCKED, "未提供构建产物目录，无法比对 build-manifest.json")
    path = artifact / "build-manifest.json"
    if not path.exists():
        return _entry(CHECK_MANIFEST, FAILED, f"缺少构建清单：{path} 不存在")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return _entry(CHECK_MANIFEST, FAILED, f"build-manifest.json 不是合法 JSON：{exc}")
    if not isinstance(manifest, dict):
        return _entry(CHECK_MANIFEST, FAILED, "build-manifest.json 顶层不是对象")

    problems: list[str] = []
    version = manifest.get("version")
    if str(version or "") != str(candidate.get("version") or ""):
        problems.append(f"version 不一致：manifest={version!r} 期望={candidate.get('version')!r}")
    commit = candidate.get("commit_sha")
    if commit and str(manifest.get("commit") or "") != str(commit):
        problems.append(f"commit 不一致：manifest={manifest.get('commit')!r} 期望={commit!r}")
    expected_lock = candidate.get("pruned_lock_sha256")
    if expected_lock and manifest.get("pruned_lock_sha256") != expected_lock:
        problems.append(
            f"pruned_lock_sha256 不一致：manifest={manifest.get('pruned_lock_sha256')!r} 期望={expected_lock!r}"
        )
    if not manifest.get("pruned_lock_sha256"):
        problems.append("pruned_lock_sha256 缺失（裁剪后的依赖锁未经记录，无法复现）")
    if problems:
        return _entry(CHECK_MANIFEST, FAILED, "；".join(problems))
    return _entry(
        CHECK_MANIFEST,
        PASSED,
        f"version/manifest 一致（commit={manifest.get('commit')!r}, node={manifest.get('node')!r}）",
    )


def _run_node_checks(artifact: Path | None) -> dict:
    """网关侧（node 才能做）的检查：导出符号 + 事件名。"""
    if artifact is None:
        return {"status": BLOCKED, "detail": "未提供构建产物目录，无法交给网关校验", "checks": []}
    return toolchain.run_verify(artifact, {"symbols": list(EXPECTED_SYMBOLS)})


def _node_check(node: dict, name: str) -> dict:
    status = str(node.get("status") or BLOCKED)
    for item in node.get("checks") or []:
        if str(item.get("check")) == name:
            return _entry(name, str(item.get("status") or BLOCKED), str(item.get("detail") or ""))
    detail = str(node.get("detail") or "") or f"网关未回传「{name}」检查项（整体状态 {status}）"
    if status == BLOCKED and not node.get("checks"):
        return _entry(name, BLOCKED, detail)
    # 网关明确给出了结论但缺这一项：不能当作通过
    return _entry(name, BLOCKED if status == BLOCKED else FAILED, detail)


def _tool_check(artifact: Path | None) -> dict:
    """静态断言：平台白名单的 `sr_*` 工具全部可由候选注册（不是文案断言）。"""
    names = _platform_tool_names()
    if artifact is None:
        return _entry(CHECK_TOOLS, BLOCKED, "未提供构建产物目录，无法断言工具注册能力")
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        return _entry(CHECK_TOOLS, FAILED, f"平台工具白名单自身重复：{duplicated}")
    not_platform = [name for name in names if not name.startswith("sr_")]
    if not_platform:
        return _entry(CHECK_TOOLS, FAILED, f"平台工具白名单含非 sr_* 名称：{not_platform}")

    symbols = exported_symbols(artifact / "packages" / "core" / "dist")
    if not symbols:
        return _entry(CHECK_TOOLS, FAILED, "无法从产物 dist 读出导出符号集合，工具注册能力无法成立")
    missing = [name for name in ("defineTool", "AragonAgent") if name not in symbols]
    if missing:
        return _entry(
            CHECK_TOOLS,
            FAILED,
            f"候选未导出 {missing}：平台白名单的 {len(names)} 个 sr_* 工具无法注册",
        )
    return _entry(
        CHECK_TOOLS,
        PASSED,
        f"平台白名单 {len(names)} 个 sr_* 工具均可由候选注册（defineTool/AragonAgent 均在 {len(symbols)} 个导出内）",
    )


def _smoke_check(candidate: dict) -> dict:
    if candidate.get("smoke_ok"):
        return _entry(CHECK_SMOKE, PASSED, str(candidate.get("smoke_detail") or "冒烟调用成功"))
    return _entry(
        CHECK_SMOKE,
        BLOCKED,
        "未做真实推理验证：本版本不自动执行冒烟推理（需要一次真实模型调用）；"
        "本次结论因此是 blocked —— 确认风险后可带 acknowledge_blocked 手动切换",
    )


def _platform_tool_names() -> list[str]:
    """平台工具白名单（与网关 `policy.ts` 同源的 `TOOL_SPECS`）。"""
    from ..agent.toolspec import TOOL_SPECS

    return [str(item.get("name")) for item in TOOL_SPECS]


_EXPORT_BLOCK = re.compile(r"export\s+(type\s+)?\{([^}]*)\}", re.S)
_EXPORT_DECL = re.compile(
    r"export\s+(?:declare\s+)?(?:abstract\s+)?(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)"
)


def exported_symbols(dist: Path) -> list[str]:
    """从产物 `dist` 的 barrel（`index.d.ts` / `index.js`）读出**运行时**导出符号集合。

    导出块里可能夹注释（上游 barrel 就有），先剥掉 `//` 行注释再按逗号切分。
    """
    symbols: set[str] = set()
    for name in ("index.d.ts", "index.js"):
        path = dist / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for is_type, body in _EXPORT_BLOCK.findall(text):
            if is_type:
                continue
            for raw in re.sub(r"//[^\n]*", "", body).split(","):
                part = raw.strip()
                if not part or part.startswith("type "):
                    continue
                alias = part.split(" as ")[-1].strip()
                if alias:
                    symbols.add(alias)
        for match in _EXPORT_DECL.findall(text):
            symbols.add(match)
    return sorted(symbols)


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


__all__ = [
    "BLOCKED",
    "CHECK_ORDER",
    "EXPECTED_SYMBOLS",
    "FAILED",
    "PASSED",
    "REQUIRED_EXPORTS",
    "conformance_suite",
    "exported_symbols",
    "refresh_capability_status",
]
