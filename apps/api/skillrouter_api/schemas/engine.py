"""引擎 DTO（§9.2 / §12.4）。"""

from __future__ import annotations

from pydantic import BaseModel


class UpgradeRequest(BaseModel):
    version: str
    mode: str = "verify_only"
    #: `verify_status='blocked'` 时仍要切换，必须显式确认风险（§A.3.7-1-4）
    acknowledge_blocked: bool = False


class RollbackEngineRequest(BaseModel):
    to_version: str | None = None


class SettingsUpdate(BaseModel):
    payload_policy: str | None = None
    retention_days: int | None = None
    fetch_allowlist: list[str] | None = None
    engine_policy: str | None = None
    default_budget_usd: float | None = None
    budget_usd: float | None = None
    agent_model: dict | None = None
