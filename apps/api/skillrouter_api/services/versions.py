"""不可变版本行、content_hash 与版本 diff（D8 / §10.2）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..db import now_iso, uuid_pk
from ..errors import ApiError, Code


def content_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def next_version(latest: str | None) -> str:
    if not latest:
        return "1.0.0"
    parts = latest.split(".")
    try:
        major, minor, patch = (int(p) for p in (parts + ["0", "0", "0"])[:3])
    except ValueError:
        return "1.0.0"
    return f"{major}.{minor}.{patch + 1}"


def bump_major(latest: str | None) -> str:
    if not latest:
        return "1.0.0"
    try:
        major = int(latest.split(".")[0])
    except ValueError:
        major = 0
    return f"{major + 1}.0.0"


def version_tuple(version: str) -> tuple[int, int, int]:
    parts = (version or "0.0.0").split(".")
    out = []
    for part in (parts + ["0", "0", "0"])[:3]:
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)  # type: ignore[return-value]


def in_range(version: str, spec: str | None) -> bool:
    """支持 `1.2.x` / `>=1.2.0 <2.0.0` / `*` / 精确版本。"""
    if not spec or spec == "*":
        return True
    spec = spec.strip()
    if spec.startswith(">="):
        parts = spec.split()
        ok = version_tuple(version) >= version_tuple(parts[0][2:])
        if len(parts) > 1 and parts[1].startswith("<"):
            ok = ok and version_tuple(version) < version_tuple(parts[1][1:])
        return ok
    if spec.endswith(".x"):
        prefix = spec[:-2]
        return version.startswith(prefix + ".")
    return version == spec


def assert_mutable(row: Any) -> None:
    if getattr(row, "status", None) == "published":
        raise ApiError(
            Code.VERSION_IMMUTABLE,
            message="已发布版本不可修改，请创建新版本",
            details={"version_id": getattr(row, "id", None)},
        )


def freeze_capability(
    session,  # noqa: ANN001
    *,
    capability_id: str,
    version: str,
    definition: dict,
    changelog: str | None,
    actor: str | None,
    test_run_ids: list[str] | None = None,
    status: str = "published",
) -> str:
    from ..models import CapabilityVersion

    version_id = uuid_pk("cpv")
    session.add(
        CapabilityVersion(
            id=version_id,
            capability_id=capability_id,
            version=version,
            content_hash=content_hash(definition),
            definition=definition,
            status=status,
            published_at=now_iso() if status == "published" else None,
            published_by=actor,
            changelog=changelog,
            test_run_ids=test_run_ids or [],
            created_at=now_iso(),
        )
    )
    return version_id


def freeze_skill(
    session,  # noqa: ANN001
    *,
    skill_id: str,
    version: str,
    skill_md: str,
    frontmatter: dict,
    execution_plan: dict,
    dependencies: list,
    permissions: dict,
    examples: list,
    status: str = "published",
) -> str:
    from ..models import SkillVersion

    version_id = uuid_pk("skv")
    session.add(
        SkillVersion(
            id=version_id,
            skill_id=skill_id,
            version=version,
            content_hash=content_hash(
                {
                    "skill_md": skill_md,
                    "execution_plan": execution_plan,
                    "dependencies": dependencies,
                }
            ),
            skill_md=skill_md,
            frontmatter=frontmatter,
            execution_plan=execution_plan,
            dependencies=dependencies,
            permissions=permissions,
            examples=examples,
            status=status,
            published_at=now_iso() if status == "published" else None,
            created_at=now_iso(),
        )
    )
    return version_id


def diff(a: Any, b: Any, path: str = "") -> list[dict]:
    """字段级结构化 diff，返回 `[{path, change, from, to}]`。"""
    changes: list[dict] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{path}.{key}" if path else key
            if key not in a:
                changes.append({"path": child, "change": "added", "from": None, "to": b[key]})
            elif key not in b:
                changes.append({"path": child, "change": "removed", "from": a[key], "to": None})
            else:
                changes.extend(diff(a[key], b[key], child))
        return changes
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            changes.append({"path": path, "change": "length", "from": len(a), "to": len(b)})
        for index, (left, right) in enumerate(zip(a, b)):
            changes.extend(diff(left, right, f"{path}[{index}]"))
        return changes
    if a != b:
        changes.append({"path": path, "change": "changed", "from": a, "to": b})
    return changes
