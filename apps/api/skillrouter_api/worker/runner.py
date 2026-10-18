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

        handler = self._handlers.get(row.kind)
        if handler is None:
            leases.release(row.id, ok=False, error=f"unknown handler {row.kind}")
            return
        timeout = DEFAULT_TIMEOUT_S.get(row.kind, 600)
        lease_task = asyncio.create_task(self._renew_loop(row.id))
        try:
            await asyncio.wait_for(handler(row.payload or {}), timeout=timeout)
            leases.release(row.id, ok=True)
        except asyncio.TimeoutError:
            log.error("handler_timeout", extra={"object": row.id, "detail": {"kind": row.kind}})
            leases.release(row.id, ok=False, error="TIMEOUT")
            self._mark_task_failed(row.payload or {}, "TIMEOUT")
            try:
                from ..sse import broker

                broker.publish("tasks", "task.finished", {"task_id": (row.payload or {}).get("task_id"), "status": "failed", "reason": "TIMEOUT"})
            except Exception:  # noqa: BLE001
                pass
        except ApiError as exc:
            if exc.code == Code.INVALID_STATE_TRANSITION:
                leases.release(row.id, ok=True)
            else:
                log.error("handler_api_error", extra={"object": row.id, "code": exc.code})
                leases.release(row.id, ok=False, error=exc.message)
                self._mark_task_failed(row.payload or {}, exc.code)
        except Exception as exc:  # noqa: BLE001
            log.exception("handler_error", extra={"object": row.id, "detail": str(exc)})
            leases.release(row.id, ok=False, error=str(exc))
            self._mark_task_failed(row.payload or {}, "INTERNAL")
        finally:
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task

    async def _renew_loop(self, queue_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            if not leases.renew(queue_id, self.worker_id):
                log.error("lease_lost", extra={"object": queue_id})
                return

    @staticmethod
    def _mark_task_failed(payload: dict, code: str) -> None:
        task_id = payload.get("task_id")
        if not task_id:
            return
        try:
            from ..services import tasks

            tasks.finish(
                task_id,
                status="failed",
                error={"code": code, "message": f"后台任务失败：{code}"},
            )
        except Exception:  # noqa: BLE001
            pass

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
