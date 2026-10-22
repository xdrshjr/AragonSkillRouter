"""SKILLs: versioned, validated execution plans built on published capabilities.

A SKILL is *not* code. It is a declarative plan (see docs/CONTRACT.md §5.7) plus
instructions written for an external agent. Every plan step is one of:

    capability  -> call a published capability through `execution.invoke`
    transform   -> a limited declarative mapping between steps (no eval)
    agent       -> live reasoning, which only the AragonAgent engine can do

The third kind is deliberately *not* simulated: a plan that needs live agent
reasoning fails with `409 engine_required` and says so, because pretending to
reason would silently produce a worthless result.

Publishing is the point where a SKILL becomes real, so it is also where the
dependency gate lives: every capability the plan touches must resolve to a
published capability version, and the manifest must have no blocking issue.
Published versions are immutable.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.security.redact import preview, truncate
from app.security.tokens import slugify
from app.services import audit, capability_config

SKILL_STATUSES = ("draft", "published", "disabled")
VERSION_STATUSES = ("draft", "published")
PLAN_KINDS = ("capability", "agent", "transform")
REQUIRED_MANIFEST_KEYS = ("name", "description", "instructions", "plan")
MAX_PLAN_STEPS = 24
MAX_TRANSFORM_KEYS = 24
RESULT_PREVIEW_LIMIT = 800

STARTER_INSTRUCTIONS = (
    "# {name}\n\n"
    "Describe here, for an external agent, when to use this SKILL and in which order "
    "to run its steps. Keep provider secrets out of this document: the platform "
    "injects credentials at call time.\n"
)


# --------------------------------------------------------------------- helpers --
def _now() -> str:
    return db.iso()


def _manifest_of(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = row.get("manifest")
    parsed = raw if isinstance(raw, dict) else db.loads(raw, {})
    return parsed if isinstance(parsed, dict) else {}


def _unique_slug(base: str, *, exclude_id: str | None = None) -> str:
    slug = slugify(base or "skill", fallback="skill")[:64]
    candidate = slug
    suffix = 1
    while db.query_scalar(
        "SELECT 1 FROM skills WHERE slug = ? AND id != ?", (candidate, exclude_id or "")
    ):
        suffix += 1
        candidate = f"{slug}-{suffix}"[:80]
    return candidate


def _unique_version_label(skill_id: str, requested: str | None) -> str:
    existing = {
        str(row["version"])
        for row in db.query_all("SELECT version FROM skill_versions WHERE skill_id = ?", (skill_id,))
    }
    label = str(requested).strip() if requested not in (None, "") else ""
    if label:
        if label in existing:
            raise conflict(
                "version_exists",
                f"Version {label} already exists for this SKILL.",
                actions=["Pick a different version label.", "Or edit the existing draft."],
            )
        return label
    numeric = [int(v) for v in existing if v.isdigit()]
    return str(max(numeric) + 1 if numeric else len(existing) + 1)


def _capability_rows() -> dict[str, dict[str, Any]]:
    return {row["slug"]: row for row in db.query_all("SELECT * FROM capabilities")}


def _published_versions(capability_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT id, version, status, published_at FROM capability_versions "
        "WHERE capability_id = ? AND status = 'published' ORDER BY version DESC",
        (capability_id,),
    )


def _step_id(step: dict[str, Any], index: int) -> str:
    raw = step.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"step{index + 1}"


# ------------------------------------------------------------------ validation --
def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Structural + dependency validation of a SKILL manifest.

    Returns ``{"issues": [{severity, field, message}], "blocking": bool}``.
    Only *blocking* issues prevent publishing; a draft may be saved invalid so the
    author can see exactly which field is wrong.
    """
    issues: list[dict[str, Any]] = []

    def error(field: str, message: str) -> None:
        issues.append({"severity": "error", "field": field, "message": message})

    def warn(field: str, message: str) -> None:
        issues.append({"severity": "warning", "field": field, "message": message})

    if not isinstance(manifest, dict):
        return {
            "issues": [{"severity": "error", "field": "manifest", "message": "The manifest must be a JSON object."}],
            "blocking": True,
        }

    for key in REQUIRED_MANIFEST_KEYS:
        if key == "plan":
            continue
        value = manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            error(key, f"'{key}' is required and must be a non-empty string.")

    plan = manifest.get("plan")
    if plan is None:
        error("plan", "'plan' is required (a list of steps).")
        plan = []
    elif not isinstance(plan, list):
        error("plan", "'plan' must be a list of steps.")
        plan = []
    if isinstance(plan, list) and len(plan) > MAX_PLAN_STEPS:
        error("plan", f"A plan may have at most {MAX_PLAN_STEPS} steps.")

    capabilities = _capability_rows()
    seen_ids: set[str] = set()

    for index, step in enumerate(plan if isinstance(plan, list) else []):
        field = f"plan[{index}]"
        if not isinstance(step, dict):
            error(field, "Each plan step must be an object.")
            continue
        raw_id = step.get("id")
        step_id = _step_id(step, index)
        if not isinstance(raw_id, str) or not raw_id.strip():
            warn(f"{field}.id", "Give the step an explicit id so later steps can reference its result.")
        if step_id in seen_ids:
            error(f"{field}.id", f"Duplicate step id '{step_id}'.")
        seen_ids.add(step_id)

        kind = step.get("kind")
        if kind not in PLAN_KINDS:
            error(f"{field}.kind", f"'kind' must be one of {', '.join(PLAN_KINDS)}; got {kind!r}.")
            continue

        if kind == "capability":
            slug = step.get("capability")
            if not isinstance(slug, str) or not slug.strip():
                error(f"{field}.capability", "A capability step must name the capability slug to call.")
                continue
            capability = capabilities.get(slug.strip())
            if not capability:
                error(f"{field}.capability", f"Unknown capability '{slug}'.")
                continue
            published = _published_versions(capability["id"])
            requested = step.get("version")
            if requested not in (None, ""):
                match = next((v for v in published if str(v["version"]) == str(requested)), None)
                if match is None:
                    error(
                        f"{field}.version",
                        f"'{slug}' has no published version {requested}.",
                    )
            elif not published:
                error(
                    f"{field}.capability",
                    f"'{slug}' has no published version, so the step cannot run.",
                )
            elif capability.get("status") != "published":
                warn(
                    f"{field}.capability",
                    f"'{slug}' is {capability.get('status')} even though a published version exists.",
                )
            if "input" in step and step["input"] is not None and not isinstance(step["input"], (dict, str)):
                error(f"{field}.input", "Step input must be an object of templated values.")

        elif kind == "agent":
            instruction = step.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                error(f"{field}.instruction", "An agent step needs an instruction for the live agent.")

        elif kind == "transform":
            mapping = step.get("map", step.get("set"))
            if not isinstance(mapping, dict) or not mapping:
                error(f"{field}.map", "A transform step needs a 'map' object.")
            elif len(mapping) > MAX_TRANSFORM_KEYS:
                error(f"{field}.map", f"A transform step may set at most {MAX_TRANSFORM_KEYS} keys.")
            else:
                for key, value in mapping.items():
                    if not isinstance(key, str) or not key.strip():
                        error(f"{field}.map", "Transform keys must be non-empty strings.")
                    if isinstance(value, str) and ("__" in value and "import" in value):
                        error(f"{field}.map", "Transform values are templates only; running code is not allowed.")

        if step.get("on_error") not in (None, "stop", "continue"):
            error(f"{field}.on_error", "'on_error' must be 'stop' or 'continue'.")

    dependencies = manifest.get("dependencies")
    if dependencies is None:
        dependencies = []
    if not isinstance(dependencies, list):
        error("dependencies", "'dependencies' must be a list of {capability, version?} entries.")
        dependencies = []
    for index, dep in enumerate(dependencies if isinstance(dependencies, list) else []):
        field = f"dependencies[{index}]"
        if not isinstance(dep, dict):
            error(field, "Each dependency must be an object with a 'capability' slug.")
            continue
        slug = dep.get("capability")
        if not isinstance(slug, str) or not slug.strip():
            error(f"{field}.capability", "A dependency must name a capability slug.")
            continue
        capability = capabilities.get(slug.strip())
        if not capability:
            error(f"{field}.capability", f"Unknown capability '{slug}'.")
            continue
        published = _published_versions(capability["id"])
        requested = dep.get("version")
        if requested not in (None, ""):
            if not any(str(v["version"]) == str(requested) for v in published):
                error(f"{field}.version", f"'{slug}' version {requested} is not published.")
        elif not published:
            error(
                f"{field}.capability",
                f"'{slug}' has no published version, so the SKILL cannot depend on it.",
            )
        if dep.get("required") is not True and dep.get("required") is not None:
            warn(f"{field}.required", "Use true/false for 'required'.")

    permissions = manifest.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        error("permissions", "'permissions' must be an object.")
    elif isinstance(permissions, dict):
        for key in ("network", "files", "credentials"):
            value = permissions.get(key)
            if value is not None and not isinstance(value, list):
                error(f"permissions.{key}", f"'permissions.{key}' must be a list.")

    if not manifest.get("examples"):
        warn("examples", "Add at least one example input; the external agent uses it as documentation.")
    if not manifest.get("when_to_use"):
        warn("when_to_use", "Explain when an agent should choose this SKILL.")
    if any(isinstance(step, dict) and step.get("kind") == "agent" for step in (plan or [])):
        warn(
            "plan",
            "This plan contains agent steps, so running it requires the AragonAgent engine.",
        )
    if not any(isinstance(step, dict) and step.get("kind") == "capability" for step in (plan or [])):
        warn("plan", "This plan calls no capability; it may not do anything on its own.")

    return {"issues": issues, "blocking": capability_config.has_blocking_issues(issues)}


def _require_valid_structure(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise unprocessable(
            "validation_failed",
            "The manifest must be a JSON object.",
            details=[{"field": "manifest", "message": "Expected an object."}],
        )
    problems: list[dict[str, str]] = []
    for key in REQUIRED_MANIFEST_KEYS:
        value = manifest.get(key)
        if key == "plan":
            if not isinstance(value, list):
                problems.append({"field": "plan", "message": "'plan' is required and must be a list."})
        elif not isinstance(value, str) or not value.strip():
            problems.append({"field": key, "message": f"'{key}' is required and must be a non-empty string."})
    if problems:
        raise unprocessable(
            "validation_failed",
            "The SKILL manifest is missing required fields.",
            details=problems,
            actions=["Fill in name, description, instructions and a plan."],
        )
    return manifest


def normalise_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Fill the optional sections so stored manifests have a stable shape."""
    out = dict(manifest)
    out.setdefault("when_to_use", "")
    out.setdefault("inputs", {"type": "object", "properties": {}})
    out.setdefault("outputs", {"type": "object", "properties": {}})
    out.setdefault("dependencies", [])
    out.setdefault("permissions", {})
    out.setdefault("examples", [])
    out.setdefault("sources", [])
    plan = []
    for index, step in enumerate(out.get("plan") or []):
        if not isinstance(step, dict):
            plan.append(step)
            continue
        item = dict(step)
        item["id"] = _step_id(item, index)
        item.setdefault("on_error", "stop")
        item["requires_live_agent"] = item.get("kind") == "agent"
        plan.append(item)
    out["plan"] = plan
    return out


def checksum(manifest: dict[str, Any]) -> str:
    return capability_config.config_checksum(manifest)


# -------------------------------------------------------------------- CRUD -----
def create_skill(
    *,
    name: str,
    description: str = "",
    category: str = "general",
    slug: str | None = None,
    source_type: str = "manual",
    source_ref: str | None = None,
    manifest: dict[str, Any] | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise unprocessable("validation_failed", "A SKILL needs a name.", details=[{"field": "name", "message": "Required."}])
    now = _now()
    skill_id = db.new_id("skill")
    final_slug = _unique_slug(slug or name)
    db.insert(
        "skills",
        {
            "id": skill_id,
            "slug": final_slug,
            "name": name[:160],
            "description": (description or "").strip()[:2000],
            "category": (category or "general").strip()[:60] or "general",
            "status": "draft",
            "source_type": source_type if source_type in {"manual", "git", "web", "file"} else "manual",
            "source_ref": source_ref,
            "current_version_id": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    payload = normalise_manifest(
        manifest
        if isinstance(manifest, dict)
        else {
            "name": name,
            "description": description or "",
            "instructions": STARTER_INSTRUCTIONS.format(name=name),
            "plan": [],
        }
    )
    payload.setdefault("name", name)
    payload.setdefault("description", description or "")
    if not str(payload.get("instructions") or "").strip():
        payload["instructions"] = STARTER_INSTRUCTIONS.format(name=name)
    version_id = db.new_id("skver")
    db.insert(
        "skill_versions",
        {
            "id": version_id,
            "skill_id": skill_id,
            "version": "1",
            "status": "draft",
            "manifest": db.dumps(payload),
            "checksum": checksum(payload),
            "created_at": now,
            "published_at": None,
        },
    )
    db.update("skills", skill_id, {"current_version_id": version_id, "updated_at": now})
    audit.record(
        actor_type="user",
        action="skill.create",
        object_type="skill",
        object_id=skill_id,
        actor_id=actor_id,
        summary=f"created SKILL {name[:80]}",
        metadata={"slug": final_slug, "source_type": source_type},
        ip=ip,
    )
    return get_skill(skill_id)


def get_skill(skill_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM skills WHERE id = ? OR slug = ?", (skill_id, skill_id))
    if not row:
        raise not_found("SKILL")
    return row


def list_skills(
    *,
    status: str | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append("category = ?")
        params.append(category)
    if search:
        where.append("(name LIKE ? OR slug LIKE ? OR description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM skills WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM skills WHERE {clause} ORDER BY created_at DESC, name ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return rows, int(total)


def update_skill(
    skill_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    category: str | None = None,
    status: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    skill = get_skill(skill_id)
    values: dict[str, Any] = {}
    if name is not None:
        if not name.strip():
            raise unprocessable("validation_failed", "The name cannot be empty.", details=[{"field": "name", "message": "Required."}])
        values["name"] = name.strip()[:160]
    if description is not None:
        values["description"] = description.strip()[:2000]
    if category is not None:
        values["category"] = category.strip()[:60] or "general"
    if status is not None:
        if status not in SKILL_STATUSES:
            raise unprocessable(
                "validation_failed",
                f"Unknown SKILL status {status!r}.",
                details=[{"field": "status", "message": f"Use one of {', '.join(SKILL_STATUSES)}."}],
            )
        if status == "published":
            current = current_version(skill)
            if not current or current["status"] != "published":
                raise conflict(
                    "version_not_published",
                    "Publish a version before marking the SKILL published.",
                    actions=["Open the version and press Publish.", "Or keep the SKILL as a draft."],
                )
        values["status"] = status
    if not values:
        return skill
    values["updated_at"] = _now()
    db.update("skills", skill["id"], values)
    audit.record(
        actor_type="user",
        action="skill.update",
        object_type="skill",
        object_id=skill["id"],
        actor_id=actor_id,
        summary=f"updated SKILL fields: {', '.join(sorted(values))}",
        metadata={"changes": {k: v for k, v in values.items() if k != "updated_at"}},
        ip=ip,
    )
    return get_skill(skill["id"])


def delete_skill(skill_id: str, *, actor_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
    skill = get_skill(skill_id)
    db.execute("DELETE FROM skills WHERE id = ?", (skill["id"],))
    audit.record(
        actor_type="user",
        action="skill.delete",
        object_type="skill",
        object_id=skill["id"],
        actor_id=actor_id,
        summary=f"deleted SKILL {skill['name'][:80]}",
        metadata={"slug": skill["slug"], "status": skill["status"]},
        ip=ip,
    )
    return {"ok": True, "id": skill["id"], "slug": skill["slug"]}


# ----------------------------------------------------------------- versions ----
def list_versions(skill_id: str) -> list[dict[str, Any]]:
    skill = get_skill(skill_id)
    return db.query_all(
        "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY created_at DESC", (skill["id"],)
    )


def get_version(skill_id: str, version_id: str) -> dict[str, Any]:
    skill = get_skill(skill_id)
    row = db.query_one(
        "SELECT * FROM skill_versions WHERE id = ? AND skill_id = ?", (version_id, skill["id"])
    )
    if not row:
        raise not_found("SKILL version")
    return row


def current_version(skill: dict[str, Any]) -> dict[str, Any] | None:
    if skill.get("current_version_id"):
        row = db.query_one("SELECT * FROM skill_versions WHERE id = ?", (skill["current_version_id"],))
        if row:
            return row
    return db.query_one(
        "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY created_at DESC LIMIT 1", (skill["id"],)
    )


def add_version(
    skill_id: str,
    manifest: dict[str, Any],
    version: str | None = None,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    skill = get_skill(skill_id)
    _require_valid_structure(manifest)
    payload = normalise_manifest(manifest)
    label = _unique_version_label(skill["id"], version)
    now = _now()
    version_id = db.new_id("skver")
    db.insert(
        "skill_versions",
        {
            "id": version_id,
            "skill_id": skill["id"],
            "version": label,
            "status": "draft",
            "manifest": db.dumps(payload),
            "checksum": checksum(payload),
            "created_at": now,
            "published_at": None,
        },
    )
    db.update("skills", skill["id"], {"current_version_id": version_id, "updated_at": now})
    audit.record(
        actor_type="user",
        action="skill.version_create",
        object_type="skill",
        object_id=skill["id"],
        actor_id=actor_id,
        summary=f"created version {label}",
        metadata={"version_id": version_id, "checksum": checksum(payload)},
        ip=ip,
    )
    return get_version(skill["id"], version_id)


def update_version(
    skill_id: str,
    version_id: str,
    manifest: dict[str, Any],
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    skill = get_skill(skill_id)
    row = get_version(skill["id"], version_id)
    if row["status"] == "published":
        raise conflict(
            "version_immutable",
            "This version is published and can no longer be edited.",
            details={"version_id": row["id"], "version": row["version"]},
            actions=["Create a new version instead.", "Published versions stay reproducible."],
        )
    _require_valid_structure(manifest)
    payload = normalise_manifest(manifest)
    db.update(
        "skill_versions",
        row["id"],
        {"manifest": db.dumps(payload), "checksum": checksum(payload)},
    )
    db.update("skills", skill["id"], {"updated_at": _now()})
    audit.record(
        actor_type="user",
        action="skill.version_update",
        object_type="skill_version",
        object_id=row["id"],
        actor_id=actor_id,
        summary=f"edited version {row['version']}",
        metadata={"skill_id": skill["id"], "checksum": checksum(payload)},
        ip=ip,
    )
    return get_version(skill["id"], row["id"])


def publish_version(
    skill_id: str,
    version_id: str,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    skill = get_skill(skill_id)
    row = get_version(skill["id"], version_id)
    if row["status"] == "published":
        raise conflict(
            "version_immutable",
            "This version is already published; published versions cannot change.",
            details={"version_id": row["id"], "version": row["version"]},
            actions=["Create a new version to make changes."],
        )
    manifest = _manifest_of(row)
    validation = validate_manifest(manifest)
    if validation["blocking"]:
        raise unprocessable(
            "validation_failed",
            "This SKILL cannot be published: its dependencies do not resolve to published capability versions.",
            details={"issues": validation["issues"], "blocking": True},
            actions=[
                "Publish the missing capability versions.",
                "Or remove the unresolved dependency from the manifest.",
            ],
        )
    now = _now()
    db.update("skill_versions", row["id"], {"status": "published", "published_at": now})
    db.update(
        "skills",
        skill["id"],
        {"status": "published", "current_version_id": row["id"], "updated_at": now},
    )
    audit.record(
        actor_type="user",
        action="skill.publish",
        object_type="skill",
        object_id=skill["id"],
        actor_id=actor_id,
        summary=f"published SKILL version {row['version']}",
        metadata={"version_id": row["id"], "checksum": row["checksum"]},
        ip=ip,
    )
    return serialise(get_skill(skill["id"]))


# -------------------------------------------------------------------- impact ---
def _manifest_capability_refs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map capability slug -> {requested_versions, dependency_fields, step_ids}."""
    refs: dict[str, dict[str, Any]] = {}
    for index, dep in enumerate(manifest.get("dependencies") or []):
        if not isinstance(dep, dict):
            continue
        slug = str(dep.get("capability") or "").strip()
        if not slug:
            continue
        entry = refs.setdefault(slug, {"requested_versions": [], "fields": [], "step_ids": []})
        if dep.get("version") not in (None, ""):
            entry["requested_versions"].append(str(dep["version"]))
        entry["fields"].append(f"dependencies[{index}]")
    for index, step in enumerate(manifest.get("plan") or []):
        if not isinstance(step, dict) or step.get("kind") != "capability":
            continue
        slug = str(step.get("capability") or "").strip()
        if not slug:
            continue
        entry = refs.setdefault(slug, {"requested_versions": [], "fields": [], "step_ids": []})
        if step.get("version") not in (None, ""):
            entry["requested_versions"].append(str(step["version"]))
        entry["step_ids"].append(_step_id(step, index))
        entry["fields"].append(f"plan[{index}]")
    return refs


def dependency_impact(skill_id: str, *, version_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """What breaks for this SKILL if a capability version is depublished/deleted."""
    skill = get_skill(skill_id)
    row = version_row or current_version(skill)
    manifest = _manifest_of(row)
    refs = _manifest_capability_refs(manifest)
    capabilities = _capability_rows()
    published_skillers = db.query_all(
        """SELECT s.id, s.slug, s.name, v.manifest
             FROM skills s JOIN skill_versions v ON v.id = s.current_version_id
            WHERE s.status = 'published' AND s.id != ?""",
        (skill["id"],),
    )
    impact: list[dict[str, Any]] = []
    for slug, info in sorted(refs.items()):
        capability = capabilities.get(slug)
        published = _published_versions(capability["id"]) if capability else []
        others = []
        for other in published_skillers:
            other_refs = _manifest_capability_refs(_manifest_of({"manifest": other.get("manifest")}))
            if slug in other_refs:
                others.append({"id": other["id"], "slug": other["slug"], "name": other["name"]})
        resolvable = bool(published)
        if resolvable and info["requested_versions"]:
            resolvable = any(
                str(v["version"]) in info["requested_versions"] for v in published
            )
        impact.append(
            {
                "capability": slug,
                "capability_id": capability["id"] if capability else None,
                "capability_name": capability["name"] if capability else None,
                "capability_status": capability["status"] if capability else None,
                "exists": capability is not None,
                "requested_versions": sorted(set(info["requested_versions"])),
                "published_versions": [
                    {"id": v["id"], "version": v["version"], "published_at": v["published_at"]}
                    for v in published
                ],
                "fields": info["fields"],
                "step_ids": info["step_ids"],
                "resolvable": resolvable,
                "used_by_published_skills": others,
                "blocking": not resolvable,
            }
        )
    blocking = any(item["blocking"] for item in impact)
    return {
        "skill_id": skill["id"],
        "skill_slug": skill["slug"],
        "skill_status": skill["status"],
        "version_id": row["id"] if row else None,
        "version": str(row["version"]) if row else None,
        "version_status": row["status"] if row else None,
        "impact": impact,
        "blocking": blocking,
        "summary": (
            "All dependencies resolve to published capability versions."
            if impact and not blocking
            else "No capability dependencies."
            if not impact
            else "Publishing or running would fail: at least one capability has no published version."
        ),
    }


# --------------------------------------------------------------- serialisation --
def serialise_plan(plan: Iterable[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, step in enumerate(plan or []):
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        item = {
            "id": _step_id(step, index),
            "kind": kind,
            "on_error": step.get("on_error", "stop"),
            "save_as": step.get("save_as"),
            "note": step.get("note"),
            # 'agent' steps are the only kind that needs live reasoning.
            "requires_live_agent": kind == "agent",
        }
        if kind == "capability":
            item["capability"] = step.get("capability")
            item["version"] = step.get("version")
        elif kind == "agent":
            item["instruction"] = step.get("instruction")
        elif kind == "transform":
            item["map"] = step.get("map") or step.get("set") or {}
        out.append(item)
    return out


def serialise_version(row: dict[str, Any] | None, *, include_manifest: bool = True, include_validation: bool = True) -> dict[str, Any] | None:
    if not row:
        return None
    manifest = _manifest_of(row)
    validation = validate_manifest(manifest) if include_validation else None
    payload: dict[str, Any] = {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "version": str(row["version"]),
        "status": row["status"],
        "checksum": row["checksum"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "immutable": row["status"] == "published",
        "dependencies": manifest.get("dependencies") or [],
        "plan": serialise_plan(manifest.get("plan")),
        "requires_live_agent": any(
            isinstance(step, dict) and step.get("kind") == "agent" for step in (manifest.get("plan") or [])
        ),
        "step_count": len(manifest.get("plan") or []),
    }
    if include_validation and validation is not None:
        payload["validation"] = validation
        payload["can_publish"] = row["status"] != "published" and not validation["blocking"]
    if include_manifest:
        payload["manifest"] = manifest
    return payload


def serialise(
    skill: dict[str, Any] | str,
    *,
    include_versions: bool = True,
    include_impact: bool = True,
    include_manifest: bool = True,
) -> dict[str, Any]:
    """Public SKILL shape used by every route and by export/import callers."""
    row = get_skill(skill) if isinstance(skill, str) else skill
    versions = list_versions(row["id"]) if include_versions else []
    current = current_version(row)
    published = next((v for v in versions if v["status"] == "published"), None)
    if published is None and current is not None and current["status"] == "published":
        published = current
    payload: dict[str, Any] = {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "status": row["status"],
        "source": {"type": row["source_type"], "ref": row["source_ref"]},
        "current_version_id": row["current_version_id"],
        "current_version": serialise_version(
            current, include_manifest=include_manifest, include_validation=True
        ),
        "version_count": int(
            db.query_scalar("SELECT COUNT(*) FROM skill_versions WHERE skill_id = ?", (row["id"],)) or 0
        ),
        "latest_published_version": serialise_version(
            published, include_manifest=False, include_validation=False
        ),
        "requires_live_agent": any(
            isinstance(step, dict) and step.get("kind") == "agent"
            for step in (_manifest_of(current).get("plan") or [])
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_versions:
        payload["versions"] = [
            serialise_version(v, include_manifest=include_manifest, include_validation=True)
            for v in versions
        ]
    if include_impact:
        payload["dependency_impact"] = dependency_impact(row["id"], version_row=current)
    return payload


# -------------------------------------------------------------- execution ------
def _empty_usage() -> dict[str, Any]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}


def _accumulate_usage(total: dict[str, Any], usage: dict[str, Any] | None, cost: dict[str, Any] | None) -> None:
    usage = usage or {}
    total["calls"] = int(total["calls"]) + 1
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total[key] = int(total.get(key, 0)) + int(value)
    total["total_tokens"] = max(
        int(total.get("total_tokens", 0)),
        int(total.get("input_tokens", 0)) + int(total.get("output_tokens", 0)),
    )
    cost = cost or {}
    for key in ("estimated_usd", "actual_usd"):
        value = cost.get(key)
        if isinstance(value, (int, float)):
            total["cost_usd"] = round(float(total["cost_usd"]) + float(value), 8)


def _apply_transform(step: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """Declarative mapping only: templates are resolved, nothing is executed."""
    mapping = step.get("map") or step.get("set") or {}
    if not isinstance(mapping, dict):
        raise unprocessable(
            "invalid_step",
            "A transform step needs a 'map' object.",
            details={"field": "map"},
        )
    base: Any = {}
    if step.get("from"):
        base = capability_config.render(step["from"], ctx)
    local_ctx = {**ctx, "value": base, "steps": ctx.get("steps", {})}
    rendered = capability_config.render(mapping, local_ctx)
    out = dict(base) if isinstance(base, dict) else ({"value": base} if base is not None else {})
    if not isinstance(rendered, dict):
        raise unprocessable("invalid_step", "A transform step must map to an object.")
    out.update({str(k): v for k, v in rendered.items()})
    return out


async def execute_skill(
    skill_id: str,
    version_id: str | None,
    input_payload: dict[str, Any] | None,
    principal: Any,
    *,
    surface: str = "admin",
    allow_draft: bool = True,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Run the plan step by step through `execution.invoke`.

    One invocation row is written per capability step by `execution.invoke`
    itself; this function only correlates the steps and aggregates usage.
    """
    from app.services import execution as execution_service

    skill = get_skill(skill_id)
    if version_id:
        version = get_version(skill["id"], version_id)
    else:
        version = current_version(skill)
        if version is None:
            raise unprocessable("no_version", "This SKILL has no version to run.")
    if version["status"] != "published" and not allow_draft:
        raise conflict(
            "skill_not_published",
            "This SKILL version is not published, so it cannot be called.",
            details={"status": version["status"]},
            actions=["Publish the version first."],
        )
    manifest = _manifest_of(version)
    plan = manifest.get("plan") or []
    if not isinstance(plan, list) or not plan:
        raise unprocessable(
            "empty_plan",
            "This SKILL has no plan steps to run.",
            actions=["Add at least one capability step to the plan."],
        )

    payload_in = input_payload if isinstance(input_payload, dict) else {}
    step_results: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    usage_total = _empty_usage()
    status = "succeeded"
    started_all = time.perf_counter()

    for index, step in enumerate(plan):
        if not isinstance(step, dict):
            raise unprocessable("invalid_step", f"Plan step {index} is not an object.")
        step_id = _step_id(step, index)
        kind = step.get("kind")
        save_as = str(step.get("save_as") or step_id)
        ctx = {
            "input": payload_in,
            "steps": step_results,
            "skill": {"slug": skill["slug"], "version": str(version["version"])},
        }
        on_error = step.get("on_error") or "stop"
        started = time.perf_counter()

        if kind == "capability":
            slug = str(step.get("capability") or "").strip()
            if not slug:
                raise unprocessable(
                    "invalid_step",
                    f"Plan step '{step_id}' does not name a capability.",
                    details={"field": f"plan[{index}].capability"},
                )
            rendered = capability_config.render(step.get("input") or {}, ctx)
            if not isinstance(rendered, dict):
                rendered = {"value": rendered}
            options = execution_service.InvokeOptions(
                mode=step.get("mode"),
                surface=surface,
                version_id=step.get("version_id"),
                allow_draft=False,
                actor_type="agent" if getattr(principal, "token_id", "") else "user",
                actor_id=actor_id or getattr(principal, "agent_id", None) or None,
            )
            try:
                result = await execution_service.invoke(principal, slug, rendered, options)
            except ApiError as exc:
                entry = {
                    "id": step_id,
                    "kind": kind,
                    "status": "failed",
                    "capability": slug,
                    "result_preview": None,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": {"code": exc.code, "message": exc.message, "action": (exc.actions or [None])[0]},
                }
                steps.append(entry)
                status = "failed"
                if on_error == "continue":
                    continue
                break
            duration = int((time.perf_counter() - started) * 1000)
            data = result.to_dict()
            _accumulate_usage(usage_total, data.get("usage"), data.get("cost"))
            step_results[save_as] = data.get("result")
            steps.append(
                {
                    "id": step_id,
                    "kind": kind,
                    "status": data.get("status"),
                    "capability": slug,
                    "version": data.get("version"),
                    "invocation_id": data.get("invocation_id"),
                    "request_id": data.get("request_id"),
                    "result_preview": preview(data.get("result"), limit=RESULT_PREVIEW_LIMIT),
                    "duration_ms": duration,
                    "error": data.get("error"),
                }
            )
            if data.get("status") not in {"succeeded", "preview"}:
                status = "failed"
                if on_error != "continue":
                    break
            elif status == "succeeded" and data.get("status") == "preview":
                status = "preview"

        elif kind == "transform":
            value = _apply_transform(step, ctx)
            step_results[save_as] = value
            steps.append(
                {
                    "id": step_id,
                    "kind": kind,
                    "status": "succeeded",
                    "result_preview": preview(value, limit=RESULT_PREVIEW_LIMIT),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )

        elif kind == "agent":
            raise ApiError(
                "engine_required",
                (
                    f"Plan step '{step_id}' needs live agent reasoning, which only the AragonAgent "
                    "engine can perform. Skill Router does not simulate it."
                ),
                status_code=409,
                details={
                    "step_id": step_id,
                    "step_index": index,
                    "kind": "agent",
                    "instruction": step.get("instruction"),
                    "completed_steps": steps,
                    "supported_here": ["capability", "transform"],
                    "engine": {"status": _engine_status(), "surface": surface},
                },
                actions=[
                    "Run this SKILL from the agent panel, where the AragonAgent engine is available.",
                    "Or replace the agent step with capability steps the platform can execute.",
                ],
            )

        else:
            raise unprocessable(
                "invalid_step",
                f"Unknown plan step kind {kind!r}.",
                details={"field": f"plan[{index}].kind", "allowed": list(PLAN_KINDS)},
            )

    output = step_results.get("final", step_results.get("output"))
    if output is None and steps:
        last = steps[-1]["id"]
        output = step_results.get(last)
    return {
        "skill": {"id": skill["id"], "slug": skill["slug"], "name": skill["name"]},
        "version": {"id": version["id"], "version": str(version["version"]), "status": version["status"]},
        "status": status,
        "steps": steps,
        "output": output,
        "outputs": step_results,
        "usage": usage_total,
        "duration_ms": int((time.perf_counter() - started_all) * 1000),
        "surface": surface,
    }


def _engine_status() -> str:
    row = db.query_one("SELECT version FROM engine_releases WHERE status = 'active' LIMIT 1")
    return f"active:{row['version']}" if row else "not_configured"


# ------------------------------------------------------------ plan inspection --
def plan_preview(skill_id: str, version_id: str | None = None) -> dict[str, Any]:
    """Non-executing view used by the UI before a test run."""
    skill = get_skill(skill_id)
    version = get_version(skill["id"], version_id) if version_id else current_version(skill)
    if version is None:
        raise not_found("SKILL version")
    manifest = _manifest_of(version)
    plan = serialise_plan(manifest.get("plan"))
    return {
        "skill": {"id": skill["id"], "slug": skill["slug"], "name": skill["name"]},
        "version": {"id": version["id"], "version": str(version["version"]), "status": version["status"]},
        "steps": plan,
        "requires_live_agent": any(step["requires_live_agent"] for step in plan),
        "validation": validate_manifest(manifest),
        "dependency_impact": dependency_impact(skill["id"], version_row=version),
        "inputs": manifest.get("inputs") or {},
        "examples": manifest.get("examples") or [],
        "engine": _engine_status(),
    }


def instructions_of(version_row: dict[str, Any]) -> str:
    return truncate(str(_manifest_of(version_row).get("instructions") or ""), 40000)
