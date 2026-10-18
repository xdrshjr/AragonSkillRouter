"""部署切换（§A.3.7-3）：先备份 → 装入 home → 网关热切换 → 健康检查 → 写指针。

顺序是硬约束，不能调换：

1. `backup_job.quick_backup(label="pre-upgrade")` —— 失败即 abort，**不写指针**、
   `backup_file_id` 保持 `None`；
2. `install_home.install(artifact_dir, version)` —— 装到
   `AGENT_ENGINE_HOME_ROOT/<version>/node_modules/@aragon-agent/core`（幂等）；
3. `POST /internal/admin/reload`（**既有路径**，不新造 `/internal/engine/*`）带
   `{version, engine_home}`；失败 → 指针写回上一版本，`result='failed'`；
4. 成功 → 写 `current.json` + `engine_upgrade_runs.backup_file_id` +
   `system_state.engine_pin` + `/healthz` 连续 3 次版本匹配，然后异步触发一次 gc。

`verify_status` 是候选结论，`result` 是本次运行怎么结束的 —— 本模块**只写 `result`**，
永不顶替 `verify_status`（§A.6.2 I5）。
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from ..agent.client import get_adapter
from ..db import db_session, db_write, now_iso, uuid_pk
from ..db.seed import get_state, set_state
from ..errors import Code
from ..logging import get_logger, redact
from ..models import EngineDeployment, EngineUpgradeRun
from ..settings import settings

log = get_logger("skillrouter.run")

HEALTH_ATTEMPTS = 3
HEALTH_INTERVAL_S = 2

#: 后台 gc 任务的强引用（否则任务可能被 GC 掉）
_GC_TASKS: set[asyncio.Task] = set()


def write_pointer(
    version: str,
    provenance: str = "tagged",
    *,
    engine_home: str | None = None,
    backup_file_id: str | None = None,
    schema_version: int = 1,
) -> dict:
    """`<data>/engine-runtime/current.json` 是唯一实现（不依赖 symlink/junction）。

    网关侧读同一个文件（`AGENT_ENGINE_RUNTIME_DIR/current.json`）并回显 `pin_source`。
    """
    settings.engine_runtime_dir.mkdir(parents=True, exist_ok=True)
    path = settings.engine_runtime_dir / "current.json"
    home = engine_home or str(settings.engine_home_root / version)
    payload = {
        "version": version,
        "pin": f"{version}@{schema_version}",
        "schema_version": schema_version,
        "provenance": provenance,
        "artifact": str(settings.builds_dir / f"{version}-{provenance}" / "runtime"),
        "home": home,
        "engine_home": home,
        "adapter_version": "1",
        "backup_file_id": backup_file_id,
        "switched_at": now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_pointer() -> dict:
    path = settings.engine_runtime_dir / "current.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def pin_source() -> str:
    """`pointer`（`current.json` 与 db 里的 pin 一致）/ `env`（回落环境变量）。"""
    pointer = read_pointer()
    version = str(pointer.get("version") or "")
    if not version:
        return "env"
    with db_session() as s:
        current = get_state(s, "engine_pin", settings.engine_pin).split("@", 1)[0]
    return "pointer" if version == current else "env"


def resolved_path(version: str | None = None) -> str:
    """当前解析到的引擎包目录（「装了但没生效」的排查入口，§A.5.3）。"""
    from . import install_home

    target = version or str(read_pointer().get("version") or "")
    if not target:
        with db_session() as s:
            target = get_state(s, "engine_pin", settings.engine_pin).split("@", 1)[0]
    return str(install_home.target(target))


async def health_check(expected_version: str) -> dict:
    adapter = get_adapter()
    attempts = []
    for index in range(HEALTH_ATTEMPTS):
        try:
            health = await adapter.health()
            engine = health.get("engine") or {}
            actual = str(engine.get("version") or "")
            # `/healthz` 在引擎不可用时会把 version 回落成 pin：只比版本号会让
            # 「引擎起不来但版本对得上」通过健康门，因此必须同时要求 `ok is not False`（§A.3.7-3-4）
            healthy = health.get("ok") is not False and actual == expected_version
            attempts.append(
                {
                    "attempt": index + 1,
                    "ok": healthy,
                    "actual": actual,
                    "engine_status": engine.get("status"),
                    "pin_source": health.get("pin_source"),
                    "resolved_path": health.get("resolved_path"),
                }
            )
            if healthy:
                return {"ok": True, "attempts": attempts, "health": health}
        except Exception as exc:  # noqa: BLE001
            attempts.append({"attempt": index + 1, "ok": False, "error": redact(str(exc), max_len=200)})
        await asyncio.sleep(HEALTH_INTERVAL_S)
    return {"ok": False, "attempts": attempts}


def _run_brief(run_id: str) -> dict:
    with db_session() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            return {}
        return {"verify_status": row.verify_status, "blocked_reasons": list(row.blocked_reasons or [])}


def _finish(run_id: str, result: str, error: str | None = None) -> None:
    """只写 `result`/`error`/`finished_at`；`verify_status` 一律不动。"""
    with db_write() as s:
        row = s.get(EngineUpgradeRun, run_id)
        if row is None:
            return
        row.result = result
        row.error = error
        row.finished_at = now_iso()


def default_artifact(version: str, provenance: str = "tagged") -> str | None:
    candidate = settings.builds_dir / f"{version}-{provenance}" / "runtime"
    return str(candidate) if candidate.exists() else None


async def switch(run_id: str, candidate: dict, *, acknowledge_blocked: bool = False) -> dict:
    """执行一次切换；返回 `{ok, stage, detail, …}`（失败路径都带原因，不抛异常）。"""
    from ..services import backup_job
    from . import install_home
    from . import rollback as rollback_module

    target_version = candidate["version"]
    provenance = candidate.get("provenance", "tagged")
    run = _run_brief(run_id)
    verify_status = run.get("verify_status")

    if verify_status == "failed":
        detail = "候选验证未通过（verify_status=failed），不得切换；请先修复失败项"
        _finish(run_id, "failed", detail)
        return {"ok": False, "stage": "verify", "detail": detail, "code": Code.ENGINE_SWITCH_FAILED}
    if verify_status == "blocked" and not acknowledge_blocked:
        reasons = run.get("blocked_reasons") or []
        detail = (
            "候选未能验证（verify_status=blocked；"
            + ("、".join(str(r) for r in reasons) if reasons else "存在 blocked 检查项")
            + "）；确认风险后带 acknowledge_blocked=true 才能切换"
        )
        _finish(run_id, "aborted", detail)
        return {
            "ok": False,
            "stage": "verify",
            "detail": detail,
            "code": Code.ENGINE_SWITCH_FAILED,
            "needs_acknowledge": True,
        }

    with db_session() as s:
        from_pin = get_state(s, "engine_pin", settings.engine_pin)
    from_version = from_pin.split("@", 1)[0]

    # ── 1) 先备份：失败即中止（不写指针、不装 home、backup_file_id 保持 None）────────
    try:
        backup = backup_job.quick_backup(label="pre-upgrade")
    except Exception as exc:  # noqa: BLE001 - 备份失败必须变成「不切换」，不是崩溃
        detail = f"升级前备份失败，已中止切换（未写入指针）：{redact(str(exc), max_len=200)}"
        _finish(run_id, "failed", detail)
        log.error("engine_switch_backup_failed", extra={"detail": {"version": target_version, "error": detail}})
        return {
            "ok": False,
            "stage": "backup",
            "detail": detail,
            "code": Code.ENGINE_SWITCH_FAILED,
            "backup_file_id": None,
            "pointer_changed": False,
        }
    backup_file_id = backup.get("file_id")

    deployment_id = uuid_pk("egd")
    with db_write() as s:
        s.add(
            EngineDeployment(
                id=deployment_id,
                version=target_version,
                commit_sha=candidate.get("commit_sha"),
                provenance=provenance,
                source_url=candidate.get("source_url"),
                integrity=candidate.get("integrity"),
                adapter_version="1",
                status="pending",
                health={"backup": {"file_id": backup_file_id, "label": backup.get("label")}},
            )
        )

    # ── 2) 装入 home（幂等）─────────────────────────────────────────────────────
    artifact_dir = candidate.get("artifact_dir") or default_artifact(target_version, provenance)
    install: dict = {"action": "skipped", "detail": "无构建产物目录"}
    if artifact_dir:
        try:
            install = install_home.install(artifact_dir, target_version)
        except (install_home.InstallError, OSError) as exc:
            detail = f"装入引擎 home 失败，已中止切换（未写入指针）：{redact(str(exc), max_len=200)}"
            _finish(run_id, "failed", detail)
            with db_write() as s:
                row = s.get(EngineDeployment, deployment_id)
                if row:
                    row.status = "failed"
                    row.health = {"backup": backup, "install": {"status": "failed", "detail": detail}}
            return {
                "ok": False,
                "stage": "install_home",
                "detail": detail,
                "code": Code.ENGINE_SWITCH_FAILED,
                "backup_file_id": backup_file_id,
            }
    else:
        log.warning(
            "engine_switch_without_artifact",
            extra={"detail": {"version": target_version, "why": "候选无产物目录：沿用网关自带 node_modules"}},
        )
    engine_home = str(install.get("home") or (settings.engine_home_root / target_version))

    # ── 3) 网关热切换（既有路径 /internal/admin/reload）──────────────────────────
    adapter = get_adapter()
    try:
        reload_result = await adapter.reload_engine(target_version, engine_home=engine_home)
    except Exception as exc:  # noqa: BLE001
        reload_result = {"ok": False, "stage": "reload", "detail": redact(str(exc), max_len=300)}
    if not reload_result.get("ok", False):
        detail = reload_result.get("detail") or "网关未确认切换"
        # 指针绝不能指向一个没被确认的版本：网关重启会按它启动，
        # 否则「旧引擎继续服务 / engine_pin 未改变」这句话是假的（§12.4 可靠性）。
        pointer_restored = False
        try:
            write_pointer(
                from_version,
                "previous",
                engine_home=str(settings.engine_home_root / from_version),
            )
            pointer_restored = True
        except OSError as exc:  # pragma: no cover - 磁盘不可写
            log.error("pointer_restore_failed", extra={"detail": redact(str(exc), max_len=200)})
        with db_write() as s:
            row = s.get(EngineDeployment, deployment_id)
            if row:
                row.status = "failed"
                row.health = {"backup": backup, "install": install, "reload": reload_result}
            run_row = s.get(EngineUpgradeRun, run_id)
            if run_row:
                run_row.backup_file_id = backup_file_id
        _finish(run_id, "failed", detail)
        return {
            "ok": False,
            "stage": reload_result.get("stage", "reload"),
            "detail": detail,
            "code": Code.ENGINE_SWITCH_FAILED,
            "deployment_id": deployment_id,
            "backup_file_id": backup_file_id,
            "pointer_restored": pointer_restored,
            "note": "切换未确认，指针已指回上一版本；engine_pin 未改变",
        }

    # ── 4) 健康检查 → 写指针/审计 ───────────────────────────────────────────────
    health = await health_check(target_version)
    if not health["ok"]:
        rollback = await rollback_module.rollback_to(from_version, reason="健康检查未通过")
        with db_write() as s:
            row = s.get(EngineDeployment, deployment_id)
            if row:
                row.status = "rolled_back"
                row.health = {"backup": backup, "install": install, "health": health}
            run_row = s.get(EngineUpgradeRun, run_id)
            if run_row:
                run_row.backup_file_id = backup_file_id
        _finish(run_id, "failed", f"健康检查未确认版本 {target_version}")
        return {
            "ok": False,
            "stage": "health_check",
            "detail": f"健康检查未确认版本 {target_version}",
            "health": health,
            "rollback": rollback,
            "deployment_id": deployment_id,
            "backup_file_id": backup_file_id,
        }

    pointer = write_pointer(
        target_version,
        provenance,
        engine_home=engine_home,
        backup_file_id=backup_file_id,
    )
    with db_write() as s:
        new_row = s.get(EngineDeployment, deployment_id)
        if new_row:
            new_row.status = "active"
            new_row.deployed_at = now_iso()
            new_row.health = {"backup": backup, "install": install, "health": health, "pointer": pointer}
        for other in s.scalars(select(EngineDeployment).where(EngineDeployment.status == "active")):
            if other.id != deployment_id:
                other.status = "previous"
        set_state(s, "engine_previous_pin", from_pin)
        set_state(s, "engine_pin", f"{target_version}@{candidate.get('commit_sha') or ''}".rstrip("@"))
        run_row = s.get(EngineUpgradeRun, run_id)
        if run_row:
            run_row.backup_file_id = backup_file_id
            run_row.result = "success"
            run_row.finished_at = now_iso()
    from .verify import refresh_capability_status

    refresh_capability_status(target_version)
    from ..services import audit

    audit.write(
        "engine.switched",
        object_type="engine",
        object_id=deployment_id,
        summary={"from": from_version, "to": target_version, "pointer": pointer},
    )
    trigger_gc()
    return {
        "ok": True,
        "stage": "healthy",
        "deployment_id": deployment_id,
        "backup_file_id": backup_file_id,
        "engine_home": engine_home,
        "pointer": pointer,
        "health": health,
    }


def trigger_gc() -> None:
    """成功后异步回收一次：回收失败只记日志，不影响切换结果。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 同步上下文（脚本/测试）没有 loop，跳过
        return
    task = loop.create_task(_gc_job())
    _GC_TASKS.add(task)
    task.add_done_callback(_GC_TASKS.discard)


async def _gc_job() -> None:
    from . import gc

    try:
        await asyncio.to_thread(gc.collect)
    except Exception as exc:  # noqa: BLE001 - 回收是清洁工作
        log.error("engine_gc_failed", extra={"detail": {"error": redact(str(exc), max_len=200)}})


__all__ = [
    "HEALTH_ATTEMPTS",
    "default_artifact",
    "health_check",
    "pin_source",
    "read_pointer",
    "resolved_path",
    "switch",
    "trigger_gc",
    "write_pointer",
]
