"""Engine upgrade flow: detect → validate → activate → roll back (CONTRACT §5.10).

The shape of this module is a consequence of one rule: **a failed or unproven
candidate must never disturb the release that is currently serving traffic.** So
every step is recorded on its own ``engine_releases`` row before it can influence
anything, and the "active" pointer only moves after a health check passes. If the
health check fails, activation rolls back in the same request and says so.

What each status means here:

    detected → a remote actually reported this ref/commit (never a guess)
    validating → checks are running; the active release is untouched
    verified → every non-skipped check passed; the row is now activatable
    failed → at least one check failed, with the reason stored on the row
    active → the running engine has been confirmed to be this release
    rolled_back → it was active, failed its health check, and was reverted
    superseded → it was active before a newer activation

Checks are honest about what they could not do. A check that could not run is
recorded as ``skipped`` **with a reason**, never as ``passed``: the platform's
whole upgrade story is that "verified" means something.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.core import db
from app.core.config import REPO_DIR, get_settings
from app.core.paths import bundled_sidecar_dir
from app.core.errors import ApiError, conflict, not_found
from app.services import audit

log = logging.getLogger("skillrouter.engine_upgrade")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.agent_adapter import AgentAdapter


def adapter_version() -> str:
    """Read the adapter version lazily so importing the app stays engine-free.

    `app.services.agent_adapter` is deliberately absent from this module's import
    graph: `app.main` imports this module, and a plain capability invocation must
    never pull the AragonAgent client into the process
    (`backend/tests/test_execution.py` asserts exactly that).
    """
    from app.services.agent_adapter import ADAPTER_VERSION

    return ADAPTER_VERSION


def new_adapter(**kwargs: Any) -> "AgentAdapter":
    """Construct the sidecar client lazily (see `adapter_version`)."""
    from app.services.agent_adapter import AgentAdapter

    return AgentAdapter(**kwargs)


def _compat() -> dict[str, Any]:
    """The compatibility block for the adapter actually installed."""
    version = adapter_version()
    block = COMPATIBILITY.get(version)
    if block is None:
        raise ApiError(
            "adapter_incompatible",
            f"This build's adapter version ({version}) has no compatibility matrix entry.",
            status_code=500,
            details={"known": sorted(COMPATIBILITY)},
        )
    return {**block, "adapter_version": version}

#: What this adapter can drive. A candidate outside the range is refused at
#: validation, before anything is installed.
COMPATIBILITY: dict[str, dict[str, Any]] = {
    "1.0.0": {
        "sidecar_protocol": 1,
        "core_min": (0, 2, 0),
        "core_max_exclusive": (0, 4, 0),
        "cli_min": "0.6.34",
        "event_union": [
            "run.started",
            "assistant.delta",
            "tool.call",
            "tool.result",
            "todo.update",
            "run.progress",
            "run.stopped",
            "run.failed",
            "run.finished",
        ],
        "endpoints": [
            "GET /health",
            "GET /info",
            "POST /sessions",
            "GET /sessions/{id}",
            "DELETE /sessions/{id}",
            "POST /sessions/{id}/messages",
            "GET /sessions/{id}/events",
            "POST /sessions/{id}/stop",
            "GET /releases/check",
        ],
        "platform_tools": 12,
    }
}

BUILD_TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel": row["channel"],
        "ref": row["ref"],
        "version": row["version"],
        "commit_sha": row["commit_sha"],
        "npm_version": row.get("npm_version"),
        "status": row["status"],
        "adapter_version": row.get("adapter_version") or "",
        "capabilities": db.loads(row.get("capabilities"), {}),
        "checks": db.loads(row.get("checks"), []),
        "dependency_fingerprint": db.loads(row.get("dependency_fingerprint"), {}),
        "previous_release_id": row.get("previous_release_id"),
        "activated_at": row.get("activated_at"),
        "verified_at": row.get("verified_at"),
        "created_at": row["created_at"],
        "notes": row.get("notes"),
    }


def list_releases(*, limit: int = 50, offset: int = 0, status: str | None = None) -> tuple[list[dict], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM engine_releases WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM engine_releases WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_serialize(row) for row in rows], int(total)


def _active_row() -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM engine_releases WHERE status = 'active' ORDER BY activated_at DESC, created_at DESC LIMIT 1"
    )


def _candidate_row() -> dict[str, Any] | None:
    return db.query_one(
        """SELECT * FROM engine_releases
            WHERE status IN ('detected','validating','verified','pending_approval')
            ORDER BY created_at DESC LIMIT 1"""
    )


def capability_matrix() -> dict[str, Any]:
    """What this backend + adapter can do, so the UI can show a real matrix."""
    adapter = _compat()
    counts = db.query_all("SELECT status, COUNT(*) AS n FROM capabilities GROUP BY status")
    return {
        "adapter_version": adapter_version(),
        "sidecar_protocol": adapter["sidecar_protocol"],
        "supported_core": {
            "min": ".".join(str(p) for p in adapter["core_min"]),
            "max_exclusive": ".".join(str(p) for p in adapter["core_max_exclusive"]),
        },
        "cli_min": adapter["cli_min"],
        "event_union": adapter["event_union"],
        "endpoints": adapter["endpoints"],
        "platform_tools": adapter["platform_tools"],
        "capabilities_by_status": {row["status"]: row["n"] for row in counts},
    }


def engine_overview(*, limit: int = 20) -> dict[str, Any]:
    settings = get_settings()
    active = _active_row()
    candidate = _candidate_row()
    history, total = list_releases(limit=limit)
    return {
        "current": _serialize(active) if active else None,
        "candidate": _serialize(candidate) if candidate else None,
        "history": history,
        "history_total": total,
        "capability_matrix": capability_matrix(),
        "policy": {
            "upgrade_policy": db.get_setting("agent.upgrade_policy", settings.aragon_upgrade_policy),
            "upgrade_channel": db.get_setting("agent.upgrade_channel", settings.aragon_upgrade_channel),
            "track_branch": db.get_setting("agent.track_branch", settings.aragon_track_branch),
        },
        "aligned": {
            "commit": settings.aragon_aligned_commit,
            "core_version": settings.aragon_aligned_core_version,
            "cli_version": settings.aragon_aligned_cli_version,
        },
        "npm": {"core": settings.aragon_npm_package, "cli": settings.aragon_cli_package},
        "sidecar": {
            "url": settings.aragon_sidecar_url,
            "token_configured": bool(settings.aragon_sidecar_token),
        },
    }


def _version_tuple(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    parts = version.strip().lstrip("v").split(".")
    try:
        numbers = [int(part.split("-")[0]) for part in parts[:3]]
    except (TypeError, ValueError):
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


# --------------------------------------------------------------------------- #
# 1. Detect
# --------------------------------------------------------------------------- #
async def check_for_updates(*, actor_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
    """Ask the sidecar what the remote says, and record a candidate row."""
    settings = get_settings()
    channel = str(db.get_setting("agent.upgrade_channel", settings.aragon_upgrade_channel))
    adapter = new_adapter()
    detection = await adapter.check_releases(channel=channel)

    if not detection.get("ok") or not detection.get("commit"):
        # A failed detection must not create a row: a release that names a commit
        # nobody verified is a lie the UI would later show as fact.
        audit.record(
            actor_type="user",
            actor_id=actor_id,
            action="engine.check_failed",
            object_type="engine",
            object_id=None,
            summary=f"upstream detection failed ({channel})",
            metadata={"channel": channel, "result": detection},
            ip=ip,
        )
        raise ApiError(
            "engine_detection_failed",
            detection.get("error")
            or "The sidecar could not determine the upstream commit, so no candidate was recorded.",
            status_code=502,
            details={"channel": channel, "source": detection.get("source")},
            actions=[
                "Check the sidecar log for the failing git command.",
                "Confirm SR_ARAGON_REPO_URL is reachable from the sidecar's host.",
                "Switch SR_ARAGON_UPGRADE_CHANNEL if the remote has no semver tags.",
            ],
        )

    commit = str(detection["commit"])
    ref = str(detection.get("ref") or channel)
    version = _version_from_ref(ref) or str(detection.get("version") or "")

    existing = db.query_one(
        "SELECT * FROM engine_releases WHERE commit_sha = ? AND status NOT IN ('superseded','rolled_back') ORDER BY created_at DESC LIMIT 1",
        (commit,),
    )
    if existing:
        return {
            "detected": _serialize(existing),
            "existing": True,
            "channel": channel,
            "detected_at": detection.get("detected_at"),
            "source": detection.get("source"),
            "note": "This commit is already recorded; no new candidate was created.",
        }

    active = _active_row()
    if active and active["commit_sha"] == commit:
        return {
            "detected": _serialize(active),
            "existing": True,
            "up_to_date": True,
            "channel": channel,
            "detected_at": detection.get("detected_at"),
            "source": detection.get("source"),
            "note": "The detected commit is already the active release.",
        }

    release_id = db.new_id("rel")
    db.insert(
        "engine_releases",
        {
            "id": release_id,
            "channel": channel,
            "ref": ref,
            "version": version or "unknown",
            "commit_sha": commit,
            "npm_version": version or None,
            "status": "detected",
            "adapter_version": adapter_version(),
            "dependency_fingerprint": db.dumps({}),
            "capabilities": db.dumps(capability_matrix()),
            "checks": db.dumps([]),
            "previous_release_id": active["id"] if active else None,
            "created_at": db.iso(),
            "notes": (
                f"Detected via {detection.get('source')} at {detection.get('detected_at')}. "
                f"Candidates considered: {len(detection.get('candidates') or [])}."
            ),
        },
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="engine.candidate_detected",
        object_type="engine_release",
        object_id=release_id,
        summary=f"candidate {ref} @ {commit[:10]} detected ({channel})",
        metadata={"channel": channel, "ref": ref, "commit": commit, "source": detection.get("source")},
        ip=ip,
    )
    return {
        "detected": _serialize(db.query_one("SELECT * FROM engine_releases WHERE id = ?", (release_id,))),
        "existing": False,
        "channel": channel,
        "detected_at": detection.get("detected_at"),
        "source": detection.get("source"),
        "next_step": f"POST /api/admin/engine/releases/{release_id}/validate to run the checks.",
    }


def _version_from_ref(ref: str) -> str | None:
    text = ref.rsplit("/", 1)[-1].lstrip("v")
    parts = text.split(".")
    if len(parts) >= 2 and all(part.split("-")[0].isdigit() for part in parts[:3]):
        return text
    return None


# --------------------------------------------------------------------------- #
# 2. Validate
# --------------------------------------------------------------------------- #
def _sidecar_source_dir() -> Path | None:
    """Locate the sidecar project to build a candidate against.

    A source checkout keeps it at <repo>/sidecar; a desktop build has no repo, so
    the sidecar sources are shipped as a bundled resource. Returning None means
    "cannot validate here", which the caller reports as skipped rather than failed.
    """
    candidates: list[Path] = []
    bundled = bundled_sidecar_dir()
    if bundled is not None:
        candidates.append(bundled)
    candidates.append(REPO_DIR / "sidecar")
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    return None


async def _check_npm_available(version: str | None) -> dict[str, Any]:
    """Ask the npm registry whether the candidate version is published."""
    if not version:
        return {"name": "npm_available", "status": "skipped", "detail": "No version on the release row."}
    settings = get_settings()
    package = settings.aragon_npm_package
    url = f"https://registry.npmjs.org/{package.replace('/', '%2F')}/{version}"
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, headers={"accept": "application/json"})
    except (httpx.HTTPError, OSError) as exc:
        return {
            "name": "npm_available",
            "status": "skipped",
            "detail": f"Could not reach the npm registry ({type(exc).__name__}); the version was not verified.",
        }
    if response.status_code == 200:
        return {"name": "npm_available", "status": "passed", "detail": f"{package}@{version} is published."}
    if response.status_code == 404:
        return {
            "name": "npm_available",
            "status": "failed",
            "detail": f"{package}@{version} is not published on npm (HTTP 404). A release cannot be activated from a version that does not exist.",
        }
    return {
        "name": "npm_available",
        "status": "skipped",
        "detail": f"Registry answered HTTP {response.status_code}; inconclusive.",
    }


def _check_adapter_compat(version: str | None) -> dict[str, Any]:
    adapter = _compat()
    parsed = _version_tuple(version)
    if parsed is None:
        return {
            "name": "adapter_compatibility",
            "status": "skipped",
            "detail": "The release has no parseable version, so compatibility could not be evaluated.",
        }
    if parsed < adapter["core_min"]:
        return {
            "name": "adapter_compatibility",
            "status": "failed",
            "detail": (
                f"Core {version} is older than the adapter's minimum "
                f"{'.'.join(str(p) for p in adapter['core_min'])}."
            ),
        }
    if parsed >= adapter["core_max_exclusive"]:
        return {
            "name": "adapter_compatibility",
            "status": "failed",
            "detail": (
                f"Core {version} is outside adapter {adapter_version()}'s tested range "
                f"(< {'.'.join(str(p) for p in adapter['core_max_exclusive'])}). "
                "Update the adapter and its mapping table first, or stay on the current release."
            ),
        }
    return {
        "name": "adapter_compatibility",
        "status": "passed",
        "detail": f"Core {version} is inside adapter {adapter_version()}'s supported range.",
    }


async def _check_sidecar_reports(version: str | None) -> dict[str, Any]:
    adapter = new_adapter()
    health = await adapter.check_health()
    if not health.get("reachable"):
        return {
            "name": "sidecar_reachable",
            "status": "skipped",
            "detail": "The sidecar is not running, so the installed engine version could not be read.",
        }
    installed = (health.get("engine") or {}).get("version")
    if version and installed and installed != version:
        return {
            "name": "sidecar_reachable",
            "status": "passed",
            "detail": (
                f"The sidecar is running {installed}; the candidate is {version}. "
                "Activation will require the sidecar to be reinstalled and restarted."
            ),
        }
    return {
        "name": "sidecar_reachable",
        "status": "passed",
        "detail": f"Sidecar reachable, running engine {installed or 'unknown'}.",
    }


async def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "npm_config_audit": "false", "npm_config_fund": "false"},
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"timed out after {int(timeout)}s"
    return process.returncode or 0, (stdout or b"").decode("utf-8", "replace")


async def _check_isolated_build(version: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy the sidecar, pin the candidate core version, build and test it.

    This is the "isolated build + regression" pair from the contract. It runs in a
    temp directory so the working sidecar is never touched; a candidate that cannot
    build or whose tests fail is rejected before anything is installed for real.
    """
    sidecar_dir = _sidecar_source_dir()
    if sidecar_dir is None:
        skipped = {
            "name": "isolated_build",
            "status": "skipped",
            "detail": f"No sidecar project at {sidecar_dir}, so nothing could be built.",
        }
        return skipped, {"name": "regression_tests", "status": "skipped", "detail": "No sidecar project."}
    if not version:
        skipped = {
            "name": "isolated_build",
            "status": "skipped",
            "detail": "The release has no version to pin.",
        }
        return skipped, {"name": "regression_tests", "status": "skipped", "detail": "No version to pin."}

    tmp = Path(tempfile.mkdtemp(prefix="sr-engine-validate-"))
    try:
        for item in ("src", "test"):
            source = sidecar_dir / item
            if source.exists():
                shutil.copytree(source, tmp / item)
        for item in ("package.json", "tsconfig.json"):
            shutil.copy2(sidecar_dir / item, tmp / item)
        package_path = tmp / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package.setdefault("dependencies", {})[get_settings().aragon_npm_package] = version
        # The candidate is installed for real, so a build failure is a genuine
        # incompatibility rather than a type-checking artefact of the old tree.
        package.pop("devDependencies", None)
        package["devDependencies"] = {"@types/node": "22.10.2", "typescript": "5.7.2"}
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        npm = shutil.which("npm")
        if not npm:
            skipped = {
                "name": "isolated_build",
                "status": "skipped",
                "detail": "npm is not on PATH, so the candidate could not be built in isolation.",
            }
            return skipped, {"name": "regression_tests", "status": "skipped", "detail": "npm unavailable."}

        code, output = await _run([npm, "install", "--no-audit", "--no-fund"], tmp, BUILD_TIMEOUT_SECONDS)
        if code != 0:
            return (
                {
                    "name": "isolated_build",
                    "status": "failed",
                    "detail": f"npm install failed (exit {code}): {_tail(output)}",
                },
                {"name": "regression_tests", "status": "skipped", "detail": "Install failed."},
            )
        code, output = await _run([npm, "run", "build"], tmp, BUILD_TIMEOUT_SECONDS)
        if code != 0:
            return (
                {
                    "name": "isolated_build",
                    "status": "failed",
                    "detail": f"TypeScript build failed against core {version} (exit {code}): {_tail(output)}",
                },
                {"name": "regression_tests", "status": "skipped", "detail": "Build failed."},
            )
        build = {
            "name": "isolated_build",
            "status": "passed",
            "detail": f"Built the sidecar against core {version} in an isolated copy.",
        }
        code, output = await _run([npm, "test"], tmp, BUILD_TIMEOUT_SECONDS)
        if code != 0:
            return build, {
                "name": "regression_tests",
                "status": "failed",
                "detail": f"The sidecar test suite failed against core {version} (exit {code}): {_tail(output)}",
            }
        return build, {
            "name": "regression_tests",
            "status": "passed",
            "detail": f"The sidecar suite passed against core {version}.",
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return (
            {"name": "isolated_build", "status": "failed", "detail": f"{type(exc).__name__}: {exc}"},
            {"name": "regression_tests", "status": "skipped", "detail": "Build could not run."},
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tail(output: str, limit: int = 900) -> str:
    text = output.strip().splitlines()
    joined = "\n".join(text[-12:])
    return joined[-limit:] if joined else "(no output)"


#: Test seam: tests replace this to avoid running a real npm build.
BuildRunner = Callable[[str | None], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]
_build_runner: BuildRunner = _check_isolated_build


async def validate_release(
    release_id: str, *, actor_id: str | None = None, ip: str | None = None
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM engine_releases WHERE id = ?", (release_id,))
    if not row:
        raise not_found("Engine release")
    if row["status"] == "active":
        raise conflict(
            "already_active",
            "This release is the active one; there is nothing to validate.",
            details={"release_id": release_id},
        )
    if row["status"] in {"superseded", "rolled_back"}:
        raise conflict(
            "release_retired",
            "This release was retired; detect a fresh candidate instead.",
            details={"release_id": release_id, "status": row["status"]},
        )

    db.update("engine_releases", release_id, {"status": "validating"})
    version = row.get("npm_version") or row.get("version")

    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "release_ref",
            "status": "passed" if row.get("commit_sha") else "failed",
            "detail": f"ref={row.get('ref')} commit={row.get('commit_sha')}",
        }
    )
    checks.append(_check_adapter_compat(version))
    checks.append(await _check_npm_available(version))
    checks.append(await _check_sidecar_reports(version))

    build_check, regression_check = await _build_runner(version)
    checks.append(build_check)
    checks.append(regression_check)

    failed = [check for check in checks if check["status"] == "failed"]
    skipped = [check for check in checks if check["status"] == "skipped"]
    status = "failed" if failed else "verified"
    now = db.iso()

    db.update(
        "engine_releases",
        release_id,
        {
            "status": status,
            "checks": db.dumps(checks),
            "verified_at": now if status == "verified" else None,
            "capabilities": db.dumps(capability_matrix()),
            "notes": (
                f"{len(checks) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
                f"{len(skipped)} skipped."
            ),
        },
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="engine.release_validated",
        object_type="engine_release",
        object_id=release_id,
        summary=f"release {row.get('ref')} validation {status}",
        metadata={"checks": checks, "status": status},
        ip=ip,
    )

    if failed:
        # The active release is deliberately untouched: a failed candidate simply
        # has nowhere to go from here.
        active = _active_row()
        raise ApiError(
            "engine_validation_failed",
            f"The candidate failed {len(failed)} check(s) and was not made activatable. "
            "The current release is unchanged.",
            status_code=409,
            details={
                "release_id": release_id,
                "status": status,
                "checks": checks,
                "active_release": _serialize(active) if active else None,
            },
            actions=[
                "Read the failing check's detail — it names the version and the command output.",
                "Stay on the current release, or detect a different candidate.",
            ],
        )

    return {
        "release_id": release_id,
        "status": status,
        "checks": checks,
        "skipped_checks": skipped,
        "note": (
            "Skipped checks did not fail, but they did not prove anything either. "
            "The row is activatable on the strength of the checks that ran."
        ),
        "next_step": f"POST /api/admin/engine/releases/{release_id}/activate",
    }


# --------------------------------------------------------------------------- #
# 3. Activate / roll back
# --------------------------------------------------------------------------- #
async def activate_release(
    release_id: str, *, actor_id: str | None = None, ip: str | None = None
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM engine_releases WHERE id = ?", (release_id,))
    if not row:
        raise not_found("Engine release")
    if row["status"] == "active":
        return {"activated": False, "release": _serialize(row), "note": "Already the active release."}
    if row["status"] != "verified":
        raise conflict(
            "release_not_verified",
            "Only a verified release can be activated.",
            details={"release_id": release_id, "status": row["status"]},
            actions=["POST /api/admin/engine/releases/{id}/validate first."],
        )

    previous = _active_row()
    adapter = new_adapter()

    # Confirm the running sidecar is actually the candidate before recording it as
    # active. Without this the platform would claim a version it is not running.
    health = await adapter.check_health()
    running_version = (health.get("engine") or {}).get("version")
    expected = row.get("npm_version") or row.get("version")
    mismatch = bool(expected) and bool(running_version) and running_version != expected

    if not health.get("ok") or mismatch:
        reason = (
            f"the sidecar is running {running_version}, but this release is {expected}"
            if mismatch and health.get("ok")
            else "the sidecar did not answer its health check"
        )
        db.update(
            "engine_releases",
            release_id,
            {
                "status": "failed",
                "notes": f"Activation refused: {reason}.",
                "previous_release_id": previous["id"] if previous else None,
            },
        )
        audit.record(
            actor_type="user",
            actor_id=actor_id,
            action="engine.activation_refused",
            object_type="engine_release",
            object_id=release_id,
            summary=f"activation refused: {reason}",
            metadata={"running_version": running_version, "expected": expected, "health": health.get("error")},
            ip=ip,
        )
        raise ApiError(
            "engine_activation_refused",
            f"Activation was refused because {reason}. The current release is unchanged.",
            status_code=409,
            details={"release_id": release_id, "running_version": running_version, "expected_version": expected},
            actions=[
                "Install and restart the sidecar with the candidate engine version.",
                "Re-run validation once the sidecar reports the new version on /health.",
            ],
        )

    now = db.iso()
    with db.transaction() as conn:
        if previous:
            conn.execute(
                "UPDATE engine_releases SET status = 'superseded' WHERE id = ?", (previous["id"],)
            )
        conn.execute(
            """UPDATE engine_releases
                  SET status = 'active', activated_at = ?, previous_release_id = ?
                WHERE id = ?""",
            (now, previous["id"] if previous else None, release_id),
        )
    db.set_setting("engine.active_release", {
        "id": release_id,
        "ref": row["ref"],
        "version": row.get("npm_version") or row.get("version"),
        "commit": row["commit_sha"],
        "activated_at": now,
        "adapter_version": adapter_version(),
    })

    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="engine.release_activated",
        object_type="engine_release",
        object_id=release_id,
        summary=f"release {row['ref']} activated (was {previous['ref'] if previous else 'none'})",
        metadata={"previous_release_id": previous["id"] if previous else None, "running_version": running_version},
        ip=ip,
    )
    return {
        "activated": True,
        "release_id": release_id,
        "release": _serialize(db.query_one("SELECT * FROM engine_releases WHERE id = ?", (release_id,))),
        "previous_release_id": previous["id"] if previous else None,
        "sidecar": {"running_version": running_version, "health_ok": True},
        "note": "The active pointer moved only after the sidecar confirmed its version.",
    }


async def rollback(*, actor_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
    """Return to the release that was active before the current one."""
    active = _active_row()
    if not active:
        raise conflict(
            "no_active_release",
            "There is no active release to roll back from.",
            actions=["Run POST /api/admin/engine/check to establish one."],
        )
    target_id = active.get("previous_release_id")
    if not target_id:
        raise conflict(
            "no_previous_release",
            "The active release has no recorded predecessor.",
            details={"active_release_id": active["id"]},
        )
    target = db.query_one("SELECT * FROM engine_releases WHERE id = ?", (target_id,))
    if not target:
        raise conflict(
            "previous_release_missing",
            "The recorded predecessor no longer exists.",
            details={"previous_release_id": target_id},
        )

    adapter = new_adapter()
    health = await adapter.check_health()
    running_version = (health.get("engine") or {}).get("version")
    expected = target.get("npm_version") or target.get("version")

    now = db.iso()
    with db.transaction() as conn:
        conn.execute("UPDATE engine_releases SET status = 'rolled_back' WHERE id = ?", (active["id"],))
        conn.execute(
            "UPDATE engine_releases SET status = 'active', activated_at = ? WHERE id = ?",
            (now, target_id),
        )
    db.set_setting("engine.active_release", {
        "id": target_id,
        "ref": target["ref"],
        "version": expected,
        "commit": target["commit_sha"],
        "activated_at": now,
        "adapter_version": adapter_version(),
    })
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="engine.rolled_back",
        object_type="engine_release",
        object_id=target_id,
        summary=f"rolled back from {active['ref']} to {target['ref']}",
        metadata={"from": active["id"], "to": target_id, "running_version": running_version},
        ip=ip,
    )
    matches = (not expected) or (running_version == expected)
    return {
        "rolled_back": True,
        "release_id": target_id,
        "release": _serialize(db.query_one("SELECT * FROM engine_releases WHERE id = ?", (target_id,))),
        "from_release_id": active["id"],
        "sidecar": {"running_version": running_version, "matches_release": matches},
        "warning": None
        if matches
        else (
            f"The rollback was recorded, but the running sidecar reports {running_version} rather "
            f"than {expected}. Reinstall and restart the sidecar to complete the rollback."
        ),
    }


def record_activation_rollback(release_id: str, reason: str) -> None:
    """Used by tests and by a caller that detects a failed health check later."""
    row = db.query_one("SELECT * FROM engine_releases WHERE id = ?", (release_id,))
    if not row:
        raise not_found("Engine release")
    db.update("engine_releases", release_id, {"status": "rolled_back", "notes": reason})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
