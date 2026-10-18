"""适配层事件映射、停止报告（假网关，§7.12）。"""

from __future__ import annotations

import httpx
import pytest

from skillrouter_api.agent.client import AragonAgentAdapter, SessionSpec, TurnRequest
from skillrouter_api.errors import ApiError
from tests.fixtures.fake_gateway import FakeGateway, patch_gateway


@pytest.fixture()
def gateway(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    patch_gateway(monkeypatch, fake)
    return fake


async def test_health_maps_engine_info(gateway) -> None:
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    health = await adapter.health()
    assert health["engine"]["version"] == "0.2.34"
    assert "sr_capabilities_list" in health["tools"]


async def test_health_unreachable_is_actionable(monkeypatch) -> None:
    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        def handler(request):  # noqa: ANN001, ANN202
            raise httpx.ConnectError("refused", request=request)

        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    with pytest.raises(ApiError) as exc:
        await adapter.health()
    assert exc.value.code == "ENGINE_UNAVAILABLE"
    assert "gateway" in exc.value.hint


async def test_send_posts_turn(gateway) -> None:
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    turn_id = await adapter.send(
        "ags_1",
        TurnRequest(
            turn_id="trn_1",
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            budget={"max_turns": 5},
        ),
    )
    assert turn_id == "trn_1"
    assert gateway.turns[-1]["session_id"] == "ags_1"
    assert gateway.turns[-1]["system_prompt"] == "sys"


async def test_send_rejected_when_engine_down(gateway) -> None:
    gateway.available = False
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    with pytest.raises(ApiError) as exc:
        await adapter.send("ags_1", TurnRequest(turn_id="trn_1", messages=[]))
    assert exc.value.code == "ENGINE_UNAVAILABLE"


async def test_stop_report_lists_steps_and_pending(gateway) -> None:
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    report = await adapter.stop("ags_1", "trn_1")
    assert report["stopped_steps"] == ["sr_test_capability（已完成）"]
    assert report["external_pending"] == ["up-task-9"]
    assert report["note"]


async def test_stop_when_gateway_unreachable_still_stops_locally(monkeypatch) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        def handler(request):  # noqa: ANN001, ANN202
            raise httpx.ConnectError("refused", request=request)

        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    report = await adapter.stop("ags_1", "trn_1")
    assert report["stopped_steps"] == []
    assert "远端可能仍在执行" in report["note"]


async def test_session_start_returns_id() -> None:
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    spec = SessionSpec(session_id="ags_x", system_prompt="sys", tools=["sr_capabilities_list"])
    assert await adapter.start_session(spec) == "ags_x"


async def test_history_reads_local_table(gateway) -> None:
    from skillrouter_api.agent import sessions as agent_sessions
    from skillrouter_api.db import db_write
    from skillrouter_api.models import AgentTurn

    session = agent_sessions.create_session({"title": "测试会话"})
    with db_write() as s:
        s.add(
            AgentTurn(
                id="trn_h1",
                session_id=session["id"],
                ordinal=1,
                user_text="第一轮",
                status="succeeded",
                created_at="2026-01-01T00:00:00Z",
            )
        )
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    history = await adapter.history(session["id"], limit=5)
    assert history and history[-1]["user_text"] == "第一轮"


async def test_connectivity_reports_tools(gateway) -> None:
    adapter = AragonAgentAdapter(base_url="http://gw.test", secret="s" * 32)
    info = await adapter.connectivity()
    assert info["gateway"]["ok"] is True
    assert info["engine"]["version"] == "0.2.34"


def test_toolspec_matches_policy() -> None:
    from skillrouter_api.agent import toolspec

    assert len(toolspec.names()) == 12
    assert not toolspec.drift()


def test_engine_capability_status_has_four_states(client, admin) -> None:
    r = client.get("/api/admin/engine/capabilities", headers=admin["headers"])
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert set(groups) >= {"available", "needs_adapter", "permission_limited", "unsupported"}
    assert groups["unsupported"]
