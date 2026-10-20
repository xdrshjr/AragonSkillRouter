"""模型目录的行为测试（T-14…T-19，spec §3.5 / §7.1）。

目录这一层的价值全在**降级**：上游抖动时不许让用户看到「一个模型都没有」，
但也绝不许把旧清单当成新地址的清单展示出来。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from skillrouter_api.db import db_write, now_iso
from skillrouter_api.models import LlmModelCatalog
from skillrouter_api.services.llm import catalog, probe, protocols
from skillrouter_api.services.llm import identity as llm_identity
from tests.fixtures.fake_upstream import FakeLlmUpstream, patch_httpx

SECRET = "sk-catalogsecret0123456"


@pytest.fixture(autouse=True)
def _clean_state():
    probe.reset_state()
    _clear_catalog()
    yield
    probe.reset_state()
    _clear_catalog()


def _clear_catalog() -> None:
    with db_write() as s:
        for row in list(s.scalars(select(LlmModelCatalog))):
            s.delete(row)


def _identity(protocol: str = "anthropic", base_url: str | None = None, **extra) -> object:
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
        base_url=extra.pop("base_url", None) or base_url or netloc,
        auth_style=extra.pop("auth_style", None) or spec.default_auth_style,
        header_name=None,
        secret=extra.pop("secret", SECRET),
        credential_id=extra.pop("credential_id", None),
        provider_id=None,
        source="draft",
        notes=[],
    )


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


def _backdate(
    identity, *, fetched_at: str, expires_at: str, models: list[dict] | None = None
) -> None:
    """把缓存行的时间戳改成「很久以前」，模拟过期 / 陈旧。"""
    key = catalog.dedupe_key(identity)
    with db_write() as s:
        row = s.scalar(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key))
        assert row is not None
        row.fetched_at = fetched_at
        row.expires_at = expires_at
        if models is not None:
            row.models = models
            row.model_count = len(models)


# ── T-14：命中未过期缓存不发请求 ─────────────────────────────
def test_fresh_cache_skips_upstream(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("anthropic")

    first = _run(catalog.list_models(identity, refresh=True, record_probe=False))
    assert first["source"] == "upstream"
    assert first["count"] == 1
    calls_after_first = len(upstream.calls)

    second = _run(catalog.list_models(identity, refresh=False, record_probe=False))
    assert second["source"] == "cache"
    assert len(upstream.calls) == calls_after_first, "未过期缓存不得再打上游"
    assert second["items"][0]["id"] == "claude-sonnet-4-5"


def test_expired_cache_hits_upstream_again(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("anthropic")
    _run(catalog.list_models(identity, refresh=True, record_probe=False))
    _backdate(identity, fetched_at="2020-01-01T00:00:00Z", expires_at="2020-01-01T00:10:00Z")

    again = _run(catalog.list_models(identity, refresh=False, record_probe=False))
    assert again["source"] == "upstream"
    assert len(upstream.models_calls) == 2


# ── T-15 / T-16：降级 ────────────────────────────────────────
def test_upstream_failure_falls_back_to_stale_cache(monkeypatch) -> None:
    identity = _identity("anthropic")
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    _run(catalog.list_models(identity, refresh=True, record_probe=False))

    # 12 分钟前的清单 + 现在上游 500 → 200 + cache-stale + warning
    _backdate(
        identity,
        fetched_at=_minutes_ago(12),
        expires_at="2020-01-01T00:00:00Z",
        models=[{"id": "claude-sonnet-4-5", "display_name": "Claude", "kind": "chat"}],
    )
    upstream.fail_models = True
    degraded = _run(catalog.list_models(identity, refresh=False, record_probe=False))
    assert degraded["source"] == "cache-stale"
    assert degraded["warning"] and "12 分钟前" in degraded["warning"]
    assert degraded["count"] == 1


def test_upstream_failure_without_cache_returns_503(monkeypatch) -> None:
    from skillrouter_api.errors import ApiError

    upstream = FakeLlmUpstream(protocol="anthropic", fail_models=True)
    patch_httpx(monkeypatch, upstream)
    with pytest.raises(ApiError) as excinfo:
        _run(catalog.list_models(_identity("anthropic"), refresh=False, record_probe=False))
    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"
    assert excinfo.value.status == 503
    assert excinfo.value.details["upstream"]["stage"] == "auth"


# ── T-17：分类与过滤 ────────────────────────────────────────
def test_classify_and_default_filter(monkeypatch) -> None:
    upstream = FakeLlmUpstream(
        protocol="openai", model_ids=["gpt-4o", "text-embedding-3-large", "whisper-1"]
    )
    patch_httpx(monkeypatch, upstream)
    identity = _identity("openai")
    result = _run(catalog.list_models(identity, refresh=True, record_probe=False))
    ids = [item["id"] for item in result["items"]]
    assert "gpt-4o" in ids
    assert "text-embedding-3-large" not in ids, "embedding 默认应被过滤"
    gpt = next(item for item in result["items"] if item["id"] == "gpt-4o")
    assert gpt["kind"] == "chat" and gpt["recommended"] is True

    with_all = _run(
        catalog.list_models(identity, refresh=False, include_non_llm=True, record_probe=False)
    )
    assert "text-embedding-3-large" in [item["id"] for item in with_all["items"]]


def test_classify_rules() -> None:
    assert catalog.classify("text-embedding-3-large") == "embedding"
    assert catalog.classify("bge-rerank-v2") == "rerank"
    assert catalog.classify("whisper-large-v3") == "audio"
    assert catalog.classify("dall-e-3") == "image"
    assert catalog.classify("glm-4-plus") == "chat"
    assert catalog.classify("mystery-model") == "other"
    assert catalog.is_recommended("claude-sonnet-4-5")
    assert not catalog.is_recommended("my-private-finetune")


# ── T-18：limit 上限 500 ────────────────────────────────────
def test_limit_is_capped_and_truncated() -> None:
    entries = [
        protocols.ModelEntry(id=f"model-{index:04d}", display_name=f"m{index}")
        for index in range(600)
    ]
    items, truncated = catalog.shape_items(entries, include_non_llm=False, limit=1000)
    assert len(items) == 500
    assert truncated is True


# ── T-19：anthropic 分页 ────────────────────────────────────
def test_anthropic_pagination_follows_has_more(monkeypatch) -> None:
    upstream = FakeLlmUpstream(
        protocol="anthropic",
        model_ids=["claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus"],
        page_size=1,
    )
    patch_httpx(monkeypatch, upstream)
    result = _run(catalog.list_models(_identity("anthropic"), refresh=True, record_probe=False))
    assert result["count"] == 3, "has_more=true 时必须按 last_id 续页"
    assert len(upstream.models_calls) == 3
    assert "after_id=" in upstream.models_calls[1]["url"]


def test_google_does_not_follow_next_page_token(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="google", model_ids=["gemini-2.5-pro"])
    patch_httpx(monkeypatch, upstream)
    result = _run(catalog.list_models(_identity("google"), refresh=True, record_probe=False))
    assert result["count"] == 1
    assert len(upstream.models_calls) == 1
    assert any("Google 只取第一页" in note for note in result["notes"])


# ── 失效时机（R-11）────────────────────────────────────────
def test_invalidate_drops_rows_for_credential(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("anthropic", credential_id="crd_llm_test")
    _run(catalog.list_models(identity, refresh=True, record_probe=False))
    assert catalog.invalidate("crd_llm_test") == 1
    with db_write() as s:
        assert s.scalar(select(LlmModelCatalog)) is None


def test_invalidate_by_target_uses_same_dedupe_key(monkeypatch) -> None:
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("anthropic")
    _run(catalog.list_models(identity, refresh=True, record_probe=False))
    removed = catalog.invalidate_by_target(identity.protocol, identity.wire, identity.base_url)
    assert removed == 1


def test_cache_row_with_other_base_url_is_not_used(monkeypatch) -> None:
    """命中校验：换了地址就必须当未命中，不能拿旧行凑一个 200（R-11）。"""
    upstream = FakeLlmUpstream(protocol="anthropic", model_ids=["claude-sonnet-4-5"])
    patch_httpx(monkeypatch, upstream)
    identity = _identity("anthropic")
    _run(catalog.list_models(identity, refresh=True, record_probe=False))

    other = _identity("anthropic", base_url="http://127.0.0.1:8")
    assert catalog.dedupe_key(other) != catalog.dedupe_key(identity)
    result = _run(catalog.list_models(other, refresh=True, record_probe=False))
    assert result["source"] == "upstream"


def test_ttl_is_clamped() -> None:
    from skillrouter_api.settings import settings

    original = settings.llm_catalog_ttl_s
    try:
        settings.llm_catalog_ttl_s = 5
        assert catalog.ttl_seconds() == catalog.TTL_MIN
        settings.llm_catalog_ttl_s = 10**9
        assert catalog.ttl_seconds() == catalog.TTL_MAX
    finally:
        settings.llm_catalog_ttl_s = original


def _minutes_ago(minutes: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (
        (datetime.now(UTC) - timedelta(minutes=minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_now_iso_is_utc() -> None:
    assert now_iso().endswith("Z")
