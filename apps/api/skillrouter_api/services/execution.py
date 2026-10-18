"""统一执行编排：同步 / 异步 / 流式 + 幂等 + 配额 + 记录（§4.4(c)(d) / §7.6 / §7.8 / §A.3）。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import jsonschema
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..executors.http_executor import invoke_sync, stream_events
from ..logging import get_logger, redact
from ..models import CallLog, CapabilityVersion, IdempotencyKey, PlatformToken, Task
from ..security.ratelimit import check_and_count, check_budget, release_concurrency
from ..security.tokens import TokenContext
from ..settings import settings
from ..sse import broker
from . import capabilities, files, routing, settings_runtime, tasks, usage

log = get_logger("skillrouter.run")

PREVIEW = 2048

#: 执行器状态 → `call_logs.status` 的**唯一**映射（§A.3.1-2 / 不变式 I7）。
#: 任何写 `call_logs.status` 的地方都必须查这张表，禁止手写字符串。
LOGGED_STATUS = {"succeeded": "success", "failed": "failed", "status_unknown": "status_unknown"}

#: `LOGGED_STATUS` 的逆映射（回放落库记录时把 `success` 还原成执行器状态）
_EXECUTOR_STATUS = {logged: status for status, logged in LOGGED_STATUS.items()}

#: 五作用域的固定顺序：token → agent → capability → provider → global（§A.3.4-2）
SCOPE_ORDER = ("token", "agent", "capability", "provider", "global")

IDEM_TTL_HOURS = 24



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


def _idem_expires() -> str:
    """占位有效期：`now + 24h`（沿用历史语义）。"""
    return (
        (datetime.now(timezone.utc) + timedelta(hours=IDEM_TTL_HOURS))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _idem_get(scope: str, key: str) -> dict | None:
    """新开一个只读会话取幂等行快照（**不**复用可能已失效的写会话）。"""
    with db_session() as s:
        row = s.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == scope, IdempotencyKey.idem_key == key
            )
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "fingerprint": row.fingerprint,
            "request_id": row.request_id,
            "capability_key": row.capability_key,
            "state": row.state or "done",
            "expires_at": row.expires_at,
        }


def _idem_insert(
    scope: str, key: str, fingerprint: str, request_id: str, capability_key: str
) -> str:
    """独立事务占位 `state='in_flight'`（唯一索引 `uq_idem_scope_key` 做互斥）。"""
    reservation_id = uuid_pk("idm")
    with db_write() as s:
        s.add(
            IdempotencyKey(
                id=reservation_id,
                scope=scope,
                idem_key=key,
                fingerprint=fingerprint,
                request_id=request_id,
                capability_key=capability_key,
                state="in_flight",
                created_at=now_iso(),
                expires_at=_idem_expires(),
            )
        )
    return reservation_id


def _idem_take_over(record_id: str, request_id: str) -> bool:
    """条件 UPDATE 抢占「过期占位」：影响行数 0 表示别人先抢到了。"""
    now = now_iso()
    with db_write() as s:
        result = s.execute(
            update(IdempotencyKey)
            .where(
                IdempotencyKey.id == record_id,
                IdempotencyKey.state == "in_flight",
                IdempotencyKey.expires_at < now,
            )
            .values(request_id=request_id, created_at=now, expires_at=_idem_expires())
        )
        return (result.rowcount or 0) > 0


def _idem_complete(reservation_id: str | None) -> None:
    """完成态：`UPDATE … SET state='done' WHERE id=? AND state='in_flight'`。

    影响行数 0 ⇒ 本次占位已被判过期并被他人接管：记一条 `idem_reservation_lost`，
    调用结果照常返回，但不覆盖对手的 `request_id`（§A.3.5-3）。
    """
    if not reservation_id:
        return
    with db_write() as s:
        result = s.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.id == reservation_id, IdempotencyKey.state == "in_flight")
            .values(state="done")
        )
        lost = (result.rowcount or 0) == 0
    if lost:
        log.warning(
            "idem_reservation_lost",
            extra={"reservation_id": reservation_id},
        )


def _idem_discard(reservation_id: str | None) -> None:
    """本次请求没真正执行就失败：删掉自己的 `in_flight` 占位，让客户端能用同一键重试。"""
    if not reservation_id:
        return
    with db_write() as s:
        s.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.id == reservation_id, IdempotencyKey.state == "in_flight"
            )
        )


def _replay_body(scope: str, request_id: str) -> dict:
    """按 `request_id` 回放已完成请求的结果（任务优先于调用记录）。"""
    with db_session() as s:
        task = s.scalar(select(Task).where(Task.request_id == request_id))
        if task is not None:
            return {
                "request_id": request_id,
                "task_id": task.id,
                "status": task.status,
                "body": {"request_id": request_id, "task_id": task.id, "status": task.status},
            }
        call = s.scalar(select(CallLog).where(CallLog.request_id == request_id))
        if call is not None:
            preview = call.response_preview
            # 回放体也遵守 §A.5.1 的取值集合：把落库的 success 映射回执行器状态
            executor_status = _EXECUTOR_STATUS.get(call.status, call.status)
            return {
                "request_id": request_id,
                "status": executor_status,
                "body": {
                    "request_id": request_id,
                    "status": executor_status,
                    "status_unknown": executor_status == "status_unknown",
                    "needs_reconcile": bool(call.needs_reconcile),
                    "capability": {"key": call.capability_key},
                    "output": json.loads(preview) if preview and preview.startswith("{") else None,
                    "error": {"code": call.error_code} if call.error_code else None,
                    "idempotent_replay": True,
                },
            }
    return {"request_id": request_id, "body": {"request_id": request_id}}


def _idempotency_reserve(
    scope: str, key: str | None, fingerprint: str, request_id: str, capability_key: str
) -> dict:
    """幂等三态占位（§A.3.5 / 不变式 I1、I3）。

    返回 `{"replayed": bool, "reservation_id": str|None, "body": dict|None}`：
    - `replayed=True` → 命中 `state='done'`，直接回放；
    - `replayed=False` → 本次请求拿到执行权，`reservation_id` 用于完成态的条件更新。

    命中 `in_flight` 且未过期 → 409 `IDEMPOTENCY_IN_PROGRESS`；
    命中 `in_flight` 但已过期 → 条件 UPDATE 抢占，抢不到就按「已有记录」处理。
    """
    if not key:
        return {"replayed": False, "reservation_id": None, "body": None}
    record = _idem_get(scope, key)
    if record is None:
        try:
            # 占位用独立事务：IntegrityError 之后会话已失效，必须退出 with 再重查
            reservation_id = _idem_insert(scope, key, fingerprint, request_id, capability_key)
            return {"replayed": False, "reservation_id": reservation_id, "body": None}
        except IntegrityError:
            # 并发对手赢了唯一索引：新开会话重查，走「已有记录」分支
            record = _idem_get(scope, key)
            if record is None:  # pragma: no cover - 对手刚回滚
                reservation_id = _idem_insert(scope, key, fingerprint, request_id, capability_key)
                return {"replayed": False, "reservation_id": reservation_id, "body": None}
    for _ in range(2):
        if record["fingerprint"] != fingerprint:
            raise ApiError(
                Code.IDEMPOTENCY_KEY_REUSED,
                message="同一幂等键对应了不同的请求体",
                hint="使用新的幂等键",
                details={"idempotency_key": key},
            )
        if record["state"] != "in_flight":
            return {"replayed": True, "reservation_id": None, "body": _replay_body(scope, record["request_id"])}
        if not record["expires_at"] or record["expires_at"] >= now_iso():
            raise ApiError(
                Code.IDEMPOTENCY_IN_PROGRESS,
                message="同一幂等键的请求正在执行中",
                hint="等待首个请求完成，或换用新的 Idempotency-Key",
                details={"idempotency_key": key, "state": "in_flight"},
            )
        # 陈旧占位：条件 UPDATE 抢占后继续执行
        if _idem_take_over(record["id"], request_id):
            return {"replayed": False, "reservation_id": record["id"], "body": None}
        fresh = _idem_get(scope, key)
        if fresh is None:  # pragma: no cover - 抢占失败且行被删除
            reservation_id = _idem_insert(scope, key, fingerprint, request_id, capability_key)
            return {"replayed": False, "reservation_id": reservation_id, "body": None}
        record = fresh
    raise ApiError(
        Code.IDEMPOTENCY_IN_PROGRESS,
        message="同一幂等键的请求正在执行中",
        hint="等待首个请求完成，或换用新的 Idempotency-Key",
        details={"idempotency_key": key, "state": "in_flight"},
    )


def _quota_gate(*, token_ctx: TokenContext | None, definition: dict, provider_id: str | None) -> list[dict]:
    """令牌 → Agent → 能力 → 提供商 → 全局，任意一层超限即拒绝（§A.3.4-2）。

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
            if token_ctx.agent_id:
                agent_entry = {"scope_type": "agent", "scope_id": token_ctx.agent_id}
                check_and_count(**agent_entry)
                scopes.append(agent_entry)
                check_budget(scope_type="agent", scope_id=token_ctx.agent_id, max_cost_usd=None)
        for scope_type, scope_id in (
            ("capability", definition.get("key", "")),
            ("provider", provider_id or "*"),
            ("global", "*"),
        ):
            entry = {"scope_type": scope_type, "scope_id": scope_id}
            check_and_count(**entry)
            scopes.append(entry)
            check_budget(scope_type=scope_type, scope_id=scope_id, max_cost_usd=None)
    except BaseException:
        _release(scopes)
        raise
    return scopes


def _release(scopes: list[dict]) -> int:
    """逐层归还并发名额（幂等）；返回实际归还的层数。"""
    released = 0
    for entry in scopes:
        if release_concurrency(scope_type=entry["scope_type"], scope_id=entry["scope_id"]):
            released += 1
    return released


def _route_unit_price(selection: Any) -> float | None:
    """route 显式单价（定价阶梯第一级，§A.3.4-3）；没有就是 `None`，交给价目表/默认价兜底。"""
    chosen = getattr(selection, "chosen", None) or {}
    raw = (chosen.get("price") or {}).get("unit_price_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def task_scopes(task_row: Any) -> list[dict]:
    """按任务行**确定性**重算 5 层 scope（异步终态释放用，§A.3.4 / T-46）。

    入参可以是 `Task` ORM 行，也可以是含同样字段的 dict：
    只用任务落库时就冻结的值（`token_id` / `capability_key` / 提交时记录的 provider），
    不重新选路，保证「释放的作用域」与「提交时占用的作用域」完全一致。
    """
    task_id = _row_field(task_row, "id") or _row_field(task_row, "task_id")
    token_id = _row_field(task_row, "token_id")
    capability_key = _row_field(task_row, "capability_key")
    agent_id = None
    provider_id = None
    if token_id:
        with db_session() as s:
            token = s.get(PlatformToken, token_id)
            agent_id = token.agent_id if token is not None else None
    if task_id or token_id or capability_key:
        with db_session() as s:
            call = None
            if task_id:
                call = s.scalar(select(CallLog).where(CallLog.task_id == task_id))
            if call is None and token_id and capability_key:
                call = s.scalar(
                    select(CallLog)
                    .where(CallLog.capability_key == capability_key, CallLog.caller_id == token_id)
                    .order_by(CallLog.created_at.desc())
                )
            provider_id = call.provider_id if call is not None else None
    scope_ids = {
        "token": token_id,
        "agent": agent_id,
        "capability": capability_key,
        "provider": provider_id or "*",
        "global": "*",
    }
    return [
        {"scope_type": scope_type, "scope_id": scope_ids[scope_type]}
        for scope_type in SCOPE_ORDER
        if scope_ids[scope_type]
    ]


def _row_field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def finalize_task_call(task_id: str) -> bool:
    """把异步提交时写入的 `call_logs.status='queued'` 行推进到终态（§A.3.1-2 / I7）。

    异步调用的调用记录在 `submit_async` 时就落库（`queued`），终态必须由 worker 回写：
    否则 `/calls` 永远显示「排队中」，且成功率聚合的分母里塞着一个永不成败的行。
    映射仍走 `LOGGED_STATUS`；`canceled` 在 `call_logs` 里没有独立取值，按 `failed` 记
    （`error_code='CANCELED'`），语义是「本次调用没有成功」。
    """
    if not task_id:
        return False
    with db_write() as s:
        task = s.get(Task, task_id)
        if task is None:
            return False
        call = s.scalar(select(CallLog).where(CallLog.task_id == task_id))
        if call is None:
            return False
        logged = LOGGED_STATUS.get(str(task.status), None)
        if logged is None:
            if str(task.status) != "canceled":
                # 尚未终态：留给下一次终态回写，不要提前把行写死
                return False
            logged = LOGGED_STATUS["failed"]
        if call.status == logged and call.needs_reconcile == (1 if logged == "status_unknown" else 0):
            return False
        call.status = logged
        call.needs_reconcile = 1 if logged == "status_unknown" else 0
        error = task.error or {}
        if isinstance(error, dict) and error.get("code"):
            call.error_code = str(error["code"])
        elif str(task.status) == "canceled":
            call.error_code = "CANCELED"
        call.http_status = call.http_status or (200 if logged == "success" else None)
        task_updated = task.updated_at
        if task_updated and call.created_at:
            # 端到端耗时（含排队）：`latency_ms` 在异步场景里没有同步测量的机会
            call.latency_ms = _elapsed_ms(call.created_at, task_updated)
        return True


def _elapsed_ms(start_iso: str, end_iso: str) -> int | None:
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def release_task_quota(task_row: Any) -> int:
    """异步任务终态释放 5 层并发名额（幂等，§A.3.4 / T-46）。

    - 幂等标记写在 `Task.result["quota_released"]`，先落库再释放：
      重复调用返回 `0`，且不会把任何计数减成负数；
    - 返回本次实际归还的层数（首次调用）。
    """
    task_id = _row_field(task_row, "id") or _row_field(task_row, "task_id")
    if not task_id:
        return 0
    snapshot: dict = {}
    with db_write() as s:
        row = s.get(Task, task_id)
        if row is None:
            return 0
        result = dict(row.result or {})
        if result.get("quota_released"):
            return 0
        snapshot = {
            "id": row.id,
            "token_id": row.token_id,
            "capability_key": row.capability_key,
        }
        result["quota_released"] = True
        result["quota_released_at"] = now_iso()
        # JSON 列必须整体赋值，原地修改不会被检测为 dirty
        row.result = result
        row.updated_at = now_iso()
    # 标记落库之后再释放：即便释放中途异常，重复调用也只会返回 0，不会二次扣减
    return _release(task_scopes(snapshot))


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
    needs_reconcile: int = 0,
) -> str:
    call_id = uuid_pk("cal")
    # 设置页改完立即生效（§A.3.9）：直接读 env 快照会让「载荷策略」永远停在启动值
    policy = settings_runtime.get("payload_policy", settings.payload_policy)
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
                needs_reconcile=int(needs_reconcile or 0),
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


def _needs_reconcile(result: Any, timings: dict | None = None) -> int:
    """是否需要人工对账（§A.3.5 / 不变式 I6）：`status_unknown` 或执行器显式打了标记。"""
    marker = (result.error or {}).get("needs_reconcile") or (result.timings or {}).get("needs_reconcile")
    if timings:
        marker = marker or timings.get("needs_reconcile")
    return 1 if (marker or result.status == "status_unknown") else 0


def _signed_output_files(items: list | None) -> list[dict]:
    """给 `output_files[]` 每项补 `signed_url` / `expires_at`（签名只走 `services.files.signed_url`）。"""
    decorated: list[dict] = []
    for item in items or []:
        entry = dict(item) if isinstance(item, dict) else {"file_id": item}
        file_id = entry.get("file_id") or entry.get("id")
        if file_id:
            try:
                signed = files.signed_url(str(file_id), entry.get("owner_id"))
            except Exception:  # noqa: BLE001 - 签名失败不能影响调用结果
                signed = None
            if signed:
                entry["signed_url"] = signed["url"]
                entry["expires_at"] = signed["expires_at"]
        decorated.append(entry)
    return decorated


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
    reservation = _idempotency_reserve(scope, idem_key, fingerprint, request_id, capability_key)
    if reservation["replayed"]:
        return reservation["body"]
    reservation_id = reservation["reservation_id"]

    if mode == "async":
        try:
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
                reservation_id=reservation_id,
            )
        except BaseException:
            _idem_discard(reservation_id)
            raise
    try:
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
    except BaseException:
        # 还没真正触达上游就失败：丢弃占位，客户端可用同一幂等键重试（§A.3.5）
        _idem_discard(reservation_id)
        raise
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
    except BaseException:
        _idem_discard(reservation_id)
        raise
    finally:
        _release(scopes)

    total_ms = int((time.perf_counter() - started) * 1000)
    timings = {**(result.timings or {}), "total_ms": total_ms, "failover_switches": switches}
    needs_reconcile = _needs_reconcile(result, timings)
    call_id = _log_call(
        request_id=request_id,
        caller_type=caller_type,
        caller_id=caller_id,
        capability_key=capability_key,
        version_id=capability.get("version_id"),
        provider_id=(selection.chosen or {}).get("provider_id"),
        credential_id=(selection.chosen or {}).get("credential_id"),
        mode=mode,
        status=LOGGED_STATUS.get(result.status, LOGGED_STATUS["failed"]),
        needs_reconcile=needs_reconcile,
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
    # 记账：写 usage_records + 推进五层成本（token/agent/capability/provider/global）
    usage_cost = usage.account(
        call_id=call_id,
        provider_id=(selection.chosen or {}).get("provider_id"),
        capability_key=capability_key,
        token_id=token_ctx.token_id if token_ctx else None,
        agent_id=token_ctx.agent_id if token_ctx else None,
        unit=(result.usage or {}).get("unit") or "tokens",
        qty=(result.usage or {}).get("total") or (result.usage or {}).get("qty") or 0,
        source=(result.usage or {}).get("source") or "estimated",
        unit_price_usd=_route_unit_price(selection),
    )
    _idem_complete(reservation_id)
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
        "price_source": usage_cost.get("price_source"),
    }
    # §A.5.1：`status` 取值集合固定为 succeeded | failed | status_unknown，并给出两个布尔位
    body["status"] = result.status
    body["status_unknown"] = result.status == "status_unknown"
    body["needs_reconcile"] = bool(needs_reconcile)
    body["output_files"] = _signed_output_files(body.get("output_files"))
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
    reservation_id: str | None = None,
) -> dict:
    selection, _material, _base_url = routing.resolve(
        capability_id=capability["id"],
        definition=definition,
        mode="async",
        options=options,
        token_ctx=token_ctx,
        tags=capability.get("tags"),
    )
    # 异步提交：并发名额在这里 +1，但**不释放**（提交侧不得释放，§A.3.4）。
    # 释放责任交给任务终态（`release_task_quota`）；提交本身失败时才立刻归还，避免泄漏。
    scopes = _quota_gate(
        token_ctx=token_ctx, definition=definition, provider_id=(selection.chosen or {}).get("provider_id")
    )
    try:
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
    except BaseException:
        _release(scopes)
        raise
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
    _idem_complete(reservation_id)
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
    needs_reconcile = 0
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
                status = "succeeded"
                yield {"type": "call.done", "request_id": request_id, "timings": event.get("timings")}
            elif event["type"] == "error":
                error_code = (event.get("error") or {}).get("code")
                if event.get("needs_reconcile") or event.get("status_unknown"):
                    status = "status_unknown"
                    needs_reconcile = 1
                yield {
                    "type": "call.error",
                    "request_id": request_id,
                    "error": event.get("error"),
                    "needs_reconcile": bool(event.get("needs_reconcile")),
                }
    finally:
        # 异常路径也必须释放并发名额并记账（§A.3.4）
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
            status=LOGGED_STATUS.get(status, LOGGED_STATUS["failed"]),
            needs_reconcile=needs_reconcile,
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
        # 流尾 usage；上游没给就记 qty=0 / source='unknown'，保证「有调用就有账」
        usage.account(
            call_id=call_id,
            provider_id=(selection.chosen or {}).get("provider_id"),
            capability_key=capability_key,
            token_id=token_ctx.token_id if token_ctx else None,
            agent_id=token_ctx.agent_id if token_ctx else None,
            unit=usage_final.get("unit") or "tokens",
            qty=usage_final.get("total") or usage_final.get("qty") or 0,
            source=usage_final.get("source") or "unknown",
            unit_price_usd=_route_unit_price(selection),
        )
        broker.publish(
            "calls",
            "call.finished",
            {
                "request_id": request_id,
                "status": LOGGED_STATUS.get(status, LOGGED_STATUS["failed"]),
                "streamed": produced,
            },
        )


__all__ = [
    "LOGGED_STATUS",
    "SCOPE_ORDER",
    "invoke",
    "submit_async",
    "stream",
    "finalize_task_call",
    "release_task_quota",
    "task_scopes",
    "Any",
]
