"""双通道版本检测与 provenance 判定（§12.4-1）。"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import EngineCandidate, SystemState
from ..settings import settings

log = get_logger("skillrouter.run")

REPO = "https://github.com/xdrshjr/AragonAgent"
GITHUB_API = "https://api.github.com/repos/xdrshjr/AragonAgent/releases"
NPM_CORE = "https://registry.npmjs.org/@aragon-agent/core"
NPM_CLI = "https://registry.npmjs.org/@aragon-agent/cli"
TAG_RE = re.compile(r"^(core|cli)-v(\d+\.\d+\.\d+)$")


def current_pin() -> str:
    with db_session() as s:
        row = s.get(SystemState, "engine_pin")
        return row.value if row and row.value else settings.engine_pin


def previous_pin() -> str | None:
    with db_session() as s:
        row = s.get(SystemState, "engine_previous_pin")
        return row.value if row and row.value else None


def _git_tags() -> list[dict]:
    """通道 A：git tag（权威来源）。API 失败回退 git ls-remote。"""
    out: list[dict] = []
    if settings.egress_policy == "allow":
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(GITHUB_API, headers={"Accept": "application/vnd.github+json"})
            if response.status_code < 400:
                for release in response.json():
                    tag = release.get("tag_name") or ""
                    match = TAG_RE.match(tag)
                    if not match or match.group(1) != "core":
                        continue
                    out.append(
                        {
                            "version": match.group(2),
                            "tag": tag,
                            "published_at": release.get("published_at"),
                            "notes": (release.get("body") or "")[:1000],
                        }
                    )
        except httpx.HTTPError as exc:
            log.info("engine_detect_github_failed", extra={"detail": redact(str(exc), max_len=200)})
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", REPO],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            match = TAG_RE.search(parts[1])
            if not match or match.group(1) != "core":
                continue
            version = match.group(2)
            if any(item["version"] == version for item in out):
                continue
            out.append({"version": version, "tag": match.group(1), "commit": None, "published_at": None})
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return out


def _npm_versions() -> dict:
    """通道 B：npm dist-tags + integrity。"""
    if settings.egress_policy != "allow":
        return {}
    try:
        with httpx.Client(timeout=10.0) as client:
            core = client.get(NPM_CORE).json()
            cli = client.get(NPM_CLI).json()
        core_latest = (core.get("dist-tags") or {}).get("latest")
        cli_latest = (cli.get("dist-tags") or {}).get("latest")
        integrity = (((core.get("versions") or {}).get(core_latest) or {}).get("dist") or {}).get("integrity")
        return {
            "core_latest": core_latest,
            "cli_latest": cli_latest,
            "integrity": integrity,
            "published_at": (((core.get("time") or {}).get(core_latest)) or None),
        }
    except (httpx.HTTPError, ValueError) as exc:
        log.info("engine_detect_npm_failed", extra={"detail": redact(str(exc), max_len=200)})
        return {}


def compare(a: str, b: str) -> int:
    def parts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in (v.split("-")[0].split(".") + ["0", "0", "0"])[:3])
        except ValueError:
            return (0, 0, 0)

    pa, pb = parts(a), parts(b)
    return (pa > pb) - (pa < pb)


def check(*, triggered_by: str | None = None) -> dict:
    pin = current_pin()
    pin_version = pin.split("@", 1)[0]
    tags = _git_tags()
    npm = _npm_versions()
    found: list[dict] = []
    seen: set[str] = set()
    for tag in tags:
        if compare(tag["version"], pin_version) <= 0:
            continue
        found.append(
            {
                "version": tag["version"],
                "commit_sha": tag.get("commit"),
                "provenance": "tagged",
                "published_at": tag.get("published_at"),
                "notes": tag.get("notes") or f"git tag {tag['tag']}",
                "integrity": None,
            }
        )
        seen.add(tag["version"])
    npm_latest = npm.get("core_latest")
    if npm_latest and compare(npm_latest, pin_version) > 0 and npm_latest not in seen:
        found.append(
            {
                "version": npm_latest,
                "commit_sha": None,
                "provenance": "npm-only",
                "published_at": npm.get("published_at"),
                "notes": "上游无对应 git tag，需隔离构建与完整性校验后才可部署",
                "integrity": npm.get("integrity"),
            }
        )
    if settings.engine_channel.startswith("branch:"):
        found.append(
            {
                "version": f"branch-{settings.engine_channel.split(':', 1)[1]}",
                "commit_sha": None,
                "provenance": "branch-commit",
                "published_at": now_iso(),
                "notes": f"跟随分支 {settings.engine_channel}，锁定 ls-remote 的 commit",
                "integrity": None,
            }
        )
    created = []
    with db_write() as s:
        for item in found:
            existing = s.scalar(
                select(EngineCandidate).where(
                    EngineCandidate.version == item["version"],
                    EngineCandidate.provenance == item["provenance"],
                )
            )
            if existing:
                continue
            candidate_id = uuid_pk("egc")
            s.add(
                EngineCandidate(
                    id=candidate_id,
                    version=item["version"],
                    commit_sha=item["commit_sha"],
                    provenance=item["provenance"],
                    published_at=item["published_at"],
                    channel=settings.engine_channel,
                    notes=item["notes"],
                    detected_at=now_iso(),
                    verification_status="pending",
                    verification_report={},
                )
            )
            created.append(item["version"])
    result = {
        "current": pin,
        "channel": settings.engine_channel,
        "egress_policy": settings.egress_policy,
        "detected": found,
        "created": created,
        "npm": npm,
        "git_tag_count": len(tags),
        "checked_at": now_iso(),
        "note": (
            "SKILLROUTER_EGRESS_POLICY=deny，已跳过出站检测；仅支持手动上传 tarball 升级"
            if settings.egress_policy == "deny"
            else None
        ),
    }
    log.info("engine_check", extra={"detail": {"created": created, "current": pin}})
    return result


def list_candidates() -> list[dict]:
    with db_session() as s:
        rows = list(s.scalars(select(EngineCandidate).order_by(EngineCandidate.detected_at.desc())))
        return [
            {
                "id": r.id,
                "version": r.version,
                "commit_sha": r.commit_sha,
                "provenance": r.provenance,
                "published_at": r.published_at,
                "channel": r.channel,
                "notes": r.notes,
                "detected_at": r.detected_at,
                "verification_status": r.verification_status,
                "verification_report": r.verification_report or {},
            }
            for r in rows
        ]


def due_for_check() -> bool:
    interval = timedelta(hours=settings.engine_check_interval_hours)
    with db_session() as s:
        row = s.get(SystemState, "engine_last_check")
        if row is None or not row.value:
            return True
        try:
            last = datetime.fromisoformat(row.value.replace("Z", "+00:00"))
        except ValueError:
            return True
    return datetime.now(UTC) - last >= interval


def mark_checked() -> None:
    from ..db.seed import set_state

    with db_write() as s:
        set_state(s, "engine_last_check", now_iso())


def settings_note() -> dict:
    return {
        "egress_policy": settings.egress_policy,
        "engine_channel": settings.engine_channel,
        "engine_pin": current_pin(),
        "engine_previous_pin": previous_pin(),
        "adapter_version": "1",
    }


__all__ = ["check", "list_candidates", "current_pin", "previous_pin", "compare", "due_for_check", "mark_checked", "settings_note", "Code", "ApiError"]
