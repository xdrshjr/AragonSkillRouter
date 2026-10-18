"""上游异步任务：提交 / 轮询 / 取消能力探测（§7.8 / §4.4(d)）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..errors import ApiError, Code
from ..logging import redact
from ..security.netpolicy import client_kwargs
from .auth import build_auth_headers
from .http_executor import build_request
from .mapping import fill_template
from .parsers import jsonpath, load_json

STATUS_MAP = {
    "queued": ("queued", 5),
    "pending": ("running", 20),
    "starting": ("running", 10),
    "processing": ("running", 40),
    "running": ("running", 60),
    "in_progress": ("running", 60),
    "succeeded": ("succeeded", 100),
    "success": ("succeeded", 100),
    "completed": ("succeeded", 100),
    "failed": ("failed", 100),
    "error": ("failed", 100),
    "canceled": ("canceled", 100),
    "cancelled": ("canceled", 100),
}


async def submit(
    *,
    capability: dict,
    route: dict,
    input_payload: dict,
    material: dict,
    base_url: str,
    timeout_ms: int,
) -> dict:
    """提交上游异步任务，返回 `{upstream_task_id, http_status, raw_preview}`。"""
    async_conf = route.get("async") or {}
    submit_conf = async_conf.get("submit") or {}
    req = build_request(
        capability=capability,
        route=route,
        input_payload=input_payload,
        material=material,
        base_url=base_url,
        options={"submit": submit_conf},
    )
    if submit_conf.get("url_template"):
        req["url"] = fill_template(
            submit_conf["url_template"],
            {"base_url": base_url.rstrip("/"), "model": route.get("model_hint") or ""},
        )
    async with httpx.AsyncClient(
        **client_kwargs(req["url"], timeout=httpx.Timeout(timeout_ms / 1000), follow_redirects=False)
    ) as client:
        try:
            response = await client.request(
                req["method"],
                req["url"],
                headers=req["headers"],
                params=req["query"] or None,
                content=req["content"],
                files=req["files"],
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                Code.TASK_STATUS_UNKNOWN,
                message="提交上游异步任务超时，无法判定是否已受理",
                hint="先调用对账接口查询，不要重复提交",
            ) from exc
    text = response.text
    if response.status_code >= 400:
        raise ApiError(
            Code.UPSTREAM_ERROR,
            message=f"上游拒绝异步提交（{response.status_code}）",
            hint=redact(text, max_len=300),
        )
    body = load_json(text)
    task_id_path = submit_conf.get("task_id_path", "$.id")
    upstream_task_id = jsonpath(body, task_id_path)
    if not upstream_task_id:
        raise ApiError(
            Code.UPSTREAM_INVALID_RESPONSE,
            message=f"上游响应中没有任务标识（{task_id_path}）",
            hint="检查 async.submit.task_id_path",
        )
    return {
        "upstream_task_id": str(upstream_task_id),
        "http_status": response.status_code,
        "raw_preview": redact(text, max_len=1024),
    }


async def query(
    *, route: dict, material: dict, base_url: str, upstream_task_id: str, timeout_ms: int = 30000
) -> dict:
    """查询上游任务状态，返回 `{status, progress, raw, error}`。"""
    query_conf = (route.get("async") or {}).get("query") or {}
    url = fill_template(
        query_conf.get("url_template") or "",
        {"base_url": base_url.rstrip("/"), "task_id": upstream_task_id},
    )
    url = url.replace("{task_id}", upstream_task_id)
    if not url.startswith(("http://", "https://")):
        raise ApiError(Code.ASYNC_QUERY_MISSING, "async.query.url_template 缺失或非法")
    method = (query_conf.get("method") or "GET").upper()
    headers = build_auth_headers(material)
    async with httpx.AsyncClient(
        **client_kwargs(url, timeout=httpx.Timeout(timeout_ms / 1000), follow_redirects=False)
    ) as client:
        response = await client.request(method, url, headers=headers)
    text = response.text
    body = load_json(text)
    raw_status = str(jsonpath(body, query_conf.get("status_path", "$.status"), "")).lower()
    status, progress = STATUS_MAP.get(raw_status, ("running", 50))
    success = [s.lower() for s in query_conf.get("success", ["succeeded", "success"])]
    failure = [s.lower() for s in query_conf.get("failure", ["failed", "error"])]
    if raw_status in success:
        status, progress = "succeeded", 100
    elif raw_status in failure:
        status, progress = "failed", 100
    return {
        "status": status,
        "progress": progress,
        "raw_status": raw_status,
        "body": body,
        "http_status": response.status_code,
        "preview": redact(text, max_len=2048),
    }


def supports_cancel(route: dict) -> bool:
    return bool(((route.get("async") or {}).get("query") or {}).get("cancel"))


async def cancel(
    *, route: dict, material: dict, base_url: str, upstream_task_id: str, timeout_ms: int = 20000
) -> dict:
    cancel_conf = ((route.get("async") or {}).get("query") or {}).get("cancel") or {}
    if not cancel_conf:
        raise ApiError(
            Code.CANCEL_UNSUPPORTED,
            message="该上游不支持取消任务",
            hint="只能停止平台侧等待，远端任务可能继续执行并产生费用",
        )
    url = fill_template(
        cancel_conf.get("url_template") or "",
        {"base_url": base_url.rstrip("/"), "task_id": upstream_task_id},
    ).replace("{task_id}", upstream_task_id)
    method = (cancel_conf.get("method") or "POST").upper()
    async with httpx.AsyncClient(
        **client_kwargs(url, timeout=httpx.Timeout(timeout_ms / 1000), follow_redirects=False)
    ) as client:
        response = await client.request(method, url, headers=build_auth_headers(material))
    return {"ok": response.status_code < 400, "http_status": response.status_code}


def backoff_ms(attempt: int, poll_conf: dict | None = None) -> int:
    poll_conf = poll_conf or {}
    initial = int(poll_conf.get("initial_ms", 2000))
    maximum = int(poll_conf.get("max_ms", 30000))
    return min(initial * (2 ** max(0, attempt - 1)), maximum)


async def sleep_ms(ms: int) -> None:
    await asyncio.sleep(max(0, ms) / 1000)


def deadline(poll_conf: dict | None) -> float:
    poll_conf = poll_conf or {}
    return time.time() + int(poll_conf.get("timeout_s", 900))


def output_from(body: Any, route: dict) -> Any:
    from .parsers import apply_response_map

    return apply_response_map(route.get("response_map") or {}, body)


__all__ = ["submit", "query", "cancel", "supports_cancel", "backoff_ms", "sleep_ms", "deadline", "output_from"]
