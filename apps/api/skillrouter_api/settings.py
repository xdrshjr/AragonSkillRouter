"""配置（§7.1）。全部环境变量前缀 `SKILLROUTER_`。"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SKILLROUTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 基础
    env: str = "development"
    app_name: str = "Skill Router"
    public_base_url: str = "http://localhost:3000"
    db_path: str = "var/skillrouter.db"
    data_dir: str = "var"

    # 主密钥
    master_key: str | None = None
    master_key_previous: str | None = None
    master_key_id: str = "mk1"
    file_signing_secret: str | None = None

    # 初始管理员（仅首次初始化生效）
    admin_user: str = "admin"
    admin_password: str = "adminjr"

    # 会话与登录
    session_ttl_minutes: int = 480
    login_max_failures: int = 5
    login_window_seconds: int = 300
    login_lock_seconds: int = 300

    # Agent 网关
    agent_gateway_url: str = "http://127.0.0.1:8500"
    agent_gateway_secret: str = "dev-gateway-secret-change-me"
    agent_gateway_bind: str = "127.0.0.1"
    agent_model: str | None = None

    # 引擎
    engine_policy: str = "auto"
    engine_channel: str = "stable"
    engine_pin: str = "0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8"
    engine_check_interval_hours: int = 24
    #: 已安装引擎根目录（与网关 `AGENT_ENGINE_HOME_ROOT` 必须同值，§A.3.7-2）
    agent_engine_home_root: str = ""
    #: 构建/验证工具链归属：`gateway`（生产，默认）/ `local`（仅 development）
    engine_toolchain: str = "gateway"
    #: 「取消中」悬挂行的自动收口阈值（分钟，§A.3.6 重启恢复）
    cancel_reconcile_minutes: int = 30

    # 出站 / 抓取
    fetch_allowlist: str = ""
    ssrf_deny: str = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.169.254,::1,fc00::/7"
    max_upload_mb: int = 50
    fetch_timeout_s: int = 20
    egress_policy: str = Field(
        default="deny",
        validation_alias=AliasChoices(
            "SKILLROUTER_EGRESS_POLICY", "SKILLROUTER_TELEMETRY_EGRESS", "egress_policy"
        ),
    )

    # 数据与配额
    retention_days: int = 30
    payload_policy: str = "redacted_preview"
    default_budget_usd: float = 0.0
    worker_concurrency: int = 4
    #: 平台令牌是否允许放进查询串（`?token=`）；默认关闭，`[compat]` 逃生阀（§A.3.3）
    allow_query_token: bool = False

    # CLI / 安装
    install_mode: str = "tarball"  # tarball | npm
    cli_package_version: str = "0.1.0"

    @field_validator("*", mode="before")
    @classmethod
    def _strip_whitespace(cls, v: object) -> object:
        # Windows 的 `set VAR=value && cmd` 会把尾随空格带进环境变量；
        # 空格进入 HTTP 头（共享密钥）会直接变成 "Illegal header value"。
        return v.strip() if isinstance(v, str) else v

    @field_validator("env")
    @classmethod
    def _env_values(cls, v: str) -> str:
        if v not in ("development", "staging", "production"):
            raise ValueError("SKILLROUTER_ENV 必须是 development / staging / production")
        return v

    @field_validator("payload_policy")
    @classmethod
    def _policy(cls, v: str) -> str:
        if v not in ("none", "redacted_preview", "full"):
            raise ValueError("SKILLROUTER_PAYLOAD_POLICY 必须是 none / redacted_preview / full")
        return v

    @field_validator("engine_policy")
    @classmethod
    def _engine_policy(cls, v: str) -> str:
        if v not in ("auto", "notify", "manual"):
            raise ValueError("SKILLROUTER_ENGINE_POLICY 必须是 auto / notify / manual")
        return v

    @field_validator("egress_policy")
    @classmethod
    def _egress(cls, v: str) -> str:
        if v not in ("allow", "deny"):
            raise ValueError("SKILLROUTER_EGRESS_POLICY 必须是 allow / deny")
        return v

    @model_validator(mode="after")
    def _gateway_secret_required_in_production(self) -> Settings:
        """生产环境必须显式配置网关共享密钥。

        内置默认值是公开的，而 `/internal/credentials/materialize` 会返回**明文凭证**；
        带着默认值上生产等于把密钥库交给任何能访问 127.0.0.1:8000 的本地进程（§7.1 / §4.3）。
        """
        if self.env == "production":
            secret = (self.agent_gateway_secret or "").strip()
            if not secret or secret == "dev-gateway-secret-change-me" or len(secret) < 32:
                raise RuntimeError(
                    "[FATAL] GATEWAY_SECRET_MISSING：生产环境必须设置 "
                    "SKILLROUTER_AGENT_GATEWAY_SECRET（≥32 字符，且不能是内置默认值）"
                )
        return self

    # ── 派生路径 ───────────────────────────────────────────────
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    @property
    def db_file(self) -> Path:
        """相对路径按 **当前工作目录** 解析（默认 `var/skillrouter.db` 与 `data_dir=var` 一致）。"""
        p = Path(self.db_path)
        return p.resolve() if not p.is_absolute() else p

    @property
    def files_dir(self) -> Path:
        return self.data_path / "files"

    @property
    def logs_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_path / "backup"

    @property
    def builds_dir(self) -> Path:
        return self.data_path / "engine-builds"

    @property
    def aragon_home(self) -> Path:
        return self.data_path / "aragon-home"

    @property
    def engine_runtime_dir(self) -> Path:
        return self.data_path / "engine-runtime"

    @property
    def engine_home_root(self) -> Path:
        """已安装引擎根目录；`SKILLROUTER_ENGINE_HOME_ROOT` 为空时取 `<data>/aragon-home/engines`。"""
        if self.agent_engine_home_root.strip():
            return Path(self.agent_engine_home_root.strip()).resolve()
        return (self.aragon_home / "engines").resolve()

    @property
    def downloads_dir(self) -> Path:
        return self.data_path / "downloads"

    @property
    def agent_policy_path(self) -> Path:
        return self.data_path / "agent-policy.json"

    @property
    def cookie_secure(self) -> bool:
        return self.env == "production"

    @property
    def db_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_file.as_posix()}"

    @property
    def allowlist(self) -> list[str]:
        return [x.strip() for x in self.fetch_allowlist.split(",") if x.strip()]

    @property
    def ssrf_deny_list(self) -> list[str]:
        return [x.strip() for x in self.ssrf_deny.split(",") if x.strip()]

    @property
    def gateway_pin_version(self) -> str:
        return self.engine_pin.split("@", 1)[0]

    def install_command(self, token_placeholder: str = "<srk_...>") -> str:
        base = self.public_base_url
        if self.install_mode == "npm":
            return (
                f"npx @skillrouter/cli@latest install --base-url {base} "
                f"--token-stdin"
            )
        tarball = f"skillrouter-cli-{self.cli_package_version}.tgz"
        return (
            f"npx ./{tarball} install --base-url {base} --token {token_placeholder}"
        )

    def ensure_dirs(self) -> None:
        for p in (
            self.data_path,
            self.files_dir,
            self.logs_dir,
            self.builds_dir,
            self.aragon_home,
            self.engine_runtime_dir,
            self.engine_home_root,
            self.downloads_dir,
            self.backup_dir,
            self.db_file.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def generate_missing_secrets(self) -> None:
        """主密钥 / 文件签名密钥缺失时生成并 0600 落盘（§7.1）。"""
        self.ensure_dirs()
        self._ensure_secret_file("credentials-master.key", "master_key")
        self._ensure_secret_file("file-signing.key", "file_signing_secret")

    def _ensure_secret_file(self, filename: str, attr: str) -> None:
        import base64
        import os

        current = getattr(self, attr)
        if current:
            return
        path = self.data_path / filename
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
        else:
            value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            path.write_text(value + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - Windows
                pass
        if not value:
            raise RuntimeError(f"[FATAL] MASTER_KEY_MISSING {path} 为空，无法启动")
        object.__setattr__(self, attr, value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.generate_missing_secrets()
    return s


settings = get_settings()
