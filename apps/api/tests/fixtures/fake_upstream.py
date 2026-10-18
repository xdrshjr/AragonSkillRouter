"""可编排的假上游（同步 / 异步 / 流式 / 错误 / 计数）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx


@dataclass
class FakeUpstream:
    """记录每次请求并按脚本返回响应，`calls` 用于断言上游被真实调用几次。"""

    calls: list[dict] = field(default_factory=list)
    script: list[tuple[int, dict | str]] = field(default_factory=list)
    stream_chunks: list[str] = field(default_factory=lambda: ["你", "好"])
    task_status: str = "succeeded"
    task_polls: int = 0
    stream_status: int = 200

    def push(self, status: int, body: dict | str) -> None:
        self.script.append((status, body))

    # ── httpx MockTransport 分发 ───────────────────────────
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content.decode("utf-8", errors="replace"),
            }
        )
        path = request.url.path
        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=self._sse_body(),
            )
        # 脚本优先：任何路径都可被 push() 编排（含凭证探测的 /probe）
        if self.script:
            status, body = self.script.pop(0)
            return self._response(status, body)
        if path.endswith("/v1/generate"):
            return httpx.Response(
                200,
                json={
                    "result": {"text": "假的回复"},
                    "usage": {"input_tokens": 12, "output_tokens": 7},
                },
            )
        if path.endswith("/probe"):
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        if path.endswith("/v1/jobs"):
            return httpx.Response(200, json={"id": "up-task-1", "status": "queued"})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"id": "up-task-1"})
        if "/v1/tasks" in path:
            self.task_polls += 1
            if self.task_status == "pending" and self.task_polls < 3:
                return httpx.Response(200, json={"status": "running", "progress": 30})
            return httpx.Response(
                200,
                json={
                    "status": self.task_status,
                    "result": {"text": "异步结果"},
                    "usage": {"input_tokens": 5, "output_tokens": 9},
                },
            )
        if path.endswith("/v2/generate"):
            return httpx.Response(200, json={"result": {"text": "备用提供商回复"}, "usage": {}})
        return httpx.Response(404, json={"error": "not found"})

    def _sse_body(self) -> bytes:
        frames = []
        for chunk in self.stream_chunks:
            frames.append(f"data: {json.dumps({'delta': chunk})}\n\n")
        frames.append("data: [DONE]\n\n")
        return "".join(frames).encode()

    def _response(self, status: int, body: dict | str) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    @property
    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, path_suffix: str) -> int:
        return sum(1 for call in self.calls if call["url"].endswith(path_suffix))


def patch_httpx(monkeypatch, upstream: FakeUpstream) -> None:
    """把 httpx.AsyncClient / Client 的 transport 换成假上游。"""
    original_async = httpx.AsyncClient
    original_sync = httpx.Client

    def async_factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = upstream.mock
        return original_async(*args, **kwargs)

    def sync_factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = upstream.mock
        return original_sync(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(httpx, "Client", sync_factory)


__all__ = ["FakeUpstream", "patch_httpx"]
