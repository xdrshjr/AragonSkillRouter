"""统一执行编排：同步 / 异步 / 流式 + 幂等 + 配额 + 记录（§4.4(c)(d) / §7.6 / §7.8）。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import jsonschema
from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..executors.http_executor import invoke_sync, stream_events
from ..logging import get_logger, redact
from ..models import CallLog, IdempotencyKey, Task
from ..models import CapabilityVersion
from ..security.ratelimit import check_and_count, check_budget, record_cost, release_concurrency
from ..security.tokens import TokenContext
from ..sse import broker
from ..settings import settings
from . import capabilities, routing, tasks, usage

log = get_logger("skillrouter.run")

PREVIEW = 2048


# ── 公共辅助 ─────────────────────────────────────────────────


def _validate_input(definition: dict, input_payload: dict) -> None:
    schema = definition.get("input_schema") or {}
    try:
        jsonschema.Draft202012Validator(schema).validate(input_payload)
    except jsonschema.ValidationError as exc:
        field = ".".join(str(p) for p in exc.path if p not in ("$",))
        raise ApiError(
            Code.INPUT_SCHEMA_INVALID,
            message=f"输入不满足 input_schema：{exc.message}",
            hint="按 details.fields 修正；提供商特有参数请放进 options.extensions",
            details={"fields": [field] if field else []},
        ) from exc


def _fingerprint(capability_key: str, version: str, input_payload: dict, mode: str) -> str:
    payload = json.dumps(
        {"capability": capability_key, "version": version, "mode": mode, "input": input_payload},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _idempotency_lookup(scope: str, key: str | None, fingerprint: str) -> dict | None:
    if not key:
        return None
    with db_write() as s:
        row = s.scalar(
            select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.idem_key == key)
        )
        if row is None:
            return None
        if row.fingerprint != fingerprint:
            raise ApiError(
                Code.IDEMPOTENCY_KEY_REUSED,
                message="同一幂等键对应了不同的请求体",
                hint="使用新的幂等键",
                details={"idempotency_key": key},
            )
        call = s.scalar(select(CallLog).where(CallLog.request_id == row.request_id))
        task = s.scalar(select(Task).where(Task.request_id == row.request_id))
        if task is not None:
            return {
                "replayed": True,
                "request_id": row.request_id,
                "task_id": task.id,
                "status": task.status,
                "body": {"request_id": row.request_id, "task_id": task.id, "status": task.status},
            }
        if call is not None:
            return {
                "replayed": True,
                "request_id": row.request_id,
                "status": call.status,
                "body": {
                    "request_id": row.request_id,
                    "status": call.status,
                    "capability": {"key": call.capability_key},
                    "output": json.loads(call.response_preview) if call.response_preview and call.response_preview.startswith("{") else None,
                    "error": {"code": call.error_code} if call.error_code else None,
                    "idempotent_replay": True,
                },
            }
        return {"replayed": True, "request_id": row.request_id, "body": {"request_id": row.request_id}}


def _idempotency_store(scope: str, key: str | None, fingerprint: str, request_id: str, capability_key: str) -> None:
    if not key:
        return
    with db_write() as s:
        s.add(
            IdempotencyKey(
                id=uuid_pk("idm"),
                scope=scope,
                idem_key=key,
                fingerprint=fingerprint,
                request_id=request_id,
                capability_key=capability_key,
                created_at=now_iso(),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=24))
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )
        )


def _quota_gate(*, token_ctx: TokenContext | None, definition: dict, provider_id: str | None) -> list[dict]:
    """令牌 → 能力 → 提供商 → 全局，任意一层超限即拒绝。

    逐层计数是「先加后判」：某一层拒绝时必须把已计入的并发名额退回，
    否则反复被拒的调用会把令牌/提供商的并发额度永久吃光（§7.10）。
    """
    scopes: list[dict] = []
    try:
        if token_ctx is not None:
            entry = {"scope_type": "token", "scope_id": token_ctx.token_id,
                     "rpm": token_ctx.rpm, "concurrency": token_ctx.concurrency, "daily_calls": token_ctx.daily_calls}
            check_and_count(**entry)
            scopes.append(entry)
            check_budget(scope_type="token", scope_id=token_ctx.token_id, max_cost_usd=token_ctx.budget_usd)
        for scope_type, scope_id, kwargs in (
            ("capability", definition.get("key", ""), {}),
            ("provider", provider_id or "*", {}),
            ("global", "*", {}),
        ):
            entry = {"scope_type": scope_type, "scope_id": scope_id, **kwargs}
            check_and_count(**entry)
            scopes.append(entry)
    except BaseException:
        _release(scopes)
        raise
    return scopes


def _release(scopes: list[dict]) -> None:
    for entry in scopes:
        release_concurrency(scope_type=entry["scope_type"], scope_id=entry["scope_id"])


def _log_call(
    *,
    request_id: str,
    caller_type: str,
    caller_id: str | None,
    capability_key: str,
    version_id: str | None,
    provider_id: str | None,
    credential_id: str | None,
    mode: str,
    status: str,
    http_status: int | None,
    error_code: str | None,
    latency_ms: int | None,
    upstream_ms: int | None,
    retries: int,
    idempotency_key: str | None,
    fingerprint: str,
    request_preview: str | None,
    response_preview: str | None,
    task_id: str | None = None,
    agent_session_id: str | None = None,
) -> str:
    call_id = uuid_pk("cal")
    policy = settings.payload_policy
    with db_write() as s:
        s.add(
            CallLog(
                id=call_id,
                request_id=request_id,
                caller_type=caller_type,
                caller_id=caller_id,
                capability_key=capability_key,
                capability_version_id=version_id,
                provider_id=provider_id,
                credential_id=credential_id,
                mode=mode,
                status=status,
                http_status=http_status,
                error_code=error_code,
                latency_ms=latency_ms,
                queue_ms=0,
                upstream_ms=upstream_ms,
                retries=retries,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                payload_policy=policy,
                request_preview=redact(request_preview, max_len=PREVIEW) if policy != "none" else None,
                response_preview=redact(response_preview, max_len=PREVIEW) if policy == "full" else (
                    redact(response_preview, max_len=PREVIEW) if policy == "redacted_preview" else None
                ),
                agent_session_id=agent_session_id,
                task_id=task_id,
                created_at=now_iso(),
            )
        )
    return call_id


def _caller(caller: dict | None, token_ctx: TokenContext | None) -> tuple[str, str | None, str]:
    if token_ctx is not None:
        return "token", token_ctx.token_id, f"token:{token_ctx.token_id}"
    caller = caller or {}
    return caller.get("type", "admin"), caller.get("id"), f"admin:{caller.get('id') or 'unknown'}"


# ── 同步调用 ─────────────────────────────────────────────────


async def invoke(
    *,
    capability_key: str,
    input_payload: dict,
    options: dict | None = None,
    caller: dict | None = None,
    token_ctx: TokenContext | None = None,
    agent_session_id: str | None = None,
) -> dict:
    options = dict(options or {})
    request_id = uuid_pk("req")
    started = time.perf_counter()
    capability = capabilities.lookup(capability_key)
    definition = capability["definition"]
    version = options.get("capability_version") or capability["version"]
    if options.get("capability_version") and options["capability_version"] != capability["version"]:
        with db_session() as s:
            pinned = s.scalar(
                select(CapabilityVersion).where(
                    CapabilityVersion.capability_id == capability["id"],
                    CapabilityVersion.version == options["capability_version"],
                )
            )
        if pinned is None:
            raise ApiError(Code.NOT_FOUND, f"能力版本 {options['capability_version']} 不存在")
        definition = pinned.definition
        version = pinned.version
        capability["version_id"] = pinned.id
    mode = options.get("mode") or (definition.get("modes") or ["sync"])[0]
    if mode not in (definition.get("modes") or ["sync"]):
        raise ApiError(
            Code.UNSUPPORTED_MODE,
            message=f"该能力不支持 {mode} 模式",
            details={"supported": definition.get("modes")},
        )
    extensions = options.get("extensions") or {}
    if extensions:
        allowed = set((definition.get("input_schema") or {}).get("properties") or {})
        for key in extensions:
            if key not in allowed and key not in ("openai", "anthropic", "google", "extra_headers"):
                raise ApiError(
                    Code.UNSUPPORTED_PARAM,
                    message=f"未声明的扩展参数：{key}",
                    hint="提供商特有参数统一放在 options.extensions 下的已知命名空间",
                )
    _validate_input(definition, input_payload)

    caller_type, caller_id, scope = _caller(caller, token_ctx)
    idem_key = options.get("idempotency_key")
    fingerprint = _fingerprint(capability_key, version, input_payload, mode)
    replay = _idempotency_lookup(scope, idem_key, fingerprint)
    if replay:
        return replay["body"]

    if mode == "async":
        return await submit_async(
            request_id=request_id,
            capability=capability,
            definition=definition,
            version=version,
            input_payload=input_payload,
            options=options,
            caller_type=caller_type,
            caller_id=caller_id,
            scope=scope,
            idem_key=idem_key,
            fingerprint=fingerprint,
            token_ctx=token_ctx,
            agent_session_id=agent_session_id,
        )
    if mode == "stream":
        raise ApiError(
            Code.UNSUPPORTED_MODE,
            message="流式调用必须使用 SSE 端点",
            hint="POST /api/v1/capabilities/{key}/invoke 时不要设置 mode=stream，改用 options.stream=true",
        )

    selection, material, base_url = routing.resolve(
        capability_id=capability["id"],
        definition=definition,
        mode=mode,
        options=options,
        token_ctx=token_ctx,
        tags=capability.get("tags"),
    )
    scopes = _quota_gate(
        token_ctx=token_ctx, definition=definition, provider_id=(selection.chosen or {}).get("provider_id")
    )
    try:
        result = await invoke_sync(
            request_id=request_id,
            capability=definition,
            route=selection.chosen or {},
            input_payload=input_payload,
            material=material,
            base_url=base_url,
            timeout_ms=int((selection.chosen or {}).get("timeout_ms") or 60000),
            options=options,
            retry_conf=(selection.chosen or {}).get("retry"),
        )
        # 受限故障切换：仅 retryable + 未产生输出 + 未提交异步任务
        failover = definition.get("failover") or {}
        switches = 0
        max_switches = int(failover.get("max_switches", 2)) if failover.get("enabled", True) else 0
        tried = {(selection.chosen or {}).get("route_id", "")}
        while (
            result.status == "failed"
            and (result.error or {}).get("retryable")
            and switches < max_switches
        ):
            nxt = routing.alt_route(definition=definition, mode=mode, tried=tried, options=options)
            if not nxt:
                break
            switches += 1
            tried.add(nxt.get("id") or nxt.get("route_key") or "")
            try:
                from ..executors.parsers import jsonpath  # noqa: F401

                from . import credentials  # noqa: PLC0415

                material = credentials.materialize(nxt["credential_id"])
            except ApiError:
                continue
            previous = (selection.chosen or {}).get("route_id")
            selection.chosen = {**nxt, "route_id": nxt.get("id") or nxt.get("route_key")}
            base_url = material.get("base_url") or base_url
            retried = await invoke_sync(
                request_id=request_id,
                capability=definition,
                route=selection.chosen,
                input_payload=input_payload,
                material=material,
                base_url=base_url,
                timeout_ms=int(nxt.get("timeout_ms") or 60000),
                options=options,
                retry_conf=nxt.get("retry"),
            )
            retried.timings = {**(retried.timings or {}), "failover_switches": switches}
            retried.retries += switches
            result = retried
            routing.record_decision(
                call_id=None,
                selection=selection,
                reason=f"从 {previous} 故障切换到 {selection.chosen.get('route_id')}（原因：{(result.error or {}).get('code') or 'retryable'}）",
                failover_from=previous,
            )
    finally:
        _release(scopes)

    total_ms = int((time.perf_counter() - started) * 1000)
    timings = {**(result.timings or {}), "total_ms": total_ms, "failover_switches": switches}
    call_id = _log_call(
        request_id=request_id,
        caller_type=caller_type,
        caller_id=caller_id,
        capability_key=capability_key,
        version_id=capability.get("version_id"),
        provider_id=(selection.chosen or {}).get("provider_id"),
        credential_id=(selection.chosen or {}).get("credential_id"),
        mode=mode,
        status="success" if result.status == "succeeded" else result.status,
        http_status=result.http_status,
        error_code=(result.error or {}).get("code"),
        latency_ms=total_ms,
        upstream_ms=(result.timings or {}).get("upstream_ms"),
        retries=result.retries,
        idempotency_key=idem_key,
        fingerprint=result.request_fingerprint or fingerprint,
        request_preview=result.request_preview,
        response_preview=result.response_preview,
        agent_session_id=agent_session_id,
    )
    routing.record_decision(call_id=call_id, selection=selection)
    usage_cost = usage.record(
        call_id=call_id,
        provider_id=(selection.chosen or {}).get("provider_id"),
        capability_key=capability_key,
        token_id=token_ctx.token_id if token_ctx else None,
        usage=result.usage,
    )
    if usage_cost["cost_usd"]:
        record_cost(
            scope_type="token",
            scope_id=token_ctx.token_id if token_ctx else "*",
            cost_usd=usage_cost["cost_usd"],
        )
    _idempotency_store(scope, idem_key, fingerprint, request_id, capability_key)
    body = result.as_response(
        {"key": capability_key, "version": capability["version"]},
        {
            "id": (selection.chosen or {}).get("provider_id"),
            "name": (selection.chosen or {}).get("provider_name", ""),
        },
    )
    body["timings"] = timings
    body["usage"] = {
        **(result.usage or {}),
        "cost_usd": usage_cost["cost_usd"],
        "source": (result.usage or {}).get("source", usage_cost["source"]),
    }
    return body


# ── 异步提交（请求内不触达上游）──────────────────────────────


async def submit_async(
    *,
    request_id: str,
    capability: dict,
    definition: dict,
    version: str,
    input_payload: dict,
    options: dict,
    caller_type: str,
    caller_id: str | None,
    scope: str,
    idem_key: str | None,
    fingerprint: str,
    token_ctx: TokenContext | None,
    agent_session_id: str | None = None,
) -> dict:
    selection, _material, _base_url = routing.resolve(
        capability_id=capability["id"],
        definition=definition,
        mode="async",
        options=options,
        token_ctx=token_ctx,
        tags=capability.get("tags"),
    )
    scopes = _quota_gate(
        token_ctx=token_ctx, definition=definition, provider_id=(selection.chosen or {}).get("provider_id")
    )
    _release(scopes)
    created = tasks.create(
        request_id=request_id,
        capability_key=capability["key"],
        capability_version_id=capability.get("version_id"),
        mode="async",
        input_snapshot={"input": input_payload, "options": {k: v for k, v in options.items() if k != "idempotency_key"}},
        route_id=(selection.chosen or {}).get("route_id"),
        token_id=token_ctx.token_id if token_ctx else None,
        agent_session_id=agent_session_id,
    )
    _log_call(
        request_id=request_id,
        caller_type=caller_type,
        caller_id=caller_id,
        capability_key=capability["key"],
        version_id=capability.get("version_id"),
        provider_id=(selection.chosen or {}).get("provider_id"),
        credential_id=(selection.chosen or {}).get("credential_id"),
        mode="async",
        status="queued",
        http_status=202,
        error_code=None,
        latency_ms=None,
        upstream_ms=None,
        retries=0,
        idempotency_key=idem_key,
        fingerprint=fingerprint,
        request_preview=json.dumps({"input": input_payload}, ensure_ascii=False),
        response_preview=None,
        task_id=created["task_id"],
        agent_session_id=agent_session_id,
    )
    routing.record_decision(call_id=None, selection=selection)
    _idempotency_store(scope, idem_key, fingerprint, request_id, capability["key"])
    broker.publish(
        "tasks",
        "task.updated",
        {"task_id": created["task_id"], "status": "queued", "capability": capability["key"]},
    )
    return {
        "request_id": request_id,
        "task_id": created["task_id"],
        "status": "queued",
        "capability": {"key": capability["key"], "version": version},
        "timings": {"queue_ms": 0},
    }


# ── 流式调用 ─────────────────────────────────────────────────


async def stream(
    *,
    capability_key: str,
    input_payload: dict,
    options: dict | None = None,
    caller: dict | None = None,
    token_ctx: TokenContext | None = None,
    agent_session_id: str | None = None,
) -> AsyncIterator[dict]:
    options = dict(options or {})
    options["mode"] = "stream"
    started = time.perf_counter()
    request_id = uuid_pk("req")
    capability = capabilities.lookup(capability_key)
    definition = capability["definition"]
    if "stream" not in (definition.get("modes") or []):
        raise ApiError(
            Code.UNSUPPORTED_MODE,
            message="该能力不支持流式调用",
            details={"supported": definition.get("modes")},
        )
    _validate_input(definition, input_payload)
    caller_type, caller_id, scope = _caller(caller, token_ctx)
    selection, material, base_url = routing.resolve(
        capability_id=capability["id"],
        definition=definition,
        mode="stream",
        options=options,
        token_ctx=token_ctx,
        tags=capability.get("tags"),
    )
    scopes = _quota_gate(
        token_ctx=token_ctx, definition=definition, provider_id=(selection.chosen or {}).get("provider_id")
    )
    usage_final: dict = {}
    status = "failed"
    error_code = None
    produced = False
    try:
        yield {"type": "call.started", "request_id": request_id, "capability": capability_key}
        async for event in stream_events(
            request_id=request_id,
            capability=definition,
            route=selection.chosen or {},
            input_payload=input_payload,
            material=material,
            base_url=base_url,
            timeout_ms=int((selection.chosen or {}).get("timeout_ms") or 120000),
            options=options,
        ):
            if event["type"] == "delta":
                produced = True
                yield {"type": "call.delta", "request_id": request_id, "chunk": event["text"]}
            elif event["type"] == "usage":
                usage_final = event["usage"]
                yield {"type": "call.usage", "request_id": request_id, "usage": usage_final}
            elif event["type"] == "done":
                status = "success"
                yield {"type": "call.done", "request_id": request_id, "timings": event.get("timings")}
            elif event["type"] == "error":
                error_code = (event.get("error") or {}).get("code")
                yield {"type": "call.error", "request_id": request_id, "error": event.get("error")}
    finally:
        _release(scopes)
        total_ms = int((time.perf_counter() - started) * 1000)
        call_id = _log_call(
            request_id=request_id,
            caller_type=caller_type,
            caller_id=caller_id,
            capability_key=capability_key,
            version_id=capability.get("version_id"),
            provider_id=(selection.chosen or {}).get("provider_id"),
            credential_id=(selection.chosen or {}).get("credential_id"),
            mode="stream",
            status=status,
            http_status=None,
            error_code=error_code,
            latency_ms=total_ms,
            upstream_ms=total_ms,
            retries=0,
            idempotency_key=options.get("idempotency_key"),
            fingerprint=_fingerprint(capability_key, capability["version"], input_payload, "stream"),
            request_preview=json.dumps({"input": input_payload}, ensure_ascii=False),
            response_preview=None,
            agent_session_id=agent_session_id,
        )
        routing.record_decision(call_id=call_id, selection=selection)
        if usage_final:
            usage.record(
                call_id=call_id,
                provider_id=(selection.chosen or {}).get("provider_id"),
                capability_key=capability_key,
                token_id=token_ctx.token_id if token_ctx else None,
                usage=usage_final,
            )
        broker.publish("calls", "call.finished", {"request_id": request_id, "status": status, "streamed": produced})


__all__ = ["invoke", "submit_async", "stream", "Any"]
