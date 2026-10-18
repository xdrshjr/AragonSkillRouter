"""性能基线（标记 slow，默认不执行；`pytest -m slow` 才会跑）。"""

from __future__ import annotations

import statistics
import time

import pytest


@pytest.mark.slow
def test_admin_endpoints_p95(client, admin) -> None:
    """管理接口 P95 ≤ 500ms（本地小数据集；生产基线见 docs/runbook）。"""
    endpoints = ["/api/admin/overview", "/api/admin/providers", "/api/admin/capabilities"]
    samples: dict[str, list[float]] = {path: [] for path in endpoints}
    for _ in range(20):
        for path in endpoints:
            started = time.perf_counter()
            r = client.get(path, headers=admin["headers"])
            samples[path].append((time.perf_counter() - started) * 1000)
            assert r.status_code == 200
    for path, timings in samples.items():
        p95 = statistics.quantiles(timings, n=20)[-1]
        print(f"{path}: p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms")
        assert p95 <= 500, f"{path} P95={p95:.1f}ms 超过 500ms"


@pytest.mark.slow
def test_async_submit_latency(client, admin, make_capability, make_token, monkeypatch) -> None:
    """异步提交 ≤1s（平台侧校验 + 持久化）。"""
    from tests.fixtures.fake_upstream import FakeUpstream, patch_httpx

    patch_httpx(monkeypatch, FakeUpstream())
    cap = make_capability(key="text.generate.perf", modes=["sync", "async"])
    client.post(
        f"/api/admin/capabilities/{cap['id']}/test", headers=admin["headers"], json={"input": {"prompt": "x"}}
    )
    client.post(
        f"/api/admin/capabilities/{cap['id']}/publish", headers=admin["headers"], json={"version": "1.0.0"}
    )
    token = make_token()
    timings = []
    for index in range(10):
        started = time.perf_counter()
        r = client.post(
            f"/api/v1/capabilities/{cap['key']}/invoke",
            headers=token["headers"],
            json={"input": {"prompt": f"perf {index}"}, "options": {"mode": "async"}},
        )
        timings.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 202
    p95 = statistics.quantiles(timings, n=20)[-1]
    print(f"async submit: p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms")
    assert p95 <= 1000
