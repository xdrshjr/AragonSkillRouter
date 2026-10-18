"""handler 注册表与默认超时（§7.8 v2 清单）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from . import (
    agent_turn,
    capability_invoke,
    credential_verify,
    expiry_scan,
    file_parse,
    retention,
    skill_import,
)

DEFAULT_TIMEOUT_S = {
    "capability_invoke_async": 1800,
    "agent_turn": 1800,
    "skill_import": 600,
    "file_parse": 300,
    "credential_verify": 120,
    "engine_upgrade": 3600,
    "retention_sweep": 600,
    "expiry_scan": 300,
    "backup": 1800,
}

Handler = Callable[[dict], Awaitable[None]]


def registry() -> dict[str, Handler]:
    from ...services import backup_job
    from ...upgrade import jobs as upgrade_jobs

    return {
        "capability_invoke_async": capability_invoke.handle,
        "agent_turn": agent_turn.handle,
        "skill_import": skill_import.handle,
        "file_parse": file_parse.handle,
        "credential_verify": credential_verify.handle,
        "retention_sweep": retention.handle,
        "expiry_scan": expiry_scan.handle,
        "engine_upgrade": upgrade_jobs.handle,
        "backup": backup_job.handle,
    }
