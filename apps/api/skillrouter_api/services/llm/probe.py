"""两阶段探测引擎：可解释、可限流、可单飞（spec §3.4）。

三件事必须做对，否则「实时连接测试」就是假绿：

1. **分阶段超时**：`auth` 只拉模型清单（20s 足够）；`completion` 是一次真实生成，
   必须留 45s —— 思考型模型（glm / o1）在 20s 内可能只吐了思考、没吐正文，
   压到 20s 会让既有 `POST /agent/config/test` 出现新超时失败（INV-4 回归 / R-09）。
2. **传输策略复用 `netpolicy`**：`trust_env` 由 `security/netpolicy.client_kwargs()` 决定，
   本模块**不硬编码** `True/False`。本机/私网目标从不过代理；只有外部主机在网络类异常时
   才允许「直连回退一次」（R-04）。
3. **密钥不外泄**：`detail` 先过 `redact()`，再把本次密钥原文显式替换成 `***`
   （自建网关的 `gw-xxxx` 类 key 不命中任何既有正则，必须补这一刀）。
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ...db import db_write, now_iso, uuid_pk
from ...errors import ApiError, Code
from ...logging import current_request_id, get_logger, redact
from ...models import LlmConnectionProbe
from ...security.netpolicy import client_kwargs, trust_env_for
from ...settings import settings
from . import catalog as model_catalog
from . import protocols
from .protocols import PROBE_MAX_TOKENS, PROBE_PROMPT

log = get_logger("skillrouter.run")

AUTH_STAGE = "auth"
COMPLETION_STAGE = "completion"
MODES = ("auth", "completion", "both")

#: 5xx / 429 的重试次数（一次就够：把「上游打了个嗝」与「上游坏了」区分开）
RETRY_ONCE = 1
#: 没给 Retry-After 时的固定退避
BACKOFF_S = 0.4
#: Retry-After 的上限：不让一次探测被上游拖到分钟级
RETRY_AFTER_CAP_S = 5.0
#: 单飞结果的复用窗口：同一身份的并发/双击请求共享同一次上游调用与同一个 probe_id
SINGLEFLIGHT_WINDOW_S = 2.0
#: 限流窗口
THROTTLE_WINDOW_S = 60.0

#: 进程内限流与单飞状态（前提：单 uvicorn 进程，见 spec §3.4 / R-18）
_BUCKET: dict[str, list[float]] = {}
_MEMO: dict[str, tuple[float, Any]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(slots=True)
class StageResult:
    stage: str
    ok: bool
    http_status: int | None = None
    latency_ms: int = 0
    detail: str = ""
    model_count: int | None = None
    model: str | None = None
    text_chars: int | None = None
    error_code: str | None = None

    def as_dict(self) -> dict:
        data: dict = {
            "stage": self.stage,
            "ok": self.ok,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }
        if self.model_count is not None:
            data["model_count"] = self.model_count
        if self.model is not None:
            data["model"] = self.model
        if self.text_chars is not None:
            data["text_chars"] = self.text_chars
        if self.error_code:
            data["error_code"] = self.error_code
        return data


def reset_state() -> None:
    """清空进程内的限流 / 单飞状态（测试与「立即重测」用）。"""
    _BUCKET.clear()
    _MEMO.clear()
    _LOCKS.clear()


def _bucket_key(identity) -> str:  # noqa: ANN001
    """限流键：`credential_id`，草稿用 `sha256(protocol|base_url)[:12]`。"""
    credential_id = str(getattr(identity, "credential_id", None) or "")
    if credential_id:
        return f"cred:{credential_id}"
    import hashlib

    raw = f"{getattr(identity, 'protocol', '')}|{getattr(identity, 'base_url', '')}"
    return f"draft:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def check_throttle(identity, *, limit: int | None = None) -> None:  # noqa: ANN001
    """进程内 token bucket：默认 10 次 / 60 秒，超出 → `LLM_PROBE_THROTTLED`(429)。"""
    cap = int(limit if limit is not None else getattr(settings, "llm_probe_rate_per_min", 10) or 10)
    if cap <= 0:
        return
    key = _bucket_key(identity)
    now = time.monotonic()
    events = [ts for ts in _BUCKET.get(key, []) if now - ts < THROTTLE_WINDOW_S]
    if len(events) >= cap:
        retry_after = max(1, int(math.ceil(THROTTLE_WINDOW_S - (now - events[0]))))
        _BUCKET[key] = events
        log.warning(
            "llm_probe_throttled", extra={"object": key, "detail": {"retry_after_s": retry_after}}
        )
        raise ApiError(
            Code.LLM_PROBE_THROTTLED,
            message=f"连接测试过于频繁（{cap} 次/60 秒）",
            hint=f"{retry_after} 秒后重试；同一凭证的探测配额是进程内的，重启即清零",
            details={
                "retry_after_s": retry_after,
                "limit": cap,
                "window_s": int(THROTTLE_WINDOW_S),
            },
        )
    events.append(now)
    _BUCKET[key] = events


def _lock_for(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


async def single_flight(key: str, factory) -> dict:  # noqa: ANN001
    """同一 key 的并发请求只打一次上游，并在 2 秒窗口内共享同一个结论。"""
    async with _lock_for(key):
        cached = _MEMO.get(key)
        if cached is not None and time.monotonic() - cached[0] <= SINGLEFLIGHT_WINDOW_S:
            value = cached[1]
            if isinstance(value, Exception):
                raise value
            return dict(value)  # type: ignore[arg-type]
        try:
            result = await factory()
        except Exception as exc:
            _MEMO[key] = (time.monotonic(), exc)
            raise
        _MEMO[key] = (time.monotonic(), result)
        return result


def _backoff_seconds(response: httpx.Response | None) -> float:
    header = (response.headers.get("Retry-After") if response is not None else "") or ""
    try:
        parsed = float(str(header).strip())
    except (TypeError, ValueError):
        return BACKOFF_S
    return max(0.0, min(parsed, RETRY_AFTER_CAP_S))


async def request(  # noqa: ANN201
    identity: Any,
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    timeout_s: float | None = None,
    stage: str = AUTH_STAGE,
) -> httpx.Response:
    """发一次上游请求（含「外部主机直连回退」与「5xx/429 退避重试一次」）。

    全部失败时抛 `catalog.UpstreamFetchError`（调用方据此给出可读结论，而不是 500）。
    """
    headers = protocols.merge_headers(identity)
    if json_body is not None:
        headers.setdefault("content-type", "application/json")
    timeout = float(timeout_s if timeout_s is not None else settings.llm_probe_timeout_s)
    kwargs = client_kwargs(url, timeout=timeout, follow_redirects=False)
    method_upper = method.upper()
    attempts: list[str] = []

    async def _send(**extra) -> httpx.Response:  # noqa: ANN003
        async with httpx.AsyncClient(**{**kwargs, **extra}) as client:
            # 用 `.get()` / `.post()` 而不是 `.request()`：既有测试的打桩客户端只实现了
            # 这两个方法（`test_agent_model._patch_probe_http`），改签名会把它们打红。
            if method_upper == "GET":
                return await client.get(url, headers=headers)
            if method_upper == "POST":
                return await client.post(url, headers=headers, json=json_body)
            return await client.request(method_upper, url, headers=headers, json=json_body)

    started = time.perf_counter()
    try:
        response = await _send()
    except httpx.TimeoutException as exc:
        # 超时也是「网络类异常」，但结论不同（504 而不是 503），所以单独处理
        attempts.append(f"timeout:{type(exc).__name__}")
        if not trust_env_for(url):
            raise _timeout(exc, url, stage, attempts) from exc
        try:
            response = await _send(trust_env=False)
        except httpx.TimeoutException as direct_exc:
            attempts.append(f"direct:{type(direct_exc).__name__}")
            raise _timeout(direct_exc, url, stage, attempts) from direct_exc
        except httpx.HTTPError as direct_exc:
            attempts.append(f"direct:{type(direct_exc).__name__}")
            raise _unavailable(direct_exc, url, stage, attempts) from direct_exc
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
        attempts.append(f"env:{type(exc).__name__}")
        if not trust_env_for(url):
            # 本机/私网目标本来就没走代理：再直连一次只会把一次失败拖成两次
            raise _unavailable(exc, url, stage, attempts) from exc
        try:
            response = await _send(trust_env=False)
        except httpx.TimeoutException as direct_exc:
            attempts.append(f"direct:{type(direct_exc).__name__}")
            raise _timeout(direct_exc, url, stage, attempts) from direct_exc
        except httpx.HTTPError as direct_exc:
            attempts.append(f"direct:{type(direct_exc).__name__}")
            raise _unavailable(direct_exc, url, stage, attempts) from direct_exc
    except httpx.HTTPError as exc:
        attempts.append(f"env:{type(exc).__name__}")
        raise _unavailable(exc, url, stage, attempts) from exc

    if response.status_code >= 500 or response.status_code == 429:
        await asyncio.sleep(_backoff_seconds(response))
        try:
            retry = await _send()
        except httpx.HTTPError:
            pass
        else:
            response = retry
    latency_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "llm_probe_stage",
        extra={
            "object": getattr(identity, "credential_id", None) or "draft",
            "latency_ms": latency_ms,
            "detail": {
                "protocol": getattr(identity, "protocol", None),
                "wire": getattr(identity, "wire", None),
                "stage": stage,
                "http_status": response.status_code,
                "attempts": attempts,
            },
        },
    )
    return response


def _unavailable(exc: Exception, url: str, stage: str, attempts: list[str]) -> Exception:
    return model_catalog.UpstreamFetchError(
        Code.UPSTREAM_UNAVAILABLE,
        http_status=None,
        detail=redact(f"{type(exc).__name__}: {exc} | attempts={attempts}", max_len=300),
        hint=f"连不上 {url}：检查地址、DNS、代理与防火墙（{stage} 阶段）",
    )


def _timeout(exc: Exception, url: str, stage: str, attempts: list[str]) -> Exception:
    return model_catalog.UpstreamFetchError(
        Code.UPSTREAM_TIMEOUT,
        http_status=None,
        detail=redact(f"{type(exc).__name__}: {exc} | attempts={attempts}", max_len=300),
        hint=f"{stage} 阶段超时：上游可能在 20 秒内没能返回（模型清单）或 45 秒内没能生成完",
    )


def _safe_detail(response: httpx.Response, identity) -> str:  # noqa: ANN001
    text = redact(response.text or "", max_len=400)
    secret = str(getattr(identity, "secret", "") or "")
    if secret and isinstance(text, str):
        text = text.replace(secret, "***")
    return text


async def _run_auth(identity) -> StageResult:  # noqa: ANN001
    """阶段 1：拉模型清单（不计费），成功后顺手把清单写进目录缓存。"""
    started = time.perf_counter()
    try:
        result = await model_catalog.fetch_from_upstream(identity)
    except model_catalog.UpstreamFetchError as exc:
        return StageResult(
            stage=AUTH_STAGE,
            ok=False,
            http_status=exc.http_status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=exc.detail,
            error_code=exc.error_code,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    stored = model_catalog.store(identity, result.entries, truncated=result.truncated)
    return StageResult(
        stage=AUTH_STAGE,
        ok=True,
        http_status=result.http_status or 200,
        latency_ms=latency_ms,
        detail=f"识别到 {stored['model_count']} 个模型",
        model_count=stored["model_count"],
    )


async def _run_completion(identity, model: str) -> StageResult:  # noqa: ANN001
    """阶段 2：打一次最小生成（计费 1 次）。200 但形状不对一律判失败（INV-2）。"""
    spec = protocols.get(getattr(identity, "protocol", ""))
    wire = protocols.wire_of(spec.key, getattr(identity, "wire", None))
    url, body = protocols.build_chat_request(
        spec, identity, model, prompt=PROBE_PROMPT, max_tokens=PROBE_MAX_TOKENS
    )
    started = time.perf_counter()
    try:
        response = await request(
            identity,
            "POST",
            url,
            json_body=body,
            timeout_s=settings.llm_completion_timeout_s,
            stage=COMPLETION_STAGE,
        )
    except model_catalog.UpstreamFetchError as exc:
        return StageResult(
            stage=COMPLETION_STAGE,
            ok=False,
            http_status=exc.http_status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=exc.detail,
            model=model,
            error_code=exc.error_code,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code in (401, 403):
        return StageResult(
            stage=COMPLETION_STAGE,
            ok=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            detail=_safe_detail(response, identity),
            model=model,
            error_code=Code.UPSTREAM_AUTH,
        )
    if response.status_code >= 400:
        return StageResult(
            stage=COMPLETION_STAGE,
            ok=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            detail=_safe_detail(response, identity),
            model=model,
            error_code=Code.UPSTREAM_ERROR,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not protocols.looks_like_completion(spec, payload, wire=wire):
        return StageResult(
            stage=COMPLETION_STAGE,
            ok=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            detail=_safe_detail(response, identity) or "上游 200，但响应体不是可识别的协议格式",
            model=model,
            error_code=Code.UPSTREAM_INVALID_RESPONSE,
        )
    text = protocols.extract_text(spec, payload, wire=wire)
    detail = "模型正常回话"
    if not text:
        # 结构合法但没有文本：思考型模型在极小的 max_tokens 下会这样。
        # 这仍然是「凭证 + 模型 + 地址」可用的证据，但要如实说明（不伪装成功也不伪装失败）。
        detail = (
            "上游 200 且响应结构合法；本次探测没有产出文本（模型先输出思考，或 max_tokens 过小）"
        )
    return StageResult(
        stage=COMPLETION_STAGE,
        ok=True,
        http_status=response.status_code,
        latency_ms=latency_ms,
        detail=detail,
        model=model,
        text_chars=len(text),
    )


def resolve_model(payload, identity) -> str | None:  # noqa: ANN001
    """完成态探测用的模型名：请求 > `agent_model` 设置 > 凭证 `meta.model`。"""
    explicit = str(getattr(payload, "model", None) or "").strip()
    if explicit:
        return explicit
    from .. import settings as settings_service

    config = settings_service.get_value("agent_model", {}) or {}
    configured = str(config.get("model") or "").strip()
    if configured:
        return configured
    if identity is not None:
        credential_id = getattr(identity, "credential_id", None)
        if credential_id:
            from .. import credentials as credential_service

            try:
                meta = credential_service.materialize(credential_id).get("meta") or {}
            except ApiError:
                meta = {}
            candidate = str(meta.get("model") or "").strip()
            if candidate:
                return candidate
    return None


def _probe_key(identity, mode: str, model: str | None) -> str:  # noqa: ANN001
    """单飞/复用的键：身份 + 模式 + 模型 + **密钥哈希**。

    密钥必须进键（两把不同的钥匙打同一地址不能共享结论），但只进哈希，
    不把明文留在进程内的字典键里。
    """
    import hashlib

    secret_digest = hashlib.sha256(str(getattr(identity, "secret", "") or "").encode()).hexdigest()[
        :8
    ]
    return f"{identity.dedupe_key}|{mode}|{model or ''}|{secret_digest}"


async def test_connection(
    identity: Any,
    *,
    mode: str = "auth",
    model: str | None = None,
    actor: str | None = None,
    record: bool = True,
) -> dict:
    """两阶段探测的入口，返回 spec §3.4 的响应形状。"""
    chosen_mode = (mode or "auth").strip().lower()
    if chosen_mode not in MODES:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="mode 只能是 auth / completion / both",
            details={"field": "mode", "allowed": list(MODES)},
        )
    resolved_model = resolve_model(_PayloadWithModel(model), identity)
    if chosen_mode != "auth" and not resolved_model:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="完成态探测需要一个模型名",
            hint="在「模型」里选一个，或先点「获取模型列表」",
            details={"field": "model"},
        )
    check_throttle(identity)
    log.info(
        "llm_probe_started",
        extra={
            "object": getattr(identity, "credential_id", None) or "draft",
            "detail": {
                "protocol": getattr(identity, "protocol", None),
                "wire": getattr(identity, "wire", None),
                "mode": chosen_mode,
            },
        },
    )
    key = _probe_key(identity, chosen_mode, resolved_model)
    return await single_flight(
        key,
        lambda: _run_probe(
            identity,
            mode=chosen_mode,
            model=resolved_model,
            actor=actor,
            record=record,
        ),
    )


class _PayloadWithModel:
    """把「只给了模型名」包成一个带 `.model` 的对象，供 `resolve_model()` 统一读取。"""

    __slots__ = ("model",)

    def __init__(self, model: str | None) -> None:
        self.model = model


async def _run_probe(
    identity: Any,
    *,
    mode: str,
    model: str | None,
    actor: str | None,
    record: bool,
) -> dict:
    stages: list[StageResult] = []
    problems: list[str] = []
    resolved_url = ""
    spec = protocols.get(getattr(identity, "protocol", ""))
    try:
        _method, resolved_url = protocols.build_models_request(spec, identity)
    except ApiError:  # pragma: no cover - 注册表保证有路径
        resolved_url = str(getattr(identity, "base_url", "") or "")

    if mode in ("auth", "both"):
        auth_stage = await _run_auth(identity)
        stages.append(auth_stage)
        if mode == "both" and not auth_stage.ok:
            # 鉴权没过就**不发**计费请求：短路规则是「不花用户的钱」（spec §3.4）
            stages.append(
                StageResult(
                    stage=COMPLETION_STAGE,
                    ok=False,
                    detail="未执行（鉴权未通过）",
                    model=model,
                    error_code=auth_stage.error_code,
                )
            )
            problems.append("鉴权阶段失败，未发出计费请求")
    if mode == "completion" or (mode == "both" and stages and stages[0].ok):
        assert model is not None  # 上方已校验
        stages.append(await _run_completion(identity, model))

    first_bad = next((stage for stage in stages if not stage.ok), None)
    ok = first_bad is None and bool(stages)
    error_code = first_bad.error_code if first_bad else None
    hint = _HINTS.get(error_code or "") if error_code else None
    latency_ms = sum(stage.latency_ms for stage in stages)
    detail_redacted = first_bad.detail if first_bad else None
    probe_id = None
    if record:
        probe_id = record_probe(
            identity,
            mode=mode,
            model=model,
            ok=ok,
            stages=[stage.as_dict() for stage in stages],
            http_status=first_bad.http_status if first_bad else stages[-1].http_status,
            latency_ms=latency_ms,
            error_code=error_code,
            detail_redacted=detail_redacted,
            actor=actor,
        )
    log.info(
        "llm_probe_finished",
        extra={
            "object": getattr(identity, "credential_id", None) or "draft",
            "latency_ms": latency_ms,
            "code": error_code,
            "detail": {"mode": mode, "ok": ok, "stages": [s.stage for s in stages]},
        },
    )
    result = {
        "ok": ok,
        "protocol": getattr(identity, "protocol", None),
        "wire": getattr(identity, "wire", None),
        "base_url": getattr(identity, "base_url", None),
        "resolved_url": resolved_url,
        "mode": mode,
        "stages": [stage.as_dict() for stage in stages],
        "probe_id": probe_id,
        "checked_at": now_iso(),
        "problems": problems,
        "credential": getattr(identity, "credential", None),
        "inferred": bool(getattr(identity, "inferred", False)),
        "notes": list(getattr(identity, "notes", []) or []),
        # 完成态探测的模型名回显（前端 ③ 要显示「测的是哪个模型」）
        "model": model,
        "latency_ms": latency_ms,
    }
    if error_code:
        result["error_code"] = error_code
        result["hint"] = hint or "查看脱敏后的上游摘要；检查地址、路径与凭证"
    return result


#: 失败阶段的处置建议（前端 ③ 直接渲染，不再自己猜）
_HINTS = {
    Code.UPSTREAM_AUTH: "上游拒绝鉴权：检查 API key 是否属于该项目，或该 key 是否启用了对应 API",
    Code.UPSTREAM_TIMEOUT: "上游超时：确认地址可达；思考型模型可能需要更长的生成时间",
    Code.UPSTREAM_UNAVAILABLE: "连不上上游：检查地址、DNS、代理与防火墙",
    Code.UPSTREAM_INVALID_RESPONSE: "上游 200 但不是该协议的响应结构：多半是路径不对，或 wire 选错了",
    Code.UPSTREAM_RATE_LIMIT: "上游限流：稍后重试，或降低探测频率",
    Code.UPSTREAM_ERROR: "上游返回错误：核对协议、路径与模型名",
}


def record_probe(
    identity: Any,
    *,
    mode: str,
    model: str | None,
    ok: bool,
    stages: list[dict],
    http_status: int | None,
    latency_ms: int | None,
    error_code: str | None,
    detail_redacted: str | None,
    actor: str | None = None,
) -> str:
    """写一条探测历史（**不含密钥**），并给草稿/凭证两种来源都写一条审计。"""
    probe_id = uuid_pk("llmp")
    safe_stages = [
        {
            **stage,
            "detail": redact(str(stage.get("detail") or ""), max_len=400),
        }
        for stage in stages
    ]
    safe_detail = redact(detail_redacted or "", max_len=400) or None
    with db_write() as s:
        s.add(
            LlmConnectionProbe(
                id=probe_id,
                credential_id=getattr(identity, "credential_id", None),
                provider_id=getattr(identity, "provider_id", None),
                protocol=str(getattr(identity, "protocol", "") or ""),
                wire=str(getattr(identity, "wire", "") or ""),
                base_url=str(getattr(identity, "base_url", "") or ""),
                model_id=model,
                mode=mode,
                ok=1 if ok else 0,
                stages=safe_stages,
                http_status=http_status,
                latency_ms=latency_ms,
                error_code=error_code,
                detail_redacted=safe_detail,
                actor_id=actor,
                request_id=current_request_id(),
                at=now_iso(),
            )
        )
    from .. import audit

    credential_id = getattr(identity, "credential_id", None)
    audit.write(
        "llm.probe",
        object_type="credential" if credential_id else "llm_draft",
        object_id=credential_id or getattr(identity, "protocol", None),
        summary={
            "protocol": getattr(identity, "protocol", None),
            "mode": mode,
            "ok": ok,
            "stage": next((s.get("stage") for s in safe_stages if not s.get("ok")), "auth"),
        },
        actor_id=actor,
    )
    return probe_id


def probe_row_to_dict(row: LlmConnectionProbe) -> dict:
    """`GET /llm/probes` 的列表项 —— 已带**完整** `stages`，不需要详情端点（R-14）。"""
    return {
        "id": row.id,
        "protocol": row.protocol,
        "wire": row.wire,
        "base_url": row.base_url,
        "model_id": row.model_id,
        "mode": row.mode,
        "ok": bool(row.ok),
        "stages": row.stages or [],
        "http_status": row.http_status,
        "latency_ms": row.latency_ms,
        "error_code": row.error_code,
        "detail_redacted": row.detail_redacted,
        "credential_id": row.credential_id,
        "at": row.at,
    }


__all__ = [
    "AUTH_STAGE",
    "COMPLETION_STAGE",
    "MODES",
    "StageResult",
    "check_throttle",
    "probe_row_to_dict",
    "record_probe",
    "request",
    "reset_state",
    "resolve_model",
    "single_flight",
    "test_connection",
]
