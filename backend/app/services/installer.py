"""Where the connection CLI lives, and what the UI may claim about it.

Both `/api/meta` and the connect-command endpoint need this, and neither should have to
import the FastAPI app to get it.
"""

from __future__ import annotations

from typing import Any

from app.core.paths import installer_cli_path, installer_tarball_path


def installer_meta() -> dict[str, Any]:
    """Facts the Overview page needs to print a command that actually runs.

    A guide whose command does not resolve is worse than no guide, so the CLI path is
    resolved on the server (where the file layout is known) and `available` tells the UI
    whether it may show a copy-paste command at all.
    """
    cli = installer_cli_path()
    tarball = installer_tarball_path()
    if cli is None:
        return {
            "available": False,
            "note": (
                "The skill-router-connect CLI is not present on this machine. "
                "It ships with the desktop app and in the repository under packages/installer."
            ),
        }
    return {
        "available": True,
        "cli_path": str(cli),
        "tarball_path": str(tarball) if tarball else None,
        "targets": ["codex-skills", "claude-code", "codex", "generic", "install-self"],
        "note": "Run the CLI with node; the package is distributed locally, not from the public npm registry.",
    }
