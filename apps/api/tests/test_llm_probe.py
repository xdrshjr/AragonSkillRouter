"""探测引擎的行为测试（T-07…T-13，spec §3.4 / §7.1）。

这些用例把「实时连接测试」的四条硬要求钉死：

1. `mode=auth` 绝不发计费请求；
2. `both` 在鉴权失败时短路（不花用户的钱）；
3. 传输层**复用** `netpolicy`：本机目标不先吃一次代理失败；
4. 密钥不进响应、不进探测行、不进日志。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from skillrouter_api.db import db_session
from skillrouter_api.models import LlmConnectionProbe
from skillrouter_api.services.llm import identity as llm_identity
from skillrouter_api.services.llm import probe, protocols
from tests.fixtures.fake_upstream import FakeLlmUpstream, patch_httpx

SECRET = "sk-testsecret0123456789"


def _run(coro):
    """运行协程，并把事件循环还给线程。

    `asyncio.run()` 退出时会把线程的 current event loop 置空，而既有测试
    （`tests/test_ssrf_guard.py`）仍用 `asyncio.get_event_loop()` —— 直接调
    `asyncio.run()` 会让它们在「谁先跑」上出现顺序性失败。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _clean_probe_state():
    """每个用例都从干净的限流/单飞状态开始（否则上一次的 memo 会污染断言）。"""
    probe.reset_state()
    yield
    probe.reset_state()


def _identity(protocol: str = "openai", base_url: str | None = None, **extra) -> object:
    spec = protocols.get(protocol)
    netloc = {
        "openai": "http://127.0.0.1:9/v1",
        "anthropic": "http://127.0.0.1:9",
        "google": "http://127.0.0.1:9",
        "custom": "http://127.0.0.1:9/openai",
    }[protocol]
    return llm_identity.ProbeIdentity(
        protocol=protocol,
        wire=extra.pop("wire", None) or spec.wire or "openai",
        base_url=base_url if base_url is not None else netloc,
        auth_style=extra.pop("auth_style", None) or spec.default_auth_style,
        header_name=extra.pop("header_name", None),
        secret=extra.pop("secret", SECRET),
        extra_headers=extra.pop("extra_headers", {}),
        query_params=extra.pop("query_params", {}),
        paths=extra.pop("paths", None),
        credential_id=extra.pop("credential_id", None),
        provider_id=None,
        source="draft",
        notes=[],
        inferred=False,
        credential=None,
    )


def _rows() -> list[LlmConnectionProbe]:
    with db_session() as s:
        return list(s.scalars(select(LlmConnectionProbe).order_by(LlmConnectionProbe.at)))


# ── T-07：mode=auth 不发完成请求 ──────────────────────────────
def test_auth_mode_only_lists_models(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o"])
    patch_httpx(monkeypatch, upstream)
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=False))
    assert result["ok"] is True
    assert [stage["stage"] for stage in result["stages"]] == ["auth"]
    assert len(upstream.calls) == 1
    assert upstream.completion_calls == []
    assert result["stages"][0]["model_count"] == 1


# ── T-08：both 的短路 / completion 只发一次 POST ─────────────
def test_both_mode_short_circuits_completion_when_auth_fails(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", reject_auth=True)
    patch_httpx(monkeypatch, upstream)
    result = _run(
        probe.test_connection(_identity("openai"), mode="both", model="gpt-4o", record=False)
    )
    assert result["ok"] is False
    assert upstream.completion_calls == [], "鉴权失败时不得发出计费请求"
    assert result["stages"][1]["stage"] == "completion"
    assert "未执行" in result["stages"][1]["detail"]
    assert any("未发出计费请求" in item for item in result["problems"])
    assert result["error_code"] == "UPSTREAM_AUTH"
    assert result["hint"] and "API key" in result["hint"]


def test_completion_mode_sends_only_one_post(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o"])
    patch_httpx(monkeypatch, upstream)
    result = _run(
        probe.test_connection(_identity("openai"), mode="completion", model="gpt-4o", record=False)
    )
    assert result["ok"] is True
    assert len(upstream.calls) == 1
    assert upstream.models_calls == [], "completion 模式不得先拉一次清单"
    assert result["stages"][0]["stage"] == "completion"


def test_completion_mode_uses_explicit_model_over_settings(monkeypatch) -> None:
    """显式给的模型优先于 `agent_model` 设置（请求 > 设置 > 凭证 meta）。"""
    from skillrouter_api.services import settings as settings_service

    settings_service.update_agent_config({"model": "from-settings"})
    resolved = probe.resolve_model(_Payload("explicit-model"), None)
    assert resolved == "explicit-model"
    resolved = probe.resolve_model(_Payload(None), None)
    assert resolved == "from-settings"


class _Payload:
    def __init__(self, model: str | None) -> None:
        self.model = model


# ── T-09：重试与传输回退 ─────────────────────────────────────
def test_5xx_is_retried_once(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", fail_models=True)
    patch_httpx(monkeypatch, upstream)
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=False))
    assert result["ok"] is False
    assert result["error_code"] == "UPSTREAM_ERROR"
    # 5xx 退避后重试 1 次 → 上游被打了 2 次
    assert len(upstream.models_calls) == 2


def test_4xx_is_not_retried(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", reject_auth=True)
    patch_httpx(monkeypatch, upstream)
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=False))
    assert result["ok"] is False
    assert len(upstream.models_calls) == 1


class _RaisingClient:
    """记录构造次数并在请求时抛 `httpx.ConnectError` 的打桩客户端。"""

    attempts = 0
    kwargs_seen: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        type(self).kwargs_seen.append(dict(kwargs))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        type(self).attempts += 1
        raise httpx.ConnectError("boom")

    async def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
        type(self).attempts += 1
        raise httpx.ConnectError("boom")

    async def request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        type(self).attempts += 1
        raise httpx.ConnectError("boom")


def test_local_target_does_not_retry_with_direct_connection(monkeypatch) -> None:
    """T-09：`127.0.0.1` 目标 `trust_env=False`，失败后**不**再直连重试（不产生两次请求）。"""
    from skillrouter_api.security.netpolicy import trust_env_for

    assert trust_env_for("http://127.0.0.1:9/v1/models") is False
    _RaisingClient.attempts = 0
    _RaisingClient.kwargs_seen = []
    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=False))
    assert result["ok"] is False
    assert result["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert _RaisingClient.attempts == 1
    assert _RaisingClient.kwargs_seen[0]["trust_env"] is False


def test_public_target_allows_one_direct_fallback(monkeypatch) -> None:
    """公网目标（`trust_env_for=True`）在网络类异常时允许一次直连回退。

    这里把 `trust_env_for` / `client_kwargs` 打桩成「外部主机」：本机 DNS 可能把任意
    主机名解析到私网地址（企业 DNS / hosts 覆盖），那样 `is_local_host` 会返回 True，
    断言就与运行环境耦合了。
    """
    monkeypatch.setattr(probe, "trust_env_for", lambda url: True)
    monkeypatch.setattr(
        probe,
        "client_kwargs",
        lambda url, **kwargs: {"trust_env": True, **kwargs},
    )
    _RaisingClient.attempts = 0
    _RaisingClient.kwargs_seen = []
    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    result = _run(
        probe.test_connection(
            _identity("openai", base_url="https://llm-probe.invalid/v1"),
            mode="auth",
            record=False,
        )
    )
    assert result["ok"] is False
    assert _RaisingClient.attempts == 2, "公网目标应允许一次直连回退"
    assert _RaisingClient.kwargs_seen[0]["trust_env"] is True
    assert _RaisingClient.kwargs_seen[1]["trust_env"] is False


# ── T-10：限流 ────────────────────────────────────────────────
def test_eleventh_probe_in_a_minute_is_throttled() -> None:
    from skillrouter_api.errors import ApiError

    identity = _identity("openai")
    for _ in range(10):
        probe.check_throttle(identity)
    with pytest.raises(ApiError) as excinfo:
        probe.check_throttle(identity)
    assert excinfo.value.code == "LLM_PROBE_THROTTLED"
    assert excinfo.value.status == 429
    assert excinfo.value.details["retry_after_s"] >= 1


def test_throttle_is_per_identity() -> None:
    probe.check_throttle(_identity("openai", secret="a" * 20))
    for _ in range(10):
        probe.check_throttle(_identity("anthropic", secret="b" * 20))
    # 换了身份不该被前一个身份的配额拖累
    assert probe._bucket_key(_identity("openai")) != probe._bucket_key(_identity("anthropic"))


# ── T-11：单飞 ────────────────────────────────────────────────
def test_concurrent_identical_probes_hit_upstream_once(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("openai")

    async def _gather() -> list[dict]:
        return await asyncio.gather(
            *[probe.test_connection(identity, mode="auth", record=False) for _ in range(5)]
        )

    results = _run(_gather())
    assert len(results) == 5
    assert all(item["ok"] for item in results)
    assert len(upstream.calls) == 1, "并发 5 次同一身份，上游只该被调用 1 次"


# ── T-12：脱敏 ────────────────────────────────────────────────
def test_upstream_echoed_secret_is_redacted(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f'{{"error":"invalid key {SECRET}"}}')

    monkeypatch.setattr(httpx, "AsyncClient", _echo_client(handler))
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=False))
    assert result["ok"] is False
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def _echo_client(handler):  # noqa: ANN001, ANN202
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return factory


# ── T-13：探测行落库且不含密钥 ───────────────────────────────
def test_probe_row_is_persisted_without_secret(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="openai", model_ids=["gpt-4o"])
    patch_httpx(monkeypatch, upstream)
    result = _run(probe.test_connection(_identity("openai"), mode="auth", record=True))
    assert result["probe_id"].startswith("llmp_")
    row = next(item for item in reversed(_rows()) if item.id == result["probe_id"])
    assert row.credential_id is None, "草稿测试不得写 credential_id"
    assert row.mode == "auth"
    assert row.ok == 1
    dumped = json.dumps({"stages": row.stages, "detail": row.detail_redacted}, ensure_ascii=False)
    assert SECRET not in dumped
    assert result["ok"] is True
