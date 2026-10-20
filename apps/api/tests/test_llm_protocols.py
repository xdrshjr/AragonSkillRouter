"""协议注册表的契约测试（T-01…T-06、T-34，spec §3.1 / §3.2 / §7.1）。

这一组用例的存在理由很直接：地址拼错、认证头写错是这个功能的**主要失败模式**，
而且都是「一眼可见」的错。把它们做成参数化矩阵，回归时能直接定位到行号。
"""

from __future__ import annotations

import pytest

from skillrouter_api.executors import auth as executors_auth
from skillrouter_api.services.llm import protocols


class _Identity:
    """最小的探测身份（只为拼 URL / 造认证头，不含任何真实密钥）。"""

    def __init__(self, protocol: str, base_url: str = "", **extra) -> None:
        spec = protocols.get(protocol) if protocols.is_supported(protocol) else None
        self.protocol = protocol
        self.wire = extra.pop("wire", None) or (spec.wire if spec else None) or "openai"
        self.base_url = base_url
        self.secret = extra.pop("secret", "test-secret-value")
        self.auth_style = extra.pop("auth_style", None) or (
            spec.default_auth_style if spec else "bearer"
        )
        self.header_name = extra.pop("header_name", None)
        self.username = extra.pop("username", None)
        self.extra_headers = extra.pop("extra_headers", {})
        self.query_params = extra.pop("query_params", {})
        self.paths = extra.pop("paths", None)
        self.credential_id = extra.pop("credential_id", None)
        self.provider_id = None
        self.source = "draft"
        self.notes = []
        self.inferred = False
        self.credential = None


# ── T-01：8 行 URL 矩阵 ────────────────────────────────────────
@pytest.mark.parametrize(
    ("protocol", "wire", "raw_base", "expect_models", "expect_chat"),
    [
        # 1. openai 标准（base 已含 /v1）
        (
            "openai",
            None,
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/models",
            "https://api.openai.com/v1/chat/completions",
        ),
        # 2. openai 只填 host → 自动补 /v1
        (
            "openai",
            None,
            "https://api.openai.com",
            "https://api.openai.com/v1/models",
            "https://api.openai.com/v1/chat/completions",
        ),
        # 3. anthropic 用户贴了 /v1 → 剥掉再拼
        (
            "anthropic",
            None,
            "https://api.anthropic.com/v1",
            "https://api.anthropic.com/v1/models?limit=1000",
            "https://api.anthropic.com/v1/messages",
        ),
        # 4. anthropic 兼容网关（自托管，多段路径）
        (
            "anthropic",
            None,
            "https://open.bigmodel.cn/api/anthropic",
            "https://open.bigmodel.cn/api/anthropic/v1/models?limit=1000",
            "https://open.bigmodel.cn/api/anthropic/v1/messages",
        ),
        # 5. google 用户贴了 /v1beta → 剥掉再拼
        (
            "google",
            None,
            "https://generativelanguage.googleapis.com/v1beta",
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        ),
        # 6. custom + wire=openai（子路径）
        (
            "custom",
            "openai",
            "https://gw.internal/openai",
            "https://gw.internal/openai/models",
            "https://gw.internal/openai/chat/completions",
        ),
        # 7. custom + wire=anthropic 且未给 paths → 继承 wire 的默认路径（R-10）
        (
            "custom",
            "anthropic",
            "https://gw.internal/anthropic",
            "https://gw.internal/anthropic/v1/models?limit=1000",
            "https://gw.internal/anthropic/v1/messages",
        ),
        # 8. custom 用户已把完整路径贴进 base → 不得出现 /models/models（R-16）
        (
            "custom",
            "openai",
            "https://gw.internal/openai/models",
            "https://gw.internal/openai/models",
            "https://gw.internal/openai/chat/completions",
        ),
    ],
)
def test_compose_matrix(protocol, wire, raw_base, expect_models, expect_chat) -> None:
    base, _notes = protocols.normalize_base_url(protocol, raw_base, wire=wire)
    identity = _Identity(protocol, base, wire=wire)
    _method, models_url = protocols.build_models_request(protocols.get(protocol), identity)
    chat_url, _body = protocols.build_chat_request(
        protocols.get(protocol), identity, "gemini-2.5-pro"
    )
    assert models_url == expect_models
    assert chat_url == expect_chat


def test_custom_paths_inherit_from_wire() -> None:
    """T-34：`custom + wire=anthropic` 未给 paths 时**不得**退化成 chat/completions。"""
    paths = protocols.effective_paths("custom", "anthropic")
    assert paths.models == "/v1/models"
    assert paths.chat == "/v1/messages"
    assert protocols.effective_paths("custom", "google").chat == (
        "/v1beta/models/{model}:generateContent"
    )
    # 非 custom 一律用注册表常量，override 被忽略（避免不可解释状态）
    assert protocols.effective_paths("anthropic", "anthropic", {"chat": "/x"}).chat == (
        "/v1/messages"
    )


def test_custom_paths_can_be_overridden() -> None:
    paths = protocols.effective_paths("custom", "openai", {"models": "/v2/list", "chat": "/v2/go"})
    assert paths.models == "/v2/list"
    assert paths.chat == "/v2/go"


# ── T-02：归一化幂等 ──────────────────────────────────────────
@pytest.mark.parametrize(
    "protocol",
    ["openai", "anthropic", "google", "custom"],
)
def test_normalize_base_url_is_idempotent(protocol) -> None:
    raw = {
        "openai": "https://api.openai.com/v1/",
        "anthropic": "https://api.anthropic.com/v1/",
        "google": "https://generativelanguage.googleapis.com/v1beta/",
        "custom": "https://gw.internal/v1",
    }[protocol]
    once, notes_first = protocols.normalize_base_url(protocol, raw)
    twice, notes_second = protocols.normalize_base_url(protocol, once)
    assert once == twice
    assert notes_second == [] or notes_second == notes_first


def test_normalize_empty_is_empty() -> None:
    assert protocols.normalize_base_url("openai", "  ") == ("", [])


# ── T-03：SSRF ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    ["ftp://example.com/v1", "file:///etc/passwd", "http://169.254.169.254/latest"],
)
def test_normalize_rejects_non_http_and_metadata(raw) -> None:
    from skillrouter_api.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        protocols.normalize_base_url("openai", raw)
    assert excinfo.value.code == "SSRF_BLOCKED"


# ── T-04：认证头 ─────────────────────────────────────────────
def test_auth_headers_per_protocol() -> None:
    assert protocols.build_auth_headers(_Identity("openai"))["Authorization"] == (
        "Bearer test-secret-value"
    )

    anthropic = protocols.build_auth_headers(_Identity("anthropic"))
    assert anthropic["x-api-key"] == "test-secret-value"
    assert anthropic["anthropic-version"] == "2023-06-01"

    google = protocols.build_auth_headers(_Identity("google"))
    assert google["x-goog-api-key"] == "test-secret-value"

    custom = protocols.build_auth_headers(
        _Identity("custom", auth_style="header", header_name="X-Gw-Key")
    )
    assert custom["X-Gw-Key"] == "test-secret-value"


def test_auth_style_none_produces_no_auth_header() -> None:
    headers = protocols.build_auth_headers(_Identity("custom", auth_style="none"))
    assert "Authorization" not in headers
    assert "X-API-Key" not in headers
    # 协议级常量头仍然在（content-type 与密钥无关）
    assert headers["content-type"] == "application/json"


def test_auth_headers_delegate_to_executors() -> None:
    """T-04：与直接调 `executors.auth.build_auth_headers` **逐键一致**（防第二份实现）。"""
    default_headers = {
        "anthropic": "x-api-key",
        "google": "x-goog-api-key",
        "openai": "X-API-Key",
        "custom": "X-API-Key",
    }
    cases = [
        ("openai", "bearer", None),
        ("anthropic", "api_key", None),
        ("google", "api_key", None),
        ("custom", "api_key", "X-Custom-Key"),
    ]
    for protocol, style, header_name in cases:
        identity = _Identity(protocol, auth_style=style, header_name=header_name)
        ours = protocols.build_auth_headers(identity)
        spec = protocols.get(protocol)
        direct = executors_auth.build_auth_headers(
            {
                "auth_type": style,
                "secret": identity.secret,
                "header_name": header_name or default_headers[protocol],
                "meta": {},
            }
        )
        # 去掉协议级常量头（anthropic-version / content-type）后必须逐键相等
        filtered = {
            key: value
            for key, value in ours.items()
            if key not in spec.auth_extra_headers and key != "anthropic-version"
        }
        assert filtered == direct, f"{protocol} 的认证头与 executors 不一致（出现了第二份实现）"


# ── T-05 / T-06：四种上游形状的解析 ──────────────────────────
def test_parse_openai_shape() -> None:
    payload = {"object": "list", "data": [{"id": "gpt-4o", "created": 1, "owned_by": "openai"}]}
    entries = protocols.parse_models(protocols.get("openai"), payload)
    assert [e.id for e in entries] == ["gpt-4o"]
    assert entries[0].display_name == "gpt-4o"
    assert entries[0].created_at is not None and entries[0].created_at.endswith("Z")


def test_parse_anthropic_shape() -> None:
    payload = {
        "data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5", "type": "model"}],
        "has_more": False,
        "last_id": "claude-sonnet-4-5",
    }
    entries = protocols.parse_models(protocols.get("anthropic"), payload)
    assert entries[0].id == "claude-sonnet-4-5"
    assert entries[0].display_name == "Claude Sonnet 4.5"


def test_parse_google_shape_strips_models_prefix() -> None:
    payload = {
        "models": [
            {
                "name": "models/gemini-2.5-pro",
                "displayName": "Gemini 2.5 Pro",
                "inputTokenLimit": 1_048_576,
                "supportedGenerationMethods": ["generateContent"],
            }
        ]
    }
    entries = protocols.parse_models(protocols.get("google"), payload)
    assert entries[0].id == "gemini-2.5-pro"
    assert entries[0].context_window == 1_048_576
    assert entries[0].methods == ["generateContent"]


def test_parse_fallback_keys() -> None:
    spec = protocols.get("custom")
    for payload in (
        {"items": [{"id": "a"}]},
        {"result": {"data": [{"id": "a"}]}},
        [{"id": "a"}],
    ):
        assert [e.id for e in protocols.parse_models(spec, payload)] == ["a"]


def test_parse_without_model_array_raises_invalid_response() -> None:
    """T-06：200 但没有模型数组 → `UPSTREAM_INVALID_RESPONSE`（不得静默返回空列表）。"""
    from skillrouter_api.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        protocols.parse_models(protocols.get("openai"), {"detail": "not a list"})
    assert excinfo.value.code == "UPSTREAM_INVALID_RESPONSE"


# ── 协议注册表本身 ───────────────────────────────────────────
def test_public_catalog_exposes_four_protocols() -> None:
    catalog = protocols.public_catalog()
    assert [item["key"] for item in catalog] == ["openai", "anthropic", "google", "custom"]
    custom = catalog[-1]
    # custom 的路径取决于用户选的 wire → 必须回 null，而不是假装有默认路径
    assert custom["models_path"] is None and custom["chat_path"] is None
    assert custom["supports_models_list"] is True
    assert custom["paths_inherit_from_wire"] is True


def test_unknown_protocol_raises_llm_protocol_unsupported() -> None:
    from skillrouter_api.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        protocols.get("zzz")
    assert excinfo.value.code == "LLM_PROTOCOL_UNSUPPORTED"
    assert excinfo.value.status == 400


def test_wire_of_rejects_bad_wire() -> None:
    from skillrouter_api.errors import ApiError

    assert protocols.wire_of("custom", None) == "openai"
    assert protocols.wire_of("custom", "anthropic") == "anthropic"
    assert protocols.wire_of("anthropic", "openai") == "anthropic"
    with pytest.raises(ApiError):
        protocols.wire_of("custom", "zzz")


def test_looks_like_completion_and_extract_text() -> None:
    anthropic = protocols.get("anthropic")
    payload = {"type": "message", "content": [{"type": "text", "text": "正常"}]}
    assert protocols.looks_like_completion(anthropic, payload)
    assert protocols.extract_text(anthropic, payload) == "正常"
    assert not protocols.looks_like_completion(anthropic, {"detail": "x"})

    google = protocols.get("google")
    g_payload = {"candidates": [{"content": {"parts": [{"text": "好"}]}}]}
    assert protocols.looks_like_completion(google, g_payload)
    assert protocols.extract_text(google, g_payload) == "好"

    openai = protocols.get("openai")
    o_payload = {"choices": [{"message": {"content": "hi"}}]}
    assert protocols.looks_like_completion(openai, o_payload)
    assert protocols.extract_text(openai, o_payload) == "hi"
