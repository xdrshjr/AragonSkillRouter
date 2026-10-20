"""Agent 模型解析与连通性检测（§9.2 `/agent/config`、§7.12 轮次请求）。

**为什么需要这个模块**：网关（`agents/aragon-gateway`）不读任何环境变量里的
provider 密钥，也不认识平台的提供商 UUID —— 它只接受
`model{providerId,modelId,baseUrl?}` + `credential_refs{providerId: credential_id}`
（spec §4.3 / §9.5）。而平台侧把 `agent_model` 存成
`{provider, model, base_url, credential_id}`（`db/seed.py`）。

历史实现直接把 `agent_config().get("model")` 这个**字符串**塞进 `model` 字段，
并且从不发送 `credential_refs`，于是网关每次都回
`TURN_CONFLICT: no model configured for this turn`，Agent 面板永远拿不到回复。
本模块是这条契约的**唯一**翻译层：

- `resolve()`：把设置投影成「网关能用」的模型引用 + 凭证引用，并列出问题清单；
- `validate_patch()`：`PATCH /agent/config` 的白名单与取值校验；
- `check()`：拿真实凭证打一次最小请求，作为「Agent 可用」的可复现证据。

密钥只在本模块的 `check()` 里出现一次，且只进内存（不进日志、不进返回值）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from ..db import db_session
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import Credential, Provider
from ..security.netpolicy import validate_admin_target
from . import settings as settings_service

log = get_logger("skillrouter.run")

#: 网关 `initProviders()` 注册的 provider id（`@aragon-agent/core@0.2.34`）
PROVIDER_IDS: tuple[str, ...] = ("anthropic", "openai", "google")

#: `PATCH /agent/config` 允许修改的键（其余一律拒绝，避免静默丢弃）
EDITABLE = (
    "provider",
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

#: 一次最小真实调用的 token 上限。
#: 注意：思考型模型（如 glm-5.3-flash）会先输出 thinking，预算太小会让整次探测
#: 只拿到思考、没有正文。512 足以容纳「简短思考 + 一句话」，成本可忽略。
PROBE_MAX_TOKENS = 512
PROBE_TIMEOUT_S = 45.0
#: 探测提示词：要求极短回答，尽量把预算留给正文
PROBE_PROMPT = "只回复两个字：正常"


@dataclass
class Resolution:
    """一次「把设置翻译成轮次请求」的结果。"""

    model: dict | None
    credential_refs: dict[str, str] = field(default_factory=dict)
    credential: dict | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.model is not None and not self.problems

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "model": self.model,
            "credential_refs": dict(self.credential_refs),
            "credential": self.credential,
            "problems": list(self.problems),
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


def _pick_credential(
    provider_kind: str, base_url: str | None, explicit: str | None
) -> tuple[Credential | None, Provider | None, str | None]:
    """选凭证：显式优先；否则按「base_url 完全一致 > 名字/ slug 命中 provider 类型」打分。

    返回 `(credential, provider, problem)`。
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
            "再到「设置 → Agent 模型」选择它",
        )

    wanted = _normalize_url(base_url)
    best_score = -1
    best_pair: tuple[Credential, Provider] | None = None
    for credential, provider in candidates:
        provider_url = _normalize_url(provider.base_url)
        score = 0
        if wanted and provider_url and provider_url == wanted:
            score = 3
        elif not wanted and provider_kind in f"{provider.slug} {provider.name}".lower():
            score = 2
        elif wanted and provider_url and provider_url in wanted:
            score = 1
        if score > best_score:
            best_score = score
            best_pair = (credential, provider)
    assert best_pair is not None
    return best_pair[0], best_pair[1], None


def resolve(config: dict | None = None) -> Resolution:
    """把 `agent_model` 设置投影成网关可用的 `model` + `credential_refs`。"""
    cfg = dict(config if config is not None else (settings_service.get_value("agent_model", {}) or {}))
    problems: list[str] = []

    provider_kind = str(cfg.get("provider") or "anthropic").strip().lower()
    if provider_kind not in PROVIDER_IDS:
        problems.append(
            f"provider={provider_kind!r} 不是网关支持的协议族（可选 {' / '.join(PROVIDER_IDS)}）"
        )

    model_id = str(cfg.get("model") or "").strip()
    if not model_id:
        problems.append("还没有填模型名（model），例如 glm-5.3-flash / claude-sonnet-4-5 / gpt-4o-mini")

    base_url_raw = str(cfg.get("base_url") or "").strip()
    if base_url_raw:
        try:
            validate_admin_target(base_url_raw)
        except ApiError as exc:
            problems.append(exc.message)

    credential, provider, problem = _pick_credential(
        provider_kind, base_url_raw or None, str(cfg.get("credential_id") or "").strip() or None
    )
    if problem:
        problems.append(problem)

    base_url = base_url_raw
    if not base_url and provider is not None and provider.base_url:
        # 没填 base_url 时用提供商自己的地址：这是最常见的一条路径（一个提供商一个凭证）
        base_url = provider.base_url

    if model_id == "" or provider_kind not in PROVIDER_IDS:
        return Resolution(model=None, credential=None, problems=problems)

    model_ref: dict = {"providerId": provider_kind, "modelId": model_id}
    if base_url:
        model_ref["baseUrl"] = base_url
    credential_refs: dict[str, str] = {}
    summary = None
    if credential is not None:
        credential_refs[provider_kind] = credential.id
        summary = {
            "id": credential.id,
            "name": credential.name,
            "provider_id": credential.provider_id,
            "provider_name": provider.name if provider else None,
            "secret_mask": credential.secret_mask,
        }
    elif not problems:
        problems.append("没有可用的凭证引用（credential_refs 为空）")

    return Resolution(model=model_ref, credential_refs=credential_refs, credential=summary, problems=problems)


def for_turn() -> Resolution:
    """worker 发轮次时用：解析 + 记录脱敏日志。"""
    resolution = resolve()
    if not resolution.ready:
        log.warning("agent_model_unready", extra={"object": "agent_model", "detail": resolution.problems})
    return resolution


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
    if "provider" in patch:
        value = str(patch["provider"] or "").strip().lower()
        if value not in PROVIDER_IDS:
            raise ApiError(
                Code.VALIDATION_FAILED,
                message=f"provider 只能是 {' / '.join(PROVIDER_IDS)}",
                details={"field": "provider", "allowed": list(PROVIDER_IDS)},
            )
        cleaned["provider"] = value

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
    """返回 `(url, headers, body)`：三个协议族各一条最小真实请求。"""
    base = base_url.rstrip("/")
    if provider_kind == "anthropic":
        return (
            f"{base}/v1/messages",
            {
                "x-api-key": secret,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model_id,
                "max_tokens": PROBE_MAX_TOKENS,
                "messages": [{"role": "user", "content": PROBE_PROMPT}],
            },
        )
    if provider_kind == "openai":
        return (
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {secret}", "content-type": "application/json"},
            {
                "model": model_id,
                "max_tokens": PROBE_MAX_TOKENS,
                "messages": [{"role": "user", "content": PROBE_PROMPT}],
            },
        )
    return (
        f"{base}/v1beta/models/{model_id}:generateContent",
        {"x-goog-api-key": secret, "content-type": "application/json"},
        {"contents": [{"role": "user", "parts": [{"text": PROBE_PROMPT}]}]},
    )


async def _post_with_transport(
    url: str, headers: dict, body: dict, *, trust_env: bool
) -> httpx.Response:
    async with httpx.AsyncClient(trust_env=trust_env, timeout=PROBE_TIMEOUT_S) as client:
        return await client.post(url, headers=headers, json=body)


async def check(config: dict | None = None) -> dict:
    """真实打一次模型：证明「这个凭证 + 这个模型 + 这个地址」能work。

    返回 `{ok, provider, model, base_url, status, latency_ms, detail?, credential?}`；
    `detail` 里的密钥已被 `redact()` 处理，不会回显明文。
    """
    resolution = resolve(config)
    if resolution.model is None or not resolution.credential_refs:
        return {
            "ok": False,
            "checked": False,
            "problems": resolution.problems,
            "detail": "Agent 模型尚未配置完整，未发出真实请求",
        }

    provider_kind = resolution.model["providerId"]
    model_id = resolution.model["modelId"]
    base_url = resolution.model.get("baseUrl") or ""
    credential_id = next(iter(resolution.credential_refs.values()))

    from . import credentials as credential_service

    try:
        data = credential_service.materialize(credential_id)
    except ApiError as exc:
        # 解析到这里之后凭证可能刚好被停用/轮换/删除：这是「测不了」而不是「服务器炸了」，
        # 必须回一个可读的结论（否则前端只看到 500，把凭证问题误报成平台故障）。
        return {
            "ok": False,
            "checked": False,
            "provider": provider_kind,
            "model": model_id,
            "base_url": base_url or None,
            "credential": resolution.credential,
            "problems": [*resolution.problems, f"{exc.message}"],
            "detail": "凭证当前不可用，未发出真实请求",
        }
    secret = data.get("secret") or ""
    if not secret:
        return {
            "ok": False,
            "checked": False,
            "provider": provider_kind,
            "model": model_id,
            "base_url": base_url or None,
            "credential": resolution.credential,
            "problems": [*resolution.problems, "凭证里没有可用的密钥（secret 为空）"],
            "detail": "凭证内容不可用，未发出真实请求",
        }
    base_url = base_url or (data.get("base_url") or "")
    if not base_url:
        return {
            "ok": False,
            "checked": False,
            "problems": [*resolution.problems, "没有可用的上游地址（provider/base_url 都为空）"],
            "detail": "缺少 base_url，未发出真实请求",
        }

    url, headers, body = _probe_request(provider_kind, base_url, model_id, secret)
    started = time.perf_counter()
    transport = "proxy"
    attempts: list[str] = []
    response = None
    try:
        response = await _post_with_transport(url, headers, body, trust_env=True)
    except httpx.HTTPError as exc:
        attempts.append(f"proxy:{type(exc).__name__}")
        # 环境代理存在但连不上该主机时（自托管场景常见），再直连一次：
        # 这个地址是管理员自己配的，不允许代理把「能用」误报成「不可用」。
        transport = "direct"
        try:
            response = await _post_with_transport(url, headers, body, trust_env=False)
        except httpx.HTTPError as direct_exc:
            attempts.append(f"direct:{type(direct_exc).__name__}")
            log.warning(
                "agent_model_check_failed",
                extra={
                    "object": "agent_model",
                    "detail": {"attempts": attempts, "message": str(direct_exc)[:200]},
                },
            )
            return {
                "ok": False,
                "checked": True,
                "provider": provider_kind,
                "model": model_id,
                "base_url": base_url,
                "credential": resolution.credential,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error_type": type(direct_exc).__name__,
                "transport_attempts": attempts,
                "detail": redact(str(direct_exc) or type(direct_exc).__name__, max_len=300),
            }

    latency_ms = int((time.perf_counter() - started) * 1000)
    ok = 200 <= response.status_code < 300
    detail = "" if ok else redact(response.text, max_len=400)
    text = ""
    if ok:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not _looks_like_completion(provider_kind, payload):
            # 200 但不是该协议族的响应形状（多半是打到了错误的路径/网关）——不伪装成功
            ok = False
            detail = redact(response.text, max_len=400) or "上游 200，但响应体不是可识别的协议格式"
        else:
            text = _extract_text(provider_kind, payload)
            if not text:
                # 结构合法但没有文本：思考型模型在极小的 max_tokens 下会这样，
                # 这仍然是「凭证 + 模型 + 地址」可用的证据，但要如实说明。
                detail = "上游 200 且响应结构合法；本次探测没有产出文本（模型先输出思考，或 max_tokens 过小）"
    return {
        "ok": ok,
        "checked": True,
        "provider": provider_kind,
        "model": model_id,
        "base_url": base_url,
        "status": response.status_code,
        "latency_ms": latency_ms,
        "transport": transport,
        "text_chars": len(text),
        "credential": resolution.credential,
        "detail": detail,
    }


def _looks_like_completion(provider_kind: str, payload: dict) -> bool:
    """响应体是否是该协议族的一次完成结果（不是错误信封、不是别的 API）。"""
    if not isinstance(payload, dict):
        return False
    if provider_kind == "anthropic":
        return isinstance(payload.get("content"), list) or payload.get("type") == "message"
    if provider_kind == "openai":
        return isinstance(payload.get("choices"), list)
    return isinstance(payload.get("candidates"), list)


def _extract_text(provider_kind: str, payload: dict) -> str:
    try:
        if provider_kind == "anthropic":
            blocks = payload.get("content") or []
            return "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict)).strip()
        if provider_kind == "openai":
            choices = payload.get("choices") or []
            if choices:
                return str((choices[0].get("message") or {}).get("content") or "").strip()
            return ""
        candidates = payload.get("candidates") or []
        if candidates:
            parts = ((candidates[0].get("content") or {}).get("parts")) or []
            return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    except (AttributeError, TypeError, IndexError):
        return ""
    return ""


__all__ = [
    "PROVIDER_IDS",
    "EDITABLE",
    "Resolution",
    "resolve",
    "for_turn",
    "validate_patch",
    "check",
]
