"""认证注入（四种内置方式 + 扩展位，§7.5）。"""

from __future__ import annotations

import base64

SUPPORTED = ("api_key", "bearer", "basic", "header")


def build_auth_headers(material: dict) -> dict[str, str]:
    """material 来自凭证代理：`{auth_type, secret, header_name, meta}`。"""
    auth_type = (material.get("auth_type") or "api_key").lower()
    secret = material.get("secret") or ""
    meta = material.get("meta") or {}
    header_name = material.get("header_name") or meta.get("header_name")
    if auth_type == "api_key":
        name = header_name or "X-API-Key"
        return {name: secret}
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth_type == "basic":
        username = meta.get("username")
        if not username:
            username = secret.split(":", 1)[0] if ":" in secret else ""
            password = secret.split(":", 1)[1] if ":" in secret else secret
        else:
            password = secret
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    if auth_type == "header":
        name = header_name or "X-Api-Key"
        return {name: secret}
    # 扩展位：OAuth / 动态签名由适配器实现（本阶段只声明支持范围）
    raise ValueError(f"不支持的认证方式：{auth_type}（本阶段支持 {', '.join(SUPPORTED)}）")


SENSITIVE_HEADERS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
)


def redact_headers(
    headers: dict[str, str],
    *,
    secret_names: set[str] | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, str]:
    """脱敏后再落库/回显。

    仅靠固定头名白名单会漏掉自定义认证头（`auth_type=header` 可任意命名）与
    调用方自带的 `options.extra_headers`，因此额外按「本次注入的头名」和
    「本次密钥原文」做屏蔽（§7.5 / §11：日志与调用记录不得含真实凭证）。
    """
    names = {name.lower() for name in (secret_names or set())}
    secrets_present = tuple(value for value in secret_values if value)
    out = {}
    for key, value in headers.items():
        lowered = key.lower()
        masked = lowered in SENSITIVE_HEADERS or lowered in names
        if not masked and secrets_present:
            masked = any(secret in value for secret in secrets_present)
        out[key] = "***" if masked else value
    return out
