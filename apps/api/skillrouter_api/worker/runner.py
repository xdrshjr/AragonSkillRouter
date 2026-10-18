"""worker 领取循环、并发控制、心跳与超时（§7.8）。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from ..db import db_session, db_write, now_iso
from ..errors import ApiError, Code
from ..logging import get_logger, setup_logging
from ..models import WorkerHeartbeat
from ..settings import settings
from . import leases

log = get_logger("skillrouter.run")

IDLE_SLEEP_S = 0.5
HEARTBEAT_S = 20

Handler = Callable[[dict], Awaitable[None]]


class Worker:
    def __init__(self, worker_id: str | None = None, concurrency: int | None = None) -> None:
        self.worker_id = worker_id or f"w-{socket.gethostname()}-{os.getpid()}"
        self.concurrency = concurrency or settings.worker_concurrency
        self._stop = asyncio.Event()
        self._handlers: dict[str, Handler] = {}
        self._running: set[asyncio.Task] = set()
        self._lost_leases: set[str] = set()
        self._started_at = now_iso()

    # ── 注册 ────────────────────────────────────────────────
    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def register_all(self) -> None:
        from .handlers import registry

        for kind, handler in registry().items():
            self.register(kind, handler)

    # ── 生命周期 ────────────────────────────────────────────
    async def startup(self) -> None:
        from ..services import tasks
        from ..sse import broker

        setup_logging(settings.logs_dir)
        settings.ensure_dirs()
        reaped = leases.reap()
        recovered = tasks.recover_on_startup()
        broker.cleanup()
        self._heartbeat(reaped)
        log.info("worker_started", extra={"detail": {"worker_id": self.worker_id, **recovered}})

    def _heartbeat(self, leases_held: int = 0) -> None:
        with db_write() as s:
            row = s.get(WorkerHeartbeat, self.worker_id)
            if row is None:
                s.add(
                    WorkerHeartbeat(
                        worker_id=self.worker_id,
                        started_at=self._started_at,
                        last_seen_at=now_iso(),
                        leases_held=leases_held,
                        engine_version=settings.gateway_pin_version,
                    )
                )
            else:
                row.last_seen_at = now_iso()
                row.leases_held = leases_held

    async def stop(self) -> None:
        self._stop.set()
        if self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    # ── 主循环 ──────────────────────────────────────────────
    async def run_forever(self) -> None:
        await self.startup()
        self.register_all()
        if not self._handlers:
            log.error("worker_no_handlers")
            return
        last_beat = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_beat > HEARTBEAT_S:
                leases.reap()
                self._heartbeat(len(self._running))
                last_beat = time.monotonic()
            free = self.concurrency - len(self._running)
            if free <= 0:
                await asyncio.sleep(0.05)
                continue
            claimed = leases.claim(self.worker_id, list(self._handlers), limit=min(free, 2))
            if not claimed:
                await asyncio.sleep(IDLE_SLEEP_S)
                continue
            for row in claimed:
                task = asyncio.create_task(self._execute(row))
                self._running.add(task)
                task.add_done_callback(self._running.discard)

    async def _execute(self, row) -> None:  # noqa: ANN001
        from .handlers import DEFAULT_TIMEOUT_S

        queue_id = row.id
        handler = self._handlers.get(row.kind)
        if handler is None:
            leases.release(
                queue_id, worker_id=self.worker_id, ok=False, error=f"unknown handler {row.kind}"
            )
            return
        timeout = DEFAULT_TIMEOUT_S.get(row.kind, 600)
        handler_task = asyncio.create_task(handler(row.payload or {}))
        lease_task = asyncio.create_task(self._renew_loop(queue_id, handler_task))
        try:
            result = await asyncio.wait_for(handler_task, timeout=timeout)
            if self._is_terminal(result):
                leases.release(queue_id, worker_id=self.worker_id, ok=True)
            else:
                # handler 自己说「没走完」：不是成功，按 STATE_RACE 重排队（别盲判成功）
                leases.release(
                    queue_id, worker_id=self.worker_id, ok=False, error="STATE_RACE"
                )
        except asyncio.CancelledError:
            lost = queue_id in self._lost_leases
            self._lost_leases.discard(queue_id)
            reason = "LEASE_LOST" if lost else "SHUTDOWN"
            log.warning("handler_cancelled", extra={"object": queue_id, "detail": {"reason": reason}})
            handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handler_task
            # 条件释放：租约可能已被他人重新领取（owner 不匹配时 release 返回 False，队列行不动）
            leases.release(queue_id, worker_id=self.worker_id, ok=False, error=reason)
            # 注意：租约丢失/优雅停机时**不**释放并发额度、不回收调用记录——
            # 任务会在新 owner 或下一轮继续执行，此刻收口会让额度被二次占用（§A.3.6-7）。
            if not lost:
                # 优雅停机：清理完继续向上传播取消
                raise
        except asyncio.TimeoutError:
            log.error("handler_timeout", extra={"object": queue_id, "detail": {"kind": row.kind}})
            leases.release(queue_id, worker_id=self.worker_id, ok=False, error="TIMEOUT")
            self._mark_task_failed(row.payload or {}, "TIMEOUT")
            self._finalize_task(row.payload or {}, "TIMEOUT")
            try:
                from ..sse import broker

                broker.publish("tasks", "task.finished", {"task_id": (row.payload or {}).get("task_id"), "status": "failed", "reason": "TIMEOUT"})
            except Exception:  # noqa: BLE001
                pass
        except ApiError as exc:
            # `INVALID_STATE_TRANSITION` 不再盲判成功：别人已经写过终态不代表本条做完了
            error_code = "STATE_RACE" if exc.code == Code.INVALID_STATE_TRANSITION else exc.code
            if exc.code != Code.INVALID_STATE_TRANSITION:
                log.error("handler_api_error", extra={"object": queue_id, "code": exc.code})
            leases.release(queue_id, worker_id=self.worker_id, ok=False, error=error_code)
            self._mark_task_failed(row.payload or {}, error_code)
            self._finalize_task(row.payload or {}, error_code)
        except Exception as exc:  # noqa: BLE001
            log.exception("handler_error", extra={"object": queue_id, "detail": str(exc)})
            leases.release(queue_id, worker_id=self.worker_id, ok=False, error=str(exc))
            self._mark_task_failed(row.payload or {}, "INTERNAL")
            self._finalize_task(row.payload or {}, "INTERNAL")
        finally:
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task

    @staticmethod
    def _is_terminal(result: object) -> bool:
        """handler 返回约定：`{"terminal": True}` 才算做完；`None` 视为历史 handler 的完成。"""
        if isinstance(result, dict) and "terminal" in result:
            return bool(result["terminal"])
        return True

    async def _renew_loop(self, queue_id: str, handler_task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            if not leases.renew(queue_id, self.worker_id):
                # 租约丢失 = 处理权已转移：立刻停手，别和新 owner 抢同一个任务
                log.error("lease_lost", extra={"object": queue_id})
                self._lost_leases.add(queue_id)
                handler_task.cancel()
                return

    @staticmethod
    def _mark_task_failed(payload: dict, code: str) -> None:
        task_id = payload.get("task_id")
        if not task_id:
            return
        try:
            from ..services import tasks

            current = tasks.get(task_id)
            if current["status"] in ("succeeded", "failed", "canceled"):
                # 别人（或上一轮）已经写了终态：不要用超时/异常覆盖真实结果
                log.warning(
                    "task_failure_skipped",
                    extra={"object": task_id, "detail": {"status": current["status"], "code": code}},
                )
                return
            if current["status"] == "canceling":
                # 用户已请求取消：取消意图优先，不要改写成 failed
                return
            tasks.finish(
                task_id,
                status="failed",
                error={"code": code, "message": f"后台任务失败：{code}"},
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _finalize_task(payload: dict, code: str) -> None:
        """超时/异常/租约丢失后的收口：释放五层并发 + 回写调用记录（§A.3.4 / §A.3.1-2）。

        `tasks.finish(failed)` 只改任务行，不会动并发计数与 `call_logs`，
        这里补上（两个入口都幂等，重复调用返回 0/False，不会把计数减成负数）。
        """
        task_id = payload.get("task_id")
        if not task_id:
            return
        try:
            from ..services import execution

            execution.release_task_quota({"id": task_id})
            execution.finalize_task_call(task_id)
        except Exception as exc:  # noqa: BLE001 - 收口失败不能把 worker 循环打断
            log.warning(
                "task_finalize_failed", extra={"object": task_id, "detail": {"code": code, "error": str(exc)}}
            )

    async def run_once(self, kinds: list[str] | None = None) -> int:
        """单次领取执行（测试与运维手工触发）。"""
        self.register_all()
        claimed = leases.claim(self.worker_id, kinds or list(self._handlers), limit=1)
        for row in claimed:
            await self._execute(row)
        return len(claimed)


async def main() -> None:
    from ..db import db_session
    from ..models import SystemState

    with db_session() as s:
        if s.get(SystemState, "admin_initialized") is None:
            log.error("worker_not_initialized", extra={"detail": "请先运行 python scripts/bootstrap.py"})
            return
    worker = Worker()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
    try:
        await worker.run_forever()
    finally:
        await worker.stop()


__all__ = ["Worker", "main", "leases", "select"]
