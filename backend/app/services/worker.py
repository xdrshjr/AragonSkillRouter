"""The durable background worker.

A separate process (`python -m app.worker_main`) that drains two things:

  * async capability tasks whose ``poll_after`` is due — one HTTP status probe
    each, mapped onto the task row by `app.services.tasks.poll_task`;
  * the generic ``jobs`` queue (``task.poll``, ``files.cleanup``).

Design notes:

  * Claiming happens inside ``BEGIN IMMEDIATE`` with a lease, so running two
    workers (or restarting one) can never hand the same row to two processes.
    A crashed worker's rows become claimable again when the lease expires.
  * Per-item failures are logged and contained; the loop never dies because one
    provider is unreachable.
  * ``SIGINT``/``SIGTERM`` finish the in-flight batch, then release every lease
    this worker still holds, so the next start picks the work straight up.
  * Unknown job kinds are marked ``failed`` with a readable error instead of
    being retried forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from datetime import timedelta
from typing import Any, Awaitable

from app.core import db
from app.core.config import get_settings
from app.security.redact import redact_text
from app.services import files as file_service
from app.services import tasks as task_service

log = logging.getLogger("skillrouter.worker")

KNOWN_JOB_KINDS = ("task.poll", "task.reconcile", "files.cleanup", "file.cleanup")

# How often (in loop iterations) the worker re-sweeps deadline/lease recovery.
SWEEP_EVERY = 30

_DB_READY = False


class UnknownJobKind(RuntimeError):
    """A job row carries a kind this worker cannot execute (permanent error)."""


def default_owner(prefix: str = "worker") -> str:
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}"


def queue_depth() -> dict[str, int]:
    """Synchronous queue depth for `/api/health/ready` and CLI status output."""
    return task_service.queue_depth_now()


def _ensure_db() -> None:
    global _DB_READY
    if _DB_READY:
        return
    db.init_db()
    _DB_READY = True


# ------------------------------------------------------------------- jobs ----
async def _dispatch_job(kind: str, payload: dict[str, Any]) -> Any:
    if kind in {"task.poll", "task.reconcile"}:
        task_id = payload.get("task_id")
        if not task_id:
            raise ValueError("This job is missing the 'task_id' payload field.")
        return await task_service.poll_task(str(task_id))
    if kind in {"files.cleanup", "file.cleanup"}:
        return file_service.cleanup_expired()
    raise UnknownJobKind(
        f"Unknown job kind '{kind}'. This worker supports: {', '.join(sorted(KNOWN_JOB_KINDS))}."
    )


async def _run_job(owner: str, job: dict[str, Any], semaphore: asyncio.Semaphore, summary: dict[str, Any]) -> None:
    async with semaphore:
        job_id = job["id"]
        kind = job.get("kind") or ""
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(job.get("max_attempts") or 3))
        try:
            await _dispatch_job(kind, job.get("payload") or {})
        except UnknownJobKind as exc:
            log.error("job %s (%s) cannot be executed: %s", job_id, kind, exc)
            await task_service.finish_job(job_id, error=str(exc))
            summary["errors"].append({"kind": "job", "id": job_id, "error": redact_text(str(exc))})
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the loop
            message = redact_text(str(exc)) or exc.__class__.__name__
            log.exception("job %s (%s) failed (attempt %s/%s)", job_id, kind, attempts, max_attempts)
            retry_at = None
            if attempts < max_attempts:
                backoff_s = min(300, 15 * attempts)
                retry_at = db.iso(db.utcnow() + timedelta(seconds=backoff_s))
            await task_service.finish_job(job_id, error=message, retry_at=retry_at)
            summary["errors"].append({"kind": "job", "id": job_id, "error": message})
        else:
            await task_service.finish_job(job_id)
            summary["jobs_processed"] += 1
        finally:
            log.debug("job %s handled by %s", job_id, owner)


# ------------------------------------------------------------------ tasks ----
async def _run_task(owner: str, task: dict[str, Any], semaphore: asyncio.Semaphore, summary: dict[str, Any]) -> None:
    async with semaphore:
        task_id = task["id"]
        try:
            result = await task_service.poll_task(task_id)
            summary["tasks_processed"] += 1
            log.debug("task %s polled -> %s", task_id, (result or {}).get("status"))
        except Exception as exc:  # noqa: BLE001 - one bad task must not stop the loop
            message = redact_text(str(exc)) or exc.__class__.__name__
            log.exception("task %s poll failed", task_id)
            summary["errors"].append({"kind": "task", "id": task_id, "error": message})
        finally:
            await task_service.release_task_lease(task_id, owner)


def _new_summary(owner: str) -> dict[str, Any]:
    return {"owner": owner, "tasks_processed": 0, "jobs_processed": 0, "errors": []}


async def _tick(owner: str, semaphore: asyncio.Semaphore, concurrency: int) -> dict[str, Any]:
    summary = _new_summary(owner)
    task_rows = await task_service.claim_due_tasks(owner, limit=concurrency)
    job_rows = await task_service.claim_jobs(owner, limit=concurrency)
    pending: list[Awaitable[None]] = []
    for row in task_rows:
        pending.append(_run_task(owner, row, semaphore, summary))
    for row in job_rows:
        pending.append(_run_job(owner, row, semaphore, summary))
    if pending:
        await asyncio.gather(*pending)
    return summary


async def run_once(
    owner: str | None = None,
    *,
    concurrency: int | None = None,
    recover: bool = True,
) -> dict[str, Any]:
    """Single-shot pass over due tasks and jobs (tests, cron style runs)."""
    _ensure_db()
    settings = get_settings()
    owner = owner or default_owner("worker-once")
    concurrency = max(1, int(concurrency or settings.worker_concurrency or 1))
    if recover:
        try:
            recovery = await task_service.recover_stale_tasks()
            log.info("startup recovery: %s", recovery)
        except Exception:  # noqa: BLE001
            log.exception("startup recovery failed")
    semaphore = asyncio.Semaphore(concurrency)
    summary = await _tick(owner, semaphore, concurrency)
    summary["queue_depth"] = await task_service.queue_depth()
    return summary


# ------------------------------------------------------------------- loop ----
def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Graceful SIGINT/SIGTERM: first signal stops the loop, second forces exit."""
    loop = asyncio.get_running_loop()
    pressed = {"count": 0}

    def _fallback_handler(signum: int, _frame: Any) -> None:
        pressed["count"] += 1
        stop.set()
        if pressed["count"] > 1:
            # A second signal means "stop now": put the default handler back so the
            # process dies immediately, and unwind the loop via KeyboardInterrupt.
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (ValueError, OSError, RuntimeError):  # pragma: no cover
                pass
            raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
            continue
        except (NotImplementedError, RuntimeError, AttributeError, ValueError):
            # Windows proactor loops do not support add_signal_handler.
            pass
        try:
            signal.signal(sig, _fallback_handler)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover - non main thread
            log.debug("cannot install handler for %s", name)


async def _run_forever(
    owner: str | None = None,
    *,
    concurrency: int | None = None,
    recover: bool = True,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    _ensure_db()
    settings = get_settings()
    owner = owner or default_owner()
    concurrency = max(1, int(concurrency or settings.worker_concurrency or 1))
    interval = max(0.1, float(settings.worker_poll_seconds or 1.0))
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    semaphore = asyncio.Semaphore(concurrency)
    totals = _new_summary(owner)

    if recover:
        try:
            recovery = await task_service.recover_stale_tasks()
            log.info("startup recovery: %s", recovery)
        except Exception:  # noqa: BLE001
            log.exception("startup recovery failed")

    log.info("worker %s started (concurrency=%s, poll=%ss)", owner, concurrency, interval)
    iterations = 0
    try:
        while not stop.is_set():
            iterations += 1
            try:
                summary = await _tick(owner, semaphore, concurrency)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("worker tick failed")
                summary = _new_summary(owner)
            totals["tasks_processed"] += summary["tasks_processed"]
            totals["jobs_processed"] += summary["jobs_processed"]
            totals["errors"].extend(summary["errors"])
            if recover and iterations % SWEEP_EVERY == 0:
                # Periodically expire tasks past their deadline and requeue rows
                # abandoned by a worker that died holding a lease.
                try:
                    await task_service.recover_stale_tasks()
                except Exception:  # noqa: BLE001
                    log.exception("periodic recovery failed")
            worked = summary["tasks_processed"] or summary["jobs_processed"] or summary["errors"]
            if max_iterations is not None and iterations >= max_iterations:
                break
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
    finally:
        released = await task_service.release_owner_leases(owner)
        log.info("worker %s stopped; released leases %s", owner, released)
    return totals


def run_forever(
    owner: str | None = None,
    *,
    concurrency: int | None = None,
    recover: bool = True,
) -> dict[str, Any]:
    """Blocking entrypoint: owns the asyncio loop and handles shutdown."""
    return asyncio.run(_run_forever(owner=owner, concurrency=concurrency, recover=recover))
