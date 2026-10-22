"""Shared test isolation.

A developer shell may export a relaxed outbound policy (an allowlist entry or
the fake-IP opt-in), which changes whether a probe is blocked and would make the
suite depend on the machine it runs on. Tests that need the relaxed policy set it
explicitly with monkeypatch, so this autouse fixture clears it first.

`conftest.py` applies to every module in this directory, including test files
added later.
"""

from __future__ import annotations

import os

import pytest

AMBIENT_NETWORK_ENV = (
    "SR_OUTBOUND_ALLOWED_HOSTS",
    "SR_OUTBOUND_ALLOW_PRIVATE",
    "SR_OUTBOUND_ALLOW_FAKE_IP",
)


@pytest.fixture(autouse=True)
def _clear_ambient_network_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AMBIENT_NETWORK_ENV:
        monkeypatch.delenv(name, raising=False)
    # Settings memoisation would otherwise keep a policy loaded from a past test.
    try:
        from app.core import config

        config.reset_settings_cache()
    except Exception:  # noqa: BLE001 - app package is importable in every test
        pass
    yield
    for name in AMBIENT_NETWORK_ENV:
        os.environ.pop(name, None)
