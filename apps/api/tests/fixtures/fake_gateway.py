"""假网关（NDJSON / SSE 事件脚本，§8.2）。"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class FakeGateway:
    available: bool = True
    turns: list[dict] = field(default_factory=list)
    interrupts: list[dict] = field(default_factory=list)
    reloads: list[dict] = field(default_factory=list)
    engine_version: str = "0.2.34"
    reload_ok: bool = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.available:
            raise httpx.ConnectError("gateway down", request=request)
        from skillrouter_api.settings import settings

        expected = {settings.agent_gateway_secret, "s" * 32}
        if request.headers.get("X-Internal-Token") not in expected:
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "engine": {
                        "package": "@aragon-agent/core",
                        "version": self.engine_version,
                        "commit": "0a119a6",
                        "schema_version": 1,
                        "status": "available",
                    },
                    "tools": [
                        "sr_capabilities_list",
                        "sr_capability_get",
                        "sr_test_capability",
                        "sr_request_approval",
                    ],
                    "active_turns": 0,
                    "uptime_s": 42,
                },
            )
        if path == "/internal/turns":
            import json

            body = json.loads(request.content or b"{}")
            self.turns.append(body)
            return httpx.Response(
                200, json={"accepted": True, "ipc_stream": f"/internal/turns/{body.get('turn_id')}/events"}
            )
        if path.endswith("/interrupt"):
            self.interrupts.append({"path": path})
            return httpx.Response(
                200,
                json={
                    "stopped_steps": ["sr_test_capability（已完成）"],
                    "external_pending": ["up-task-9"],
                    "note": "已停止当前轮次；external_pending 中的上游请求可能仍在执行",
                },
            )
        if path.endswith("/steer"):
            return httpx.Response(200, json={"ok": True})
        if path == "/internal/admin/reload":
            import json

            body = json.loads(request.content or b"{}")
            self.reloads.append(body)
            if not self.reload_ok:
                return httpx.Response(
                    200,
                    json={"ok": False, "stage": "start_child", "engine_version": self.engine_version,
                          "detail": "无法启动新引擎子进程"},
                )
            self.engine_version = body.get("target_version", self.engine_version)
            return httpx.Response(
                200, json={"ok": True, "engine_version": self.engine_version, "stage": "healthy", "detail": ""}
            )
        if path.startswith("/internal/credentials/materialize"):
            return httpx.Response(
                200,
                json={
                    "provider_id": "prv_x",
                    "base_url": "https://api.example.com",
                    "kind": "llm",
                    "auth_type": "bearer",
                    "header_name": None,
                    "secret": "sk-short-lived",
                },
            )
        if path == "/internal/capabilities":
            return httpx.Response(200, json={"items": []})
        if path == "/internal/audit":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})


def patch_gateway(monkeypatch, fake: FakeGateway) -> None:
    import httpx as _httpx

    original = _httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = _httpx.MockTransport(fake.handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(_httpx, "AsyncClient", factory)


__all__ = ["FakeGateway", "patch_gateway"]
