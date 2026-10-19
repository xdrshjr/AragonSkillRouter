"""用量与预算记账（§A.3.4 / §A.7.1：T-07、T-08、T-45、T-46）。

断言全部走「行为」：真实 HTTP 调用 + 真实假上游，只在需要检查落库口径时读表。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from skillrouter_api.db import db_session, db_write, now_iso, uuid_pk
from skillrouter_api.models import CallLog, QuotaCounter, QuotaLimit, Task
from skillrouter_api.security import ratelimit
from skillrouter_api.services import execution, usage
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

# ── 辅助 ─────────────────────────────────────────────────────


def _publish(client, admin, capability_id: str, version: str = "1.0.0") -> None:
    """先用一次真实（假上游）测试留下发布证据。"""
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "发布前测试"}},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/publish",
        headers=admin["headers"],
        json={"version": version},
    )
    assert r.status_code == 200, r.text


def _periods() -> tuple[str, str]:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return today, today[:7]


def _cost(scope_type: str, scope_id: str, period: str) -> float:
    with db_session() as s:
        row = s.scalar(
            select(QuotaCounter).where(
                QuotaCounter.scope_type == scope_type,
                QuotaCounter.scope_id == scope_id,
                QuotaCounter.period == period,
            )
        )
    return float(row.cost_usd) if row else 0.0


def _concurrent(scope_type: str, scope_id: str) -> int:
    return int(ratelimit.quota_snapshot(scope_type, scope_id)["concurrent"])


async def _drain(worker) -> None:  # noqa: ANN001
    for _ in range(20):
        claimed = await worker.run_once(["capability_invoke_async"])
        if claimed == 0:
            break


# ── T-07：流式/异步都要计费，日/月两行都递增 ─────────────────


def test_stream_and_async_charge_budget(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async", "stream"])
    _publish(client, admin, cap["id"])
    # 预算极小：一次调用就会用尽，第二次必须 402
    token = make_token(budget_usd=0.00001)
    today, month = _periods()
    url = f"/api/v1/capabilities/{cap['key']}/invoke"

    before_day = _cost("token", token["token_id"], today)
    before_month = _cost("token", token["token_id"], month)
    first = client.post(url, headers=token["headers"], json={"input": {"prompt": "同步记账"}})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "succeeded"
    assert first.json()["usage"]["cost_usd"] > 0
    after_day = _cost("token", token["token_id"], today)
    after_month = _cost("token", token["token_id"], month)
    assert after_day > before_day, "当日 quota_counters.cost_usd 必须递增"
    assert after_month > before_month, "当月 quota_counters.cost_usd 必须递增"

    # 同步路径第二次：预算已用尽
    second = client.post(url, headers=token["headers"], json={"input": {"prompt": "同步记账 2"}})
    assert second.status_code == 402, second.text
    assert second.json()["error"]["code"] == "BUDGET_EXCEEDED"

    # 流式走同一条配额门：SSE 以 call.error 事件返回同一个错误码（不静默放行）
    with client.stream(
        "POST",
        url,
        headers={**token["headers"], "Accept": "text/event-stream"},
        json={"input": {"prompt": "流式记账"}, "options": {"mode": "stream"}},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    assert "BUDGET_EXCEEDED" in text, text

    # 异步：提交时不记账，终态才记账（预算终态记账，§A.3.4）
    token_async = make_token(budget_usd=0.00001)
    submit = client.post(
        url,
        headers=token_async["headers"],
        json={"input": {"prompt": "异步记账"}, "options": {"mode": "async"}},
    )
    assert submit.status_code == 202, submit.text
    task_id = submit.json()["task_id"]
    assert _cost("token", token_async["token_id"], month) == 0.0, "异步提交不应记账"

    from skillrouter_api.worker.runner import Worker  # 局部导入：避免影响其它用例的收集顺序

    worker = Worker(worker_id="pytest-budget-worker")
    worker.register_all()
    asyncio.get_event_loop().run_until_complete(_drain(worker))

    task = client.get(f"/api/v1/tasks/{task_id}", headers=token_async["headers"]).json()
    assert task["status"] == "succeeded", task
    assert _cost("token", token_async["token_id"], month) > 0, "异步终态必须记账"

    rejected = client.post(
        url,
        headers=token_async["headers"],
        json={"input": {"prompt": "异步记账 2"}, "options": {"mode": "async"}},
    )
    assert rejected.status_code == 402, rejected.text
    assert rejected.json()["error"]["code"] == "BUDGET_EXCEEDED"


# ── T-08：定价阶梯四级 ───────────────────────────────────────


def test_price_ladder(client, admin, provider, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    make_token()

    common = {
        "call_id": None,
        "provider_id": provider["id"],
        "capability_key": cap["key"],
        "token_id": None,
        "agent_id": None,
    }

    # 1) 显式价：优先于任何价目表
    explicit = usage.account(**common, unit="tokens", qty=1000, source="provider", unit_price_usd=0.5)
    assert explicit["price_source"] == "explicit"
    assert explicit["cost_usd"] == 500.0
    assert explicit["source"] == "provider"

    # 2) price_book 有当前有效行 → 用该行
    usage.set_price(provider["id"], "tokens", 0.01)
    booked = usage.account(**common, unit="tokens", qty=100, source="provider")
    assert booked["price_source"] == "price_book"
    assert booked["cost_usd"] == 1.0

    # 3) 只有 DEFAULT_PRICES → estimated 且成本 > 0
    estimated = usage.account(**common, unit="images", qty=2, source="estimated")
    assert estimated["price_source"] == "estimated"
    assert estimated["source"] == "estimated"
    assert estimated["cost_usd"] == 0.04

    # 4) 无价目 + 不在默认价表 → cost 0 / unknown，但调用照常成功
    priced = make_capability()
    definition = client.get(f"/api/admin/capabilities/{priced['id']}").json()["definition"]
    definition["routes"][0]["response_map"]["usage"]["unit"] = "widgets"
    patch = client.patch(
        f"/api/admin/capabilities/{priced['id']}",
        headers=admin["headers"],
        json={"definition": definition},
    )
    assert patch.status_code == 200, patch.text
    _publish(client, admin, priced["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{priced['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "无价目"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["usage"]["cost_usd"] == 0
    assert body["usage"]["price_source"] == "unknown"
    assert body["usage"]["unit"] == "widgets"

    unknown = usage.account(**common, unit="widgets", qty=5, source="provider")
    assert unknown["cost_usd"] == 0
    assert unknown["source"] == "unknown"
    assert unknown["price_source"] == "unknown"


# ── T-45：agent 作用域预算超限 402 ───────────────────────────


def test_agent_scope_budget(client, admin, provider, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    # 令牌自身预算充足：只有 agent 作用域的预算会拦住第二次调用
    token = make_token(budget_usd=100.0)
    with db_write() as s:
        s.add(
            QuotaLimit(
                id=uuid_pk("qlt"),
                scope_type="agent",
                scope_id=token["agent_id"],
                monthly_budget_usd=0.000001,
                updated_at=now_iso(),
            )
        )
    url = f"/api/v1/capabilities/{cap['key']}/invoke"
    first = client.post(url, headers=token["headers"], json={"input": {"prompt": "agent 预算"}})
    assert first.status_code == 200, first.text

    second = client.post(url, headers=token["headers"], json={"input": {"prompt": "agent 预算 2"}})
    assert second.status_code == 402, second.text
    error = second.json()["error"]
    assert error["code"] == "BUDGET_EXCEEDED"
    assert error["details"]["scope"] == "agent"
    assert error["details"]["limit_usd"] == 0.000001
    assert error["details"]["used_usd"] > 0
    assert error["details"]["hint"]
    assert _concurrent("agent", token["agent_id"]) == 0, "被拒的调用必须把并发名额退回"


# ── T-46：异步终态释放五层并发，且幂等 ───────────────────────


def test_async_releases_concurrency_once(
    client, admin, provider, make_capability, make_token, monkeypatch
) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async"])
    _publish(client, admin, cap["id"])
    token = make_token()
    scopes = {
        "token": token["token_id"],
        "agent": token["agent_id"],
        "capability": cap["key"],
        "provider": provider["id"],
        "global": "*",
    }
    # 基线：其它用例可能留下在途任务，本次只断言「增量」与「回到基线」
    baseline = {name: _concurrent(kind, sid) for name, (kind, sid) in _scopes(scopes).items()}
    submit = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "并发释放"}, "options": {"mode": "async"}},
    )
    assert submit.status_code == 202, submit.text
    task_id = submit.json()["task_id"]

    # 提交侧不得释放：五层并发都应 +1（用**增量**断言，避免与本轮其它用例留下的在途任务耦合）
    after_submit = {name: _concurrent(kind, sid) for name, (kind, sid) in _scopes(scopes).items()}
    assert after_submit == {name: value + 1 for name, value in baseline.items()}, after_submit

    from skillrouter_api.worker.runner import Worker  # 局部导入

    worker = Worker(worker_id="pytest-quota-worker")
    worker.register_all()
    asyncio.get_event_loop().run_until_complete(_drain(worker))

    task = client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()
    assert task["status"] == "succeeded", task
    after_terminal = {name: _concurrent(kind, sid) for name, (kind, sid) in _scopes(scopes).items()}
    assert after_terminal == baseline, f"终态必须把五层并发释放回基线：{after_terminal} vs {baseline}"

    # 幂等：重复释放返回 0，且不会把计数减成负数
    with db_session() as s:
        row = s.get(Task, task_id)
        s.expunge(row)
    assert row.result.get("quota_released") is True
    assert execution.release_task_quota(row) == 0
    assert execution.release_task_quota(row) == 0
    assert {name: _concurrent(kind, sid) for name, (kind, sid) in _scopes(scopes).items()} == baseline
    for scope_type, scope_id in _scopes(scopes).values():
        assert _concurrent(scope_type, scope_id) >= 0
    # 记账确实落在 usage_records 上（异步终态也产生一条）
    with db_session() as s:
        rows = list(s.scalars(select(CallLog).where(CallLog.task_id == task_id)))
    assert rows, "异步提交必须留一条 call_logs"


def _scopes(mapping: dict) -> dict[str, tuple[str, str]]:
    return {
        "token": ("token", mapping["token"]),
        "agent": ("agent", mapping["agent"]),
        "capability": ("capability", mapping["capability"]),
        "provider": ("provider", mapping["provider"]),
        "global": ("global", mapping["global"]),
    }
