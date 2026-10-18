"""三种模式与超时/重试/幂等（含「异步请求内不触达上游」断言，v2 C4）。"""

from __future__ import annotations

import time

from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx


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


def test_timeout_maps_to_status_unknown(client, admin, make_capability, make_token, monkeypatch) -> None:
    import httpx

    original = httpx.AsyncClient

    def boom_factory(*args, **kwargs):  # noqa: ANN001, ANN202
        def handler(request):  # noqa: ANN001, ANN202
            raise httpx.ConnectTimeout("timeout", request=request)

        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    cap = make_capability()
    # 发布需要证据：先用正常上游
    patch_httpx(monkeypatch, FakeUpstream())
    _publish(client, admin, cap["id"])
    token = make_token()
    monkeypatch.setattr(httpx, "AsyncClient", boom_factory)
    r = client.post(
        f"/api/v1/capabilities/{cap['key']}/invoke",
        headers=token["headers"],
        json={"input": {"prompt": "超时"}},
    )
    assert r.status_code in (200, 504)
    body = r.json()
    if r.status_code == 200:
        assert body["status"] == "status_unknown"
        assert body["error"]["code"] == "UPSTREAM_TIMEOUT"
    else:
        assert body["error"]["code"] == "UPSTREAM_TIMEOUT"


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
