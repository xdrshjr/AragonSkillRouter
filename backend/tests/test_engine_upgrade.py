"""Engine upgrade flow tests (CONTRACT §5.10, §12).

The contract names two required behaviours, and both are about *not* damaging the
running system:

  * a candidate that fails validation must leave the current release in place;
  * a failed activation must roll back.

Everything here is exercised against the real database and the real state machine;
only the two genuinely external things are replaced — the git/npm/tsc work (via the
``_build_runner`` seam) and the sidecar's health endpoint (via a fake adapter).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _unload_engine_adapter():
    """See the same fixture in test_agent_adapter.py.

    `engine_upgrade` reads the adapter version lazily, which imports the module;
    `test_execution.py` asserts the module is absent after a capability call, so
    this suite restores the import graph on the way out.
    """
    yield
    sys.modules.pop("app.services.agent_adapter", None)
    services_package = sys.modules.get("app.services")
    if services_package is not None and hasattr(services_package, "agent_adapter"):
        delattr(services_package, "agent_adapter")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.delenv("SR_MASTER_KEYS", raising=False)
    monkeypatch.delenv("SR_MASTER_KEY_ACTIVE", raising=False)
    config.reset_settings_cache()
    settings = config.get_settings()
    settings.ensure_dirs()
    from app.core import db

    db.init_db()
    from app.security import crypto

    crypto.create_keyring(settings.keyring_path, overwrite=True)
    yield settings
    config.reset_settings_cache()


class FakeAdapter:
    """Stand-in for AgentAdapter; only the calls engine_upgrade makes."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeAdapter.instances.append(self)

    instances: list["FakeAdapter"] = []
    health: dict[str, Any] = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    releases: dict[str, Any] = {"ok": True}

    async def check_health(self) -> dict[str, Any]:
        return dict(FakeAdapter.health)

    async def check_releases(self, *, channel: str | None = None) -> dict[str, Any]:
        return dict(FakeAdapter.releases)


@pytest.fixture()
def fake_adapter(monkeypatch):
    from app.services import engine_upgrade

    FakeAdapter.instances = []
    FakeAdapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    FakeAdapter.releases = {"ok": True}
    # The client is constructed lazily (`new_adapter`), so that is the seam.
    monkeypatch.setattr(engine_upgrade, "new_adapter", lambda **kwargs: FakeAdapter(**kwargs))
    return FakeAdapter


@pytest.fixture()
def fast_checks(monkeypatch):
    """Replace every outbound check with a scripted verdict."""
    from app.services import engine_upgrade

    state: dict[str, Any] = {
        "npm": {"name": "npm_available", "status": "passed", "detail": "published"},
        "build": {"name": "isolated_build", "status": "passed", "detail": "built"},
        "regression": {"name": "regression_tests", "status": "passed", "detail": "green"},
    }

    async def fake_npm(version: str | None) -> dict[str, Any]:
        return dict(state["npm"])

    async def fake_build(version: str | None):
        return dict(state["build"]), dict(state["regression"])

    async def fake_sidecar(version: str | None) -> dict[str, Any]:
        return {"name": "sidecar_reachable", "status": "passed", "detail": "reachable"}

    monkeypatch.setattr(engine_upgrade, "_check_npm_available", fake_npm)
    monkeypatch.setattr(engine_upgrade, "_check_sidecar_reports", fake_sidecar)
    monkeypatch.setattr(engine_upgrade, "_build_runner", fake_build)
    return state


def _insert_release(*, status: str, version: str = "0.2.46", commit: str = "a" * 40, ref: str = "v0.2.46") -> str:
    from app.core import db

    release_id = db.new_id("rel")
    db.insert(
        "engine_releases",
        {
            "id": release_id,
            "channel": "stable",
            "ref": ref,
            "version": version,
            "commit_sha": commit,
            "npm_version": version,
            "status": status,
            "adapter_version": "1.0.0",
            "dependency_fingerprint": "{}",
            "capabilities": "{}",
            "checks": "[]",
            "created_at": db.iso(),
            "activated_at": db.iso() if status == "active" else None,
        },
    )
    return release_id


def _status(release_id: str) -> str:
    from app.core import db

    return db.query_scalar("SELECT status FROM engine_releases WHERE id = ?", (release_id,))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_check_records_a_candidate_with_a_real_commit(isolated, fake_adapter):
    from app.core import db
    from app.services import engine_upgrade

    fake_adapter.releases = {
        "ok": True,
        "channel": "stable",
        "ref": "v0.2.50",
        "commit": "b" * 40,
        "detected_at": "2026-10-08T00:00:00Z",
        "source": "git ls-remote --tags https://example.test/repo.git",
        "candidates": [{"ref": "v0.2.50"}],
    }
    result = await engine_upgrade.check_for_updates()
    assert result["existing"] is False
    assert result["detected"]["commit_sha"] == "b" * 40
    assert result["detected"]["status"] == "detected"
    assert result["detected"]["npm_version"] == "0.2.50"

    # Detecting the same commit twice does not create a second row.
    again = await engine_upgrade.check_for_updates()
    assert again["existing"] is True
    assert db.query_scalar("SELECT COUNT(*) FROM engine_releases") == 1


@pytest.mark.asyncio
async def test_failed_detection_creates_no_row_and_says_why(isolated, fake_adapter):
    from app.core import db
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    fake_adapter.releases = {
        "ok": False,
        "channel": "stable",
        "ref": None,
        "commit": None,
        "source": "git ls-remote --tags https://example.test/repo.git",
        "error": "git ls-remote failed: exit code 128 (repository not found)",
    }
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.check_for_updates()
    assert excinfo.value.code == "engine_detection_failed"
    assert "repository not found" in excinfo.value.message
    assert db.query_scalar("SELECT COUNT(*) FROM engine_releases") == 0


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_validation_failure_keeps_the_current_release(isolated, fake_adapter, fast_checks):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    active = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")

    fast_checks["build"] = {
        "name": "isolated_build",
        "status": "failed",
        "detail": "TypeScript build failed against core 0.2.50 (exit 2): TS2345",
    }

    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.validate_release(candidate)
    error = excinfo.value
    assert error.code == "engine_validation_failed"
    assert "current release is unchanged" in error.message

    # The candidate is failed; the active release is untouched and still active.
    assert _status(candidate) == "failed"
    assert _status(active) == "active"
    detail = error.details
    assert detail["active_release"]["id"] == active
    failures = [check for check in detail["checks"] if check["status"] == "failed"]
    assert any("isolated_build" == check["name"] for check in failures)
    # The registry version was fine; it must not be blamed.
    assert all(check["status"] != "failed" for check in detail["checks"] if check["name"] == "npm_available")


@pytest.mark.asyncio
async def test_validation_records_skipped_checks_as_skipped_not_passed(isolated, fake_adapter, fast_checks):
    from app.services import engine_upgrade

    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    fast_checks["npm"] = {
        "name": "npm_available",
        "status": "skipped",
        "detail": "Could not reach the npm registry (ConnectError); the version was not verified.",
    }
    result = await engine_upgrade.validate_release(candidate)

    assert result["status"] == "verified"
    skipped = {check["name"]: check for check in result["skipped_checks"]}
    assert skipped["npm_available"]["status"] == "skipped"
    assert "not verified" in skipped["npm_available"]["detail"]
    assert result["note"].startswith("Skipped checks did not fail")
    assert _status(candidate) == "verified"


@pytest.mark.asyncio
async def test_unpublished_version_fails_validation(isolated, fake_adapter, fast_checks):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    candidate = _insert_release(status="detected", version="0.3.0", commit="c" * 40, ref="v0.3.0")
    fast_checks["npm"] = {
        "name": "npm_available",
        "status": "failed",
        "detail": "@aragon-agent/core@0.3.0 is not published on npm (HTTP 404).",
    }
    with pytest.raises(ApiError):
        await engine_upgrade.validate_release(candidate)
    assert _status(candidate) == "failed"


@pytest.mark.asyncio
async def test_out_of_range_core_version_is_refused(isolated, fake_adapter, fast_checks):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    candidate = _insert_release(status="detected", version="0.9.0", commit="d" * 40, ref="v0.9.0")
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.validate_release(candidate)
    assert _status(candidate) == "failed"
    checks = excinfo.value.details["checks"]
    compat = next(check for check in checks if check["name"] == "adapter_compatibility")
    assert compat["status"] == "failed"
    assert "tested range" in compat["detail"]


@pytest.mark.asyncio
async def test_activating_an_unverified_release_is_refused(isolated, fake_adapter):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.activate_release(candidate)
    assert excinfo.value.code == "release_not_verified"
    assert _status(candidate) == "detected"


# --------------------------------------------------------------------------- #
# Activation and rollback
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_activation_moves_the_pointer_only_when_the_sidecar_agrees(isolated, fake_adapter, fast_checks):
    from app.core import db
    from app.services import engine_upgrade

    active = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    await engine_upgrade.validate_release(candidate)

    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    result = await engine_upgrade.activate_release(candidate)

    assert result["activated"] is True
    assert _status(candidate) == "active"
    assert _status(active) == "superseded"
    assert db.get_setting("engine.active_release")["commit"] == "b" * 40
    assert db.get_setting("engine.active_release")["version"] == "0.2.50"


@pytest.mark.asyncio
async def test_activation_refused_when_the_sidecar_runs_a_different_version(isolated, fake_adapter, fast_checks):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    active = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    await engine_upgrade.validate_release(candidate)

    # The operator validated, but never reinstalled/restarted the sidecar.
    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.46"}}
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.activate_release(candidate)

    assert excinfo.value.code == "engine_activation_refused"
    assert "current release is unchanged" in excinfo.value.message
    assert _status(active) == "active"
    assert _status(candidate) == "failed"
    assert any("restart the sidecar" in action for action in excinfo.value.actions)


@pytest.mark.asyncio
async def test_activation_refused_when_the_sidecar_is_unreachable(isolated, fake_adapter, fast_checks):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    active = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    await engine_upgrade.validate_release(candidate)

    fake_adapter.health = {"ok": False, "reachable": False, "error": {"code": "engine_unavailable"}}
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.activate_release(candidate)
    assert excinfo.value.code == "engine_activation_refused"
    assert _status(active) == "active"


@pytest.mark.asyncio
async def test_rollback_returns_to_the_previous_release(isolated, fake_adapter, fast_checks):
    from app.core import db
    from app.services import engine_upgrade

    previous = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    await engine_upgrade.validate_release(candidate)
    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    await engine_upgrade.activate_release(candidate)

    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.46"}}
    result = await engine_upgrade.rollback()

    assert result["rolled_back"] is True
    assert result["release_id"] == previous
    assert _status(previous) == "active"
    assert _status(candidate) == "rolled_back"
    assert result["sidecar"]["matches_release"] is True
    assert db.get_setting("engine.active_release")["id"] == previous


@pytest.mark.asyncio
async def test_rollback_warns_when_the_sidecar_did_not_follow(isolated, fake_adapter, fast_checks):
    from app.services import engine_upgrade

    _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="detected", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    await engine_upgrade.validate_release(candidate)
    fake_adapter.health = {"ok": True, "reachable": True, "engine": {"version": "0.2.50"}}
    await engine_upgrade.activate_release(candidate)

    # The sidecar is still on the newer build: the pointer moved, reality did not.
    result = await engine_upgrade.rollback()
    assert result["sidecar"]["matches_release"] is False
    assert "Reinstall and restart the sidecar" in result["warning"]


@pytest.mark.asyncio
async def test_rollback_without_a_predecessor_is_refused(isolated, fake_adapter):
    from app.core.errors import ApiError
    from app.services import engine_upgrade

    _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    with pytest.raises(ApiError) as excinfo:
        await engine_upgrade.rollback()
    assert excinfo.value.code == "no_previous_release"


# --------------------------------------------------------------------------- #
# Overview and matrix
# --------------------------------------------------------------------------- #
def test_overview_reports_current_candidate_and_matrix(isolated, fake_adapter):
    from app.services import engine_upgrade

    active = _insert_release(status="active", version="0.2.46", commit="a" * 40, ref="v0.2.46")
    candidate = _insert_release(status="verified", version="0.2.50", commit="b" * 40, ref="v0.2.50")
    overview = engine_upgrade.engine_overview()

    assert overview["current"]["id"] == active
    assert overview["candidate"]["id"] == candidate
    assert overview["capability_matrix"]["adapter_version"] == "1.0.0"
    assert overview["capability_matrix"]["sidecar_protocol"] == 1
    assert len(overview["capability_matrix"]["event_union"]) >= 9
    assert overview["policy"]["upgrade_policy"] == "manual"
    assert overview["aligned"]["core_version"]


def test_history_is_paginated_newest_first(isolated):
    from app.core import db
    from app.services import engine_upgrade

    first = _insert_release(status="superseded", version="0.2.40", commit="1" * 40, ref="v0.2.40")
    second = _insert_release(status="active", version="0.2.46", commit="2" * 40, ref="v0.2.46")
    db.execute("UPDATE engine_releases SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00Z", first))
    db.execute("UPDATE engine_releases SET created_at = ? WHERE id = ?", ("2026-02-01T00:00:00Z", second))

    items, total = engine_upgrade.list_releases(limit=1, offset=0)
    assert total == 2
    assert len(items) == 1
    assert items[0]["id"] == second


def test_version_parsing_handles_tags_and_names():
    from app.services.engine_upgrade import _version_from_ref, _version_tuple

    assert _version_from_ref("v0.2.50") == "0.2.50"
    assert _version_from_ref("refs/tags/v0.2.50") == "0.2.50"
    assert _version_from_ref("main") is None
    assert _version_tuple("0.2.50") == (0, 2, 50)
    assert _version_tuple("v1.2") == (1, 2, 0)
    assert _version_tuple("not-a-version") is None
