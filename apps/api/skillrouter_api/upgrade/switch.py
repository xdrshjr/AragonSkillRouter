"""部署切换：指针文件 + 网关热切换 + 健康检查（§12.4-4）。"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from ..agent.client import get_adapter
from ..db import db_session, db_write, now_iso, uuid_pk
from ..db.seed import get_state, set_state
from ..logging import get_logger, redact
from ..models import EngineDeployment, EngineUpgradeRun
from ..settings import settings

log = get_logger("skillrouter.run")

HEALTH_ATTEMPTS = 3
HEALTH_INTERVAL_S = 2


def write_pointer(version: str, provenance: str = "tagged") -> dict:
    """`var/engine-runtime/current.json` 是唯一实现（不依赖 symlink/junction）。"""
    settings.engine_runtime_dir.mkdir(parents=True, exist_ok=True)
    path = settings.engine_runtime_dir / "current.json"
    payload = {
        "version": version,
        "provenance": provenance,
        "artifact": str(settings.builds_dir / f"{version}-{provenance}" / "runtime"),
        "home": str(settings.aragon_home / "engines" / version),
        "adapter_version": "1",
        "switched_at": now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_pointer() -> dict:
    path = settings.engine_runtime_dir / "current.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


async def health_check(expected_version: str) -> dict:
    adapter = get_adapter()
    attempts = []
    for index in range(HEALTH_ATTEMPTS):
        try:
            health = await adapter.health()
            engine = health.get("engine") or {}
            actual = str(engine.get("version") or "")
            attempts.append({"attempt": index + 1, "ok": actual == expected_version, "actual": actual})
            if actual == expected_version:
                return {"ok": True, "attempts": attempts, "health": health}
        except Exception as exc:  # noqa: BLE001
            attempts.append({"attempt": index + 1, "ok": False, "error": redact(str(exc), max_len=200)})
        await asyncio.sleep(HEALTH_INTERVAL_S)
    return {"ok": False, "attempts": attempts}


async def switch(run_id: str, candidate: dict) -> dict:
    """先备份 → 写部署行与指针 → 调网关 reload → 健康检查；失败即回滚。"""
    from . import rollback as rollback_module

    target_version = candidate["version"]
    provenance = candidate.get("provenance", "tagged")
    with db_session() as s:
        from_pin = get_state(s, "engine_pin", settings.engine_pin)
    from_version = from_pin.split("@", 1)[0]

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
                health={},
            )
        )
    pointer = write_pointer(target_version, provenance)
    adapter = get_adapter()
    try:
        reload_result = await adapter.reload_engine(target_version)
    except Exception as exc:  # noqa: BLE001
        reload_result = {"ok": False, "stage": "reload", "detail": redact(str(exc), max_len=300)}
    if not reload_result.get("ok", False):
        detail = reload_result.get("detail") or "网关未确认切换"
        # 指针已指向未验证版本：网关重启会按它启动，必须显式指回上一版本，
        # 否则「旧引擎继续服务 / engine_pin 未改变」这句话是假的（§12.4 可靠性）。
        previous_pointer = None
        try:
            previous_pointer = write_pointer(from_version, "previous")
        except OSError as exc:  # pragma: no cover - 磁盘不可写
            log.error("pointer_restore_failed", extra={"detail": redact(str(exc), max_len=200)})
        with db_write() as s:
            row = s.get(EngineDeployment, deployment_id)
            if row:
                row.status = "failed"
                row.health = {"reload": reload_result}
        return {
            "ok": False,
            "stage": reload_result.get("stage", "reload"),
            "detail": detail,
            "deployment_id": deployment_id,
            "pointer_restored": bool(previous_pointer),
            "note": "切换未确认，指针已指回上一版本；engine_pin 未改变",
        }

    health = await health_check(target_version)
    if not health["ok"]:
        rollback = await rollback_module.rollback_to(from_version, reason="健康检查未通过")
        with db_write() as s:
            row = s.get(EngineDeployment, deployment_id)
            if row:
                row.status = "rolled_back"
                row.health = health
        return {
            "ok": False,
            "stage": "health_check",
            "detail": f"健康检查未确认版本 {target_version}",
            "health": health,
            "rollback": rollback,
            "deployment_id": deployment_id,
        }

    with db_write() as s:
        new_row = s.get(EngineDeployment, deployment_id)
        if new_row:
            new_row.status = "active"
            new_row.deployed_at = now_iso()
            new_row.health = health
        for other in s.scalars(select(EngineDeployment).where(EngineDeployment.status == "active")):
            if other.id != deployment_id:
                other.status = "previous"
        set_state(s, "engine_previous_pin", from_pin)
        set_state(s, "engine_pin", f"{target_version}@{candidate.get('commit_sha') or ''}".rstrip("@"))
        run = s.get(EngineUpgradeRun, run_id)
        if run:
            run.result = "success"
            run.finished_at = now_iso()
    from .verify import refresh_capability_status

    refresh_capability_status(target_version)
    from ..services import audit

    audit.write(
        "engine.switched",
        object_type="engine",
        object_id=deployment_id,
        summary={"from": from_version, "to": target_version, "pointer": pointer},
    )
    return {"ok": True, "stage": "healthy", "deployment_id": deployment_id, "health": health}


__all__ = ["switch", "write_pointer", "read_pointer", "health_check"]
