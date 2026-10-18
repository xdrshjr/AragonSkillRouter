"""能力 DTO（§7.6 的 definition 结构）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CapabilityCreate(BaseModel):
    key: str = Field(min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    kind: str = "generic.http"
    definition: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    requires_agent_reasoning: bool = False


class CapabilityUpdate(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    requires_agent_reasoning: bool | None = None
    enabled: bool | None = None
    definition: dict[str, Any] | None = None


class CapabilityTestRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    route_id: str | None = None
    mode: str | None = None
    save_payload: bool = False


class PublishRequest(BaseModel):
    version: str | None = None
    changelog: str | None = None


class RollbackRequest(BaseModel):
    to_version: str


class CompatibilityRequest(BaseModel):
    group: str
    confirm: bool = False
    notes: str | None = None


class RouteUpsert(BaseModel):
    route_id: str | None = None
    provider_id: str | None = None
    credential_id: str | None = None
    priority: int = 0
    enabled: bool = True
    model_hint: str | None = None
    request: dict[str, Any] | None = None
    param_map: dict[str, Any] | None = None
    response_map: dict[str, Any] | None = None
    stream: dict[str, Any] | None = None
    async_: dict[str, Any] | None = Field(default=None, alias="async")
    limits: dict[str, Any] | None = None
    errors: list[Any] | None = None
    timeout_ms: int | None = None
    retry: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}
