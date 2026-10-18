"""HTTP 执行核心：同步 / 流式 / 异步提交（§7.6 / §A.3.5）。"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, AsyncIterator

import httpx

from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..security.netpolicy import client_kwargs
from .auth import build_auth_headers, redact_headers
from .base import ExecResult
from .errors import map_error
from .mapping import evaluate, fill_template
from .parsers import apply_response_map, extract_delta, load_json, parse_sse_line, parse_usage

log = get_logger("skillrouter.run")

PREVIEW_LIMIT = 2048

_DNS_MARKERS = (
    "name or service not known",
    "getaddrinfo",
    "nodename nor servname",
    "name resolution",
    "temporary failure in name",
    "no address associated",
)


def build_request(
    *,
    capability: dict,
    route: dict,
    input_payload: dict,
    material: dict,
    base_url: str,
    options: dict | None = None,
) -> dict:
    """组装上游请求：URL / headers / query / body。"""
    options = options or {}
    request_conf = route.get("request") or {}
    ctx = {"input": input_payload, "route": route, "options": options}
    params = evaluate(route.get("param_map") or {}, ctx)

    template_values = {
        **params,
        "base_url": (base_url or "").rstrip("/"),
        "model": route.get("model_hint") or params.get("model") or "",
        "model_hint": route.get("model_hint") or "",
        "route_id": route.get("id") or route.get("route_key") or "",
    }
    url = fill_template(request_conf.get("url_template") or "", template_values)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ApiError(
            Code.PARAM_MAP_UNRESOLVED,
            message=f"url_template 求值后不是合法 http(s) URL：{url!r}",
            hint="检查 request.url_template 是否包含 {base_url} 或为绝对 URL",
        )

    headers = {str(k): str(v) for k, v in (request_conf.get("headers") or {}).items()}
    auth_headers = build_auth_headers(material)
    headers.update(auth_headers)
    injected_names = set(auth_headers)
    if isinstance(options.get("extra_headers"), dict):
        extra_headers = {str(k): str(v) for k, v in options["extra_headers"].items()}
        headers.update(extra_headers)
        injected_names |= set(extra_headers)

    # 上游幂等透传（§A.3.5）：只有 route 声明了 `idempotency_header` 才把平台幂等键原样带给上游；
    # 未声明时不透传（重试策略按保守处理，由 errors[]/retry 配置决定）。
    idem_header = request_conf.get("idempotency_header") or route.get("idempotency_header")
    idem_key = options.get("idempotency_key")
    if idem_header and idem_key:
        headers[str(idem_header)] = str(idem_key)

    query: dict[str, Any] = {}
    for item in request_conf.get("query") or []:
        if isinstance(item, dict):
            name = item.get("name")
            source = item.get("from")
            if name and source and "." in source:
                ns, _, path = source.partition(".")
                container = {"input": input_payload, "route": route, "options": options}.get(ns, {})
                value = container
                for part in path.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is not None:
                    query[name] = value
        elif isinstance(item, str) and item in params:
            query[item] = params[item]

    encoding = (request_conf.get("encoding") or "json").lower()
    body_bytes: bytes | None = None
    files = None
    if encoding == "json":
        body = fill_template(request_conf.get("body_template"), template_values)
        body_bytes = json.dumps(body, ensure_ascii=False).encode()
        headers.setdefault("Content-Type", "application/json")
    elif encoding == "form":
        body = fill_template(request_conf.get("body_template"), template_values)
        body_bytes = "&".join(f"{k}={v}" for k, v in body.items()).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif encoding == "multipart":
        body = fill_template(request_conf.get("body_template"), template_values)
        files = {
            k: (None, str(v)) if not isinstance(v, (bytes, bytearray)) else (k, bytes(v))
            for k, v in body.items()
        }
        body_bytes = None
    else:
        raise ApiError(Code.UNSUPPORTED_PARAM, f"不支持的 request.encoding：{encoding}")

    fingerprint_src = f"{request_conf.get('method', 'POST')}|{url}|{json.dumps(params, sort_keys=True, default=str)}"
    return {
        "method": (request_conf.get("method") or "POST").upper(),
        "url": url,
        "headers": headers,
        "query": query,
        "content": body_bytes,
        "files": files,
        "params": params,
        "fingerprint": hashlib.sha256(fingerprint_src.encode()).hexdigest(),
        "safe_headers": redact_headers(
            headers, secret_names=injected_names, secret_values=(str(material.get("secret") or ""),)
        ),
    }


async def _send(
    req: dict, *, timeout_ms: int
) -> tuple[httpx.Response, str, int]:
    start = time.perf_counter()
    async with httpx.AsyncClient(
        **client_kwargs(
            req["url"],
            timeout=httpx.Timeout(timeout_ms / 1000, connect=min(10.0, timeout_ms / 1000)),
            follow_redirects=False,
        )
    ) as client:
        response = await client.request(
            req["method"],
            req["url"],
            headers=req["headers"],
            params=req["query"] or None,
            content=req["content"],
            files=req["files"],
        )
        text = response.text
    elapsed = int((time.perf_counter() - start) * 1000)
    return response, text, elapsed


def transport_kind(exc: BaseException) -> str:
    """把 httpx 传输异常细分成 §A.3.5 的 6 类 transport。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        # 还没拿到连接（连池都没排到）→ 视为未建立连接，可重试
        return "connect_timeout"
    if isinstance(exc, httpx.TimeoutException):
        # ReadTimeout / WriteTimeout：请求已发出但没拿到完整响应
        return "read_timeout"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return "dns_error"
        if "refused" in text:
            return "connection_refused"
        if "reset" in text or "aborted" in text:
            return "connection_reset"
        return "connection_refused"
    text = str(exc).lower()
    if "reset" in text or "aborted" in text or "disconnected" in text:
        return "connection_reset"
    return "upstream_hung"


def _unknown_result(
    *,
    request_id: str,
    req: dict,
    route: dict,
    mapped: dict,
    message: str,
    elapsed: int,
    started: float,
    attempts: int,
) -> ExecResult:
    """读超时/连接被重置：立即返回 `status_unknown` + `needs_reconcile`，**不重投、不 failover**。"""
    error = {
        "code": mapped["code"],
        "message": message,
        "hint": mapped["hint"],
        "retryable": False,
        "upstream_status": None,
        "transport": mapped.get("transport"),
        "status_unknown": True,
        "needs_reconcile": True,
    }
    timings = {
        "queue_ms": 0,
        "upstream_ms": elapsed,
        "total_ms": elapsed_ms(started),
        "needs_reconcile": True,
        "transport": mapped.get("transport"),
        "attempt": attempts,
    }
    return ExecResult(
        request_id=request_id,
        status="status_unknown",
        error=error,
        provider_id=route.get("provider_id"),
        timings=timings,
        request_fingerprint=req["fingerprint"],
        request_preview=redact(
            json.dumps(
                {
                    "method": req["method"],
                    "url": req["url"],
                    "headers": req["safe_headers"],
                    "params": req["params"],
                },
                ensure_ascii=False,
            ),
            max_len=PREVIEW_LIMIT,
        ),
        retries=attempts - 1,
    )


async def invoke_sync(
    *,
    request_id: str,
    capability: dict,
    route: dict,
    input_payload: dict,
    material: dict,
    base_url: str,
    timeout_ms: int,
    options: dict | None = None,
    retry_conf: dict | None = None,
) -> ExecResult:
    """同步调用：请求 → 状态/错误映射 → response_map → ExecResult。

    重试语义（§A.3.5）：只有 `retryable=True` 的失败才退避重投；
    `read_timeout` / `upstream_hung` / 非幂等 `connection_reset` 一旦出现立即返回
    `status_unknown`，绝不重投（同一请求可能已在对方执行）。
    """
    req = build_request(
        capability=capability,
        route=route,
        input_payload=input_payload,
        material=material,
        base_url=base_url,
        options=options,
    )
    retry_conf = retry_conf or {}
    max_attempts = max(1, min(int(retry_conf.get("max_attempts", 1)), 3))
    base_ms = int(retry_conf.get("base_ms", 500))
    attempts = 0
    last_error: dict | None = None
    started = time.perf_counter()

    while attempts < max_attempts:
        attempts += 1
        attempt_started = time.perf_counter()
        try:
            response, text, elapsed = await _send(req, timeout_ms=timeout_ms)
        except httpx.HTTPError as exc:
            kind = transport_kind(exc)
            mapped = map_error(
                status=None,
                body=None,
                rules=route.get("errors"),
                transport_error=kind,
                method=req["method"],
            )
            message = redact(str(exc) or mapped["code"], max_len=300)
            if mapped["status_unknown"]:
                return _unknown_result(
                    request_id=request_id,
                    req=req,
                    route=route,
                    mapped=mapped,
                    message=message,
                    elapsed=elapsed_ms(attempt_started),
                    started=started,
                    attempts=attempts,
                )
            last_error = {
                "code": mapped["code"],
                "message": message,
                "hint": mapped["hint"],
                "retryable": bool(mapped["retryable"]),
                "upstream_status": None,
                "transport": mapped.get("transport"),
            }
            if not mapped["retryable"] or attempts >= max_attempts:
                return ExecResult(
                    request_id=request_id,
                    status="failed",
                    error=last_error,
                    provider_id=route.get("provider_id"),
                    timings={
                        "queue_ms": 0,
                        "upstream_ms": elapsed_ms(attempt_started),
                        "total_ms": elapsed_ms(started),
                        "transport": mapped.get("transport"),
                        "attempts": attempts,
                    },
                    request_fingerprint=req["fingerprint"],
                    request_preview=redact(
                        json.dumps(
                            {
                                "method": req["method"],
                                "url": req["url"],
                                "headers": req["safe_headers"],
                                "params": req["params"],
                            },
                            ensure_ascii=False,
                        ),
                        max_len=PREVIEW_LIMIT,
                    ),
                    retries=attempts - 1,
                )
        else:
            body = load_json(text)
            if 200 <= response.status_code < 300:
                try:
                    output = apply_response_map(route.get("response_map") or {}, body, text=text)
                except ValueError as exc:
                    return ExecResult(
                        request_id=request_id,
                        status="failed",
                        error={
                            "code": "UPSTREAM_INVALID_RESPONSE",
                            "message": str(exc),
                            "hint": "检查 response_map 与真实响应结构",
                            "retryable": False,
                            "upstream_status": response.status_code,
                        },
                        provider_id=route.get("provider_id"),
                        http_status=response.status_code,
                        timings={
                            "queue_ms": 0,
                            "upstream_ms": elapsed,
                            "total_ms": elapsed_ms(started),
                        },
                        request_fingerprint=req["fingerprint"],
                        retries=attempts - 1,
                    )
                return ExecResult(
                    request_id=request_id,
                    status="succeeded",
                    output=output,
                    usage=parse_usage(route.get("response_map") or {}, body),
                    provider_id=route.get("provider_id"),
                    http_status=response.status_code,
                    timings={"queue_ms": 0, "upstream_ms": elapsed, "total_ms": elapsed_ms(started)},
                    request_preview=redact(
                        json.dumps(
                            {
                                "method": req["method"],
                                "url": req["url"],
                                "headers": req["safe_headers"],
                                "params": req["params"],
                            },
                            ensure_ascii=False,
                        ),
                        max_len=PREVIEW_LIMIT,
                    ),
                    response_preview=redact(text, max_len=PREVIEW_LIMIT),
                    request_fingerprint=req["fingerprint"],
                    retries=attempts - 1,
                )
            mapped = map_error(
                status=response.status_code, body=body, rules=route.get("errors")
            )
            last_error = {
                "code": mapped["code"],
                "message": f"上游返回 {response.status_code}",
                "hint": mapped.get("hint", ""),
                "retryable": mapped["retryable"],
                "upstream_status": response.status_code,
            }
            if not mapped["retryable"] or attempts >= max_attempts:
                return ExecResult(
                    request_id=request_id,
                    status="failed",
                    error=last_error,
                    provider_id=route.get("provider_id"),
                    http_status=response.status_code,
                    timings={"queue_ms": 0, "upstream_ms": elapsed, "total_ms": elapsed_ms(started)},
                    request_preview=redact(
                        json.dumps(
                            {"method": req["method"], "url": req["url"], "headers": req["safe_headers"], "params": req["params"]},
                            ensure_ascii=False,
                        ),
                        max_len=PREVIEW_LIMIT,
                    ),
                    response_preview=redact(text, max_len=PREVIEW_LIMIT),
                    request_fingerprint=req["fingerprint"],
                    retries=attempts - 1,
                )
        # 退避后重试（只有 retryable 的传输/上游错误会走到这里）
        import asyncio

        await asyncio.sleep(min(base_ms * (2 ** (attempts - 1)), int(retry_conf.get("max_ms", 8000))) / 1000)

    return ExecResult(
        request_id=request_id,
        status="failed",
        error=last_error
        or {"code": "UPSTREAM_ERROR", "message": "上游调用失败", "hint": "", "retryable": False, "upstream_status": None},
        provider_id=route.get("provider_id"),
        timings={"queue_ms": 0, "upstream_ms": elapsed_ms(started), "total_ms": elapsed_ms(started)},
        request_fingerprint=req["fingerprint"],
        retries=attempts - 1,
    )


async def stream_events(
    *,
    request_id: str,
    capability: dict,
    route: dict,
    input_payload: dict,
    material: dict,
    base_url: str,
    timeout_ms: int,
    options: dict | None = None,
) -> AsyncIterator[dict]:
    """流式调用：产出 `{type: 'delta'|'usage'|'done'|'error', ...}`。

    一旦产生首个 delta，绝不重试、绝不切换提供商（需求硬约束）；
    读超时按 §A.3.5 归为「结果不确定」：`retryable=False` + `needs_reconcile=True`。
    """
    req = build_request(
        capability=capability,
        route=route,
        input_payload=input_payload,
        material=material,
        base_url=base_url,
        options=options,
    )
    stream_conf = route.get("stream") or {}
    delta_path = stream_conf.get("delta_path")
    done_markers = set(stream_conf.get("done_markers") or ["[DONE]"])
    started = time.perf_counter()
    produced = False
    async with httpx.AsyncClient(
        **client_kwargs(
            req["url"],
            timeout=httpx.Timeout(timeout_ms / 1000, connect=min(10.0, timeout_ms / 1000)),
            follow_redirects=False,
        )
    ) as client:
        try:
            async with client.stream(
                req["method"],
                req["url"],
                headers={**req["headers"], "Accept": "text/event-stream"},
                params=req["query"] or None,
                content=req["content"],
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    mapped = map_error(
                        status=response.status_code,
                        body=load_json(text),
                        rules=route.get("errors"),
                    )
                    yield {
                        "type": "error",
                        "error": {
                            "code": mapped["code"],
                            "message": f"上游返回 {response.status_code}",
                            "hint": mapped.get("hint", ""),
                            "retryable": mapped["retryable"],
                            "upstream_status": response.status_code,
                        },
                    }
                    return
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    event, data = parse_sse_line(line)
                    if data is None:
                        continue
                    if data in done_markers:
                        break
                    payload = load_json(data)
                    if payload is None:
                        yield {"type": "delta", "text": data}
                        produced = True
                        continue
                    text = extract_delta(payload, delta_path) if delta_path else ""
                    if text:
                        produced = True
                        yield {"type": "delta", "text": text}
                    usage = parse_usage(route.get("response_map") or {}, payload) if isinstance(payload, dict) else None
                    if usage and (usage.get("input") or usage.get("output")):
                        yield {"type": "usage", "usage": usage}
                yield {
                    "type": "done",
                    "timings": {
                        "queue_ms": 0,
                        "upstream_ms": elapsed_ms(started),
                        "total_ms": elapsed_ms(started),
                    },
                    "produced": produced,
                }
        except httpx.HTTPError as exc:
            kind = transport_kind(exc)
            mapped = map_error(
                status=None,
                body=None,
                rules=route.get("errors"),
                transport_error=kind,
                method=req["method"],
            )
            # 流式不重投：已经产出内容 / 请求已发出 → 结果不确定，交给对账
            unresolved = bool(mapped.get("status_unknown")) or produced
            yield {
                "type": "error",
                "status_unknown": unresolved,
                "needs_reconcile": unresolved,
                "error": {
                    "code": mapped["code"],
                    "message": redact(str(exc) or mapped["code"], max_len=300),
                    "hint": mapped["hint"] if not produced else "已产生的内容不可恢复，请先对账再决定是否重试",
                    "retryable": False,
                    "upstream_status": None,
                    "transport": mapped.get("transport"),
                    "status_unknown": unresolved,
                },
            }


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["build_request", "invoke_sync", "stream_events", "elapsed_ms", "transport_kind"]
