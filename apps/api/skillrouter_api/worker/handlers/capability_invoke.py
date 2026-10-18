"""异步能力调用：上游提交 + 轮询 + 结果落库 + 恢复（§4.4(d)）。"""

from __future__ import annotations

from .. import leases  # noqa: F401  (保持包结构导入一致性)
from ...errors import ApiError, Code
from ...executors import async_poll
from ...logging import get_logger, redact
from ...services import capabilities as capability_service
from ...services import credentials, routing, tasks, usage
from ...sse import broker

log = get_logger("skillrouter.run")


async def handle(payload: dict) -> None:
    task_id = payload.get("task_id")
    if not task_id:
        return
    task = tasks.get(task_id)
    if task["status"] in ("succeeded", "failed", "canceled"):
        return
    ctx = tasks.runtime_context(task_id)
    definition = _frozen_definition(ctx)
    snapshot = ctx.get("input_snapshot") or {}
    input_payload = snapshot.get("input") or {}

    route, material, base_url = routing.resolve_pinned(
        definition=definition, route_id=ctx.get("route_id")
    )

    if not ctx.get("upstream_task_id"):
        # 只有 queued 的任务可以被认领提交：重复入队 / 多 worker 抢到同一任务时，
        # 后到者直接退出，避免向上游重复下单（§7.8）。
        if not tasks.claim_submission(task_id):
            return
        try:
            submitted = await async_poll.submit(
                capability=definition,
                route=route,
                input_payload=input_payload,
                material=material,
                base_url=base_url,
                timeout_ms=int(route.get("timeout_ms") or 60000),
            )
        except ApiError as exc:
            tasks.finish(task_id, status="failed", error={"code": exc.code, "message": exc.message})
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "failed", "error": exc.code})
            return
        tasks.mark_running(task_id, upstream_task_id=submitted["upstream_task_id"])
        broker.publish(
            "tasks",
            "task.updated",
            {"task_id": task_id, "status": "running", "upstream_task_id": submitted["upstream_task_id"]},
        )
    elif ctx["status"] == "canceling":
        try:
            cancelled = await async_poll.cancel(
                route=route,
                material=material,
                base_url=base_url,
                upstream_task_id=ctx["upstream_task_id"],
            )
            tasks.mark_canceled(task_id, upstream_ok=bool(cancelled.get("ok")))
        except ApiError as exc:
            tasks.mark_canceled(task_id, upstream_ok=False)
            broker.publish(
                "tasks",
                "task.log",
                {"task_id": task_id, "line": f"取消未获上游确认：{exc.code}"},
            )
        return
    else:
        tasks.mark_running(task_id, upstream_task_id=ctx["upstream_task_id"])

    poll_conf = (route.get("async") or {}).get("poll") or {}
    deadline = async_poll.deadline(poll_conf)
    attempt = 0
    while True:
        current = tasks.get(task_id)
        if current["status"] in ("canceled", "canceling"):
            if current["status"] == "canceling":
                try:
                    cancelled = await async_poll.cancel(
                        route=route,
                        material=material,
                        base_url=base_url,
                        upstream_task_id=current["upstream_task_id"] or "",
                    )
                    tasks.mark_canceled(task_id, upstream_ok=bool(cancelled.get("ok")))
                except ApiError as exc:
                    tasks.mark_canceled(task_id, upstream_ok=False)
                    broker.publish(
                        "tasks",
                        "task.log",
                        {"task_id": task_id, "line": f"取消未获上游确认：{exc.code}"},
                    )
            return
        attempt += 1
        try:
            state = await async_poll.query(
                route=route,
                material=material,
                base_url=base_url,
                upstream_task_id=current["upstream_task_id"] or "",
            )
        except ApiError as exc:
            tasks.finish(task_id, status="status_unknown", error={"code": exc.code, "message": exc.message})
            broker.publish("tasks", "task.updated", {"task_id": task_id, "status": "status_unknown"})
            return
        tasks.update_progress(task_id, percent=state["progress"], stage="upstream_polling")
        broker.publish(
            "tasks",
            "task.updated",
            {"task_id": task_id, "status": state["status"], "progress": state["progress"]},
        )
        if state["status"] == "succeeded":
            try:
                output = async_poll.output_from(state["body"], route)
            except Exception:  # noqa: BLE001
                output = state.get("preview")
            tasks.finish(task_id, status="succeeded", result={"output": output, "preview": state.get("preview")})
            usage.record(
                call_id=None,
                provider_id=route.get("provider_id"),
                capability_key=task["capability_key"],
                token_id=None,
                usage={"unit": "images", "total": 1, "source": "estimated"},
            )
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "succeeded"})
            return
        if state["status"] == "failed":
            tasks.finish(
                task_id,
                status="failed",
                error={
                    "code": "UPSTREAM_ERROR",
                    "message": redact(str(state.get("raw_status") or "upstream failed"), max_len=200),
                },
            )
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "failed"})
            return
        if state["status"] == "canceled":
            tasks.finish(task_id, status="canceled")
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "canceled"})
            return
        import time

        if time.time() > deadline:
            tasks.finish(
                task_id,
                status="failed",
                error={"code": "UPSTREAM_TIMEOUT", "message": "异步任务超过轮询时限"},
            )
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "failed", "reason": "timeout"})
            return
        await async_poll.sleep_ms(async_poll.backoff_ms(attempt, poll_conf))


def _frozen_definition(ctx: dict) -> dict:
    """任务创建时冻结的能力版本定义（版本不可变，改版不影响在途任务）。"""
    version_id = ctx.get("capability_version_id")
    if version_id:
        from ...db import db_session
        from ...models import CapabilityVersion

        with db_session() as s:
            row = s.get(CapabilityVersion, version_id)
        if row is not None:
            return dict(row.definition or {})
    # 兼容历史任务（创建时未写版本号）：只能退回到当前发布版本
    return capability_service.lookup(ctx["capability_key"])["definition"]


__all__ = ["handle", "credentials", "Code"]
