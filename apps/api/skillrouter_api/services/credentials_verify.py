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


async def verify(credential_id: str, *, actor: str | None = None) -> dict:
    """探测一次并写 `credential_verifications` + `last_verified_at`。"""
    material = credentials.materialize(credential_id)
    meta = material.get("meta") or {}
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
