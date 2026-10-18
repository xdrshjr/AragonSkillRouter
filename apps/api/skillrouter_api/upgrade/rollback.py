"""回滚：指针回退 + 网关重载 + 健康检查（§12.4-4 第 6 条）。"""

from __future__ import annotations

from sqlalchemy import select

from ..agent.client import get_adapter
from ..db import db_write, now_iso
from ..db.seed import get_state, set_state
from ..logging import get_logger, redact
from ..models import EngineDeployment
from ..settings import settings
from .switch import health_check, write_pointer

log = get_logger("skillrouter.run")


async def rollback_to(version: str, *, reason: str = "", actor: str | None = None) -> dict:
    adapter = get_adapter()
    engine_home = str(settings.engine_home_root / version)
    try:
        reload_result = await adapter.reload_engine(version, engine_home=engine_home)
    except Exception as exc:  # noqa: BLE001
        reload_result = {"ok": False, "stage": "reload", "detail": redact(str(exc), max_len=300)}
    write_pointer(version, engine_home=engine_home)
    health = await health_check(version)
    ok = bool(reload_result.get("ok")) and health.get("ok", False)
    with db_write() as s:
        for row in s.scalars(select(EngineDeployment)):
            if row.version == version and row.status in ("previous", "rolled_back", "failed"):
                row.status = "active"
            elif row.status == "active" and row.version != version:
                row.status = "rolled_back"
        # `engine_pin` 必须描述**回滚目标**：写 `engine_previous_pin` 会让 db 里的 pin
        # 与实际运行的版本分叉（指针是 version、pin 是更早的 P-1 → pin_source 报 env、
        # 下一次升级的基线也错），并留下一个裸 `@`。
        outgoing = get_state(s, "engine_pin", settings.engine_pin)
        commit = s.scalar(
            select(EngineDeployment.commit_sha)
            .where(EngineDeployment.version == version)
            .order_by(EngineDeployment.deployed_at.desc())
        )
        set_state(s, "engine_pin", f"{version}@{commit}" if commit else version)
        # 回滚也是一次切换：记下被替换的版本，留出反向回滚的余地
        set_state(s, "engine_previous_pin", outgoing if outgoing and outgoing != version else "")
    from ..services import audit

    audit.write(
        "engine.rolled_back",
        object_type="engine",
        summary={"to": version, "reason": reason, "ok": ok},
        actor_id=actor,
    )
    log.info("engine_rolled_back", extra={"detail": {"to": version, "ok": ok, "reason": reason}})
    return {"ok": ok, "to_version": version, "reload": reload_result, "health": health, "reason": reason}


async def rollback_to_previous(*, reason: str = "", actor: str | None = None) -> dict:
    from ..db import db_session

    with db_session() as s:
        prev = get_state(s, "engine_previous_pin", "")
        current = get_state(s, "engine_pin", settings.engine_pin)
    if not prev:
        return {
            "ok": False,
            "detail": "没有可回滚的上一版本",
            "current": current,
        }
    version = prev.split("@", 1)[0]
    return await rollback_to(version, reason=reason or "手动回滚", actor=actor)


def history() -> list[dict]:
    from ..db import db_session

    with db_session() as s:
        rows = list(s.scalars(select(EngineDeployment).order_by(EngineDeployment.deployed_at.desc())))
        return [
            {
                "id": r.id,
                "version": r.version,
                "commit_sha": r.commit_sha,
                "provenance": r.provenance,
                "status": r.status,
                "adapter_version": r.adapter_version,
                "deployed_at": r.deployed_at,
                "health": r.health or {},
            }
            for r in rows
        ]


__all__ = ["rollback_to", "rollback_to_previous", "history", "now_iso"]
