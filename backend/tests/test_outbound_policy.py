"""Outbound-policy tests: fake-IP proxy detection and the refusal message.

The bug these cover: on a machine behind a Clash/Surge fake-IP proxy every
hostname resolves into 198.18.0.0/15, so the SSRF guard refused *every* provider
and the message pointed at an allowlist that was not the problem. The policy now
detects that environment and the message says which setting to change.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def policy(tmp_path, monkeypatch):
    """Isolated settings + a clean detection cache for each test."""
    from app.core import config
    from app.services import network

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.delenv("SR_OUTBOUND_ALLOW_FAKE_IP", raising=False)
    monkeypatch.delenv("SR_OUTBOUND_FAKE_IP_MODE", raising=False)
    monkeypatch.delenv("SR_OUTBOUND_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("SR_OUTBOUND_ALLOW_PRIVATE", raising=False)
    config.reset_settings_cache()
    # A developer's `.env` may set an explicit override (it is a documented knob);
    # these tests must measure the *code*, not the machine. Neutralise it on the
    # cached instance so each test starts from the documented defaults.
    settings = config.get_settings()
    monkeypatch.setattr(settings, "outbound_allow_fake_ip", None, raising=False)
    monkeypatch.setattr(settings, "outbound_fake_ip_mode", "auto", raising=False)
    network.reset_fake_ip_detection()
    yield network
    network.reset_fake_ip_detection()
    config.reset_settings_cache()


def _resolver(mapping: dict[str, list[str]]):
    """Build a getaddrinfo replacement from `{host: [ips]}`."""

    def fake(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        addresses = mapping.get(host)
        if addresses is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port or 443))
            for addr in addresses
        ]

    return fake


def test_auto_detects_a_fake_ip_proxy_and_allows_public_calls(policy, monkeypatch):
    network = policy
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        _resolver(
            {
                "example.com": ["198.18.0.34"],
                "www.google.com": ["198.18.0.8"],
                "open.bigmodel.cn": ["198.18.0.201"],
            }
        ),
    )
    detected, reason = network.detect_fake_ip_proxy()
    assert detected is True
    assert "198.18.0.0/15" in reason
    assert network.fake_ip_allowed() is True
    # The whole point: a real provider hostname is now callable.
    network.validate_url("https://open.bigmodel.cn/api/paas/v4/chat/completions")


def test_normal_dns_keeps_the_range_blocked(policy, monkeypatch):
    network = policy
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        _resolver({"example.com": ["93.184.215.14"], "www.google.com": ["142.250.185.14"]}),
    )
    detected, reason = network.detect_fake_ip_proxy()
    assert detected is False
    assert "no fake-IP proxy detected" in reason
    assert network.fake_ip_allowed() is False


def test_deny_mode_refuses_even_when_detected(policy, monkeypatch):
    network = policy
    from app.core import config

    monkeypatch.setenv("SR_OUTBOUND_FAKE_IP_MODE", "deny")
    monkeypatch.setattr(network.socket, "getaddrinfo", _resolver({"open.bigmodel.cn": ["198.18.0.201"]}))
    config.reset_settings_cache()
    assert network.fake_ip_allowed() is False
    with pytest.raises(network.NetworkPolicyError) as excinfo:
        network.validate_url("https://open.bigmodel.cn/api/paas/v4/chat/completions")
    assert excinfo.value.code == "private_target_blocked"


def test_allow_mode_skips_detection(policy):
    network = policy
    from app.core import config

    import os

    os.environ["SR_OUTBOUND_FAKE_IP_MODE"] = "allow"
    config.reset_settings_cache()
    assert network.fake_ip_allowed() is True


def test_explicit_override_wins_over_the_mode(policy):
    network = policy
    from app.core import config

    import os

    os.environ["SR_OUTBOUND_FAKE_IP_MODE"] = "allow"
    os.environ["SR_OUTBOUND_ALLOW_FAKE_IP"] = "false"
    config.reset_settings_cache()
    assert network.fake_ip_allowed() is False


def test_loopback_and_metadata_are_still_refused_with_the_exemption(policy, monkeypatch):
    """The exemption is narrow: it must not open this machine or a metadata IP."""
    network = policy
    from app.core import config

    import os

    os.environ["SR_OUTBOUND_FAKE_IP_MODE"] = "allow"
    config.reset_settings_cache()
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        _resolver({"127.0.0.1": ["127.0.0.1"], "169.254.169.254": ["169.254.169.254"]}),
    )
    for url in ("http://127.0.0.1:8000/api", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(network.NetworkPolicyError):
            network.validate_url(url)
    assert network._is_blocked_ip("127.0.0.1") is True
    assert network._is_blocked_ip("169.254.169.254") is True
    assert network._is_blocked_ip("10.0.0.5") is True
    assert network._is_blocked_ip("198.18.0.201") is False


def test_refusal_message_names_the_setting_and_the_detection_result(policy, monkeypatch):
    network = policy
    from app.core import config

    monkeypatch.setenv("SR_OUTBOUND_FAKE_IP_MODE", "deny")
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        _resolver({"example.com": ["198.18.0.34"], "www.google.com": ["198.18.0.8"], "open.bigmodel.cn": ["198.18.0.201"]}),
    )
    config.reset_settings_cache()
    with pytest.raises(network.NetworkPolicyError) as excinfo:
        network.validate_url("https://open.bigmodel.cn/api/paas/v4/chat/completions")
    message = str(excinfo.value)
    assert "198.18.0.0/15" in message
    assert "SR_OUTBOUND_FAKE_IP_MODE" in message
    assert "fake-IP" in message


def test_genuine_private_address_gets_the_policy_message(policy, monkeypatch):
    network = policy
    monkeypatch.setattr(network.socket, "getaddrinfo", _resolver({"db.internal": ["10.1.2.3"]}))
    with pytest.raises(network.NetworkPolicyError) as excinfo:
        network.validate_url("http://db.internal/health")
    message = str(excinfo.value)
    assert "SR_OUTBOUND_ALLOWED_HOSTS" in message
    assert "fake-IP" not in message


def test_policy_summary_is_json_safe(policy, monkeypatch):
    network = policy
    import json

    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        _resolver({"example.com": ["198.18.0.34"], "www.google.com": ["198.18.0.8"]}),
    )
    summary = network.outbound_policy_summary()
    assert summary["fake_ip_detected"] is True
    assert summary["fake_ip_exemption_active"] is True
    json.dumps(summary)  # must not raise: it is served by /api/meta
