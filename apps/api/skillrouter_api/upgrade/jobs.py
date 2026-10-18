"""升级作业（worker handler）：检测 / 构建 / 验证 / 切换 / 回滚。"""

from __future__ import annotations

import time

from ..db import db_session
from ..logging import get_logger, redact
from ..settings import settings
from . import build as build_module
from . import detect, rollback as rollback_module, state, switch, verify

log = get_logger("skillrouter.run")


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
    if not run_id or not version:
        return
    with db_session() as s:
        from sqlalchemy import select

        from ..models import EngineCandidate

        candidate_row = s.scalar(
            select(EngineCandidate).where(EngineCandidate.version == version).order_by(EngineCandidate.detected_at.desc())
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

    started = time.perf_counter()
    state.mark_step(run_id, "detected", "passed", f"候选 {version}（{candidate['provenance']}）")
    if candidate.get("provenance") == "npm-only" and settings.engine_policy == "auto":
        state.mark_step(
            run_id,
            "detected",
            "passed",
            "npm-only 版本默认不自动部署，本次仅做验证",
        )
        mode = "verify_only"

    built = build_module.build(candidate)
    state.mark_step(
        run_id,
        "built",
        "passed" if built["ok"] else "failed",
        built.get("detail", ""),
        int((time.perf_counter() - started) * 1000),
    )
    if not built["ok"]:
        state.finish_run(run_id, result="failed", error=f"构建失败：{built.get('stage')}")
        state.set_candidate_status(version, "failed", {"stage": built.get("stage"), "steps": built.get("steps")})
        return

    report = verify.conformance_suite(candidate, built.get("artifact_dir"))
    state.mark_step(
        run_id,
        "verified",
        "passed" if report["status"] == "passed" else "failed",
        "; ".join(f"{c['check']}:{c['status']}" for c in report["checks"]),
    )
    state.set_candidate_status(version, report["status"], report)
    if report["status"] != "passed":
        state.finish_run(run_id, result="failed", error="一致性套件未通过")
        return

    if mode != "deploy":
        state.mark_step(run_id, "deployed", "skipped", "verify_only 模式不部署")
        state.mark_step(run_id, "healthy", "skipped", "未部署")
        state.finish_run(run_id, result="success")
        return

    switched = await switch.switch(run_id, candidate)
    state.mark_step(
        run_id,
        "deployed",
        "passed" if switched.get("ok") else "failed",
        switched.get("detail") or switched.get("stage") or "",
    )
    state.mark_step(
        run_id,
        "healthy",
        "passed" if switched.get("ok") else "failed",
        f"health={redact(str(switched.get('health')), max_len=600)}",
    )
    if not switched.get("ok"):
        state.finish_run(run_id, result="failed", error=switched.get("detail") or switched.get("stage"))
        return
    state.finish_run(run_id, result="success")
