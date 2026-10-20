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


# ═══════════════════════════════════════════════════════════════════════
# 四种 LLM 协议的假上游（spec §4.1 / R-07：**扩展**本文件，不新增文件）
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class FakeLlmUpstream:
    """按协议形状回答的假 LLM 上游（模型清单 + 一次最小生成）。

    `reject_auth=True` 时对任何请求回 401 —— 用于验证「鉴权失败必须给出
    `UPSTREAM_AUTH` + 处置建议」，而不是伪装成功或 500。
    """

    protocol: str = "openai"
    #: `custom` 挂在子路径上（例：`/openai`），用于验证 base 已含路径段的拼接
    base_path: str = ""
    model_ids: list[str] = field(default_factory=lambda: ["fake-chat-1", "fake-embed-1"])
    reject_auth: bool = False
    #: anthropic 清单分页：给了 `page_size` 就按 `has_more`/`last_id` 续页
    page_size: int = 0
    completion_text: str = "正常"
    #: 让清单端点回 500（用于 `cache-stale` 降级用例）
    fail_models: bool = False
    calls: list[dict] = field(default_factory=list)

    # ── 请求分发 ────────────────────────────────────────────
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": request.content.decode("utf-8", errors="replace"),
            }
        )
        if self.reject_auth:
            return _auth_error(self.protocol)
        missing = self._missing_auth(request)
        if missing:
            return _auth_error(self.protocol, missing)
        path = self._strip_base(request.url.path)
        if request.method == "GET" and self._is_models_path(path):
            if self.fail_models:
                return httpx.Response(500, json={"error": {"message": "upstream boom"}})
            return self._models(request, path)
        if request.method == "POST":
            return self._completion(path)
        return httpx.Response(404, json={"error": "not found"})

    @property
    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, path_suffix: str) -> int:
        return sum(1 for call in self.calls if call["url"].split("?")[0].endswith(path_suffix))

    @property
    def models_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "GET"]

    @property
    def completion_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "POST"]

    # ── 内部 ────────────────────────────────────────────────
    def _strip_base(self, path: str) -> str:
        if self.base_path and path.startswith(self.base_path):
            return path[len(self.base_path) :] or "/"
        return path

    def _is_models_path(self, path: str) -> bool:
        return path.rstrip("/").endswith("models")

    def _missing_auth(self, request: httpx.Request) -> str | None:
        """按协议校验认证头：缺了就像真实上游那样 401（避免「所有协议都看起来通」）。"""
        headers = {key.lower(): value for key, value in request.headers.items()}
        if self.protocol in ("anthropic",):
            if not headers.get("x-api-key"):
                return "x-api-key"
            if not headers.get("anthropic-version"):
                return "anthropic-version"
            return None
        if self.protocol == "google":
            if not headers.get("x-goog-api-key"):
                return "x-goog-api-key"
            return None
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return "authorization"
        return None

    def _models(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.protocol == "anthropic":
            return self._anthropic_models(request)
        if self.protocol == "google":
            rows = [
                {
                    "name": f"models/{model_id}",
                    "displayName": model_id.replace("-", " ").title(),
                    "inputTokenLimit": 1_048_576 if "pro" in model_id else 32_768,
                    "supportedGenerationMethods": ["generateContent"],
                }
                for model_id in self.model_ids
            ]
            return httpx.Response(200, json={"models": rows})
        rows = [
            {
                "id": model_id,
                "created": 1_760_000_000,
                "owned_by": "fake",
                "name": model_id,
            }
            for model_id in self.model_ids
        ]
        return httpx.Response(200, json={"object": "list", "data": rows})

    def _anthropic_models(self, request: httpx.Request) -> httpx.Response:
        ids = list(self.model_ids)
        after_id = request.url.params.get("after_id")
        if after_id and after_id in ids:
            start = ids.index(after_id) + 1
        else:
            start = 0
        page_size = self.page_size if self.page_size > 0 else len(ids)
        window = ids[start : start + page_size] if page_size else ids[start:]
        has_more = start + len(window) < len(ids)
        payload = {
            "data": [
                {
                    "id": model_id,
                    "type": "model",
                    "display_name": model_id.replace("-", " ").title(),
                    "created_at": "2025-09-29T00:00:00Z",
                }
                for model_id in window
            ],
            "has_more": has_more,
            "first_id": window[0] if window else None,
            "last_id": window[-1] if window else None,
        }
        return httpx.Response(200, json=payload)

    def _completion(self, path: str) -> httpx.Response:
        if self.protocol == "anthropic":
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": self.completion_text}],
                },
            )
        if self.protocol == "google":
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": self.completion_text}]}}]},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": self.completion_text}}]},
        )


def _auth_error(protocol: str, missing: str | None = None) -> httpx.Response:
    message = "API key not valid" if protocol == "google" else "invalid x-api-key"
    if missing:
        message = f"missing header: {missing}"
    return httpx.Response(
        401,
        json={"error": {"code": 401, "message": message, "type": "authentication_error"}},
    )


def _serve(
    protocol: str,
    port: int,
    base_path: str = "",
    *,
    model_ids: list[str] | None = None,
    reject_auth: bool = False,
) -> None:
    """把假上游跑成一个真实的 HTTP 服务（供 §7.3 手工验收与 E-01 使用）。"""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    upstream = FakeLlmUpstream(protocol=protocol, base_path=base_path)
    if model_ids:
        # CLI 的 `--models` 必须真的生效：否则「列举几个模型」这类验收会对着默认值自欺
        upstream.model_ids = list(model_ids)
    upstream.reject_auth = bool(reject_auth)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self, method: str) -> None:  # noqa: ANN001
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            request = httpx.Request(
                method,
                f"http://127.0.0.1:{port}{self.path}",
                headers=dict(self.headers),
                content=body,
            )
            response = upstream.handler(request)
            payload = response.content
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() in ("content-length", "content-type"):
                    continue
                self.send_header(key, value)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, *args) -> None:  # noqa: ANN002
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(
        json.dumps(
            {"ok": True, "protocol": protocol, "base_url": f"http://127.0.0.1:{port}{base_path}"},
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """`python -m tests.fixtures.fake_upstream --protocol anthropic --port 18901`"""
    import argparse

    parser = argparse.ArgumentParser(
        description="起一个按指定 LLM 协议形状回答的本地假上游（无需任何真实凭证）"
    )
    parser.add_argument(
        "--protocol",
        default="openai",
        choices=["openai", "anthropic", "google", "custom"],
        help="要模拟的线协议（custom = OpenAI 兼容网关挂在 --base-path 下）",
    )
    parser.add_argument("--port", type=int, default=18901, help="监听端口（默认 18901）")
    parser.add_argument(
        "--base-path",
        default="",
        help="custom 网关的子路径前缀，例如 /openai（其它协议无需设置）",
    )
    parser.add_argument(
        "--models",
        default="fake-chat-1,fake-embed-1",
        help="逗号分隔的模型 id 列表",
    )
    parser.add_argument("--reject-auth", action="store_true", help="一律回 401（验证鉴权失败）")
    args = parser.parse_args(argv)
    if args.protocol == "custom" and not args.base_path:
        args.base_path = "/openai"
    model_ids = [item.strip() for item in str(args.models).split(",") if item.strip()]
    _serve(
        args.protocol,
        args.port,
        args.base_path,
        model_ids=model_ids,
        reject_auth=args.reject_auth,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = ["FakeLlmUpstream", "FakeUpstream", "main", "patch_httpx"]
