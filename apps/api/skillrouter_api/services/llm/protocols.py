"""四种 LLM 线协议的唯一真相源（INV-1 / spec §3.1、§3.2）。

**为什么必须只有这一份**：地址拼接、认证头、取模型路径、探测/完成请求体、响应解析，
在历史上的仓库里出现过两份（`services/agent_model.py::_probe_request` 与
`services/credentials_verify.py::PROBES`），于是 Anthropic 的 `GET /models` 永远 404。
本模块把「协议差异」收敛成**数据**（`ProtocolSpec`），其它任何模块都只允许调用这里的函数，
不允许再写 `if protocol == "anthropic": ...`。

两条委托纪律（ADR-12）：

- 认证头**必须**委托 `executors.auth.build_auth_headers`（那里已有 api_key / bearer /
  basic / header 四种实现）；本模块只补「协议级常量头」（`anthropic-version` /
  `content-type`），`auth_style="none"` 在**本模块内**短路上，不改 `executors.auth`。
- 出站代理策略**不在这里**做：由 `security/netpolicy.client_kwargs()` 决定 `trust_env`。

`custom` 的语义（ADR-1 / ADR-11）是「地址与路径可改，线协议仍是三者之一」：
它没有自己的线协议，`paths` 未显式给出时**继承所选 `wire`** 的默认路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from ...errors import ApiError, Code
from ...security.netpolicy import validate_admin_target

#: 对外暴露的协议键（顺序即前端卡片顺序）
PROTOCOL_KEYS: tuple[str, ...] = ("openai", "anthropic", "google", "custom")
#: 真正的线协议键（= 网关 `initProviders()` 注册的 provider id）
WIRE_KEYS: tuple[str, ...] = ("openai", "anthropic", "google")

#: `auth_style` → `executors.auth.build_auth_headers` 的 `auth_type`（ADR-12）
AUTH_STYLES: tuple[str, ...] = ("bearer", "api_key", "basic", "header", "none")
_AUTH_TYPE_OF_STYLE = {
    "bearer": "bearer",
    "api_key": "api_key",
    "basic": "basic",
    "header": "header",
}

#: `api_key` / `header` 未指定头名时的协议默认值（`header` 用最后一项兜底）
_DEFAULT_HEADER_BY_PROTOCOL: dict[str, str] = {
    "anthropic": "x-api-key",
    "google": "x-goog-api-key",
    "openai": "X-API-Key",
    "custom": "X-API-Key",
}

#: 所有协议都要带的非密钥头（GET 带 content-type 无害，POST 必须有）
_COMMON_EXTRA_HEADERS: dict[str, str] = {"content-type": "application/json"}

#: 一次最小生成的 token 上限。思考型模型（glm / o1 等）会先吐 thinking，
#: 预算太小会让整次探测只拿到思考、没有正文 —— 沿用 `agent_model` 的既有取值。
PROBE_MAX_TOKENS = 512
#: 探测提示词：要求极短回答，尽量把预算留给正文
PROBE_PROMPT = "只回复两个字：正常"


@dataclass(frozen=True, slots=True)
class LlmPaths:
    """取模型 / 完成的路径覆盖（只有 `custom` 接受；其余协议忽略）。"""

    models: str | None = None
    chat: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"models": self.models, "chat": self.chat}


@dataclass(frozen=True, slots=True)
class ProtocolSpec:
    """一种线协议的全部静态差异。字段含义见 spec §3.1 的字段表。"""

    key: str
    label: str
    label_zh: str
    one_liner: str
    wire: str | None
    default_base_url: str
    base_url_hint: str
    docs_url: str
    auth_styles: tuple[str, ...]
    default_auth_style: str
    models_path: str | None
    chat_path: str | None
    chat_body: str
    models_array: str
    model_id_field: str
    model_name_field: str
    models_params: dict[str, str] = field(default_factory=dict)
    strip_version_suffix: str | None = None
    version_header: tuple[str, str] | None = None
    auth_extra_headers: dict[str, str] = field(default_factory=lambda: dict(_COMMON_EXTRA_HEADERS))
    paginate: str | None = None
    supports_models_list: bool = True

    def public_dict(self) -> dict:
        """`GET /api/admin/llm/protocols` 的单项（前端**唯一**的协议知识来源）。"""
        return {
            "key": self.key,
            "label": self.label,
            "label_zh": self.label_zh,
            "one_liner": self.one_liner,
            "wire": self.wire,
            "default_base_url": self.default_base_url,
            "base_url_hint": self.base_url_hint,
            "docs_url": self.docs_url,
            "auth_styles": list(self.auth_styles),
            "default_auth_style": self.default_auth_style,
            # `custom` 的路径取决于用户选的 wire → 回 null，由 effective_paths() 现算
            "models_path": self.models_path,
            "chat_path": self.chat_path,
            "supports_models_list": self.supports_models_list,
            "paths_inherit_from_wire": self.key == "custom",
            "paginate": self.paginate,
        }


_SPECS: dict[str, ProtocolSpec] = {
    "openai": ProtocolSpec(
        key="openai",
        label="OpenAI",
        label_zh="OpenAI",
        one_liner="Chat Completions 与 /models",
        wire="openai",
        default_base_url="https://api.openai.com/v1",
        base_url_hint="https://api.openai.com/v1（含 /v1）",
        docs_url="https://platform.openai.com/docs",
        auth_styles=("bearer",),
        default_auth_style="bearer",
        models_path="models",
        chat_path="chat/completions",
        chat_body="openai",
        models_array="data",
        model_id_field="id",
        model_name_field="name",
    ),
    "anthropic": ProtocolSpec(
        key="anthropic",
        label="Anthropic",
        label_zh="Anthropic",
        one_liner="Messages API（Claude 家族）",
        wire="anthropic",
        default_base_url="https://api.anthropic.com",
        base_url_hint="https://api.anthropic.com（不要带 /v1）",
        docs_url="https://docs.anthropic.com",
        auth_styles=("api_key",),
        default_auth_style="api_key",
        models_path="/v1/models",
        chat_path="/v1/messages",
        chat_body="anthropic",
        models_array="data",
        model_id_field="id",
        model_name_field="display_name",
        models_params={"limit": "1000"},
        strip_version_suffix="v1",
        version_header=("anthropic-version", "2023-06-01"),
        paginate="anthropic_after_id",
    ),
    "google": ProtocolSpec(
        key="google",
        label="Google",
        label_zh="Google",
        one_liner="Gemini generateContent",
        wire="google",
        default_base_url="https://generativelanguage.googleapis.com",
        base_url_hint="https://generativelanguage.googleapis.com（不要带 /v1beta）",
        docs_url="https://ai.google.dev/api",
        auth_styles=("api_key",),
        default_auth_style="api_key",
        models_path="/v1beta/models",
        chat_path="/v1beta/models/{model}:generateContent",
        chat_body="google",
        models_array="models",
        model_id_field="name",
        model_name_field="displayName",
        models_params={"pageSize": "1000"},
        strip_version_suffix="v1beta",
        # 只取第一页（pageSize=1000 已覆盖 Gemini 全部模型），不续页，见 spec §3.5 / R-17
        paginate="google_next_page_token",
    ),
    "custom": ProtocolSpec(
        key="custom",
        label="Custom",
        label_zh="自定义",
        one_liner="任意兼容网关（vLLM / LiteLLM / One-API / 自建）",
        wire=None,
        default_base_url="",
        base_url_hint="https://your-gateway/v1",
        docs_url="",
        auth_styles=AUTH_STYLES,
        default_auth_style="bearer",
        models_path=None,
        chat_path=None,
        chat_body="openai",
        models_array="data",
        model_id_field="id",
        model_name_field="name",
        paginate=None,
    ),
}

#: 取模型响应里可能的数组键（兜底顺序，spec §3.5 的「兜底」行）
_MODEL_ARRAY_FALLBACKS: tuple[str, ...] = ("data", "models", "items")


@dataclass(slots=True)
class ModelEntry:
    """归一化后的一个模型（kind / recommended 由 `catalog.normalize()` 填）。"""

    id: str
    display_name: str
    kind: str = "other"
    recommended: bool = False
    context_window: int | None = None
    methods: list[str] = field(default_factory=list)
    created_at: str | None = None
    owned_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "kind": self.kind,
            "recommended": self.recommended,
            "context_window": self.context_window,
            "methods": list(self.methods),
            "created_at": self.created_at,
            "owned_by": self.owned_by,
        }


def get(protocol: str) -> ProtocolSpec:
    """按协议键取规格；未知键 → `LLM_PROTOCOL_UNSUPPORTED`（400）。"""
    key = (protocol or "").strip().lower()
    spec = _SPECS.get(key)
    if spec is None:
        raise ApiError(
            Code.LLM_PROTOCOL_UNSUPPORTED,
            message=f"不支持的协议规范：{protocol or '(空)'}",
            hint=f"只支持 {' / '.join(PROTOCOL_KEYS)}",
            details={"allowed": list(PROTOCOL_KEYS), "field": "protocol"},
        )
    return spec


def is_supported(protocol: str) -> bool:
    return (protocol or "").strip().lower() in _SPECS


def public_catalog() -> list[dict]:
    """`GET /api/admin/llm/protocols` 的 `items`。"""
    return [_SPECS[key].public_dict() for key in PROTOCOL_KEYS]


def wire_of(protocol: str, override: str | None = None) -> str:
    """协议 → 网关 `providerId`。`custom` 用用户选的 `wire`（默认 openai）。"""
    spec = get(protocol)
    if spec.wire:
        return spec.wire
    candidate = (override or "").strip().lower()
    if not candidate:
        return "openai"
    if candidate not in WIRE_KEYS:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"wire 只能是 {' / '.join(WIRE_KEYS)}",
            details={"field": "wire", "allowed": list(WIRE_KEYS)},
        )
    return candidate


def effective_paths(protocol: str, wire: str, override: LlmPaths | Any | None = None) -> LlmPaths:
    """取模型 / 完成路径。

    非 `custom` 一律用注册表里的常量（`override` 被忽略，避免出现「选 anthropic 却改路径」
    的不可解释状态）。`custom` 未显式给出时**继承所选 `wire` 的默认值**（ADR-11 / R-10）。
    """
    spec = get(protocol)
    if spec.key != "custom":
        return LlmPaths(models=spec.models_path, chat=spec.chat_path)
    base = get(wire)
    models = _override_value(override, "models") or base.models_path
    chat = _override_value(override, "chat") or base.chat_path
    return LlmPaths(models=models, chat=chat)


def _override_value(override: Any, key: str) -> str | None:
    """从 `LlmPaths` / pydantic 模型 / dict 里取一个可选的路径覆盖。"""
    if override is None:
        return None
    if isinstance(override, dict):
        value = override.get(key)
    else:
        value = getattr(override, key, None)
    text = str(value or "").strip()
    return text or None


def normalize_base_url(
    protocol: str, raw: str, *, wire: str | None = None
) -> tuple[str, list[str]]:
    """把用户贴的地址归一化成「注册表期望的形态」，并回可读的提示（spec §3.2）。"""
    notes: list[str] = []
    url = (raw or "").strip()
    if url == "":
        return "", notes
    validate_admin_target(url)
    url = url.rstrip("/")
    spec = get(protocol)
    seg = spec.strip_version_suffix
    if seg:
        tail = "/" + seg
        if url.lower().endswith(tail):
            url = url[: -len(tail)]
            notes.append(f"已从地址尾部去掉重复的版本段 /{seg}")
    effective_wire = wire or (spec.wire or "openai")
    if spec.key == "openai" or (spec.key == "custom" and effective_wire == "openai"):
        parsed = urlparse(url)
        if parsed.path in ("", "/"):
            url += "/v1"
            notes.append("已按 OpenAI 规范补上 /v1")
    return url, notes


def compose_url(base_url: str, path: str, query: dict[str, str] | None = None) -> str:
    """把 base 与 path 拼成一个 URL，规则见 spec §3.2（R1 / R2 / 兜底）。

    R1：用户已经把完整路径贴进 base（`…/v1/messages`）→ 不重复拼接。
    R2：base 尾部与 path 首段重合（`…/v1` + `/v1/models`）→ 去掉 path 的重合首段。
        前置 `len(path_segments) > 1`：单段 path 在 R1 已必定命中，留着会拼出尾斜杠。
    """
    base = (base_url or "").rstrip("/")
    p = path if path.startswith("/") else "/" + path
    path_only, _, existing_qs = p.partition("?")
    base_segments = [s for s in urlparse(base).path.split("/") if s]
    path_segments = [s for s in path_only.split("/") if s]
    if (
        path_segments
        and len(base_segments) >= len(path_segments)
        and (base_segments[-len(path_segments) :] == path_segments)
    ):
        url = base
    elif base_segments and len(path_segments) > 1 and base_segments[-1] == path_segments[0]:
        url = f"{base}/{'/'.join(path_segments[1:])}"
    else:
        url = f"{base}{path_only}"
    merged = {**dict(parse_qsl(existing_qs)), **(query or {})}
    return f"{url}?{urlencode(merged)}" if merged else url


def build_auth_headers(identity: Any) -> dict[str, str]:
    """认证头 + 协议级常量头（委托 `executors.auth`，见 ADR-12 / R-05）。"""
    from ...executors.auth import build_auth_headers as executors_build_auth_headers

    spec = get(getattr(identity, "protocol", ""))
    style = str(getattr(identity, "auth_style", "") or spec.default_auth_style).strip().lower()
    headers: dict[str, str] = {}
    if style != "none":
        auth_type = _AUTH_TYPE_OF_STYLE.get(style)
        if auth_type is None:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=f"不支持的认证方式：{style}",
                details={"field": "auth_style", "allowed": list(AUTH_STYLES)},
            )
        header_name = str(getattr(identity, "header_name", "") or "").strip()
        if auth_type in ("api_key", "header") and not header_name:
            header_name = _DEFAULT_HEADER_BY_PROTOCOL.get(spec.key, "X-API-Key")
        material = {
            "auth_type": auth_type,
            "secret": getattr(identity, "secret", "") or "",
            "header_name": header_name or None,
            "meta": {"username": getattr(identity, "username", None)},
        }
        headers.update(executors_build_auth_headers(material))
    headers.update(spec.auth_extra_headers)
    if spec.version_header:
        headers[spec.version_header[0]] = spec.version_header[1]
    return headers


def merge_headers(identity: Any) -> dict[str, str]:
    """`extra_headers` 打底、认证头覆盖（同名时以认证头为准，避免被明文键盖掉密钥）。"""
    headers = {str(k): str(v) for k, v in (getattr(identity, "extra_headers", None) or {}).items()}
    headers.update(build_auth_headers(identity))
    return headers


def _spec_of(identity: Any) -> ProtocolSpec:
    """身份实际使用的**线协议**规格（`custom` 用它的 `wire`）。

    取模型清单的 `limit` / `pageSize` 属于线协议而不是「custom 这个外壳」，
    所以 `custom + wire=anthropic` 必须带上 `limit=1000`（否则矩阵第 7 行不成立）。
    """
    spec = get(getattr(identity, "protocol", ""))
    if spec.key == "custom":
        return get(wire_of("custom", getattr(identity, "wire", None)))
    return spec


def models_query(identity: Any) -> dict[str, str]:
    """取模型清单的查询参数：协议默认 + 用户的 `query_params`（后者覆盖前者）。"""
    query = dict(_spec_of(identity).models_params)
    query.update(
        {str(k): str(v) for k, v in (getattr(identity, "query_params", None) or {}).items()}
    )
    return query


def chat_query(identity: Any) -> dict[str, str]:
    """完成请求的查询参数：**只**用用户的 `query_params`。

    `limit` / `pageSize` 是「列清单」的参数，带到生成请求上会被部分上游当非法参数
    （实测 Anthropic 对未知查询参数会 400），因此不能与 `models_query()` 共用。
    """
    return {str(k): str(v) for k, v in (getattr(identity, "query_params", None) or {}).items()}


def build_models_request(spec: ProtocolSpec, identity: Any) -> tuple[str, str]:
    """返回 `(method, url)`。"""
    paths = effective_paths(
        spec.key,
        wire_of(spec.key, getattr(identity, "wire", None)),
        getattr(identity, "paths", None),
    )
    if paths.models is None:  # pragma: no cover - 注册表保证非空
        raise ApiError(Code.LLM_PROTOCOL_UNSUPPORTED, message="该协议没有取模型路径")
    url = compose_url(getattr(identity, "base_url", ""), paths.models, models_query(identity))
    return "GET", url


def strip_models_suffix(base_url: str, models_path: str | None) -> str:
    """base 尾部就是「取模型路径」时把它剥掉（矩阵第 8 行的 chat URL 需要）。

    用户常把完整地址贴进 base（`https://gw.internal/openai/models`）。取模型时
    `compose_url` 的 R1 会命中（不重复拼接），但生成请求必须回到**同一前缀**
    （`…/openai/chat/completions`），否则会拼出 `…/models/chat/completions`。
    """
    if not models_path:
        return base_url
    base = (base_url or "").rstrip("/")
    tail_segments = [s for s in models_path.split("?")[0].split("/") if s]
    if not tail_segments:
        return base
    parsed = urlparse(base)
    base_segments = [s for s in parsed.path.split("/") if s]
    if len(base_segments) <= len(tail_segments):
        return base
    if base_segments[-len(tail_segments) :] != tail_segments:
        return base
    prefix = "/" + "/".join(base_segments[: -len(tail_segments)])
    rebuilt = parsed._replace(path=prefix).geturl()
    return rebuilt.rstrip("/")


def build_chat_request(
    spec: ProtocolSpec,
    identity: Any,
    model: str,
    prompt: str | None = None,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    """返回 `(url, body)`：一次最小的真实生成请求。"""
    wire = wire_of(spec.key, getattr(identity, "wire", None))
    paths = effective_paths(spec.key, wire, getattr(identity, "paths", None))
    if paths.chat is None:  # pragma: no cover - 注册表保证非空
        raise ApiError(Code.LLM_PROTOCOL_UNSUPPORTED, message="该协议没有完成请求路径")
    path = paths.chat.replace("{model}", model)
    base = strip_models_suffix(getattr(identity, "base_url", ""), paths.models)
    url = compose_url(base, path, chat_query(identity))
    body_style = spec.chat_body if spec.key != "custom" else get(wire).chat_body
    text = prompt if prompt is not None else PROBE_PROMPT
    budget = max_tokens if max_tokens is not None else PROBE_MAX_TOKENS
    if body_style == "anthropic":
        body = {
            "model": model,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": text}],
        }
    elif body_style == "google":
        body = {"contents": [{"role": "user", "parts": [{"text": text}]}]}
    else:
        body = {
            "model": model,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": text}],
        }
    return url, body


def parse_models(spec: ProtocolSpec, payload: dict) -> list[ModelEntry]:
    """把四种上游形状的模型清单归一化成 `ModelEntry[]`（spec §3.5 的抽取规则）。"""
    if isinstance(payload, list):
        rows: Any = payload
    elif isinstance(payload, dict):
        rows = _find_model_array(spec, payload)
    else:
        rows = None
    if rows is None:
        raise ApiError(
            Code.UPSTREAM_INVALID_RESPONSE,
            message="上游 200 但响应里没有模型数组，可能是错误的路径",
            hint="检查地址是否正确、该网关是否实现 /models；也可以手填模型名",
            details={"field": "models", "protocol": spec.key},
        )
    entries: list[ModelEntry] = []
    for row in rows:
        if isinstance(row, str):
            row = {"id": row}
        if not isinstance(row, dict):
            continue
        raw_id = row.get(spec.model_id_field) or row.get("id") or row.get("name") or ""
        model_id = str(raw_id).strip()
        if spec.key == "google" and model_id.startswith("models/"):
            model_id = model_id[len("models/") :]
        if not model_id:
            continue
        display = row.get(spec.model_name_field) or row.get("display_name") or row.get("id")
        methods = row.get("supportedGenerationMethods")
        entries.append(
            ModelEntry(
                id=model_id,
                display_name=str(display or model_id),
                context_window=_as_int(row.get("inputTokenLimit") or row.get("context_window")),
                methods=[str(m) for m in methods] if isinstance(methods, list) else [],
                created_at=_as_iso(row.get("created_at") or row.get("created")),
                owned_by=str(row.get("owned_by") or "") or None,
            )
        )
    return entries


def _find_model_array(spec: ProtocolSpec, payload: dict) -> Any:
    """按 `models_array` 取数组；取不到再按兜底顺序试，最后试 `result.data`。"""
    primary = payload.get(spec.models_array)
    if isinstance(primary, list):
        return primary
    for key in _MODEL_ARRAY_FALLBACKS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    nested = payload.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("data"), list):
        return nested["data"]
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_iso(value: Any) -> str | None:
    """上游有的给秒级时间戳、有的给 ISO 串；统一成 ISO8601 Z。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        from datetime import UTC, datetime

        return (
            datetime.fromtimestamp(float(value), UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = str(value).strip()
    return text or None


def models_has_more(spec: ProtocolSpec, payload: dict) -> tuple[bool, str | None]:
    """续页判据：anthropic 用 `has_more` + `last_id`；google 只取第一页（R-17）。"""
    if spec.paginate == "anthropic_after_id" and isinstance(payload, dict):
        if bool(payload.get("has_more")):
            last_id = payload.get("last_id") or payload.get("lastId")
            return True, str(last_id) if last_id else None
    return False, None


def looks_like_completion(spec: ProtocolSpec, payload: dict, wire: str | None = None) -> bool:
    """响应体是否是该协议的**一次完成结果**（不是错误信封、不是别的 API）。"""
    if not isinstance(payload, dict):
        return False
    body_style = get(wire or spec.key).chat_body if spec.key == "custom" else spec.chat_body
    if body_style == "anthropic":
        return isinstance(payload.get("content"), list) or payload.get("type") == "message"
    if body_style == "google":
        return isinstance(payload.get("candidates"), list)
    return isinstance(payload.get("choices"), list)


def extract_text(spec: ProtocolSpec, payload: dict, wire: str | None = None) -> str:
    """从完成响应里取出文本（拿不到就返回空串，不抛错）。"""
    body_style = get(wire or spec.key).chat_body if spec.key == "custom" else spec.chat_body
    try:
        if body_style == "anthropic":
            blocks = payload.get("content") or []
            return "".join(
                str(block.get("text") or "") for block in blocks if isinstance(block, dict)
            ).strip()
        if body_style == "google":
            candidates = payload.get("candidates") or []
            if candidates:
                parts = ((candidates[0].get("content") or {}).get("parts")) or []
                return "".join(
                    str(part.get("text") or "") for part in parts if isinstance(part, dict)
                ).strip()
            return ""
        choices = payload.get("choices") or []
        if choices:
            return str((choices[0].get("message") or {}).get("content") or "").strip()
    except (AttributeError, TypeError, IndexError, KeyError):
        return ""
    return ""


def with_paths(spec: ProtocolSpec, paths: LlmPaths) -> ProtocolSpec:
    """按 `effective_paths()` 的结果派生一份规格（自定义路径时用，不污染注册表）。"""
    return replace(spec, models_path=paths.models, chat_path=paths.chat)


__all__ = [
    "AUTH_STYLES",
    "ModelEntry",
    "LlmPaths",
    "PROTOCOL_KEYS",
    "ProtocolSpec",
    "WIRE_KEYS",
    "build_auth_headers",
    "build_chat_request",
    "build_models_request",
    "compose_url",
    "effective_paths",
    "extract_text",
    "get",
    "is_supported",
    "looks_like_completion",
    "merge_headers",
    "models_has_more",
    "models_query",
    "normalize_base_url",
    "parse_models",
    "public_catalog",
    "strip_models_suffix",
    "wire_of",
    "with_paths",
]
