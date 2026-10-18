"""假网关（NDJSON / SSE 事件脚本，§8.2）。

同时提供同步客户端（`httpx.Client`，供 `upgrade/toolchain.py` 的构建/验证调用）
与异步客户端（`httpx.AsyncClient`，供适配层）的 MockTransport。
"""

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
    #: `/healthz` 回显的指针来源（`pointer` | `env`）与解析路径（§A.5.3）
    pin_source: str = "env"
    resolved_path: str = ""
    #: 构建/验证端点的调用记录与可编程结果（None = 用默认成功响应）
    builds: list[dict] = field(default_factory=list)
    verifies: list[dict] = field(default_factory=list)
    build_result: dict | None = None
    verify_result: dict | None = None

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
                    "pin_source": self.pin_source,
                    "resolved_path": self.resolved_path,
                    "maintenance": False,
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
            previous = self.engine_version
            self.engine_version = body.get("target_version") or body.get("version") or self.engine_version
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "engine_version": self.engine_version,
                    "stage": "healthy",
                    "detail": "",
                    "pin_source": "pointer" if body.get("engine_home") else self.pin_source,
                    "previous_version": previous,
                },
            )
        if path == "/internal/admin/build":
            import json

            body = json.loads(request.content or b"{}")
            self.builds.append(body)
            if self.build_result is not None:
                return httpx.Response(200, json=self.build_result)
            version = body.get("version")
            provenance = body.get("provenance") or "tagged"
            artifact = settings.builds_dir / f"{version}-{provenance}" / "runtime"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "status": "built",
                    "stage": "built",
                    "detail": f"构建完成：{artifact}",
                    "artifact_dir": str(artifact),
                    "manifest": {"version": version, "commit": body.get("commit_sha"),
                                 "pruned_lock_sha256": "sha256:fake", "node": "v22.0.0"},
                    "steps": [{"name": "clone", "code": 0, "detail": ""}],
                },
            )
        if path == "/internal/admin/verify-artifact":
            import json

            body = json.loads(request.content or b"{}")
            self.verifies.append(body)
            if self.verify_result is not None:
                return httpx.Response(200, json=self.verify_result)
            symbols = list(body.get("expected_symbols") or [])
            return httpx.Response(
                200,
                json={
                    "status": "passed",
                    "checks": [
                        {"check": "导出清单兼容", "status": "passed",
                         "detail": f"导出符号集合 ⊇ {symbols}"},
                        {"check": "事件映射契约", "status": "passed",
                         "detail": "候选事件名集合 ⊇ 网关 ALL_EVENT_NAMES"},
                    ],
                },
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
    """把同步与异步 httpx 客户端都指到假网关（toolchain 用同步，适配层用异步）。"""
    import httpx as _httpx

    sync_original = _httpx.Client
    async_original = _httpx.AsyncClient

    def sync_factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = _httpx.MockTransport(fake.handler)
        return sync_original(*args, **kwargs)

    def async_factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = _httpx.MockTransport(fake.handler)
        return async_original(*args, **kwargs)

    monkeypatch.setattr(_httpx, "Client", sync_factory)
    monkeypatch.setattr(_httpx, "AsyncClient", async_factory)


__all__ = ["FakeGateway", "patch_gateway"]
