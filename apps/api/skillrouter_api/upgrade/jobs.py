"""升级作业（worker handler）：检测 / 构建 / 验证 / 切换 / 回滚。

三条语义（§A.3.7）在这里落地：

1. 构建走 `build.build()`（= `toolchain.run_build()`）。工具链不可用 → 步骤 `blocked`、
   `result='aborted'`，**不产生候选行**、也不写 `failed`（那不是候选的问题）。
2. 验证结论写 `engine_upgrade_runs.verify_status` + `blocked_reasons`，与 `result` 分列。
3. 自动切换条件收紧为 `policy == "auto" and verify_status == "passed"`；
   `blocked` → 进「待升级」，需管理员带 `acknowledge_blocked=true` 才切换。
"""

from __future__ import annotations

import time

from sqlalchemy import select

from ..db import db_session
from ..logging import get_logger, redact
from ..models import EngineCandidate
from ..services import settings_runtime
from ..settings import settings
from . import build as build_module
from . import detect, state, switch, verify
from . import rollback as rollback_module

log = get_logger("skillrouter.run")

#: 步骤状态取值：verify 检查项只有三态，但**步骤**可以是 skipped（未部署等）
STEP_SKIPPED = "skipped"


def policy() -> str:
    """运行时生效的升级策略（DB 覆盖环境变量，60s TTL，§A.3.9）。"""
    try:
        value = settings_runtime.get("engine_policy", settings.engine_policy)
    except KeyError:  # pragma: no cover - 白名单含该键
        return settings.engine_policy
    return str(value or settings.engine_policy)


async def handle(payload: dict) -> None:
    action = payload.get("action") or "upgrade"
    if action == "check":
        result = detect.check(triggered_by="worker")
        detect.mark_checked()
        log.info("engine_check_done", extra={"detail": {"created": result["created"]}})
        return
    if action == "rollback":
        await rollback_module.rollback_to_previous(reason="手动回滚请求")
        return

    run_id = payload.get("run_id")
    version = payload.get("version")
    mode = payload.get("mode", "verify_only")
    acknowledge_blocked = bool(payload.get("acknowledge_blocked"))
    if not run_id or not version:
        return
    with db_session() as s:
        candidate_row = s.scalar(
            select(EngineCandidate)
            .where(EngineCandidate.version == version)
            .order_by(EngineCandidate.detected_at.desc())
        )
        candidate = (
            {
                "version": candidate_row.version,
                "commit_sha": candidate_row.commit_sha,
                "provenance": candidate_row.provenance,
                "integrity": None,
                "source_url": None,
            }
            if candidate_row
            else {"version": version, "provenance": "tagged"}
        )

    engine_policy = policy()
    started = time.perf_counter()
    state.mark_step(run_id, "detected", "passed", f"候选 {version}（{candidate['provenance']}）")
    if candidate.get("provenance") == "npm-only" and engine_policy == "auto":
        state.mark_step(
            run_id,
            "detected",
            "passed",
            "npm-only 版本默认不自动部署，本次仅做验证",
        )
        mode = "verify_only"

    built = build_module.build(candidate)
    backend = built.get("status") or ("built" if built.get("ok") else "failed")
    state.mark_step(
        run_id,
        "built",
        "passed" if built["ok"] else backend,
        built.get("detail", ""),
        int((time.perf_counter() - started) * 1000),
    )
    if backend == "blocked":
        # 工具链不可用：没有候选可验证，也没有候选需要标 failed
        detail = str(built.get("detail") or "构建工具链不可用")
        state.set_verify_status(run_id, "blocked", [f"构建工具链不可用：{detail}"])
        state.finish_run(run_id, result="aborted", error=f"构建工具链不可用：{detail}")
        return
    if not built["ok"]:
        state.finish_run(run_id, result="failed", error=f"构建失败：{built.get('stage')}")
        state.set_candidate_status(version, "failed", {"stage": built.get("stage"), "steps": built.get("steps")})
        return

    candidate["artifact_dir"] = built.get("artifact_dir")
    manifest = built.get("manifest") or {}
    if manifest.get("pruned_lock_sha256"):
        candidate["pruned_lock_sha256"] = manifest["pruned_lock_sha256"]

    report = verify.conformance_suite(candidate, built.get("artifact_dir"))
    status = report["status"]
    state.mark_step(
        run_id,
        "verified",
        "passed" if status == "passed" else status,
        "; ".join(f"{c['check']}:{c['status']}" for c in report["checks"]),
    )
    state.set_verify_status(run_id, status, report.get("needs_adapter_review") or [])
    state.set_candidate_status(version, status, report)
    if status == "failed":
        failed_items = [f"{c['check']}：{c['detail']}" for c in report["checks"] if c["status"] == "failed"]
        state.finish_run(run_id, result="failed", error="一致性套件未通过：" + "；".join(failed_items)[:800])
        return

    if mode != "deploy":
        state.mark_step(run_id, "deployed", STEP_SKIPPED, "verify_only 模式不部署")
        state.mark_step(run_id, "healthy", STEP_SKIPPED, "未部署")
        state.finish_run(run_id, result="success")
        return

    if status == "blocked" and engine_policy == "auto" and not acknowledge_blocked:
        reasons = report.get("needs_adapter_review") or []
        detail = (
            "未能验证（blocked）："
            + ("、".join(str(r) for r in reasons) if reasons else "存在 blocked 检查项")
            + "；已进入待升级，确认风险后可手动切换"
        )
        state.mark_step(run_id, "deployed", "blocked", detail)
        state.mark_step(run_id, "healthy", STEP_SKIPPED, "未部署")
        state.finish_run(run_id, result="aborted", error=detail)
        return

    switched = await switch.switch(run_id, candidate, acknowledge_blocked=acknowledge_blocked)
    ok = bool(switched.get("ok"))
    state.mark_step(
        run_id,
        "deployed",
        "passed" if ok else ("blocked" if switched.get("needs_acknowledge") else "failed"),
        switched.get("detail") or switched.get("stage") or "",
    )
    state.mark_step(
        run_id,
        "healthy",
        "passed" if ok else STEP_SKIPPED,
        f"health={redact(str(switched.get('health')), max_len=600)}" if ok else "未通过健康检查",
    )
    if not ok:
        state.finish_run(
            run_id,
            result="aborted" if switched.get("needs_acknowledge") else "failed",
            error=switched.get("detail") or switched.get("stage"),
        )
        return
    state.finish_run(run_id, result="success")


__all__ = ["handle", "policy"]
