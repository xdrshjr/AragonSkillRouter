"""Agent / 流程 DTO（§9.2）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    title: str | None = None
    model_profile: dict | None = None
    permission_profile: dict | None = None


class Attachment(BaseModel):
    file_id: str
    name: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    attachments: list[Attachment] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    queued_ack: bool = True
    retry_of: str | None = None


class StopRequest(BaseModel):
    turn_id: str | None = None


class RetryRequest(BaseModel):
    turn_id: str | None = None


class AgentConfig(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    credential_id: str | None = None
    allowed_providers: list[str] = Field(default_factory=list)
    max_test_calls_per_flow: int = 10
    max_cost_usd_per_flow: float = 1.0
    allowed_window_hours: int = 24


class FlowCreate(BaseModel):
    title: str | None = None
    capability_id: str | None = None
    credential_id: str | None = None
    auto_publish: bool = False
    budget: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    approve: bool
    note: str | None = None
