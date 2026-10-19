"""出站/抓取安全：SSRF 防护（§11.1 / D16 / §A.3.9）。

白名单与附加黑名单**从设置表运行时读取**（`services/settings_runtime.py`，60s TTL）：
设置页改完 `fetch_allowlist` / `ssrf_deny_extra` 后，同一进程的下一次请求即生效，
不再需要重启；跨进程（worker）最长 60s。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..errors import ApiError, Code
from ..logging import redact
from ..services import settings_runtime
from ..settings import settings

ALLOWED_SCHEMES = {"http", "https"}
METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}
MAX_REDIRECTS = 3


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str
    bytes: int
    truncated: bool


def allowlist() -> list[str]:
    """运行时白名单；DB/env 都为空时回落到部署配置（保持未配置时的既有行为）。"""
    entries = settings_runtime.allowlist()
    return entries or list(settings.allowlist)


def deny_list() -> list[str]:
    """黑名单 = 部署基线 `ssrf_deny` + 设置页追加的 `ssrf_deny_extra`。"""
    baseline = list(settings.ssrf_deny_list)
    extra = [item for item in settings_runtime.deny_extra() if item not in baseline]
    return baseline + extra


def _is_denied_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    for raw in deny_list():
        try:
            if "/" in raw and addr in ipaddress.ip_network(raw, strict=False):
                return True
            if "/" not in raw and addr == ipaddress.ip_address(raw):
                return True
        except ValueError:
            continue
    return False


def _allowlisted(host: str) -> bool:
    for entry in allowlist():
        if entry == host:
            return True
        if entry.startswith("*.") and host.endswith(entry[1:]):
            return True
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
                for info in socket.getaddrinfo(host, None):
                    if ipaddress.ip_address(info[4][0]) in network:
                        return True
        except (ValueError, socket.gaierror):
            continue
    return False


def validate_url(url: str, *, purpose: str = "fetch") -> None:
    """校验 scheme / 主机 / 解析后的每个 IP。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ApiError(
            Code.SSRF_BLOCKED,
            message=f"不支持的协议：{parsed.scheme}",
            hint="只允许 http / https",
            details={"purpose": purpose},
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ApiError(Code.SSRF_BLOCKED, "URL 缺少主机名")
    if host in METADATA_HOSTS:
        raise ApiError(Code.SSRF_BLOCKED, "云元数据地址被硬编码拒绝")
    if _allowlisted(host):
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ApiError(
            Code.SSRF_BLOCKED, f"无法解析主机：{host}", details={"reason": str(exc)}
        ) from exc
    for info in infos:
        ip = info[4][0]
        if _is_denied_ip(ip):
            raise ApiError(
                Code.SSRF_BLOCKED,
                message=f"目标解析到受限地址：{ip}",
                hint="如确需访问内网服务，请把地址加入 SKILLROUTER_FETCH_ALLOWLIST",
                details={"host": host, "ip": ip},
            )


async def fetch_text(
    url: str,
    *,
    purpose: str = "fetch",
    max_bytes: int = 5 * 1024 * 1024,
    timeout_s: int | None = None,
) -> FetchResult:
    """受控抓取：逐跳 SSRF 校验、大小/超时/重定向上限。"""
    validate_url(url, purpose=purpose)
    timeout = httpx.Timeout(timeout_s or settings.fetch_timeout_s)
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            async with client.stream(
                "GET",
                current,
                headers={"User-Agent": "SkillRouter/0.1 (+fetch-guard)", "Accept": "*/*"},
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise ApiError(Code.UPSTREAM_ERROR, "上游返回重定向但缺少 Location")
                    current = str(httpx.URL(current).join(location))
                    validate_url(current, purpose=purpose)
                    continue
                # 边读边截断：不能先 read 全量再截，否则超大响应会先把进程内存吃光（§11.3）
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    room = max_bytes - (total - len(chunk))
                    if room < len(chunk):
                        chunks.append(chunk[: max(0, room)])
                        truncated = True
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                text = body.decode("utf-8", errors="replace")
                return FetchResult(
                    url=str(resp.url),
                    status=resp.status_code,
                    content_type=content_type,
                    text=redact(text, max_len=max_bytes),
                    bytes=total,
                    truncated=truncated,
                )
    raise ApiError(
        Code.SSRF_BLOCKED,
        message=f"重定向次数超过 {MAX_REDIRECTS} 次",
        details={"url": url},
    )
