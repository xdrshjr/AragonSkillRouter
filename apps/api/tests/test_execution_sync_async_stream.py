"""三种模式与超时/重试/幂等（含「异步请求内不触达上游」断言，v2 C4 / §A.3.5）。"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import select

from skillrouter_api.db import db_session, db_write
from skillrouter_api.models import CallLog, IdempotencyKey
from tests.fixtures.builders import route as build_route
from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


class _FaultyUpstream(FakeUpstream):
    """可切换的故障上游：`fault` 为 None / 'read_timeout' / 'connect_timeout' / 'reset'。"""

    def __init__(self) -> None:
        super().__init__()
        self.fault: str | None = None

    def handler(self, request):  # noqa: ANN001, ANN202
        if not self.fault:
            return super().handler(request)
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content.decode("utf-8", errors="replace"),
            }
        )
        if self.fault == "read_timeout":
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if self.fault == "connect_timeout":
            raise httpx.ConnectTimeout("simulated connect timeout", request=request)
        raise httpx.ConnectError("connection reset by peer", request=request)


def _publish(client, admin, capability_id: str, version="1.0.0") -> None:
    """先用一次真实（假上游）测试留下发布证据。"""
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/test",
        headers=admin["headers"],
        json={"input": {"prompt": "发布前测试"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded", r.text
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/publish",
        headers=admin["headers"],
        json={"version": version},
    )
    assert r.status_code == 200, r.text


def test_sync_invoke_success(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()

    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "你好"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["output"] == "假的回复"
    assert body["capability"]["key"] == cap["key"]
    assert body["usage"]["input"] == 12 and body["usage"]["output"] == 7
    assert body["t timings"] if False else body["timings"]["upstream_ms"] is not None
    assert upstream.count("/v1/generate") == 2  # 测试 1 次 + 调用 1 次


def test_sync_invoke_unknown_field_rejected(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "x", "undeclared": 1}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INPUT_SCHEMA_INVALID"


def test_unsupported_mode_rejected(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(modes=["sync"])
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "x"}, "options": {"mode": "async"}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNSUPPORTED_MODE"
    assert "sync" in r.json()["error"]["details"]["supported"]


def test_async_submit_does_not_touch_upstream(client, admin, make_capability, make_token, monkeypatch) -> None:
    """C4：异步提交在「上游零请求」前提下 ≤1s 返回 task_id。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async"])
    _publish(client, admin, cap["id"])
    calls_before = len(upstream.calls)
    token = make_token()

    started = time.perf_counter()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "生成视频"}, "options": {"mode": "async"}},
    )
    elapsed = time.perf_counter() - started
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["task_id"].startswith("tsk_")
    assert elapsed <= 1.0, f"提交耗时 {elapsed:.3f}s 超过 1s 预算"
    assert len(upstream.calls) == calls_before, "异步提交不应触达上游"

    task = client.get(f"/api/v1/tasks/{body['task_id']}", headers=token["headers"])
    assert task.status_code == 200
    assert task.json()["status"] == "queued"
    assert task.json()["status_detail"].startswith("平台已受理")


def test_async_worker_completes_task(client, admin, make_capability, make_token, monkeypatch) -> None:
    import asyncio

    from skillrouter_api.worker.runner import Worker

    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "async"])
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "异步"}, "options": {"mode": "async"}},
    )
    task_id = r.json()["task_id"]

    worker = Worker(worker_id="pytest-worker")
    worker.register_all()
    asyncio.get_event_loop().run_until_complete(_drain(worker))

    task = client.get(f"/api/v1/tasks/{task_id}", headers=token["headers"]).json()
    assert task["status"] == "succeeded", (
        task.get("status"),
        task.get("error"),
        [c["url"] for c in upstream.calls],
    )
    assert task["upstream_task_id"] == "up-task-1"
    assert task["progress"]["percent"] == 100


async def _drain(worker) -> None:
    for _ in range(20):
        claimed = await worker.run_once(["capability_invoke_async"])
        if claimed == 0:
            break


def test_stream_mode_returns_sse(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(modes=["sync", "stream"])
    _publish(client, admin, cap["id"])
    token = make_token()
    with client.stream(
        "POST",
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers={**token["headers"], "Accept": "text/event-stream"},
        json={"input": {"prompt": "流式"}, "options": {"mode": "stream"}},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    assert "call.started" in text
    assert "call.delta" in text
    assert "你" in text and "好" in text


def test_connect_timeout_is_retryable_and_fails(client, admin, make_capability, make_token, monkeypatch) -> None:
    """连接阶段超时（未建立连接）属于可重试失败：不是「结果不确定」（§A.3.5）。"""

    upstream = _FaultyUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    upstream.calls.clear()
    upstream.fault = "connect_timeout"
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "超时"}},
    )
    body = r.json()
    assert r.status_code in (200, 504), r.text
    if r.status_code == 200:
        assert body["status"] == "failed"
        assert body["needs_reconcile"] is False
        assert body["error"]["retryable"] is True
    assert body["error"]["code"] in ("UPSTREAM_UNAVAILABLE", "UPSTREAM_TIMEOUT")


def test_upstream_401_maps_to_auth_error(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    upstream.push(401, {"error": "unauthorized"})
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "鉴权失败"}},
    )
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "UPSTREAM_AUTH"
    assert body["error"]["hint"]


def test_idempotency_returns_same_request(client, admin, make_capability, make_token, monkeypatch) -> None:
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    payload = {"input": {"prompt": "幂等"}, "options": {"idempotency_key": "idem-1"}}
    first = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=token["headers"], json=payload)
    assert first.status_code == 200
    calls_after_first = upstream.count("/v1/generate")
    second = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=token["headers"], json=payload)
    assert second.status_code == 200
    assert second.json()["request_id"] == first.json()["request_id"]
    assert upstream.count("/v1/generate") == calls_after_first, "重复请求不应再次调用上游"


def test_idempotency_key_reuse_with_new_body(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "A"}, "options": {"idempotency_key": "same-key"}},
    )
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "B"}, "options": {"idempotency_key": "same-key"}},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_idempotency_isolated_between_tokens(client, admin, make_capability, make_token, monkeypatch) -> None:
    """C5：令牌 A 的幂等键不得命中令牌 B 的请求。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token_a = make_token()
    token_b = make_token()
    payload = {"input": {"prompt": "跨令牌"}, "options": {"idempotency_key": "shared-key"}}
    a = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=token_a["headers"], json=payload)
    b = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=token_b["headers"], json=payload)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["request_id"] != b.json()["request_id"]


def test_idempotency_key_from_header(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    headers = {**token["headers"], "Idempotency-Key": "header-key-1"}
    first = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=headers, json={"input": {"prompt": "h"}})
    second = client.post(f"/api/v1/capabilities/{cap['key']}/invoke", headers=headers, json={"input": {"prompt": "h"}})
    assert first.json()["request_id"] == second.json()["request_id"]


def test_call_log_recorded_and_redacted(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "记录"}},
    )
    request_id = r.json()["request_id"]
    detail = client.get(f"/api/admin/calls/{request_id}", headers=admin["headers"])
    assert detail.status_code == 200
    body = detail.json()
    assert body["route_decision"]["chosen_route_id"] == "rt_primary"
    assert body["latency_breakdown"]["platform_ms"] is not None
    assert "sk-pytest-secret" not in detail.text


def test_provider_override_unavailable(client, admin, make_capability, make_token, monkeypatch) -> None:
    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "x"}, "options": {"preferred_provider": "prv_missing"}},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PROVIDER_NOT_AVAILABLE"
    assert r.json()["error"]["details"]["available"]


# ── §A.3.5 / T-09 · T-10 · T-11 ──────────────────────────────


def test_read_timeout_does_not_retry_post(
    client, admin, provider, credential, make_capability, make_token, monkeypatch
) -> None:
    """T-09：读超时（请求已发出、无完整响应）不重投、不 failover，`needs_reconcile=1`。"""
    primary = build_route(provider["id"], credential["id"])
    primary["retry"] = {"max_attempts": 3, "base_ms": 1, "max_ms": 2}
    backup = build_route(
        provider["id"], credential["id"], route_id="rt_backup", priority=1, url_suffix="/v2/generate"
    )
    upstream = _FaultyUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(routes=[primary, backup])
    _publish(client, admin, cap["id"])
    token = make_token()
    upstream.calls.clear()
    upstream.fault = "read_timeout"

    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "读超时"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "status_unknown"
    assert body["status_unknown"] is True
    assert body["needs_reconcile"] is True
    assert body["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert body["error"]["retryable"] is False
    assert upstream.count("/v1/generate") == 1, "读超时不得重投"
    assert upstream.count("/v2/generate") == 0, "读超时不得 failover"

    with db_session() as s:
        row = s.scalar(select(CallLog).where(CallLog.request_id == body["request_id"]))
    assert row is not None
    assert row.needs_reconcile == 1
    assert row.status == "status_unknown"


def test_connect_timeout_retries_then_fails(client, admin, make_capability, make_token, monkeypatch) -> None:
    """T-10：连接阶段（未建立连接）可重试，重试次数 == `max_attempts`。"""
    upstream = _FaultyUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    definition = client.get(f"/api/admin/capabilities/{cap['id']}").json()["definition"]
    definition["routes"][0]["retry"] = {"max_attempts": 3, "base_ms": 1, "max_ms": 2}
    patch = client.patch(
        f"/api/admin/capabilities/{cap['id']}",
        headers=admin["headers"],
        json={"definition": definition},
    )
    assert patch.status_code == 200, patch.text
    _publish(client, admin, cap["id"])
    token = make_token()
    upstream.calls.clear()
    upstream.fault = "connect_timeout"

    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "连接超时"}},
    )
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["status"] == "failed"
    assert body["error"]["retryable"] is True
    assert body["needs_reconcile"] is False
    assert body["timings"]["failover_switches"] == 0
    assert upstream.count("/v1/generate") == 3, "连接失败应按 max_attempts 重试"


def test_concurrent_same_idem_key_calls_upstream_once(
    client, admin, make_capability, make_token, monkeypatch
) -> None:
    """T-11：并发 5 个同键 → 上游只被打 1 次；其余 409 `IDEMPOTENCY_IN_PROGRESS` 或回放。"""
    real_async_client = httpx.AsyncClient  # 必须在 patch_httpx 之前抓住真实类
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    url = f"/api/v1/capabilities/{cap['key']}/invoke"
    payload = {"input": {"prompt": "并发同键"}, "options": {"idempotency_key": "concurrent-key"}}
    calls_before = len(upstream.calls)

    from skillrouter_api.main import app

    async def _fan_out() -> list:
        transport = httpx.ASGITransport(app=app)
        async with real_async_client(transport=transport, base_url="http://testserver") as client_async:
            return await asyncio.gather(
                *[client_async.post(url, headers=token["headers"], json=payload) for _ in range(5)]
            )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # 保持 current loop 不被清空，避免污染后续依赖 get_event_loop 的用例
    responses = loop.run_until_complete(_fan_out())
    assert len(upstream.calls) - calls_before == 1, "并发同键只能打上游 1 次"
    ok = [r for r in responses if r.status_code == 200]
    conflicts = [r for r in responses if r.status_code == 409]
    assert ok, [r.status_code for r in responses]
    assert len(ok) + len(conflicts) == 5
    for r in conflicts:
        assert r.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert len({r.json()["request_id"] for r in ok}) == 1, "成功的那条必须都是同一个 request_id"


def test_expired_in_flight_reservation_can_be_taken_over(
    client, admin, make_capability, make_token, monkeypatch
) -> None:
    """T-11 后半：过期的 `in_flight` 占位可被条件 UPDATE 重新占位并真正执行。"""
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability()
    _publish(client, admin, cap["id"])
    token = make_token()
    url = f"/api/v1/capabilities/{cap['key']}/invoke"
    payload = {"input": {"prompt": "陈旧占位"}, "options": {"idempotency_key": "stale-key"}}

    first = client.post(url, headers=token["headers"], json=payload)
    assert first.status_code == 200, first.text
    calls_after_first = len(upstream.calls)
    with db_write() as s:
        row = s.scalar(select(IdempotencyKey).where(IdempotencyKey.idem_key == "stale-key"))
        assert row is not None and row.state == "done"
        row.state = "in_flight"
        row.expires_at = "2020-01-01T00:00:00Z"

    second = client.post(url, headers=token["headers"], json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["request_id"] != first.json()["request_id"], "过期占位必须被重新占位"
    assert len(upstream.calls) == calls_after_first + 1, "重新占位后应真的再打一次上游"
    with db_session() as s:
        row = s.scalar(select(IdempotencyKey).where(IdempotencyKey.idem_key == "stale-key"))
    assert row.state == "done"


def test_idempotency_header_passed_through_to_upstream(
    client, admin, provider, credential, make_capability, make_token, monkeypatch
) -> None:
    """route 声明 `idempotency_header` 时，平台幂等键原样透传给上游（§A.3.5 末条）。"""
    primary = build_route(provider["id"], credential["id"])
    primary["request"]["idempotency_header"] = "Idempotency-Key"
    upstream = FakeUpstream()
    patch_httpx(monkeypatch, upstream)
    cap = make_capability(routes=[primary])
    _publish(client, admin, cap["id"])
    token = make_token()
    upstream.calls.clear()

    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers={**token["headers"], "Idempotency-Key": "pass-through-1"},
        json={"input": {"prompt": "幂等透传"}},
    )
    assert r.status_code == 200, r.text
    sent = [call for call in upstream.calls if call["url"].endswith("/v1/generate")]
    assert sent, upstream.calls
    headers = {k.lower(): v for k, v in sent[-1]["headers"].items()}
    assert headers.get("idempotency-key") == "pass-through-1"

