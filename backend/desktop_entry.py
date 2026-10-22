"""Entry point for the desktop build's bundled backend.

PyInstaller starts this module instead of `uvicorn` on the command line, so the
port and host are chosen by the desktop shell (which passes them in) and the
server is stopped by the same process signals Electron uses.

Usage (inside the frozen binary):
    skillrouter-backend --port 8123 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _prepare_sys_path() -> None:
    """Make `app` importable when frozen and when run from the repo."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))


def main(argv: list[str] | None = None) -> int:
    _prepare_sys_path()
    args_list = list(sys.argv[1:] if argv is None else argv)
    # `skillrouter-backend bootstrap --json` runs first-run setup and exits; the
    # desktop shell calls it before creating a window.
    if args_list and args_list[0] == "bootstrap":
        return desktop_bootstrap(args_list[1:])
    parser = argparse.ArgumentParser(prog="skillrouter-backend", description="Skill Router API (desktop build)")
    parser.add_argument("--host", default=os.environ.get("SR_DESKTOP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SR_DESKTOP_PORT", "8412")))
    parser.add_argument("--log-level", default=os.environ.get("SR_LOG_LEVEL", "info"))
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without Electron (useful for smoke-testing the bundled binary)",
    )
    args = parser.parse_args(argv)

    # The desktop app is a local, single-user deployment: no .env file is shipped,
    # so configuration arrives through the environment the shell sets.
    os.environ.setdefault("SR_ENVIRONMENT", "production")
    os.environ.setdefault("SR_COOKIE_SECURE", "false")  # loopback http only
    os.environ.setdefault("SR_TRUSTED_ORIGINS", "")
    os.environ.setdefault("SR_ARAGON_SIDECAR_TOKEN", "")

    import uvicorn

    from app.main import app

    logging.getLogger("skillrouter").info(
        "desktop backend starting host=%s port=%s frozen=%s", args.host, args.port, getattr(sys, "frozen", False)
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        server_header=False,
        date_header=False,
    )
    return 0


def desktop_bootstrap(argv: list[str] | None = None) -> int:
    """Run first-run setup only (migrations, keyring, admin) and exit.

    Electron calls this before opening the window so a first launch never shows a
    half-initialised UI, and so the admin password can be generated and handed to
    the user exactly once.
    """
    _prepare_sys_path()
    parser = argparse.ArgumentParser(prog="skillrouter-backend bootstrap")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv or [])
    import json

    from app.core.config import get_settings
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    status = bootstrap.get_status()
    payload = {
        "initialized": status.initialized,
        "admin_username": status.admin_username,
        "must_change_password": status.must_change_password,
        "data_dir": str(get_settings().data_dir),
        "database": str(get_settings().db_path),
        "network_allow_fake_ip": get_settings().outbound_allow_fake_ip,
    }
    if args.json:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
