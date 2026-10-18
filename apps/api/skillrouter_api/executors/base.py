"""执行器协议与统一结果（§7.6）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ExecResult:
    request_id: str
    status: str  # succeeded | failed | status_unknown
    output: Any = None
    output_files: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: dict | None = None
    provider_id: str | None = None
    http_status: int | None = None
    timings: dict = field(default_factory=dict)
    request_preview: str | None = None
    response_preview: str | None = None
    request_fingerprint: str = ""
    retries: int = 0
    upstream_task_id: str | None = None

    def as_response(self, capability: dict, provider: dict | None) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "capability": capability,
            "provider": provider,
            "output": self.output,
            "output_files": self.output_files,
            "usage": self.usage,
            "timings": self.timings,
            "error": self.error,
        }


class Executor(Protocol):
    async def invoke(self, ctx: dict) -> ExecResult:  # pragma: no cover - protocol
        ...


MODES = ("sync", "async", "stream")

#: 上游状态码 → 平台错误码（未被 errors[] 命中时的兜底）
STATUS_FALLBACK = {
    400: "UPSTREAM_BAD_REQUEST",
    401: "UPSTREAM_AUTH",
    403: "UPSTREAM_AUTH",
    404: "UPSTREAM_BAD_REQUEST",
    402: "UPSTREAM_QUOTA",
    429: "UPSTREAM_RATE_LIMIT",
    500: "UPSTREAM_UNAVAILABLE",
    502: "UPSTREAM_UNAVAILABLE",
    503: "UPSTREAM_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}
