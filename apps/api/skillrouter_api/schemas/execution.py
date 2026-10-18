"""执行 / 任务 DTO（§9.3）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InvokeOptions(BaseModel):
    mode: str | None = None
    timeout_ms: int | None = None
    stream: bool | None = None
    preferred_provider: str | None = None
    max_cost_usd: float | None = None
    idempotency_key: str | None = None
    callback_url: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class InvokeRequest(BaseModel):
    capability_version: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    options: InvokeOptions = Field(default_factory=InvokeOptions)


class InvokeResponse(BaseModel):
    request_id: str
    status: str
    capability: dict
    provider: dict | None = None
    output: Any = None
    output_files: list[dict] = Field(default_factory=list)
    usage: dict | None = None
    timings: dict | None = None
    error: dict | None = None


class CancelRequest(BaseModel):
    reason: str | None = None


class CallLogItem(BaseModel):
    request_id: str
    status: str
    capability_key: str
    mode: str
    latency_ms: int | None = None
    created_at: str
