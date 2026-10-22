"""Where files live, in a source checkout and inside a frozen desktop build.

PyInstaller unpacks bundled data files to a temporary directory and exposes it as
``sys._MEIPASS``; ``__file__`` then points inside that directory, so the layout
the source tree relies on (``backend/app/core/db.py`` → ``backend/migrations``)
is no longer two levels up from the module. Every path that must work in both
modes goes through this module instead of being derived ad hoc.

Rules encoded here:
  * read-only resources (migrations, bundled sidecar) → ``resource_root()``
  * user data (database, keyring, uploads, artifacts, logs) → the configured
    ``SR_DATA_DIR``, which the desktop shell sets to the per-user app data dir
  * nothing writes next to the executable (that directory may be read-only, and
    an installer upgrade replaces it)
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True inside a PyInstaller/Electron-bundled Python process."""
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path:
    """Directory holding bundled read-only resources."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # one-folder builds keep data next to the executable
    return Path(sys.executable).resolve().parent


def resource_root() -> Path:
    """Root that contains ``migrations/`` and other bundled resources."""
    if is_frozen():
        return _bundle_dir()
    # backend/app/core/paths.py → backend/
    return Path(__file__).resolve().parents[2]


def source_root() -> Path:
    """Repository root in a checkout; the resource root when frozen."""
    if is_frozen():
        return _bundle_dir()
    return Path(__file__).resolve().parents[3]


def migrations_dir() -> Path:
    return resource_root() / "migrations"


def bundled_sidecar_dir() -> Path | None:
    """The sidecar sources shipped with a desktop build, when present."""
    if not is_frozen():
        return None
    candidate = _bundle_dir() / "sidecar"
    return candidate if candidate.exists() else None


def installer_cli_path() -> Path | None:
    """`skill-router-connect` — the CLI other agents use to connect.

    The Overview page prints a command that people copy and run, so the command has
    to point at a file that actually exists on their machine. Three layouts are tried,
    in order of how good they look in a printed command:

      * a desktop build keeps the CLI beside its other runtimes
        (``resources/runtime/installer`` - visible, next to ``backend`` and ``sidecar``)
      * PyInstaller's bundle directory (``_MEIPASS/installer``), the same files behind a
        hidden folder name
      * a source checkout (``packages/installer``)

    Returning None means "no local installer here" (e.g. a Docker API container), and
    the caller must say so instead of printing a path that does not resolve.
    """
    candidates = []
    if is_frozen():
        # <runtime>/backend/skillrouter-backend.exe -> <runtime>/installer/bin/...
        candidates.append(
            Path(sys.executable).resolve().parent.parent / "installer" / "bin" / "skill-router-connect.js"
        )
    candidates.extend(
        [
            resource_root() / "installer" / "bin" / "skill-router-connect.js",
            source_root() / "packages" / "installer" / "bin" / "skill-router-connect.js",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def installer_tarball_path() -> Path | None:
    """The packaged npm tarball, when one was built next to the CLI."""
    cli = installer_cli_path()
    if cli is None:
        return None
    package_dir = cli.parents[1]
    tarballs = sorted(package_dir.glob("skill-router-connect-*.tgz"))
    return tarballs[-1] if tarballs else None
