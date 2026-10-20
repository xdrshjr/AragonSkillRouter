"""LLM 服务配置 DTO（spec §5.2 / §5.3 / §5.4）。

两条安全纪律：

- `secret` 用 `SecretStr`：`repr()` / 日志 / 异常体里都不会出现明文（INV-3）。
- 本模块**不 import 任何有 I/O 的 service**：只从协议注册表取两个纯数据常量
  （`AUTH_STYLES` / `WIRE_KEYS`，与 `GET /llm/protocols` 同源，避免第二份校验清单），
  这样 DTO 不会反向依赖业务实现，注册表也不需要知道 HTTP 长什么样。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from ..services.llm.protocols import AUTH_STYLES, WIRE_KEYS


class LlmPaths(BaseModel):
    """路径覆盖（只有 `custom` 生效；未给则继承所选 `wire` 的默认值）。"""

    models: str | None = Field(default=None, max_length=200)
    chat: str | None = Field(default=None, max_length=200)


class ProbeTarget(BaseModel):
    """一次探测的目标身份：**已保存凭证** 或 **未保存草稿** 二选一（spec §3.3）。"""

    protocol: str = Field(min_length=1, max_length=24)
    credential_id: str | None = Field(default=None, max_length=64)
    secret: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=512)
    auth_style: str | None = Field(default=None, max_length=16)
    header_name: str | None = Field(default=None, max_length=64)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    paths: LlmPaths | None = None
    wire: str | None = Field(default=None, max_length=24)
    model: str | None = Field(default=None, max_length=200)
    credential_name: str | None = Field(default=None, max_length=128)

    @field_validator("protocol")
    @classmethod
    def _protocol_shape(cls, value: str) -> str:
        """只做形状校验（长度/小写）；**是否支持**由协议注册表判定。

        这样 `protocol=zzz` 会得到 `LLM_PROTOCOL_UNSUPPORTED`(400) 而不是
        笼统的 `VALIDATION_FAILED` —— 前端要靠这个码区分「拼错了」与「不支持」。
        """
        return value.strip().lower()

    @field_validator("auth_style")
    @classmethod
    def _auth_style_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        style = value.strip().lower()
        if style not in AUTH_STYLES:
            raise ValueError(f"auth_style 只能是 {' / '.join(AUTH_STYLES)}")
        return style

    @field_validator("wire")
    @classmethod
    def _wire_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        wire = value.strip().lower()
        if wire not in WIRE_KEYS:
            raise ValueError(f"wire 只能是 {' / '.join(WIRE_KEYS)}")
        return wire

    @field_validator("extra_headers", "query_params")
    @classmethod
    def _header_pairs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("最多 20 个头 / 查询参数")
        for key, item in value.items():
            if not str(key).strip() or len(str(key)) > 64 or len(str(item)) > 1024:
                raise ValueError("头名/参数名不能为空且不超过 64 字符，值不超过 1024 字符")
        return {str(k).strip(): str(v) for k, v in value.items()}

    @model_validator(mode="after")
    def _secret_xor_credential(self) -> ProbeTarget:
        has_secret = bool(self.secret and self.secret.get_secret_value().strip())
        has_credential = bool((self.credential_id or "").strip())
        if has_secret and has_credential:
            raise ValueError("secret 与 credential_id 只能二选一")
        if not has_secret and not has_credential:
            raise ValueError("需要 credential_id 或 secret")
        return self


class TestConnectionRequest(ProbeTarget):
    """`POST /api/admin/llm/test-connection` 的请求体。"""

    #: `auth`（默认，不计费）| `completion` | `both`；语义见 spec §3.4 的 mode 表
    mode: str = Field(default="auth", max_length=16)

    @field_validator("mode")
    @classmethod
    def _mode_known(cls, value: str) -> str:
        mode = (value or "auth").strip().lower()
        if mode not in ("auth", "completion", "both"):
            raise ValueError("mode 只能是 auth / completion / both")
        return mode


class ModelsRequest(ProbeTarget):
    """`POST /api/admin/llm/models` 的请求体。"""

    refresh: bool = False
    include_non_llm: bool = False
    limit: int = Field(default=500, ge=1, le=500)


__all__ = ["LlmPaths", "ModelsRequest", "ProbeTarget", "TestConnectionRequest"]
