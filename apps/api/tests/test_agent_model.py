"""Agent 模型配置契约与轮次请求形状（真实缺陷的回归测试）。

三个缺陷都来自同一次真实联调（真实 GLM 凭证 + 真实网关）：

1. 平台把 `agent_model.model`（字符串）直接塞进轮次请求的 `model` 字段，且从不发
   `credential_refs` → 网关回 `TURN_CONFLICT: no model configured for this turn`；
2. `worker/handlers/agent_turn.py::ingest_stream` 没有任何调用方 → 网关的事件永远
   没有落库，面板 0 事件、轮次永远 `running`；
3. `PATCH /agent/config` 接受任意 dict（`provider=zhipu`、写错的 `credential_id`
   都能存进去），错误要等到发消息时才暴露。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skillrouter_api.agent import sessions as agent_sessions
from skillrouter_api.agent.client import AragonAgentAdapter, TurnRequest
from skillrouter_api.db import db_session
from skillrouter_api.models import AgentEventRow, AgentTurn
from skillrouter_api.services import agent_model
from skillrouter_api.services import settings as settings_service
from skillrouter_api.worker.handlers import agent_turn as agent_turn_handler
from tests.fixtures.fake_gateway import FakeGateway, patch_gateway


def _run(coro):
    """运行协程，并把事件循环还给线程。

    不用 `_run()`：它会清空线程的 current event loop，而既有测试
    （tests/test_budget_accounting.py）仍依赖 `asyncio.get_event_loop()`，
    结果是「谁先跑」决定成败的顺序性失败。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture()
def gateway(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    patch_gateway(monkeypatch, fake)
    return fake


@pytest.fixture()
def configured(client, admin, provider, credential) -> dict:
    """把真实形状的 Agent 配置写进设置（provider=anthropic + 显式凭证）。"""
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "provider": "anthropic",
            "model": "glm-5.3-flash",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "credential_id": credential["id"],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── 设置 → 网关请求的翻译 ──────────────────────────────────────


def test_resolve_produces_gateway_model_ref(configured, credential) -> None:
    resolution = agent_model.resolve()
    assert resolution.ready, resolution.problems
    assert resolution.model == {
        "providerId": "anthropic",
        "modelId": "glm-5.3-flash",
        "baseUrl": "https://open.bigmodel.cn/api/anthropic",
    }
    # 网关只认识协议族 key，不是平台提供商 UUID
    assert resolution.credential_refs == {"anthropic": credential["id"]}
    assert resolution.credential is not None
    assert resolution.credential["provider_name"]


def test_resolve_reports_missing_model_instead_of_silently_ready(client, admin) -> None:
    client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"model": "", "credential_id": None},
    )
    resolution = agent_model.resolve()
    assert resolution.model is None
    assert not resolution.ready
    assert any("模型名" in problem for problem in resolution.problems)


def test_resolve_auto_discovers_credential_by_matching_base_url(configured) -> None:
    """没选凭证时按 base_url 命中提供商（这条路径是「一个提供商一个凭证」的默认）。"""
    settings_service.update_agent_config({"credential_id": None})
    resolution = agent_model.resolve()
    assert resolution.ready, resolution.problems
    assert len(resolution.credential_refs) == 1


def test_turn_request_body_carries_model_and_refs(gateway, configured) -> None:

    resolution = agent_model.resolve()
    request = TurnRequest(
        turn_id="trn_shape",
        messages=[{"role": "user", "content": "hi"}],
        model=resolution.model,
        credential_refs=resolution.credential_refs,
    )
    _run(AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32).send("ags_shape", request))
    body = gateway.turns[-1]
    assert body["model"] == resolution.model
    assert body["credential_refs"] == resolution.credential_refs


# ── 配置 API 的校验 ───────────────────────────────────────────


def test_patch_config_rejects_unknown_provider(client, admin, provider, credential) -> None:
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"provider": "zhipu", "credential_id": credential["id"]},
    )
    assert r.status_code == 400
    assert "provider" in r.text


def test_patch_config_rejects_unknown_keys(client, admin) -> None:
    r = client.patch(
        "/api/admin/agent/config", headers=admin["headers"], json={"telemetry": True}
    )
    assert r.status_code == 400
    assert "telemetry" in r.text


def test_patch_config_rejects_unknown_credential(client, admin) -> None:
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"credential_id": "crd_does_not_exist"},
    )
    assert r.status_code == 400
    assert "credential_id" in r.text


def test_get_config_exposes_effective_readiness(configured, client, admin) -> None:
    r = client.get("/api/admin/agent/config", headers=admin["headers"])
    assert r.status_code == 200
    data = r.json()
    assert data["effective"]["ready"] is True
    assert data["effective"]["model"]["modelId"] == "glm-5.3-flash"
    assert data["providers"] == ["anthropic", "openai", "google"]


def test_send_message_refuses_when_model_not_configured(client, admin) -> None:
    client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"model": "", "credential_id": None},
    )
    session = client.post("/api/admin/agent/sessions", headers=admin["headers"], json={"title": "t"})
    assert session.status_code == 201, session.text
    r = client.post(
        f"/api/admin/agent/sessions/{session.json()['id']}/messages",
        headers=admin["headers"],
        json={"text": "你好", "attachments": []},
    )
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert "Agent 模型" in body["message"]
    assert body["details"]["problems"]


def test_config_test_endpoint_refuses_without_request_when_unconfigured(client, admin) -> None:
    client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"model": "", "credential_id": None},
    )
    r = client.post("/api/admin/agent/config/test", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["checked"] is False
    assert body["problems"]


# ── worker：把 model/refs 发出去 + 消费网关事件流 ─────────────


def _make_turn(session_title: str = "ingest") -> tuple[str, str]:
    session = agent_sessions.create_session({"title": session_title})
    queued = agent_sessions.queue_turn(session["id"], {"text": "你好", "attachments": []}, actor=None)
    return session["id"], queued["turn_id"]


def test_worker_sends_model_object_and_drains_events(gateway, configured, monkeypatch) -> None:

    session_id, turn_id = _make_turn()
    monkeypatch.setattr(
        agent_turn_handler,
        "ingest_stream",
        _fake_ingest(session_id, turn_id, ["assistant.delta", "turn.finished"]),
    )
    _run(agent_turn_handler.handle({"session_id": session_id, "turn_id": turn_id}))

    body = gateway.turns[-1]
    assert body["model"] == {
        "providerId": "anthropic",
        "modelId": "glm-5.3-flash",
        "baseUrl": "https://open.bigmodel.cn/api/anthropic",
    }
    assert body["credential_refs"]
    assert isinstance(body["model"], dict)


def _fake_ingest(session_id: str, turn_id: str, types: list[str]):
    async def _run(sid: str, tid: str, *, timeout_s: float | None = None) -> str:  # noqa: ANN001
        from skillrouter_api.agent import events as agent_events

        for event_type in types:
            agent_events.append(
                turn_id=tid,
                session_id=sid,
                seq=agent_events.next_seq(tid),
                event_type=event_type,
                payload={"text": "echo"} if event_type == "assistant.delta" else {},
            )
        agent_events.update_turn_status(tid, "succeeded")
        return "succeeded"

    return _run


def test_worker_fails_the_turn_when_model_unconfigured(client, admin, gateway) -> None:

    client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"model": "", "credential_id": None},
    )
    session_id, turn_id = _make_turn("unconfigured")
    _run(agent_turn_handler.handle({"session_id": session_id, "turn_id": turn_id}))
    assert gateway.turns == []

    with db_session() as s:
        turn = s.get(AgentTurn, turn_id)
        assert turn is not None
        assert turn.status == "failed"
        assert turn.error_code == "AGENT_MODEL_UNCONFIGURED"
        events = (
            s.query(AgentEventRow)
            .filter(AgentEventRow.turn_id == turn_id)
            .order_by(AgentEventRow.seq)
            .all()
        )
    assert [e.type for e in events] == ["turn.started", "turn.failed"]


def test_ingest_stream_persists_gateway_events(monkeypatch) -> None:
    """真正的 drain：网关 SSE 的每一行都要落库并更新轮次终态。"""

    session_id, turn_id = _make_turn("drain")

    class _Response:
        status_code = 200

        async def aiter_lines(self):
            for index, event in enumerate(
                [
                    {"type": "turn.started", "payload": {"session_id": session_id}},
                    {"type": "assistant.delta", "payload": {"text": "你好"}},
                    {"type": "turn.finished", "payload": {"usage": {"inputTokens": 1}}},
                ]
            ):
                yield f"id: {index}"
                yield f"data: {json.dumps(event)}"
                yield ""

    class _Stream:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *args):
            return False

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return _Stream()

    monkeypatch.setattr(agent_turn_handler.httpx, "AsyncClient", _Client)
    status = _run(agent_turn_handler.ingest_stream(session_id, turn_id))

    assert status == "succeeded"
    with db_session() as s:
        rows = (
            s.query(AgentEventRow)
            .filter(AgentEventRow.turn_id == turn_id)
            .order_by(AgentEventRow.seq)
            .all()
        )
        assert [row.type for row in rows] == ["turn.started", "assistant.delta", "turn.finished"]
        turn = s.get(AgentTurn, turn_id)
    assert turn is not None and turn.status == "succeeded"


def test_ingest_stream_marks_turn_failed_when_stream_breaks(monkeypatch) -> None:

    session_id, turn_id = _make_turn("broken")

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("gateway went away")

    monkeypatch.setattr(agent_turn_handler.httpx, "AsyncClient", _Client)
    status = _run(agent_turn_handler.ingest_stream(session_id, turn_id))
    assert status == "failed"
    with db_session() as s:
        turn = s.get(AgentTurn, turn_id)
        assert turn is not None
        assert turn.status == "failed"
        assert turn.error_code == "AGENT_STREAM_LOST"

# ── 连通性检测（真实请求的打桩版本）────────────────────────────


def _patch_probe_http(monkeypatch, *, status=200, body: dict | None = None):
    import httpx as _httpx

    captured: dict = {}

    class _Response:
        def __init__(self) -> None:
            self.status_code = status
            self.text = json.dumps(body or {})

        def json(self):
            return body or {}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):  # noqa: ANN001, ANN002, ANN003
            captured.update({"url": url, "headers": headers, "body": json})
            return _Response()

    monkeypatch.setattr(_httpx, "AsyncClient", _Client)
    return captured


def test_check_reports_success_for_a_real_shaped_completion(configured, monkeypatch) -> None:

    captured = _patch_probe_http(
        monkeypatch,
        body={"type": "message", "content": [{"type": "text", "text": "pong"}]},
    )
    result = _run(agent_model.check())
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["text_chars"] == 4
    assert captured["url"] == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    assert captured["headers"]["x-api-key"].startswith("sk-pytest")
    assert captured["body"]["model"] == "glm-5.3-flash"


def test_check_still_ok_when_only_thinking_came_back(configured, monkeypatch) -> None:
    """思考型模型在极小 max_tokens 下只回 thinking：仍是「可用」的证据，但要说明。"""

    _patch_probe_http(
        monkeypatch, body={"type": "message", "content": [{"type": "thinking", "thinking": "hm"}]}
    )
    result = _run(agent_model.check())
    assert result["ok"] is True
    assert result["text_chars"] == 0
    assert "没有产出文本" in result["detail"]


def test_check_refuses_200_with_a_foreign_body(configured, monkeypatch) -> None:

    _patch_probe_http(monkeypatch, body={"detail": "not a completion"})
    result = _run(agent_model.check())
    assert result["ok"] is False
    assert result["checked"] is True


# ── protocol / wire / paths（spec §3.6，T-24…T-27、T-32、T-33、T-35）──


def _custom_provider(client, admin, *, base_url: str = "https://gw.internal/openai") -> dict:
    import os as _os

    slug = f"gw-{_os.urandom(3).hex()}"
    r = client.post(
        "/api/admin/providers",
        headers=admin["headers"],
        json={"slug": slug, "name": "自建网关", "category": "llm", "base_url": base_url},
    )
    assert r.status_code == 201, r.text
    provider = r.json()
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={
            "name": "gw 凭证",
            "kind": "llm",
            "auth_type": "bearer",
            "secret": "gw-verysecret-0123456",
            "meta": {"llm_protocol": "custom"},
        },
    )
    assert r.status_code == 201, r.text
    return {"provider": provider, "credential": r.json()}


def test_custom_with_anthropic_wire(configured, client, admin) -> None:
    """T-24：`protocol=custom` + `wire=anthropic` → `providerId == "anthropic"`。"""
    bundle = _custom_provider(client, admin, base_url="https://gw.internal/anthropic")
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "protocol": "custom",
            "wire": "anthropic",
            "model": "claude-sonnet-4-5",
            "base_url": "https://gw.internal/anthropic",
            "credential_id": bundle["credential"]["id"],
        },
    )
    assert r.status_code == 200, r.text
    effective = r.json()["effective"]
    assert effective["protocol"] == "custom"
    assert effective["wire"] == "anthropic"
    assert effective["model"]["providerId"] == "anthropic"
    assert effective["problems"] == []


def test_custom_credential_refs_are_keyed_by_wire(configured, client, admin) -> None:
    """T-32（P0）：`credential_refs` 的键必须是 `wire` —— 网关按 `refs[model.providerId]` 取。"""
    bundle = _custom_provider(client, admin, base_url="https://gw.internal/openai")
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "protocol": "custom",
            "wire": "openai",
            "model": "qwen2.5-72b-instruct",
            "base_url": "https://gw.internal/openai",
            "credential_id": bundle["credential"]["id"],
        },
    )
    assert r.status_code == 200, r.text
    effective = r.json()["effective"]
    assert effective["model"]["providerId"] == "openai"
    assert list(effective["credential_refs"]) == ["openai"]
    assert effective["credential_refs"]["openai"] == bundle["credential"]["id"]
    assert "custom" not in effective["credential_refs"]


def test_wire_is_rejected_for_non_custom(configured, client, admin) -> None:
    """T-25：`protocol=google` + `wire=openai` → 400（wire 只对 custom 生效）。"""
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"protocol": "google", "wire": "openai"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "custom" in r.json()["error"]["message"]


def test_legacy_provider_only_config_still_resolves(configured, client, admin) -> None:
    """T-26：老配置只有 `provider=anthropic` → 仍然 `ready=true`（INV-4）。"""
    r = client.get("/api/admin/agent/config", headers=admin["headers"])
    assert r.status_code == 200
    payload = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"provider": "anthropic"},
    )
    assert payload.status_code == 200, payload.text
    assert payload.json()["effective"]["ready"] is True
    assert payload.json()["model"]["provider"] == "anthropic"
    assert payload.json()["model"]["protocol"] == "anthropic"
    assert r.json()["protocols"]  # 注册表随 GET 一起返回


def test_model_outside_catalog_is_only_a_warning(configured, credential) -> None:
    """T-27：模型不在目录里 → `warnings` 有提示，但 `ready` 仍为 true。"""
    from skillrouter_api.db import db_write
    from skillrouter_api.models import LlmModelCatalog
    from skillrouter_api.services.llm import catalog as model_catalog

    settings_service.update_agent_config(
        {
            "protocol": "anthropic",
            "model": "some-new-model",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "credential_id": credential["id"],
        }
    )
    resolution = agent_model.resolve()
    key = model_catalog.target_key(
        resolution.protocol,
        resolution.wire,
        (resolution.model or {}).get("baseUrl") or "",
        credential["id"],
    )
    with db_write() as s:
        s.add(
            LlmModelCatalog(
                dedupe_key=key,
                credential_id=credential["id"],
                provider_id=credential["provider_id"],
                protocol="anthropic",
                wire="anthropic",
                base_url=(resolution.model or {}).get("baseUrl") or "",
                models=[{"id": "claude-sonnet-4-5", "display_name": "Claude", "kind": "chat"}],
                model_count=1,
                truncated=0,
                source="upstream",
                fetched_at="2026-02-10T00:00:00Z",
                expires_at="2036-02-10T00:00:00Z",
                created_at="2026-02-10T00:00:00Z",
                updated_at="2026-02-10T00:00:00Z",
            )
        )
    resolution = agent_model.resolve()
    assert resolution.ready is True, resolution.problems
    assert any("不在最近一次模型清单里" in item for item in resolution.warnings)


def test_model_protocol_mismatch_blocks_readiness(configured, client, admin, credential) -> None:
    """T-35：`protocol=openai` + `model=claude-sonnet-4-5` → `ready=false`（不是 warning）。"""
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "protocol": "openai",
            "model": "claude-sonnet-4-5",
            "base_url": "https://gw.internal/openai",
            "credential_id": credential["id"],
        },
    )
    assert r.status_code == 200, r.text
    effective = r.json()["effective"]
    assert effective["ready"] is False
    assert any("不匹配" in item for item in effective["problems"])


def test_custom_without_credential_or_matching_url_is_not_ready(client, admin) -> None:
    """T-33：`custom` 没有显式凭证、地址也不命中任何提供商 → `ready=false`。

    实测旧打分函数在「全 0 分」时返回**第一个** active 凭证，等于随机挑一把钥匙。
    """
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "protocol": "custom",
            "wire": "openai",
            "model": "qwen2.5-72b-instruct",
            "base_url": "https://unmatched.example.com/v1",
            "credential_id": None,
        },
    )
    assert r.status_code == 200, r.text
    effective = r.json()["effective"]
    assert effective["ready"] is False
    assert any("自定义协议必须显式选择凭证" in item for item in effective["problems"])
    assert effective["credential_refs"] == {}


def test_paths_are_validated_for_custom(configured, client, admin) -> None:
    bundle = _custom_provider(client, admin)
    bad = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"protocol": "custom", "wire": "openai", "paths": {"models": "../etc/passwd"}},
    )
    assert bad.status_code == 400
    good = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={
            "protocol": "custom",
            "wire": "openai",
            "model": "qwen2.5-72b-instruct",
            "base_url": "https://gw.internal/openai",
            "credential_id": bundle["credential"]["id"],
            "paths": {"models": "/v2/models", "chat": "/v2/chat"},
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["model"]["paths"]["models"] == "/v2/models"


def test_provider_alias_conflict_is_rejected(client, admin) -> None:
    r = client.patch(
        "/api/admin/agent/config",
        headers=admin["headers"],
        json={"provider": "openai", "protocol": "anthropic"},
    )
    assert r.status_code == 400
    assert "旧名" in r.json()["error"]["hint"]


def test_check_keeps_legacy_fields(configured, monkeypatch) -> None:
    """`/agent/config/test` 的旧字段一个都不少，新增字段只增不改。"""
    _patch_probe_http(
        monkeypatch, body={"type": "message", "content": [{"type": "text", "text": "pong"}]}
    )
    result = _run(agent_model.check())
    for key in (
        "ok",
        "checked",
        "provider",
        "model",
        "base_url",
        "status",
        "latency_ms",
        "transport",
        "text_chars",
        "credential",
        "problems",
        "detail",
    ):
        assert key in result, f"旧字段 {key} 丢了"
    for key in ("protocol", "wire", "resolved_url", "mode", "stages", "probe_id"):
        assert key in result, f"新字段 {key} 缺失"
    assert result["mode"] == "completion"
    assert [stage["stage"] for stage in result["stages"]] == ["completion"], (
        "既有端点必须只发一次生成请求（不得先插一个 GET）"
    )
