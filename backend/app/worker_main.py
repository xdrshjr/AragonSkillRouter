"""Worker process entrypoint.

    python -m app.worker_main                 # long-running loop
    python -m app.worker_main --once          # one pass (tests, cron)
    python -m app.worker_main --concurrency 8 # override SR_WORKER_CONCURRENCY

Run it from the backend directory (or with `backend` on PYTHONPATH) so that the
`app` package resolves. It is a separate process from the API on purpose: a
slow provider can never delay an HTTP response, and the API stays stateless.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Sequence

from app.core import db
from app.core.config import get_settings
from app.services import worker

log = logging.getLogger("skillrouter.worker")


def configure_logging(level: str | None = None) -> None:
    settings = get_settings()
    resolved = (level or settings.log_level or "info").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker_main",
        description="Skill Router background worker: async task polling and the durable job queue.",
    )
    parser.add_argument("--once", action="store_true", help="process the currently due work once, then exit")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="maximum concurrent items (default: SR_WORKER_CONCURRENCY)",
    )
    parser.add_argument(
        "--owner",
        default=None,
        help="lease owner label (default: worker:<host>:<pid>)",
    )
    parser.add_argument("--log-level", default=None, help="override SR_LOG_LEVEL for this process")
    parser.add_argument(
        "--no-recover",
        action="store_true",
        help="skip the startup pass that requeues stale tasks",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    concurrency = args.concurrency if args.concurrency and args.concurrency > 0 else None

    try:
        db.init_db()
        from app.services import bootstrap

        # Idempotent: makes sure the keyring exists before a credential is read.
        bootstrap.ensure_bootstrap()
    except Exception:  # noqa: BLE001
        log.exception("worker startup failed during initialisation")
        return 2

    log.info(
        "worker starting (mode=%s, concurrency=%s, db=%s)",
        "once" if args.once else "loop",
        concurrency or get_settings().worker_concurrency,
        get_settings().db_path,
    )
    try:
        if args.once:
            summary = asyncio.run(
                worker.run_once(args.owner, concurrency=concurrency, recover=not args.no_recover)
            )
            log.info(
                "worker once finished: tasks=%s jobs=%s errors=%s queue=%s",
                summary.get("tasks_processed"),
                summary.get("jobs_processed"),
                len(summary.get("errors") or []),
                summary.get("queue_depth"),
            )
        else:
            worker.run_forever(args.owner, concurrency=concurrency, recover=not args.no_recover)
    except KeyboardInterrupt:  # pragma: no cover - operator action
        log.info("worker interrupted")
        return 0
    except Exception:  # noqa: BLE001
        log.exception("worker crashed")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
