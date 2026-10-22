"""Process configuration.

Every value can be supplied by environment variable or by a `.env` file next to
the backend directory. Secrets are NEVER read from the database; the master key
ring lives in a file on the persistent volume (or in the environment for
container deployments).
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import is_frozen, resource_root

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent

# In a frozen desktop build there is no `.env` next to the code: the shell passes
# SR_* through the process environment, and the data directory is per-user.
_ENV_FILES: tuple[Path, ...] = () if is_frozen() else (REPO_DIR / ".env", BACKEND_DIR / ".env")


def _default_data_dir() -> Path:
    if is_frozen():
        # Overridden by SR_DATA_DIR in the desktop shell; this is only a fallback
        # for a frozen binary started by hand.
        return Path.home() / ".skillrouter"
    return REPO_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SR_",
        env_file=_ENV_FILES or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- deployment identity -------------------------------------------------
    app_name: str = "Skill Router"
    environment: str = "development"          # development|staging|production
    public_base_url: str = "http://localhost:3000"
    backend_base_url: str = "http://localhost:8000"

    # ---- storage -------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    database_path: Path | None = None          # defaults to <data_dir>/skillrouter.db
    master_key_file: Path | None = None        # defaults to <data_dir>/keys/master.keyring.json
    master_keys: str | None = None             # "k1:<b64url32>,k2:<b64url32>"
    master_key_active: str | None = None       # key id to encrypt with

    # ---- bootstrap credentials (used once, only if no user exists) -----------
    initial_admin_username: str = "admin"
    initial_admin_password: str = "adminjr"

    # ---- session / cookies ---------------------------------------------------
    session_cookie_name: str = "sr_session"
    csrf_cookie_name: str = "sr_csrf"
    session_ttl_minutes: int = 480
    session_idle_timeout_minutes: int = 120
    cookie_secure: bool | None = None          # None -> auto (True in production)
    cookie_domain: str | None = None
    # Only needed when the browser talks to the API cross-origin (split dev ports).
    trusted_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ---- login throttling ----------------------------------------------------
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    login_lockout_minutes: int = 15

    # ---- outbound network policy for API testing / fetching ------------------
    outbound_allow_private: bool = False
    # Transparent HTTP proxies (Clash/Surge "fake-IP" mode) resolve every host
    # into 198.18.0.0/15. That range is benchmark space, not a path to a private
    # service, but the exemption must be deliberate:
    #   auto  → detect fake-IP mode by resolving a public name and comparing the
    #           answer range; enable the exemption only when detected (default)
    #   allow → always permit the range
    #   deny  → never permit it, even if the machine is behind such a proxy
    outbound_fake_ip_mode: str = "auto"
    outbound_allow_fake_ip: bool | None = None  # explicit override; wins over the mode
    outbound_allowed_hosts: str = ""            # comma list, explicit allowlist
    outbound_timeout_seconds: float = 20.0
    outbound_max_redirects: int = 3
    outbound_max_response_bytes: int = 8 * 1024 * 1024
    outbound_concurrency: int = 8

    # ---- uploads / artifacts -------------------------------------------------
    max_upload_bytes: int = 25 * 1024 * 1024
    file_retention_days: int = 30

    # ---- worker --------------------------------------------------------------
    worker_concurrency: int = 4
    worker_lease_seconds: int = 120
    worker_poll_seconds: float = 1.0
    task_max_attempts: int = 3
    task_default_timeout_seconds: int = 300

    # ---- AragonAgent integration --------------------------------------------
    aragon_sidecar_url: str = "http://127.0.0.1:7317"
    aragon_sidecar_token: str = ""
    aragon_repo_url: str = "https://github.com/xdrshjr/AragonAgent.git"
    aragon_npm_package: str = "@aragon-agent/core"
    aragon_cli_package: str = "@aragon-agent/cli"
    aragon_upgrade_channel: str = "stable"      # stable|branch
    aragon_track_branch: str = "main"
    aragon_aligned_commit: str = "c8b0e3df60319b81b529310e4d4f8d545c70c525"
    aragon_aligned_cli_version: str = "0.6.34"
    aragon_aligned_core_version: str = "0.2.50"
    aragon_upgrade_policy: str = "manual"       # manual|auto
    aragon_workdir: Path | None = None
    aragon_engine_home: Path | None = None

    # ---- misc ----------------------------------------------------------------
    log_level: str = "info"
    require_initialized: bool = True

    # ------------------------------------------------------------------ paths --
    @property
    def db_path(self) -> Path:
        return self.database_path or (self.data_dir / "skillrouter.db")

    @property
    def keyring_path(self) -> Path:
        return self.master_key_file or (self.data_dir / "keys" / "master.keyring.json")

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def engine_dir(self) -> Path:
        return self.data_dir / "engine"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.is_production

    @property
    def allowed_origins(self) -> list[str]:
        raw = self.trusted_origins.replace(" ", "")
        return [o for o in raw.split(",") if o]

    @property
    def allowed_private_hosts(self) -> set[str]:
        raw = self.outbound_allowed_hosts.replace(" ", "")
        return {h.lower() for h in raw.split(",") if h}

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.uploads_dir,
            self.artifacts_dir,
            self.backups_dir,
            self.engine_dir,
            self.keyring_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    """Test helper: drop the memoised settings so env changes are picked up."""
    get_settings.cache_clear()
    try:
        from app.core import db as _db

        _db.close_all_connections()
    except Exception:  # noqa: BLE001 - the db module may still be importing
        pass


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
