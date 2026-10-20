"""探测身份解析：**已保存凭证** 或 **未保存草稿** → 一个可测的 `ProbeIdentity`（spec §3.3）。

INV-3（密钥零落库零落日志）的落点就在这里：

- 明文密钥只存在于 `ProbeIdentity.secret`，它的生命周期 = 一次 `test_connection()`；
- 本模块不写库、不打日志、不把 secret 放进任何返回值；
- 草稿分支（`source="draft"`）没有 `credential_id`，因此探测行落库时那一列是 NULL。

解析优先级（顺序即实现顺序，不可调换，见 spec §3.3）：

1. 给了 `credential_id` → 走 `credentials.materialize(include_sealed=True)`；
2. 给了 `secret` → 草稿，`base_url` 必填；
3. 两者都给 → `VALIDATION_FAILED`；
4. 两者都不给 → `VALIDATION_FAILED`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...errors import ApiError, Code
from .. import credentials
from . import protocols


@dataclass(slots=True)
class ProbeIdentity:
    """一次探测的完整身份（含明文密钥，生命周期仅限本次调用）。"""

    protocol: str
    wire: str
    base_url: str
    auth_style: str
    header_name: str | None
    secret: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    paths: protocols.LlmPaths | None = None
    credential_id: str | None = None
    provider_id: str | None = None
    source: str = "draft"
    notes: list[str] = field(default_factory=list)
    inferred: bool = False
    username: str | None = None
    credential: dict | None = None

    #: 单飞/限流用的稳定键（不含密钥，也不含 secret 的任何派生）
    @property
    def dedupe_key(self) -> str:
        return f"{self.protocol}|{self.wire}|{self.base_url}|{self.credential_id or ''}"


#: `provider.slug` / `provider.name` 里的关键词 → 协议族（spec §3.3 第 1 条）
_PROTOCOL_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("anthropic", "claude"), "anthropic"),
    (("google", "gemini", "vertex"), "google"),
)


def infer_protocol_from_text(text: str | None) -> str | None:
    """按提供商命名推断协议族；认不出来返回 `None`（由调用方兜底并用 notes 说明）。"""
    haystack = (text or "").lower()
    for needles, protocol in _PROTOCOL_HINTS:
        if any(needle in haystack for needle in needles):
            return protocol
    return None


def _style_from_auth_type(
    auth_type: str | None, protocol: str, header_name: str | None
) -> tuple[str, str | None]:
    """凭证的 `auth_type`（executors 的四种）→ 本设计的 `auth_style`（§3.1 映射表）。"""
    value = (auth_type or "api_key").strip().lower()
    if value == "bearer":
        return "bearer", header_name
    if value == "basic":
        return "basic", header_name
    if value == "header":
        # 自定义头名与 api_key 同形（executors 里两者只差默认头名），映射到 api_key
        return "api_key", header_name
    return "api_key", header_name


def _paths_of(value: Any) -> protocols.LlmPaths | None:
    if value is None:
        return None
    if isinstance(value, protocols.LlmPaths):
        return value
    models = getattr(value, "models", None) if not isinstance(value, dict) else value.get("models")
    chat = getattr(value, "chat", None) if not isinstance(value, dict) else value.get("chat")
    if not models and not chat:
        return None
    return protocols.LlmPaths(models=models or None, chat=chat or None)


def _dict_of(value: Any) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _query_of(meta: dict) -> dict[str, str]:
    """`meta.api_version` 映射成 `?api-version=`（Azure OpenAI 这类端点必须带）。"""
    version = str(meta.get("api_version") or "").strip()
    return {"api-version": version} if version else {}


def infer_protocol(material: dict) -> tuple[str, bool]:
    """从 `materialize()` 的结果推断协议族，返回 `(protocol, inferred)`。

    优先读 `meta.llm_protocol`（用户显式选过），其次按提供商命名推断。
    """
    meta = material.get("meta") or {}
    declared = str(meta.get("llm_protocol") or "").strip().lower()
    if protocols.is_supported(declared):
        return declared, False
    guess = infer_protocol_from_text(
        f"{material.get('provider_slug') or ''} {material.get('provider_name') or ''}"
    )
    return guess or "openai", True


def resolve_identity(payload: Any) -> ProbeIdentity:
    """把请求 DTO（或任何带同名属性的对象）解析成 `ProbeIdentity`。"""
    declared_protocol = str(getattr(payload, "protocol", None) or "").strip().lower()
    if declared_protocol and not protocols.is_supported(declared_protocol):
        # 未知协议必须给出 LLM_PROTOCOL_UNSUPPORTED（而不是静默推断成 openai）
        protocols.get(declared_protocol)
    credential_id = str(getattr(payload, "credential_id", None) or "").strip()
    secret_value = getattr(payload, "secret", None)
    has_secret = bool(getattr(secret_value, "get_secret_value", lambda: secret_value)())
    if credential_id and has_secret:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="secret 与 credential_id 只能二选一",
            hint="要测新密钥就别带 credential_id；要测已保存凭证就别带 secret",
            details={
                "fields": [{"path": "secret", "message": "secret 与 credential_id 只能二选一"}]
            },
        )
    if not credential_id and not has_secret:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="需要 credential_id 或 secret",
            hint="选一条已保存凭证，或粘贴一把临时密钥（只用于本次测试，不落库）",
            details={"fields": [{"path": "secret", "message": "缺少 credential_id 或 secret"}]},
        )
    if credential_id:
        return _identity_from_credential(payload, credential_id)
    return _identity_from_draft(payload)


def _identity_from_credential(payload: Any, credential_id: str) -> ProbeIdentity:
    # `materialize()` 的抛错**就是结论**：不存在 → NOT_FOUND(404)；
    # 非 active → CREDENTIAL_DISABLED(409)。前端必须照实渲染。
    material = credentials.materialize(credential_id, include_sealed=True)
    meta = material.get("meta") or {}
    notes: list[str] = []
    inferred = False

    protocol = str(getattr(payload, "protocol", None) or "").strip().lower()
    if not protocols.is_supported(protocol):
        declared = str(meta.get("llm_protocol") or "").strip().lower()
        if protocols.is_supported(declared):
            protocol = declared
        else:
            guess = infer_protocol_from_text(
                f"{material.get('provider_slug') or ''} {material.get('provider_name') or ''}"
            )
            protocol = guess or "openai"
            inferred = True
            notes.append("协议族由提供商名推断：可在凭证 meta 里写 llm_protocol 固定下来")
    spec = protocols.get(protocol)
    wire = protocols.wire_of(
        protocol, str(getattr(payload, "wire", None) or meta.get("llm_wire") or "").strip() or None
    )

    base_raw = (
        str(getattr(payload, "base_url", None) or "").strip()
        or str(material.get("base_url") or "").strip()
        or spec.default_base_url
    )
    auth_style, header_name = _style_from_auth_type(
        material.get("auth_type"),
        protocol,
        str(
            getattr(payload, "header_name", None)
            or material.get("header_name")
            or meta.get("header_name")
            or ""
        ).strip()
        or None,
    )
    auth_style = (
        str(getattr(payload, "auth_style", None) or auth_style).strip().lower() or auth_style
    )
    # 协议只接受某一种认证头时（anthropic=x-api-key、google=x-goog-api-key），
    # 凭证写成 bearer 也按协议默认执行——Anthropic 的官方密钥只认 `x-api-key`，
    # 发 `Authorization: Bearer` 会 401（既有行为也正是这样，见 INV-4）。
    if auth_style not in spec.auth_styles:
        notes.append(f"该协议只使用 {spec.default_auth_style} 认证，已按协议默认执行")
        auth_style = spec.default_auth_style

    extra = _dict_of(meta.get("extra_headers"))
    extra.update(_dict_of(getattr(payload, "extra_headers", None)))
    query = _query_of(meta)
    query.update(_dict_of(getattr(payload, "query_params", None)))
    paths = _paths_of(getattr(payload, "paths", None)) or _paths_of(meta.get("llm_paths"))

    base_url, base_notes = protocols.normalize_base_url(protocol, base_raw, wire=wire)
    notes.extend(base_notes)
    if not base_url:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="自定义协议必须填上游地址（base_url）",
            hint="到提供商页填 base_url，或在设置页的「地址」里填一个 http(s) 地址",
            details={"field": "base_url"},
        )
    return ProbeIdentity(
        protocol=protocol,
        wire=wire,
        base_url=base_url,
        auth_style=auth_style,
        header_name=header_name,
        secret=str(material.get("secret") or ""),
        extra_headers=extra,
        query_params=query,
        paths=paths,
        credential_id=credential_id,
        provider_id=material.get("provider_id"),
        source="credential",
        notes=notes,
        inferred=inferred,
        username=str(meta.get("username") or "") or None,
        credential={
            "id": credential_id,
            "name": material.get("credential_name"),
            "secret_mask": material.get("secret_mask"),
        },
    )


def _identity_from_draft(payload: Any) -> ProbeIdentity:
    protocol = str(getattr(payload, "protocol", None) or "").strip().lower()
    spec = protocols.get(protocol)
    wire = protocols.wire_of(protocol, str(getattr(payload, "wire", None) or "").strip() or None)
    raw_secret = getattr(payload, "secret", None)
    secret = str(getattr(raw_secret, "get_secret_value", lambda: raw_secret)() or "").strip()
    if not secret:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="草稿密钥为空",
            details={"field": "secret"},
        )
    base_raw = str(getattr(payload, "base_url", None) or "").strip() or spec.default_base_url
    if not base_raw:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="自定义协议必须填上游地址（base_url）",
            hint="例如 https://your-gateway/v1",
            details={"field": "base_url"},
        )
    auth_style = (
        str(getattr(payload, "auth_style", None) or spec.default_auth_style).strip().lower()
    )
    base_url, notes = protocols.normalize_base_url(protocol, base_raw, wire=wire)
    return ProbeIdentity(
        protocol=protocol,
        wire=wire,
        base_url=base_url,
        auth_style=auth_style,
        header_name=str(getattr(payload, "header_name", None) or "").strip() or None,
        secret=secret,
        extra_headers=_dict_of(getattr(payload, "extra_headers", None)),
        query_params=_dict_of(getattr(payload, "query_params", None)),
        paths=_paths_of(getattr(payload, "paths", None)),
        credential_id=None,
        provider_id=None,
        source="draft",
        notes=notes,
        credential=None,
    )


def resolve_identity_for_credential(
    credential_id: str, *, protocol: str | None = None, model: str | None = None
) -> ProbeIdentity:
    """内部调用入口（`credentials_verify` / `agent_model.check()`）：只按凭证推导。

    协议优先级：显式 `protocol` 参数 > `meta.llm_protocol` > 提供商名推断。
    """
    material = credentials.materialize(credential_id, include_sealed=True)
    meta = material.get("meta") or {}
    resolved = (protocol or "").strip().lower()
    if not protocols.is_supported(resolved):
        resolved = str(meta.get("llm_protocol") or "").strip().lower()
    if not protocols.is_supported(resolved):
        guess = infer_protocol_from_text(
            f"{material.get('provider_slug') or ''} {material.get('provider_name') or ''}"
        )
        resolved = guess or "openai"

    class _Inner:
        """最小的目标对象：把 material 里的字段摊成 `ProbeTarget` 的形状。"""

        def __init__(self) -> None:
            self.protocol = resolved
            self.credential_id = credential_id
            self.secret = None
            self.base_url = None
            self.auth_style = None
            self.header_name = None
            self.extra_headers: dict[str, str] = {}
            self.query_params: dict[str, str] = {}
            self.paths = None
            self.wire = None
            self.model = model
            self.credential_name = material.get("credential_name")

    return _identity_from_credential(_Inner(), credential_id)


__all__ = [
    "ProbeIdentity",
    "infer_protocol",
    "infer_protocol_from_text",
    "resolve_identity",
    "resolve_identity_for_credential",
]
