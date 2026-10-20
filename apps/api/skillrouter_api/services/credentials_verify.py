"""凭证有效性探测（§7.5 / §15.3）。真实请求上游，不 mock。"""

from __future__ import annotations

import time

import httpx

from ..db import db_session
from ..logging import get_logger, redact
from ..models import Provider
from ..security.netpolicy import client_kwargs, validate_admin_target
from ..settings import settings
from . import credentials

log = get_logger("skillrouter.run")

PROBES = {
    "llm": ("GET", "{base_url}/models", None),
    "image": ("GET", "{base_url}/models", None),
    "video": ("GET", "{base_url}/models", None),
    "speech": ("GET", "{base_url}/models", None),
    "asr": ("GET", "{base_url}/models", None),
    "generic": ("GET", "{base_url}/health", None),
}


def _probe_for(material: dict, meta: dict) -> tuple[str, str]:
    kind = material.get("kind") or "llm"
    custom = meta.get("probe_path")
    base = (material.get("base_url") or "").rstrip("/")
    if custom:
        return meta.get("probe_method", "GET"), f"{base}{custom}"
    _, template, _ = PROBES.get(kind, PROBES["generic"])
    return "GET", template.replace("{base_url}", base)


async def _verify_llm(
    credential_id: str, material: dict, meta: dict, *, actor: str | None = None
) -> dict:
    """`llm` 类凭证：协议感知的模型清单探测（`mode="auth"`，**不计费**）。

    旧实现对 `llm` 一律 `GET {base_url}/models`，于是：
    - Anthropic（真实端点 `GET {base}/v1/models`，必须带 `anthropic-version`）→ 404 判无效；
    - Google（`GET {base}/v1beta/models`，`x-goog-api-key`）→ 404 判无效。
    这条缺陷会把**有效凭证误判为无效**，是本轮必须一起修掉的（spec §3.7 / T-28）。
    自定义 `probe_path` 仍然优先（有的网关 `/models` 不在标准位置）。
    """
    from .llm import identity as llm_identity
    from .llm import probe as llm_probe

    custom = meta.get("probe_path")
    if custom:
        # 用户显式给了探测路径 → 走原来的通用路径（不猜协议）
        return await _verify_generic(credential_id, material, meta, actor=actor)

    identity = llm_identity.resolve_identity_for_credential(credential_id)
    try:
        result = await llm_probe.test_connection(
            identity, mode="auth", model=None, actor=actor, record=True
        )
    except Exception as exc:  # noqa: BLE001 - 探测本身不能把页面打成 500
        log.warning(
            "credential_verify_llm_failed",
            extra={"object": credential_id, "detail": str(exc)},
        )
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=0,
            error_code="UPSTREAM_UNAVAILABLE",
            detail=str(exc),
        )
    stage: dict = next(
        (s for s in result.get("stages") or [] if s.get("stage") == "auth"), {}
    )
    first_bad: dict = next((s for s in result.get("stages") or [] if not s.get("ok")), {})
    ok = bool(result.get("ok"))
    recorded = credentials.record_verification(
        credential_id,
        ok=ok,
        status_code=first_bad.get("http_status") or stage.get("http_status"),
        latency_ms=sum(int(s.get("latency_ms") or 0) for s in result.get("stages") or []),
        error_code=result.get("error_code"),
        detail=(first_bad.get("detail") or stage.get("detail") or "")[:800],
    )
    from . import audit

    audit.write(
        "credential.verified",
        object_type="credential",
        object_id=credential_id,
        summary={"ok": ok, "status": recorded.get("status_code"), "protocol": identity.protocol},
        actor_id=actor,
    )
    return {
        **recorded,
        # 只增不改：既有字段（ok/status_code/latency_ms/error_code/detail_redacted）保持不变
        "protocol": identity.protocol,
        "resolved_url": result.get("resolved_url"),
        "stage": llm_probe.AUTH_STAGE,
        "model_count": stage.get("model_count"),
        "hint": result.get("hint"),
        # 凭证详情页的阶段时间线直接渲染它（一次探测最多 2 个阶段，几十字节）
        "stages": result.get("stages") or [],
    }


async def _verify_generic(
    credential_id: str, material: dict, meta: dict, *, actor: str | None = None
) -> dict:
    method, url = _probe_for(material, meta)
    if not material.get("base_url"):
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=0,
            error_code="PROVIDER_NOT_AVAILABLE",
            detail="提供商未配置 base_url，无法探测",
        )
    try:
        validate_admin_target(url)
    except Exception as exc:  # noqa: BLE001 - 白名单/SSRF 拒绝也要记录
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=0,
            error_code="SSRF_BLOCKED",
            detail=str(exc),
        )
    from ..executors.auth import build_auth_headers

    try:
        headers = build_auth_headers(material)
    except ValueError as exc:
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=0,
            error_code="VALIDATION_FAILED",
            detail=str(exc),
        )
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            **client_kwargs(
                url,
                timeout=httpx.Timeout(min(settings.fetch_timeout_s, 20.0)),
                follow_redirects=False,
            )
        ) as client:
            response = await client.request(method, url, headers=headers)
        latency = int((time.perf_counter() - started) * 1000)
    except httpx.TimeoutException:
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code="UPSTREAM_TIMEOUT",
            detail=f"探测 {url} 超时",
        )
    except httpx.HTTPError as exc:
        return credentials.record_verification(
            credential_id,
            ok=False,
            status_code=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code="UPSTREAM_UNAVAILABLE",
            detail=str(exc),
        )
    ok = response.status_code < 400
    error_code = None
    if response.status_code in (401, 403):
        error_code = "UPSTREAM_AUTH"
    elif response.status_code == 429:
        error_code = "UPSTREAM_RATE_LIMIT"
    elif response.status_code >= 400:
        error_code = "UPSTREAM_ERROR"
    detail = redact(response.text[:800], max_len=800) if not ok else f"探测成功（{response.status_code}）"
    result = credentials.record_verification(
        credential_id,
        ok=ok,
        status_code=response.status_code,
        latency_ms=latency,
        error_code=error_code,
        detail=detail,
    )
    from . import audit

    audit.write(
        "credential.verified",
        object_type="credential",
        object_id=credential_id,
        summary={"ok": ok, "status": response.status_code},
        actor_id=actor,
    )
    return result


async def verify(credential_id: str, *, actor: str | None = None) -> dict:
    """探测一次并写 `credential_verifications` + `last_verified_at`。

    `llm` 类凭证走**协议感知**的模型清单探测（不计费）；其余类别沿用原 `PROBES` 表。
    """
    material = credentials.materialize(credential_id)
    meta = material.get("meta") or {}
    if (material.get("kind") or "llm").lower() == "llm":
        return await _verify_llm(credential_id, material, meta, actor=actor)
    return await _verify_generic(credential_id, material, meta, actor=actor)


async def verify_pending(limit: int = 20) -> dict:
    """批处理：没有验证记录或最近验证失败的凭证。"""
    from sqlalchemy import select

    from ..models import Credential

    with db_session() as s:
        rows = list(
            s.scalars(
                select(Credential).where(
                    Credential.deleted_at.is_(None),
                    Credential.status == "active",
                )
            )
        )[:limit]
    results = []
    for row in rows:
        try:
            results.append({"credential_id": row.id, **await verify(row.id)})
        except Exception as exc:  # noqa: BLE001
            log.error("credential_verify_failed", extra={"object": row.id, "detail": str(exc)})
            results.append({"credential_id": row.id, "ok": False, "error_code": "INTERNAL"})
    return {"checked": len(results), "results": results}


def provider_base_url(provider_id: str) -> str | None:
    with db_session() as s:
        provider = s.get(Provider, provider_id)
        return provider.base_url if provider else None
