"""Execution engine tests (CONTRACT §3.3, §12).

The outbound HTTP layer is replaced by a fake built on `GuardedResponse`, so
these tests exercise *our* engine: input validation, error mapping, usage/cost
extraction, idempotency and the "unknown outcome" rule. They do not prove any
particular provider behaves this way — that requires a live capability test.

`app.services.execution` imports `guarded_request` by name, so the fake is
installed both on `app.services.network` and on the execution module.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point the app at a temp data dir + fresh keyring for every test."""
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.delenv("SR_MASTER_KEYS", raising=False)
    monkeypatch.delenv("SR_MASTER_KEY_ACTIVE", raising=False)
    config.reset_settings_cache()
    settings = config.get_settings()
    settings.ensure_dirs()
    from app.core import db

    db.init_db()
    from app.security import crypto

    crypto.create_keyring(settings.keyring_path, overwrite=True)
    yield settings
    config.reset_settings_cache()


class FakeUpstream:
    """Records every outbound call and answers with a scripted response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responder: Callable[..., Any] | None = None

    def respond(self, responder: Callable[..., Any]) -> None:
        self.responder = responder

    async def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        assert self.responder is not None, "no responder configured"
        result = self.responder(method, url, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


def json_response(body: Any, *, status_code: int = 200, url: str = "https://api.example.com/v1/chat") -> Any:
    from app.services.network import GuardedResponse

    return GuardedResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
        url=url,
        elapsed_ms=7,
        redirects=[],
    )


@pytest.fixture()
def upstream(isolated, monkeypatch):
    from app.services import execution, network

    fake = FakeUpstream()
    monkeypatch.setattr(network, "guarded_request", fake)
    monkeypatch.setattr(execution, "guarded_request", fake)
    return fake


def _principal():
    from app.deps import AgentPrincipal

    return AgentPrincipal(
        agent_id="",
        agent_name="engine-test",
        token_id="",
        scopes=["*"],
        budget_usd=None,
        rate_limit_per_min=None,
        max_concurrency=None,
    )


def _config(**overrides: Any) -> dict[str, Any]:
    from app.services import capability_config

    config = capability_config.default_config("text")
    config["model"] = "test-model-1"
    config["auth"] = {"mode": "none"}
    config["modes"] = {"sync": True, "async": False, "stream": False}
    config["request"]["url"] = "https://api.example.com/v1/chat"
    config["request"]["body_template"] = {"model": "{{ model }}", "input": "{{ input.prompt }}"}
    config["input_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    config["output_schema"] = {}
    config["response"] = {
        "kind": "json",
        "extract": {
            "result": "$.choices[0].message.content",
            "usage_in": "$.usage.prompt_tokens",
            "usage_out": "$.usage.completion_tokens",
        },
        "artifact": None,
    }
    config["pricing"] = {
        "per_call": 0.0,
        "input_per_1k": 0.01,
        "output_per_1k": 0.02,
        "per_second": 0.0,
        "currency": "USD",
    }
    config.update(overrides)
    return config


def _make_capability(*, config: dict[str, Any], routes: int = 0, slug: str = "text.summarize") -> dict[str, Any]:
    """Insert a published capability + version (+ routes) directly via db helpers."""
    from app.core import db

    provider_ids = []
    cap_id = db.new_id("cap")
    version_id = db.new_id("cver")
    now = db.iso()
    db.insert(
        "capabilities",
        {
            "id": cap_id,
            "slug": slug,
            "name": "Summarize",
            "type": "text",
            "description": "",
            "provider_id": None,
            "credential_id": None,
            "status": "published",
            "current_version_id": version_id,
            "needs_live_agent": 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "capability_versions",
        {
            "id": version_id,
            "capability_id": cap_id,
            "version": 2,
            "status": "published",
            "config": db.dumps(config),
            "notes": "",
            "checksum": "",
            "created_by": None,
            "created_at": now,
            "published_at": now,
        },
    )
    if routes:
        for index in range(routes):
            provider_id = db.new_id("prov")
            provider_ids.append(provider_id)
            db.insert(
                "providers",
                {
                    "id": provider_id,
                    "name": f"Provider {index + 1}",
                    "slug": f"provider-{index + 1}-{provider_id[-6:]}",
                    "category": "llm",
                    "base_url": "https://api.example.com",
                    "docs_url": None,
                    "tags": "[]",
                    "environment": "production",
                    "notes": None,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            db.insert(
                "routes",
                {
                    "id": db.new_id("route"),
                    "capability_id": cap_id,
                    "capability_version_id": version_id,
                    "provider_id": provider_id,
                    "credential_id": None,
                    "strategy": "priority",
                    "priority": 10 * (index + 1),
                    "weight": 1,
                    "enabled": 1,
                    "health_status": "unknown",
                    "last_health_at": None,
                    "conditions": "{}",
                    "created_at": now,
                    "updated_at": now,
                },
            )
    return {"capability_id": cap_id, "version_id": version_id, "provider_ids": provider_ids, "slug": slug}


def _invoke(payload: dict[str, Any], *, slug: str = "text.summarize", **options: Any):
    from app.services import execution

    return asyncio.run(
        execution.invoke(_principal(), slug, payload, execution.InvokeOptions(surface="admin", **options))
    )


# ------------------------------------------------------------------- tests ---
def test_sync_success_extracts_result_usage_and_cost(upstream):
    from app.core import db

    rows = _make_capability(config=_config(), routes=1)
    upstream.respond(
        lambda *_: json_response(
            {
                "choices": [{"message": {"content": "the answer"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 30},
            }
        )
    )

    result = _invoke({"prompt": "hello"})

    assert result.status == "succeeded"
    assert result.result == "the answer"
    assert result.usage["input_tokens"] == 12
    assert result.usage["output_tokens"] == 30
    assert result.cost["estimated_usd"] > 0
    assert result.version == 2
    assert result.http_status == 200
    assert len(upstream.calls) == 1

    invocation = db.query_one("SELECT * FROM invocations WHERE id = ?", (result.invocation_id,))
    assert invocation["status"] == "succeeded"
    assert invocation["capability_id"] == rows["capability_id"]
    assert db.loads(invocation["usage"])["input_tokens"] == 12
    assert invocation["response_preview"] and "the answer" in invocation["response_preview"]


def test_unsupported_parameter_is_rejected_before_any_call(upstream):
    from app.core.errors import ApiError

    _make_capability(config=_config(), routes=1)

    with pytest.raises(ApiError) as excinfo:
        _invoke({"prompt": "hello", "temperature": 0.5})

    assert excinfo.value.code == "unsupported_parameter"
    assert excinfo.value.status_code == 422
    assert upstream.calls == [], "validation must happen before the provider is called"


def test_input_schema_mismatch_is_reported_with_the_field(upstream):
    from app.core.errors import ApiError

    _make_capability(config=_config(), routes=1)

    with pytest.raises(ApiError) as excinfo:
        _invoke({})

    assert excinfo.value.code == "input_schema_mismatch"
    assert excinfo.value.status_code == 422
    assert upstream.calls == []


def test_idempotency_key_dedupes_and_creates_no_second_invocation(upstream):
    from app.core import db

    _make_capability(config=_config(), routes=1)
    upstream.respond(lambda *_: json_response({"choices": [{"message": {"content": "once"}}], "usage": {}}))

    first = _invoke({"prompt": "hello"}, idempotency_key="idem-1")
    second = _invoke({"prompt": "hello"}, idempotency_key="idem-1")

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.invocation_id == first.invocation_id
    assert second.request_id == first.request_id
    assert len(upstream.calls) == 1, "the provider must be called once"
    assert db.query_scalar("SELECT COUNT(*) FROM invocations") == 1


def test_read_timeout_is_uncertain_and_never_fails_over(upstream):
    from app.core import db

    _make_capability(config=_config(), routes=2)

    def boom(method: str, url: str, kwargs: dict[str, Any]) -> BaseException:
        return httpx.ReadTimeout("read timed out")

    upstream.respond(boom)

    result = _invoke({"prompt": "hello"}, idempotency_key="timeout-1")

    assert result.status == "uncertain"
    assert result.error and result.error["code"] == "upstream_timeout_uncertain"
    assert result.result is None
    assert len(upstream.calls) == 1, "an unknown outcome must not be retried or failed over"

    invocation = db.query_one("SELECT * FROM invocations WHERE id = ?", (result.invocation_id,))
    assert invocation["status"] == "uncertain"
    assert invocation["error_code"] == "upstream_timeout_uncertain"


def test_published_capability_runs_without_the_agent_engine(upstream):
    _make_capability(config=_config(), routes=1)
    upstream.respond(lambda *_: json_response({"choices": [{"message": {"content": "ok"}}], "usage": {}}))

    result = _invoke({"prompt": "hello"})

    assert result.status == "succeeded"
    assert "app.services.agent_adapter" not in sys.modules, (
        "a plain published capability must never import the AragonAgent adapter"
    )
