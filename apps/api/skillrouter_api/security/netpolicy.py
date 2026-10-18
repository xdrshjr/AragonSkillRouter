"""出站 HTTP 客户端策略（§7.6 / §11.1 的落地细节）。

两个必须显式处理的问题：

1. **系统代理**：Windows 上 `urllib.request.getproxies()` 会读到注册表里的代理，
   httpx 默认 `trust_env=True` 会把**本机/内网**上游也发给代理，表现为 502。
   因此对 loopback/私网/link-local 目标自动 `trust_env=False`（等价于 NO_PROXY），
   其它目标仍尊重部署环境的代理设置（企业网络需要）。
2. **管理员配置的目标 vs 不可信目标**：能力 route 的 `base_url` 是管理员显式配置的
   （可以是本机 Ollama / 内网网关），因此**不做私网拒绝**，只拒绝非 http(s)
   与云元数据地址；而抓取外部资料、导入 SKILL 等「不可信来源」仍走
   `fetch_guard.validate_url()` 的完整 SSRF 校验（含私网拒绝）。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from ..errors import ApiError, Code

METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}
METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


def is_local_host(host: str) -> bool:
    """loopback / 私网 / link-local / 单标签内网名。"""
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified
    # 单标签主机名（如 `ollama`、`gateway`）通常在容器/内网解析
    if "." not in host and ":" not in host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
    return False


def trust_env_for(url: str) -> bool:
    """本地目标不走系统代理；外部目标尊重环境代理。"""
    host = (urlparse(url).hostname or "").lower()
    return not is_local_host(host)


def client_kwargs(url: str, **kwargs) -> dict:  # noqa: ANN003
    return {"trust_env": trust_env_for(url), **kwargs}


def validate_admin_target(url: str) -> None:
    """校验管理员配置的上游地址：只禁非 http(s) 与云元数据（允许私网）。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ApiError(
            Code.SSRF_BLOCKED,
            message=f"上游地址只允许 http/https，当前为：{parsed.scheme or '(空)'}",
            hint="在提供商配置中填写完整 URL",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ApiError(Code.SSRF_BLOCKED, "上游地址缺少主机名")
    if host in METADATA_HOSTS or host in METADATA_IPS:
        raise ApiError(
            Code.SSRF_BLOCKED,
            message="云元数据地址被硬编码拒绝",
            hint="请使用真实的 AI 服务地址",
        )


__all__ = [
    "is_local_host",
    "trust_env_for",
    "client_kwargs",
    "validate_admin_target",
    "METADATA_HOSTS",
]
