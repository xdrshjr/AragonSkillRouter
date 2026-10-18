"""异步能力调用：上游提交 + 轮询 + 结果落库 + 恢复 + 取消竞态（§4.4(d) / §A.3.6）。"""

from __future__ import annotations

from ...db import db_session
from ...errors import ApiError, Code
from ...executors import async_poll
from ...logging import get_logger, redact
from ...models import Task
from ...services import capabilities as capability_service
from ...services import credentials, execution, routing, tasks, usage
from ...sse import broker
from .. import leases  # noqa: F401  (保持包结构导入一致性)

log = get_logger("skillrouter.run")

#: 终态：handler 看到这些状态直接收工（别人已经收敛过了）
TERMINAL = frozenset({"succeeded", "failed", "canceled"})


async def handle(payload: dict) -> dict:
    """返回 `{"terminal": bool}`：runner 只在 terminal=True 时把队列项判成功（§A.3.6-5）。"""
    task_id = payload.get("task_id")
    if not task_id:
        return {"terminal": True}
    task = tasks.get(task_id)
    if task["status"] in TERMINAL:
        return {"terminal": True}
    cancel_only = bool(payload.get("cancel_only"))
    ctx = tasks.runtime_context(task_id)
    definition = _frozen_definition(ctx)
    snapshot = ctx.get("input_snapshot") or {}
    input_payload = snapshot.get("input") or {}

    route, material, base_url = routing.resolve_pinned(
        definition=definition, route_id=ctx.get("route_id")
    )
    kind = str(definition.get("kind") or route.get("kind") or "")

    if not ctx.get("upstream_task_id") and not cancel_only:
        # 只有 queued 的任务可以被认领提交：重复入队 / 多 worker 抢到同一任务时，
        # 后到者直接退出，避免向上游重复下单（§7.8）。
        if not tasks.claim_submission(task_id):
            return _claim_lost(task_id, route, kind)
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
            # 先读状态：用户可能已经在提交途中请求取消（canceling），此时不能再写 failed
            if _status_of(task_id) == "canceling":
                tasks.mark_canceled(task_id, upstream_ok=False)
                broker.publish(
                    "tasks",
                    "task.updated",
                    {"task_id": task_id, "status": "canceled", "reason": exc.code},
                )
                _finalize(task_id, route, kind, None)
                return {"terminal": True}
            tasks.finish(task_id, status="failed", error={"code": exc.code, "message": exc.message})
            broker.publish(
                "tasks", "task.finished", {"task_id": task_id, "status": "failed", "error": exc.code}
            )
            _finalize(task_id, route, kind, None)
            return {"terminal": True}
        state = tasks.mark_running(task_id, upstream_task_id=submitted["upstream_task_id"])
        if state.get("cancel_pending"):
            # 提交途中用户已请求取消：立刻追取消，不进轮询循环
            canceled = await _cancel_upstream(
                route, material, base_url, submitted["upstream_task_id"], task_id=task_id
            )
            tasks.mark_canceled(task_id, upstream_ok=canceled)
            broker.publish(
                "tasks",
                "task.updated",
                {
                    "task_id": task_id,
                    "status": "canceled",
                    "upstream_task_id": submitted["upstream_task_id"],
                    "cancel_scope": "remote" if canceled else "local_only",
                },
            )
            _finalize(task_id, route, kind, None)
            return {"terminal": True}
        broker.publish(
            "tasks",
            "task.updated",
            {"task_id": task_id, "status": "running", "upstream_task_id": submitted["upstream_task_id"]},
        )
    elif ctx.get("upstream_task_id"):
        current = _status_of(task_id)
        if cancel_only or current == "canceling":
            canceled = await _cancel_upstream(route, material, base_url, ctx["upstream_task_id"], task_id=task_id)
            tasks.mark_canceled(task_id, upstream_ok=canceled)
            broker.publish(
                "tasks",
                "task.updated",
                {
                    "task_id": task_id,
                    "status": "canceled",
                    "cancel_scope": "remote" if canceled else "local_only",
                },
            )
            _finalize(task_id, route, kind, None)
            return {"terminal": True}
        tasks.mark_running(task_id, upstream_task_id=ctx["upstream_task_id"])
    else:
        # cancel_only 但从未提交上游：没有远端任务可追，本地直接收敛
        tasks.mark_canceled(task_id, upstream_ok=False)
        broker.publish("tasks", "task.updated", {"task_id": task_id, "status": "canceled"})
        _finalize(task_id, route, kind, None)
        return {"terminal": True}

    poll_conf = (route.get("async") or {}).get("poll") or {}
    deadline = async_poll.deadline(poll_conf)
    attempt = 0
    while True:
        current = tasks.get(task_id)
        if current["status"] in ("canceled", "canceling"):
            if current["status"] == "canceling":
                canceled = await _cancel_upstream(
                    route, material, base_url, current["upstream_task_id"] or "", task_id=task_id
                )
                tasks.mark_canceled(task_id, upstream_ok=canceled)
            # 两条路径都是终态：必须收口记账/并发/调用记录，否则名额与「排队中」永久悬挂
            _finalize(task_id, route, kind, None)
            return {"terminal": True}
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
            _finalize(task_id, route, kind, None)
            return {"terminal": True}
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
            _finalize(task_id, route, kind, _poll_usage(state, route))
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "succeeded"})
            return {"terminal": True}
        if state["status"] == "failed":
            tasks.finish(
                task_id,
                status="failed",
                error={
                    "code": "UPSTREAM_ERROR",
                    "message": redact(str(state.get("raw_status") or "upstream failed"), max_len=200),
                },
            )
            _finalize(task_id, route, kind, None)
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "failed"})
            return {"terminal": True}
        if state["status"] == "canceled":
            tasks.finish(task_id, status="canceled")
            _finalize(task_id, route, kind, None)
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "canceled"})
            return {"terminal": True}
        import time

        if time.time() > deadline:
            tasks.finish(
                task_id,
                status="failed",
                error={"code": "UPSTREAM_TIMEOUT", "message": "异步任务超过轮询时限"},
            )
            _finalize(task_id, route, kind, None)
            broker.publish("tasks", "task.finished", {"task_id": task_id, "status": "failed", "reason": "timeout"})
            return {"terminal": True}
        await async_poll.sleep_ms(async_poll.backoff_ms(attempt, poll_conf))


def _status_of(task_id: str) -> str:
    try:
        return str(tasks.get(task_id)["status"])
    except ApiError:
        return ""


def _claim_lost(task_id: str, route: dict, kind: str) -> dict:
    """认领提交失败：可能是别人正在提交，也可能用户已取消且从未提交。

    `attempts == 0` 说明没有任何执行者认领过提交 —— 上游肯定没被调用，
    可以直接把「提交前取消」收敛成 `canceled`，不必等 30 分钟对账窗口。
    """
    current = tasks.get(task_id)
    if current["status"] == "canceling" and int(current.get("attempts") or 0) == 0:
        tasks.mark_canceled(task_id, upstream_ok=False)
        broker.publish("tasks", "task.updated", {"task_id": task_id, "status": "canceled"})
        # 提交前取消：既没提交上游也没跑轮询，这里就是它的终态，必须收口
        _finalize(task_id, route, kind, None)
    return {"terminal": True}


async def _cancel_upstream(
    route: dict, material: dict, base_url: str, upstream_task_id: str, *, task_id: str | None = None
) -> bool:
    """尽力追取消：失败不抛（用户已经收到「已请求取消」），由 `mark_canceled(local_only)` 记录。"""
    if not upstream_task_id:
        return False
    try:
        canceled = await async_poll.cancel(
            route=route, material=material, base_url=base_url, upstream_task_id=upstream_task_id
        )
        return bool(canceled.get("ok"))
    except ApiError as exc:
        broker.publish(
            "tasks",
            "task.log",
            {"task_id": task_id, "line": f"取消未获上游确认：{exc.code}"},
        )
        return False


def _poll_usage(state: dict, route: dict) -> dict | None:
    """轮询结果里的 usage（`response_map.usage` 解析过就带上，否则为 None）。"""
    from ...executors.parsers import parse_usage

    try:
        parsed = parse_usage(route.get("response_map") or {}, state.get("body"))
    except Exception:  # noqa: BLE001
        return None
    return parsed or None


def _default_unit(kind: str) -> str:
    key = (kind or "").lower()
    if key.startswith("image"):
        return "images"
    if key.startswith(("video", "audio", "asr")):
        return "seconds"
    return "tokens"


def _finalize(
    task_id: str, route: dict, kind: str, usage_info: dict | None, token_id: str | None = None
) -> None:
    """终态记账 + 并发配额释放 + 调用记录回写（§A.3.6 末条 / §A.3.1-2 / T-46）。

    `usage.account`、`execution.release_task_quota`、`execution.finalize_task_call` 是唯一入口，
    这里只做调用与单位兜底，不在 worker 里自己拼 scope。三条都是幂等的，可重复调用。
    """
    provider_id = route.get("provider_id")
    unit = (usage_info or {}).get("unit") or _default_unit(kind)
    qty = float((usage_info or {}).get("total") or (usage_info or {}).get("qty") or 0)
    source = (usage_info or {}).get("source") or ("provider" if qty else "estimated")
    row = _task_row(task_id)
    capability_key = getattr(row, "capability_key", None)
    token_id = token_id or getattr(row, "token_id", None)
    account = getattr(usage, "account", None)
    explicit_price = (route.get("price") or {}).get("unit_price_usd")
    try:
        if account is not None:
            account(
                call_id=None,
                provider_id=provider_id,
                capability_key=capability_key,
                token_id=token_id,
                agent_id=None,
                unit=unit,
                qty=qty,
                source=source,
                unit_price_usd=float(explicit_price) if explicit_price is not None else None,
            )
        else:  # 兼容：并行节点尚未落地单点写入口时退回旧函数（口径一致）
            usage.record(
                call_id=None,
                provider_id=provider_id,
                capability_key=capability_key,
                token_id=token_id,
                usage={"unit": unit, "total": qty, "source": source},
            )
    except Exception as exc:  # noqa: BLE001 - 记账失败不能把已终态的任务再拖回失败
        log.warning("usage_account_failed", extra={"object": task_id, "detail": str(exc)})
    release = getattr(execution, "release_task_quota", None)
    finalize_call = getattr(execution, "finalize_task_call", None)
    if finalize_call is not None:
        try:
            finalize_call(task_id)
        except Exception as exc:  # noqa: BLE001 - 回写失败不能把已终态的任务再拖回失败
            log.warning("call_log_finalize_failed", extra={"object": task_id, "detail": str(exc)})
    if release is None or row is None:
        return
    try:
        release(row)
    except Exception as exc:  # noqa: BLE001
        log.warning("quota_release_failed", extra={"object": task_id, "detail": str(exc)})


def _task_row(task_id: str):  # noqa: ANN202
    """取出脱离会话的任务行（供 `release_task_quota(task_row)` 使用）。"""
    try:
        with db_session() as s:
            row = s.get(Task, task_id)
            if row is not None:
                s.expunge(row)
            return row
    except Exception:  # noqa: BLE001
        return None


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


__all__ = ["handle", "credentials", "Code", "TERMINAL"]
