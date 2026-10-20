"""Agent 模型解析与连通性检测（§9.2 `/agent/config`、§7.12 轮次请求）。

**为什么需要这个模块**：网关（`agents/aragon-gateway`）不读任何环境变量里的
provider 密钥，也不认识平台的提供商 UUID —— 它只接受
`model{providerId,modelId,baseUrl?}` + `credential_refs{providerId: credential_id}`
（spec §4.3 / §9.5）。而平台侧把 `agent_model` 存成
`{protocol, model, base_url, credential_id}`（旧名是 `provider`）。

本模块是这条契约的**唯一**翻译层：

- `resolve()`：把设置投影成「网关能用」的模型引用 + 凭证引用，并列出问题清单；
- `validate_patch()`：`PATCH /agent/config` 的白名单与取值校验；
- `check()`：委托 `services.llm.probe` 打一次真实生成（`mode="completion"`），
  返回字段在旧形状上**只增不改**。

两条 v2.0 硬规则（写错就等于「自定义」永远不可用）：

1. **`credential_refs` 的键必须是 `wire`，不是 `protocol`**。网关侧
   `engine.ts:846` 是 `const ref = refs[model.providerId]` —— 它拿 `providerId`（= wire）
   去取凭证引用；按协议名写键会得到 `no credential_ref for provider "openai"`。
2. **模型与协议明显不属于同一家族时进 `problems`（`ready=false`）**，
   而不是 `warnings`：否则页面亮绿灯、每轮必然 `TURN_CONFLICT`，正是 INV-2 禁止的假成功。

密钥只在本模块的 `check()` 里出现一次，且只进内存（不进日志、不进返回值）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select

from ..db import db_session
from ..errors import ApiError, Code
from ..logging import get_logger
from ..models import Credential, LlmModelCatalog, Provider
from ..security.netpolicy import trust_env_for, validate_admin_target
from . import settings as settings_service
from .llm import catalog as model_catalog
from .llm import identity as llm_identity
from .llm import probe as llm_probe
from .llm import protocols

log = get_logger("skillrouter.run")

#: 网关 `initProviders()` 注册的 provider id（`@aragon-agent/core@0.2.34`）。
#: 保留这个常量：既有测试与前端 `providers` 字段仍在读它（INV-4）。
PROVIDER_IDS: tuple[str, ...] = ("anthropic", "openai", "google")
#: 本设计对外暴露的协议键（= 注册表的四个规范）
PROTOCOL_KEYS: tuple[str, ...] = protocols.PROTOCOL_KEYS

#: `PATCH /agent/config` 允许修改的键（其余一律拒绝，避免静默丢弃）
EDITABLE = (
    "provider",
    "protocol",
    "wire",
    "paths",
    "model",
    "base_url",
    "temperature",
    "max_tokens",
    "credential_id",
    "allowed_providers",
    "max_test_calls_per_flow",
    "max_cost_usd_per_flow",
    "allowed_window_hours",
)

#: 一次最小真实调用的 token 上限（沿用 `protocols.PROBE_MAX_TOKENS` 的理由：
#: 思考型模型会先输出 thinking，预算太小会让整次探测只拿到思考、没有正文）。
PROBE_MAX_TOKENS = protocols.PROBE_MAX_TOKENS
#: `completion` 阶段的超时（45s）—— 由 `settings.llm_completion_timeout_s` 承载，
#: 这里保留常量只为兼容既有引用。
PROBE_TIMEOUT_S = 45.0
PROBE_PROMPT = protocols.PROBE_PROMPT

#: 明显跨家族的模型前缀（用于把「保存后必然失败」的组合提前拦下来）
_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "openai": ("gpt-", "gpt3", "gpt4", "o1", "o3", "o4", "chatgpt"),
    "google": ("gemini", "gemma", "palm"),
}


@dataclass
class Resolution:
    """一次「把设置翻译成轮次请求」的结果。"""

    model: dict | None
    credential_refs: dict[str, str] = field(default_factory=dict)
    credential: dict | None = None
    problems: list[str] = field(default_factory=list)
    protocol: str = "anthropic"
    wire: str = "anthropic"
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.model is not None and not self.problems

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "protocol": self.protocol,
            "wire": self.wire,
            "model": self.model,
            "credential_refs": dict(self.credential_refs),
            "credential": self.credential,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }


def _normalize_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def _credential_candidates() -> list[tuple[Credential, Provider]]:
    """所有「可用」的 llm 凭证（active 状态 + 未删除的提供商）。"""
    with db_session() as s:
        rows = list(
            s.scalars(
                select(Credential).where(
                    Credential.deleted_at.is_(None),
                    Credential.status == "active",
                )
            )
        )
        providers = {
            p.id: p
            for p in s.scalars(select(Provider).where(Provider.deleted_at.is_(None))).all()
        }
    out: list[tuple[Credential, Provider]] = []
    for row in rows:
        provider = providers.get(row.provider_id)
        if provider is None or provider.status != "active":
            continue
        if (row.kind or "").lower() not in ("llm", "generic"):
            continue
        out.append((row, provider))
    return out


def _match_by_url(base_url: str | None) -> tuple[Credential, Provider] | None:
    """按 `base_url` 精确/包含匹配一个提供商（返回它的一条可用凭证）。"""
    wanted = _normalize_url(base_url)
    if not wanted:
        return None
    for credential, provider in _credential_candidates():
        provider_url = _normalize_url(provider.base_url)
        if provider_url and (provider_url == wanted or provider_url in wanted):
            return credential, provider
    return None


def _pick_credential(
    protocol: str,
    wire: str,
    base_url: str | None,
    explicit: str | None,
) -> tuple[Credential | None, Provider | None, str | None]:
    """选凭证：显式优先；否则按「base_url 完全一致 > 名字/ slug 命中协议族」打分。

    返回 `(credential, provider, problem)`。

    `protocol=custom` 时代码不猜（R-08）：没有显式 `credential_id` 就只按地址命中，
    命中不到就回一条可读 problem —— 实测旧打分函数在「全 0 分」时返回**第一个** active
    凭证，等于随机挑一把别人的钥匙去发轮次。
    """
    if explicit:
        with db_session() as s:
            row = s.get(Credential, explicit)
            if row is None or row.deleted_at:
                return None, None, f"credential_id {explicit} 不存在"
            if row.status != "active":
                return None, None, f"凭证 {row.name} 状态为 {row.status}，不能用于 Agent"
            provider = s.get(Provider, row.provider_id)
            if provider is None or provider.deleted_at:
                return None, None, f"凭证 {row.name} 所属提供商已被删除"
        return row, provider, None

    candidates = _credential_candidates()
    if not candidates:
        return (
            None,
            None,
            "数据库里没有可用的 LLM 凭证：先在「提供商」里加一个提供商并上传凭证，"
            "再到「设置 → 模型服务」选择它",
        )

    if protocol == "custom":
        matched = _match_by_url(base_url)
        if matched is None:
            return (
                None,
                None,
                "自定义协议必须显式选择凭证：无法从地址推断用哪把钥匙",
            )
        return matched[0], matched[1], None

    wanted = _normalize_url(base_url)
    best_score = -1
    best_pair: tuple[Credential, Provider] | None = None
    for credential, provider in candidates:
        provider_url = _normalize_url(provider.base_url)
        score = 0
        if wanted and provider_url and provider_url == wanted:
            score = 3
        elif not wanted and wire in f"{provider.slug} {provider.name}".lower():
            score = 2
        elif wanted and provider_url and provider_url in wanted:
            score = 1
        if score > best_score:
            best_score = score
            best_pair = (credential, provider)
    if best_pair is None:  # pragma: no cover - candidates 非空时必有一个
        return None, None, "没有可用的 LLM 凭证"
    return best_pair[0], best_pair[1], None


def _model_protocol_mismatch(model_id: str, wire: str) -> str | None:
    """模型名与线协议是否**明显**不属于同一家族（返回一句人话或 None）。"""
    lowered = (model_id or "").lower()
    own = _FAMILY_PREFIXES.get(wire, ())
    if any(lowered.startswith(prefix) for prefix in own):
        return None
    for family, prefixes in _FAMILY_PREFIXES.items():
        if family == wire:
            continue
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return f"模型 {model_id} 与协议 {wire} 不匹配：这是保存后必然失败组合"
    return None


def _catalog_warning(
    protocol: str, wire: str, base_url: str, credential_id: str | None, model_id: str
) -> str | None:
    """模型不在最近一次清单里 → 非阻塞提示（T-27：`ready` 仍为 true）。"""
    if not model_id:
        return None
    key = model_catalog.target_key(protocol, wire, base_url, credential_id)
    try:
        with db_session() as s:
            row = s.scalar(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key))
            if row is None:
                return None
            known = {str((item or {}).get("id") or "") for item in (row.models or [])}
            fetched_at = row.fetched_at
    except Exception:  # noqa: BLE001 - 提示失败不影响解析
        return None
    if not known or model_id in known:
        return None
    return f"当前模型 {model_id} 不在最近一次模型清单里（清单时间 {fetched_at}）"


def resolve(config: dict | None = None) -> Resolution:
    """把 `agent_model` 设置投影成网关可用的 `model` + `credential_refs`。"""
    cfg = dict(config if config is not None else (settings_service.get_value("agent_model", {}) or {}))
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    protocol = str(cfg.get("protocol") or cfg.get("provider") or "anthropic").strip().lower()
    wire = "openai"
    if protocol not in PROTOCOL_KEYS:
        problems.append(
            f"协议 protocol={protocol!r} 不被支持（可选 {' / '.join(PROTOCOL_KEYS)}）"
        )
    else:
        try:
            wire = protocols.wire_of(protocol, str(cfg.get("wire") or "").strip() or None)
        except ApiError as exc:
            problems.append(exc.message)
            wire = protocols.get(protocol).wire or "openai"

    model_id = str(cfg.get("model") or "").strip()
    if not model_id:
        problems.append("还没有填模型名（model），例如 gpt-4o-mini / claude-sonnet-4-5 / gemini-2.5-pro")

    base_url_raw = str(cfg.get("base_url") or "").strip()
    if base_url_raw:
        try:
            validate_admin_target(base_url_raw)
        except ApiError as exc:
            problems.append(exc.message)

    credential, provider, problem = _pick_credential(
        protocol, wire, base_url_raw or None, str(cfg.get("credential_id") or "").strip() or None
    )
    if problem:
        problems.append(problem)

    base_url = base_url_raw
    if not base_url and provider is not None and provider.base_url:
        # 没填 base_url 时用提供商自己的地址：这是最常见的一条路径（一个提供商一个凭证）
        base_url = provider.base_url
    if not base_url and protocol in PROTOCOL_KEYS:
        base_url = protocols.get(protocol).default_base_url
    if base_url and protocol in PROTOCOL_KEYS:
        try:
            base_url, base_notes = protocols.normalize_base_url(protocol, base_url, wire=wire)
            notes.extend(base_notes)
        except ApiError as exc:
            problems.append(exc.message)
            base_url = base_url_raw or ""

    if protocol == "custom" and not base_url:
        problems.append("自定义协议必须填上游地址（base_url），例如 https://your-gateway/v1")

    if model_id and protocol in PROTOCOL_KEYS:
        mismatch = _model_protocol_mismatch(model_id, wire)
        if mismatch:
            problems.append(mismatch)

    if model_id == "" or protocol not in PROTOCOL_KEYS:
        return Resolution(
            model=None,
            credential=None,
            problems=problems,
            protocol=protocol,
            wire=wire,
            warnings=warnings,
            notes=notes,
        )

    model_ref: dict = {"providerId": wire, "modelId": model_id}
    if base_url:
        model_ref["baseUrl"] = base_url
    credential_refs: dict[str, str] = {}
    summary = None
    if credential is not None:
        # 硬规则：键是 wire（网关按 refs[model.providerId] 取）
        credential_refs[wire] = credential.id
        summary = {
            "id": credential.id,
            "name": credential.name,
            "provider_id": credential.provider_id,
            "provider_name": provider.name if provider else None,
            "secret_mask": credential.secret_mask,
        }
    elif not problems:
        problems.append("没有可用的凭证引用（credential_refs 为空）")

    if credential is not None and model_id:
        warning = _catalog_warning(
            protocol, wire, base_url, credential.id, model_id
        )
        if warning:
            warnings.append(warning)

    return Resolution(
        model=model_ref,
        credential_refs=credential_refs,
        credential=summary,
        problems=problems,
        protocol=protocol,
        wire=wire,
        warnings=warnings,
        notes=notes,
    )


def for_turn() -> Resolution:
    """worker 发轮次时用：解析 + 记录脱敏日志。"""
    resolution = resolve()
    if not resolution.ready:
        log.warning("agent_model_unready", extra={"object": "agent_model", "detail": resolution.problems})
    else:
        log.info(
            "agent_model_resolved",
            extra={
                "object": "agent_model",
                "detail": {
                    "protocol": resolution.protocol,
                    "wire": resolution.wire,
                    "base_url": (resolution.model or {}).get("baseUrl"),
                    "model": (resolution.model or {}).get("modelId"),
                },
            },
        )
    return resolution


def _validate_paths(value: object) -> dict:
    if not isinstance(value, dict):
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="paths 必须是对象（models / chat）",
            details={"field": "paths"},
        )
    cleaned: dict = {}
    # 允许一个前导 `/`（`/v2/models` 是 custom 覆盖路径的自然写法），其余必须以字母开头
    pattern = re.compile(r"^/?[A-Za-z][A-Za-z0-9_\-/{}.]*$")
    for key in ("models", "chat"):
        if key not in value or value[key] is None:
            continue
        text = str(value[key]).strip()
        if not text:
            continue
        if len(text) > 200 or ".." in text or not pattern.match(text) or " " in text:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=(
                    f"paths.{key} 必须是以字母开头（可带一个前导 /）、"
                    "不含空白与 `..` 的路径（≤200 字符）"
                ),
                details={"field": f"paths.{key}"},
            )
        cleaned[key] = text
    return cleaned


def _protocol_in_effect(cleaned: dict) -> str:
    """本次补丁生效的协议：补丁里的优先，否则取当前保存值（`provider` 是旧别名）。

    `wire` / `paths` 都只对 `custom` 生效，判断依据必须是「这次 PATCH 之后」的协议，
    而不是补丁里有没有写 `protocol` —— 只写 `wire` 的请求也要按已保存的协议判定。
    """
    if cleaned.get("protocol"):
        return str(cleaned["protocol"]).strip().lower()
    current = settings_service.get_value("agent_model", {}) or {}
    return str(current.get("protocol") or current.get("provider") or "").strip().lower()


def validate_patch(patch: dict) -> dict:
    """`PATCH /agent/config` 的校验（未知键直接拒绝，不静默丢弃）。"""
    unknown = [key for key in patch if key not in EDITABLE]
    if unknown:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"不可修改的 Agent 配置项：{', '.join(sorted(unknown))}",
            hint=f"只允许 {' / '.join(EDITABLE)}",
            details={"allowed": list(EDITABLE)},
        )

    cleaned: dict = {}
    raw_provider = patch.get("provider")
    raw_protocol = patch.get("protocol")
    if raw_provider is not None and raw_protocol is not None:
        if str(raw_provider).strip().lower() != str(raw_protocol).strip().lower():
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="provider 与 protocol 不一致",
                hint="provider 是 protocol 的旧名，请只写一个",
                details={"fields": ["provider", "protocol"]},
            )
    chosen = raw_protocol if raw_protocol is not None else raw_provider
    if chosen is not None:
        value = str(chosen or "").strip().lower()
        if value not in PROTOCOL_KEYS:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=f"provider 只能是 {' / '.join(PROTOCOL_KEYS)}",
                hint="provider 是 protocol 的旧名，新前端直接写 protocol",
                details={"field": "provider", "allowed": list(PROTOCOL_KEYS)},
            )
        # 双写一版（ADR-6）：老前端与既有测试仍读 `provider`
        cleaned["protocol"] = value
        cleaned["provider"] = value

    if "wire" in patch:
        value = str(patch["wire"] or "").strip().lower()
        effective_protocol = _protocol_in_effect(cleaned)
        if effective_protocol != "custom":
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="wire 只对 protocol=custom 生效",
                hint="其它协议的线协议是固定的（wire == protocol），不需要也不能写 wire",
                details={"field": "wire"},
            )
        if value and value not in protocols.WIRE_KEYS:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=f"wire 只能是 {' / '.join(protocols.WIRE_KEYS)}",
                details={"field": "wire", "allowed": list(protocols.WIRE_KEYS)},
            )
        cleaned["wire"] = value or None

    if "paths" in patch:
        value = patch["paths"]
        effective_protocol = _protocol_in_effect(cleaned)
        if effective_protocol != "custom":
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="paths 只对 protocol=custom 生效",
                details={"field": "paths"},
            )
        cleaned["paths"] = _validate_paths(value) if value is not None else None

    if "model" in patch:
        value = str(patch["model"] or "").strip()
        if len(value) > 200:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message="model 最长 200 字符",
                details={"field": "model"},
            )
        # 空字符串 = 清空（把 Agent 打回「未配置」），这是合法的显式操作
        cleaned["model"] = value or None

    if "base_url" in patch:
        # 存**原值**（不存归一化值）：用户看到的永远是他填的那串，
        # 归一化只发生在 resolve()/探测时，结果通过 effective.model.baseUrl 与 notes 回显。
        value = str(patch["base_url"] or "").strip()
        if value:
            validate_admin_target(value)
        cleaned["base_url"] = value or None

    if "credential_id" in patch:
        value = str(patch["credential_id"] or "").strip()
        if value:
            with db_session() as s:
                row = s.get(Credential, value)
            if row is None or row.deleted_at:
                raise ApiError(
                    Code.VALIDATION_FAILED,
                    message="credential_id 指向的凭证不存在",
                    details={"field": "credential_id"},
                )
        cleaned["credential_id"] = value or None

    if "temperature" in patch:
        try:
            temperature = float(patch["temperature"])
        except (TypeError, ValueError) as exc:
            raise ApiError(Code.VALIDATION_FAILED, message="temperature 必须是数字") from exc
        if not 0.0 <= temperature <= 2.0:
            raise ApiError(Code.VALIDATION_FAILED, message="temperature 必须在 0-2 之间")
        cleaned["temperature"] = temperature

    if "max_tokens" in patch:
        try:
            max_tokens = int(patch["max_tokens"])
        except (TypeError, ValueError) as exc:
            raise ApiError(Code.VALIDATION_FAILED, message="max_tokens 必须是整数") from exc
        if not 1 <= max_tokens <= 1_000_000:
            raise ApiError(Code.VALIDATION_FAILED, message="max_tokens 必须在 1-1000000 之间")
        cleaned["max_tokens"] = max_tokens

    for key in ("allowed_providers",):
        if key in patch:
            value = patch[key]
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ApiError(Code.VALIDATION_FAILED, message=f"{key} 必须是字符串数组")
            cleaned[key] = value

    for key in ("max_test_calls_per_flow", "allowed_window_hours"):
        if key in patch:
            try:
                parsed = int(patch[key])
            except (TypeError, ValueError) as exc:
                raise ApiError(Code.VALIDATION_FAILED, message=f"{key} 必须是整数") from exc
            if parsed < 0:
                raise ApiError(Code.VALIDATION_FAILED, message=f"{key} 不能为负数")
            cleaned[key] = parsed

    if "max_cost_usd_per_flow" in patch:
        try:
            cost = float(patch["max_cost_usd_per_flow"])
        except (TypeError, ValueError) as exc:
            raise ApiError(Code.VALIDATION_FAILED, message="max_cost_usd_per_flow 必须是数字") from exc
        if cost < 0:
            raise ApiError(Code.VALIDATION_FAILED, message="max_cost_usd_per_flow 不能为负数")
        cleaned["max_cost_usd_per_flow"] = cost

    return cleaned


def _probe_request(provider_kind: str, base_url: str, model_id: str, secret: str) -> tuple[str, dict, dict]:
    """薄封装：返回 `(url, headers, body)`（真正的协议差异在 `services.llm.protocols`）。"""

    spec = protocols.get(provider_kind)
    identity = SimpleNamespace(
        protocol=provider_kind,
        wire=provider_kind,
        header_name=None,
        username=None,
        extra_headers={},
        base_url=protocols.normalize_base_url(provider_kind, base_url)[0],
        secret=secret,
        paths=None,
        query_params={},
        auth_style=spec.default_auth_style,
    )
    url, body = protocols.build_chat_request(spec, identity, model_id)
    return url, protocols.merge_headers(identity), body


def _looks_like_completion(provider_kind: str, payload: dict) -> bool:
    """薄封装：响应体是否是该协议族的一次完成结果。"""
    return protocols.looks_like_completion(protocols.get(provider_kind), payload)


def _extract_text(provider_kind: str, payload: dict) -> str:
    return protocols.extract_text(protocols.get(provider_kind), payload)


def _legacy_shape(
    resolution: Resolution,
    *,
    ok: bool,
    checked: bool,
    detail: str,
    problems: list[str] | None = None,
) -> dict:
    """未发出请求时的旧响应形状（`/agent/config/test` 的既有字段一个都不少）。"""
    model = resolution.model or {}
    return {
        "ok": ok,
        "checked": checked,
        "provider": resolution.protocol,
        "protocol": resolution.protocol,
        "wire": resolution.wire,
        "model": model.get("modelId"),
        "base_url": model.get("baseUrl"),
        "credential": resolution.credential,
        "problems": [*resolution.problems, *(problems or [])],
        "warnings": list(resolution.warnings),
        "detail": detail,
        "mode": "completion",
    }


async def check(config: dict | None = None) -> dict:
    """真实打一次模型：证明「这个凭证 + 这个模型 + 这个地址」能 work。

    内部委托 `services.llm.probe.test_connection(..., mode="completion")` ——
    **不是 `both`**：既有端点今天就是一次生成请求，语义必须逐字节等价
    （先插一个 GET 会让「不实现 /models 的网关」从可用变成不可用）。
    """
    resolution = resolve(config)
    if resolution.model is None or not resolution.credential_refs:
        return _legacy_shape(
            resolution,
            ok=False,
            checked=False,
            detail="Agent 模型尚未配置完整，未发出真实请求",
        )

    credential_id = next(iter(resolution.credential_refs.values()))
    model_id = str(resolution.model.get("modelId") or "")
    try:
        identity = llm_identity.resolve_identity_for_credential(
            credential_id, protocol=resolution.protocol, model=model_id
        )
    except ApiError as exc:
        # 解析到这里之后凭证可能刚好被停用/轮换/删除：这是「测不了」而不是「服务器炸了」，
        # 必须回一个可读的结论（否则前端只看到 500，把凭证问题误报成平台故障）。
        return _legacy_shape(
            resolution,
            ok=False,
            checked=False,
            detail="凭证当前不可用，未发出真实请求",
            problems=[exc.message],
        )

    if resolution.model.get("baseUrl"):
        identity.base_url = resolution.model["baseUrl"]
    result = await llm_probe.test_connection(
        identity,
        mode="completion",
        model=model_id,
        record=True,
    )
    stages = result.get("stages") or []
    completion: dict = next(
        (s for s in stages if s.get("stage") == llm_probe.COMPLETION_STAGE), {}
    )
    model_ref = resolution.model or {}
    transport = "proxy"
    base = model_ref.get("baseUrl") or ""
    if base:
        try:
            url = protocols.build_chat_request(
                protocols.get(resolution.protocol),
                identity,
                model_id,
            )[0]
            transport = "proxy" if trust_env_for(url) else "direct"
        except ApiError:
            transport = "env"
    return {
        # 旧字段：一个不少，语义不变
        "ok": bool(result.get("ok")),
        "checked": True,
        "provider": resolution.protocol,
        "model": model_id,
        "base_url": model_ref.get("baseUrl"),
        "status": completion.get("http_status"),
        "latency_ms": result.get("latency_ms"),
        "transport": transport,
        "text_chars": completion.get("text_chars") or 0,
        "credential": resolution.credential,
        "problems": [*resolution.problems, *(result.get("problems") or [])],
        "detail": completion.get("detail") or result.get("hint") or "",
        # 新增字段：只增不改
        "protocol": result.get("protocol"),
        "wire": result.get("wire"),
        "resolved_url": result.get("resolved_url"),
        "mode": result.get("mode"),
        "stages": stages,
        "probe_id": result.get("probe_id"),
        "checked_at": result.get("checked_at"),
        "warnings": list(resolution.warnings),
        "notes": list(resolution.notes),
        "error_code": result.get("error_code"),
        "hint": result.get("hint"),
    }


__all__ = [
    "PROTOCOL_KEYS",
    "PROVIDER_IDS",
    "EDITABLE",
    "PROBE_MAX_TOKENS",
    "PROBE_TIMEOUT_S",
    "PROBE_PROMPT",
    "Resolution",
    "resolve",
    "for_turn",
    "validate_patch",
    "check",
]
