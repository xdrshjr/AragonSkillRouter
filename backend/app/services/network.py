"""Outbound network policy.

Every outbound request made on behalf of a capability, a web import or a health
check goes through `guarded_client` / `guarded_request`. The guard exists
because URLs are user-supplied data: without it an API configuration could be
pointed at `http://169.254.169.254/` (cloud metadata), at localhost, or at an
RFC1918 address inside the platform's own network.

Policy:
  * scheme must be http/https;
  * hostname is resolved and every resolved address is checked — a DNS name
    that resolves into private space is refused, not just literal IPs;
  * loopback / link-local / private / reserved ranges are denied unless the
    host was explicitly allowlisted by the administrator
    (SR_OUTBOUND_ALLOWED_HOSTS) or SR_OUTBOUND_ALLOW_PRIVATE is on;
  * redirects are followed manually, at most N hops, each hop re-validated;
  * the response body is streamed with a hard byte cap;
  * the total wall clock is bounded by the caller's timeout.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "169.254.169.254",
    "fd00:ec2::254",
}


class NetworkPolicyError(RuntimeError):
    """Raised when a target violates the outbound policy."""

    def __init__(self, message: str, *, code: str = "network_blocked", detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class GuardedResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str
    elapsed_ms: int
    redirects: list[str] = field(default_factory=list)

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.content.decode("utf-8", errors="replace"))
        except ValueError:
            return None

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()


_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")

# Hosts used to detect a fake-IP proxy. They are stable, HTTPS-only public
# services; the check only looks at the *address range* of the answer.
_FAKE_IP_PROBES = ("example.com", "www.google.com", "cloudflare.com")

_detection: dict[str, object] = {"done": False, "detected": False, "sample": None, "reason": ""}


def reset_fake_ip_detection() -> None:
    """Forget the cached detection result (tests, and after a network change)."""
    _detection.update({"done": False, "detected": False, "sample": None, "reason": ""})


def detect_fake_ip_proxy() -> tuple[bool, str]:
    """Is this machine behind a transparent proxy that fakes DNS answers?

    Fake-IP mode (Clash, Surge, some corporate middleboxes) answers *every* name
    with an address from 198.18.0.0/15 so it can route by hostname later. The
    tell-tale is that unrelated public names all resolve into that single narrow
    range — a real DNS answer never does. No socket is opened and no traffic is
    generated; only the resolver's answer is inspected.
    """
    if _detection["done"]:
        return bool(_detection["detected"]), str(_detection["reason"])

    imported = 0
    fake_answers = 0
    sample: str | None = None
    for host in _FAKE_IP_PROBES:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError):
            continue  # offline or blocked: try the next probe
        addresses = {info[4][0] for info in infos}
        if not addresses:
            continue
        imported += 1
        if all(_in_fake_range(addr) for addr in addresses):
            fake_answers += 1
            sample = sample or f"{host} → {', '.join(sorted(addresses))}"
        if fake_answers >= 2:
            break

    detected = fake_answers >= 2 or (imported == 1 and fake_answers == 1)
    if detected:
        reason = (
            f"fake-IP proxy detected ({sample}); hostnames resolve into 198.18.0.0/15, "
            "so that range is treated as public for outbound calls"
        )
    elif imported == 0:
        reason = "could not resolve any probe host; fake-IP detection inconclusive"
    else:
        reason = f"no fake-IP proxy detected ({imported} public name(s) resolved normally)"
    _detection.update({"done": True, "detected": detected, "sample": sample, "reason": reason})
    return detected, reason


def _in_fake_range(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return _in_fake_range(str(addr.ipv4_mapped))
    return isinstance(addr, ipaddress.IPv4Address) and addr in _FAKE_IP_RANGE


def fake_ip_allowed() -> bool:
    """Effective policy for the 198.18.0.0/15 range."""
    settings = get_settings()
    if settings.outbound_allow_fake_ip is not None:
        return bool(settings.outbound_allow_fake_ip)
    mode = (settings.outbound_fake_ip_mode or "auto").strip().lower()
    if mode == "allow":
        return True
    if mode == "deny":
        return False
    # auto (unknown values fall back to auto rather than silently allowing)
    detected, _ = detect_fake_ip_proxy()
    return detected


def outbound_policy_summary() -> dict[str, object]:
    """Non-secret description of the effective outbound policy (for /api/meta)."""
    settings = get_settings()
    detected, reason = detect_fake_ip_proxy()
    return {
        "allow_private_hosts": sorted(settings.allowed_private_hosts),
        "allow_private_networks": bool(settings.outbound_allow_private),
        "fake_ip_mode": (settings.outbound_fake_ip_mode or "auto").lower(),
        "fake_ip_detected": detected,
        "fake_ip_exemption_active": fake_ip_allowed(),
        "fake_ip_reason": reason,
        "timeout_seconds": settings.outbound_timeout_seconds,
        "max_redirects": settings.outbound_max_redirects,
    }


def _is_blocked_ip(ip: str) -> bool:
    settings = get_settings()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    # IPv4-mapped IPv6 (::ffff:127.0.0.1 must not sneak past the loopback check)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return _is_blocked_ip(str(addr.ipv4_mapped))
    if _in_fake_range(str(addr)) and fake_ip_allowed():
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_private or addr.is_reserved or addr.is_multicast:
        return True
    return False


def _host_allowlisted(host: str) -> bool:
    settings = get_settings()
    host = host.lower().strip("[]")
    allowed = settings.allowed_private_hosts
    if host in allowed:
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed if suffix)


def validate_url(url: str, *, allow_private: bool | None = None) -> None:
    settings = get_settings()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError(
            f"Only http and https URLs can be called; got {parsed.scheme or 'no scheme'!r}.",
            code="unsupported_scheme",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise NetworkPolicyError("The URL has no host.", code="invalid_url")
    if host in METADATA_HOSTS:
        raise NetworkPolicyError(
            "Requests to cloud metadata endpoints are always refused.",
            code="metadata_blocked",
        )
    permitted = settings.outbound_allow_private if allow_private is None else allow_private
    if permitted or _host_allowlisted(host):
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NetworkPolicyError(
            f"The host {host!r} could not be resolved: {exc.strerror or exc}. "
            "Check the spelling, and whether this machine needs a proxy to reach the internet.",
            code="dns_failed",
        ) from exc
    addresses = {info[4][0] for info in infos}
    blocked = sorted(addr for addr in addresses if _is_blocked_ip(addr))
    if blocked:
        raise NetworkPolicyError(_blocked_message(host, blocked, settings), code="private_target_blocked",
                                 detail={"host": host, "addresses": blocked})


def _blocked_message(host: str, blocked: list[str], settings) -> str:
    """Explain the refusal and name the exact setting that would change it.

    A message that only says "refused" forces the user to read source code. The
    distinction that matters is *why* the address looks private: a fake-IP proxy
    answering every name with 198.18.0.0/15 is an environment fact with a known
    remedy, while a genuine private address is a policy decision.
    """
    fake_only = all(_in_fake_range(addr) for addr in blocked)
    if fake_only and not fake_ip_allowed():
        _, reason = detect_fake_ip_proxy()
        return (
            f"{host!r} resolved to {', '.join(blocked)}, which is inside 198.18.0.0/15 — the range a "
            "transparent proxy (Clash/Surge fake-IP mode) hands out for every hostname. "
            "Skill Router refuses that range because it cannot distinguish it from an internal "
            f"target. Detection said: {reason}. "
            "Fix: set SR_OUTBOUND_FAKE_IP_MODE=auto (default; detects this automatically) or =allow, "
            "or add this host to SR_OUTBOUND_ALLOWED_HOSTS if it really is an internal service."
        )
    return (
        f"{host!r} resolves to an address inside a private or reserved range ({', '.join(blocked)}). "
        "This is blocked so a capability cannot reach this machine, the local network, or a cloud "
        "metadata endpoint. Add the host to SR_OUTBOUND_ALLOWED_HOSTS only if you intend to reach "
        "an internal service."
    )


def classify_network_error(exc: Exception) -> tuple[str, str]:
    """Map transport exceptions onto stable, actionable codes."""
    if isinstance(exc, NetworkPolicyError):
        return exc.code, str(exc)
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout", "The connection to the provider timed out."
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout", "The provider accepted the connection but did not answer in time."
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout", "The request body could not be sent in time."
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout", "Too many concurrent outbound requests; retry shortly."
    if isinstance(exc, httpx.ConnectError):
        return "connect_failed", f"The provider could not be reached: {exc}"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects", "The provider redirected too many times."
    if isinstance(exc, httpx.HTTPError):
        return "transport_error", str(exc) or exc.__class__.__name__
    return "network_error", str(exc) or exc.__class__.__name__


async def guarded_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    data: Any = None,
    files: Any = None,
    content: bytes | None = None,
    timeout_s: float | None = None,
    max_bytes: int | None = None,
    allow_private: bool | None = None,
    client: httpx.AsyncClient | None = None,
) -> GuardedResponse:
    import time

    settings = get_settings()
    timeout = min(timeout_s or settings.outbound_timeout_seconds, 600.0)
    cap = max_bytes or settings.outbound_max_response_bytes
    redirects: list[str] = []
    current = url
    started = time.perf_counter()

    owned_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        headers={"user-agent": "SkillRouter/1.0 (+capability-executor)"},
    )
    try:
        for hop in range(settings.outbound_max_redirects + 1):
            validate_url(current, allow_private=allow_private)
            request = http.build_request(
                method.upper(),
                current,
                headers=dict(headers or {}),
                params=dict(params or {}) if params else None,
                json=json_body,
                data=data,
                files=files,
                content=content,
            )
            response = await http.send(request, stream=True)
            try:
                status = response.status_code
                if status in {301, 302, 303, 307, 308} and hop < settings.outbound_max_redirects:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = str(response.url.join(location))
                    redirects.append(current)
                    if status == 303:
                        method, content, json_body, data, files = "GET", None, None, None, None
                    continue
                collected = bytearray()
                async for chunk in response.aiter_bytes():
                    collected.extend(chunk)
                    if len(collected) > cap:
                        raise NetworkPolicyError(
                            f"The response exceeded the {cap} byte limit.",
                            code="response_too_large",
                        )
                return GuardedResponse(
                    status_code=status,
                    headers=dict(response.headers),
                    content=bytes(collected),
                    url=str(response.url),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    redirects=redirects,
                )
            finally:
                await response.aclose()
        raise NetworkPolicyError(
            f"Too many redirects (limit {settings.outbound_max_redirects}).",
            code="too_many_redirects",
        )
    finally:
        if owned_client:
            await http.aclose()


def build_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in pairs:
        if name and value is not None:
            headers[str(name)] = str(value)
    return headers
