"""SKILL export: a portable bundle that can never leak platform secrets.

Two products live here:

  * ``export_skill(skill_id, format)`` — the SKILL bundle for a stored SKILL
    (manifest + instructions + capability descriptors + examples);
  * ``build_external_skill_bundle(principal)`` — the connection SKILL handed to
    an external agent (``GET /api/v1/skill``), i.e. instructions, the capability
    list it is granted and install/verify examples.

Both paths end the same way: strip credential ids into placeholders, run the
whole document through ``app.security.redact.Redactor`` and *assert* that no
secret shape ("sk-…", "AIza…", "sr_live_…", AES-GCM ciphertext, credential ids)
survives. If one does, the export fails loudly instead of shipping it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

import yaml

from app.core import db
from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.security.redact import Redactor
from app.services import capability_config, skills

BUNDLE_SCHEMA = "skill-router/skill-bundle@1"
EXTERNAL_SCHEMA = "skill-router/external-skill@1"

_SECRET_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}"), "vendor api key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{12,}"), "anthropic key"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}"), "google api key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "github token"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws key id"),
    (re.compile(r"\bsr_live_[A-Za-z0-9_\-]{8,}"), "platform agent token"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), "jwt"),
    # AES-256-GCM envelope: v.key_id.nonce.ciphertext
    (re.compile(r"\b\d{1,2}\.[A-Za-z0-9_\-]{1,16}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-+/=]{16,}"), "ciphertext envelope"),
    (re.compile(r"\bcred_[0-9a-f]{6,}"), "credential id"),
    (re.compile(r"\bfile_[0-9a-f]{8,}"), "file id"),
)


# ------------------------------------------------------------------ scrubbing --
def _scrub_credential_refs(value: Any) -> Any:
    """Replace credential references with placeholders (ids never leave)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in {"credential_id", "credential", "credentials", "secret", "secret_ref"}:
                if isinstance(item, list):
                    out[key] = [f"credential_placeholder_{index + 1}" for index, _ in enumerate(item)]
                elif item in (None, "", {}):
                    out[key] = item
                else:
                    out[key] = "credential_placeholder_1"
                continue
            out[key] = _scrub_credential_refs(item)
        return out
    if isinstance(value, list):
        return [_scrub_credential_refs(item) for item in value]
    return value


_EXTRA_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # shapes Redactor does not know about, scrubbed here so the assertion holds
    (re.compile(r"\bsr_live_[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"\b\d{1,2}\.[A-Za-z0-9_\-]{1,16}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-+/=]{16,}"), "[REDACTED]"),
    (re.compile(r"\bcred_[0-9a-f]{6,}"), "[credential]"),
    (re.compile(r"\bfile_[0-9a-f]{8,}"), "[file]"),
)


def _scrub_text(text: str) -> str:
    """Final pass: pattern redaction plus the shapes only this module knows."""
    out = Redactor().text(text)
    for pattern, replacement in _EXTRA_REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def assert_secret_free(text: str, *, context: str = "export") -> None:
    """Fail closed: a surviving secret shape is an error, never a warning."""
    for pattern, label in _SECRET_SHAPES:
        match = pattern.search(text)
        if match:
            raise ApiError(
                "export_secret_leak",
                f"The {context} still contained a {label} after redaction and was not returned.",
                status_code=500,
                details={"field": label, "context": context},
                actions=["Rotate the affected credential.", "Report this as a bug: export must never emit secrets."],
            )


def _redact_document(document: dict[str, Any]) -> dict[str, Any]:
    """Structural pass: credential references become placeholders, then redact."""
    scrubbed = _scrub_credential_refs(document)
    return Redactor().value(scrubbed)


def _render_text(bundle: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """Return (content, filename, content_type)."""
    skill = bundle.get("skill") or {}
    version = bundle.get("version") or {}
    slug = skill.get("slug") or "skill"
    if fmt == "json":
        content = json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=False)
        return content, f"{slug}-{version.get('version') or '1'}.json", "application/json"

    front = {
        "name": skill.get("name"),
        "description": skill.get("description"),
        "slug": slug,
        "category": skill.get("category"),
        "version": version.get("version"),
        "status": version.get("status"),
        "requires_live_agent": bundle.get("requires_live_agent", False),
        "capabilities": [item.get("slug") for item in bundle.get("capabilities", [])],
        "permissions": bundle.get("permissions", {}),
        "when_to_use": (bundle.get("manifest") or {}).get("when_to_use") or "",
        "schema": BUNDLE_SCHEMA,
        "generated_at": bundle.get("generated_at"),
    }
    parts: list[str] = ["---", yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip(), "---", ""]
    instructions = (bundle.get("manifest") or {}).get("instructions") or bundle.get("instructions") or ""
    parts.append(instructions.strip() or "_(no instructions were recorded for this SKILL)_")
    parts.append("")
    steps = bundle.get("plan") or []
    if steps:
        parts.append("## Plan")
        parts.append("")
        for index, step in enumerate(steps, start=1):
            kind = step.get("kind")
            if kind == "capability":
                parts.append(f"{index}. **capability** `{step.get('capability')}` → save as `{step.get('save_as') or step.get('id')}`")
            elif kind == "agent":
                parts.append(
                    f"{index}. **agent** (requires live agent reasoning) — {step.get('instruction') or 'no instruction'}"
                )
            else:
                parts.append(f"{index}. **transform** maps `{step.get('save_as') or step.get('id')}`")
        parts.append("")
    capabilities = bundle.get("capabilities") or []
    if capabilities:
        parts.append("## Capabilities used")
        parts.append("")
        for item in capabilities:
            live = " (needs live agent)" if item.get("needs_live_agent") else ""
            parts.append(f"### `{item.get('slug')}` — {item.get('name')}{live}")
            if item.get("description"):
                parts.append(str(item["description"]))
            parts.append("")
            parts.append("```json")
            parts.append(
                json.dumps(
                    {
                        "version": item.get("version"),
                        "modes": item.get("modes"),
                        "input_schema": item.get("input_schema"),
                        "limits": item.get("limits"),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            parts.append("```")
            parts.append("")
    examples = bundle.get("examples") or []
    if examples:
        parts.append("## Examples")
        parts.append("")
        for example in examples:
            parts.append("```json")
            parts.append(json.dumps(example, indent=2, ensure_ascii=False))
            parts.append("```")
            parts.append("")
    parts.append("## Notes")
    parts.append("")
    parts.append(
        "- This bundle contains no credentials. The platform injects them at call time; "
        "nothing here can be used to authenticate elsewhere."
    )
    if bundle.get("requires_live_agent"):
        parts.append(
            "- At least one step needs live agent reasoning. Run this SKILL where the AragonAgent "
            "engine is available; the platform will not simulate it."
        )
    parts.append("")
    return "\n".join(parts).rstrip() + "\n", f"{slug}-{version.get('version') or '1'}.SKILL.md", "text/markdown"


# ------------------------------------------------------------ descriptors -----
def _capability_descriptor(slug: str) -> dict[str, Any] | None:
    capability = db.query_one("SELECT * FROM capabilities WHERE slug = ?", (slug,))
    if not capability:
        return None
    version = None
    if capability.get("current_version_id"):
        version = db.query_one(
            "SELECT * FROM capability_versions WHERE id = ? AND status = 'published'",
            (capability["current_version_id"],),
        )
    if version is None:
        version = db.query_one(
            "SELECT * FROM capability_versions WHERE capability_id = ? AND status = 'published' "
            "ORDER BY version DESC LIMIT 1",
            (capability["id"],),
        )
    config = capability_config.merge_defaults(
        db.loads(version["config"], {}) if version else {}, capability.get("type") or "generic"
    )
    safe_config = capability_config.mask_config_for_export(config)
    return {
        "slug": capability["slug"],
        "name": capability["name"],
        "description": capability.get("description") or "",
        "type": capability.get("type"),
        "status": capability.get("status"),
        "version": version["version"] if version else None,
        "published": bool(version),
        "needs_live_agent": bool(capability.get("needs_live_agent")),
        "input_schema": config.get("input_schema") or {},
        "output_schema": config.get("output_schema") or {},
        "modes": config.get("modes") or {},
        "limits": {
            key: (config.get("limits") or {}).get(key)
            for key in ("timeout_s", "max_input_bytes", "max_output_bytes", "file_input", "max_file_mb")
        },
        "examples": (safe_config.get("examples") or [])[:3],
    }


def _referenced_slugs(manifest: dict[str, Any]) -> list[str]:
    slugs: list[str] = []
    for dep in manifest.get("dependencies") or []:
        if isinstance(dep, dict) and dep.get("capability"):
            slugs.append(str(dep["capability"]))
    for step in manifest.get("plan") or []:
        if isinstance(step, dict) and step.get("kind") == "capability" and step.get("capability"):
            slugs.append(str(step["capability"]))
    seen: list[str] = []
    for slug in slugs:
        if slug not in seen:
            seen.append(slug)
    return seen


# ------------------------------------------------------------------ export ----
def export_skill(skill_id: str, fmt: str = "skill") -> dict[str, Any]:
    """Build a secret-free SKILL bundle for a stored SKILL."""
    fmt = (fmt or "skill").strip().lower()
    if fmt not in {"skill", "json"}:
        raise ApiError(
            "validation_failed",
            f"Unknown export format {fmt!r}.",
            status_code=422,
            details=[{"field": "format", "message": "Use 'skill' or 'json'."}],
            actions=["Export as a SKILL markdown bundle or as JSON."],
        )
    skill = skills.get_skill(skill_id)
    version = skills.current_version(skill)
    if version is None:
        raise not_found("SKILL version")
    manifest = db.loads(version["manifest"], {}) or {}
    published = db.query_one(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND status = 'published' ORDER BY published_at DESC LIMIT 1",
        (skill["id"],),
    )
    permissions = manifest.get("permissions") or {}
    credentials = permissions.get("credentials") or []
    bundle: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "generated_at": db.iso(),
        "skill": {
            "slug": skill["slug"],
            "name": skill["name"],
            "description": skill["description"],
            "category": skill["category"],
            "status": skill["status"],
            "source": {"type": skill["source_type"], "ref": skill["source_ref"]},
        },
        "version": {
            "id": version["id"],
            "version": str(version["version"]),
            "status": version["status"],
            "checksum": version["checksum"],
            "published_at": version["published_at"],
        },
        "latest_published": {
            "version": str(published["version"]) if published else None,
            "published_at": published["published_at"] if published else None,
        },
        "manifest": {
            "name": manifest.get("name") or skill["name"],
            "description": manifest.get("description") or skill["description"],
            "when_to_use": manifest.get("when_to_use") or "",
            "instructions": manifest.get("instructions") or "",
            "inputs": manifest.get("inputs") or {},
            "outputs": manifest.get("outputs") or {},
            "dependencies": manifest.get("dependencies") or [],
            "examples": manifest.get("examples") or [],
            "sources": manifest.get("sources") or [],
        },
        "instructions": manifest.get("instructions") or "",
        "plan": skills.serialise_plan(manifest.get("plan")),
        "requires_live_agent": any(
            isinstance(step, dict) and step.get("kind") == "agent" for step in (manifest.get("plan") or [])
        ),
        "capabilities": [
            descriptor
            for descriptor in (_capability_descriptor(slug) for slug in _referenced_slugs(manifest))
            if descriptor is not None
        ],
        "permissions": {
            "network": permissions.get("network") or [],
            "files": permissions.get("files") or [],
            "credentials": permissions.get("credentials") or [],
            "credential_placeholders": [f"credential_placeholder_{index + 1}" for index, _ in enumerate(credentials)],
        },
        "examples": manifest.get("examples") or [],
        "includes_credentials": False,
    }
    redacted = _redact_document(bundle)
    content, filename, content_type = _render_text(redacted, fmt)
    content = _scrub_text(content)
    assert_secret_free(content, context="SKILL export")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "format": fmt,
        "filename": filename,
        "content_type": content_type,
        "content": content,
        "sha256": digest,
        "bundle": redacted,
        "includes_credentials": False,
        "notes": [
            "No credential material is included: the platform injects credentials at call time.",
            "Steps marked requires_live_agent need the AragonAgent engine.",
        ],
    }


# ------------------------------------------------------ external descriptor ---
EXTERNAL_INSTRUCTIONS = """\
# Skill Router — connection SKILL

You are connected to a **Skill Router** platform as `{agent_name}`. The platform owns the
credentials, the routing and the rate limits; you never see provider secrets, so you must
call capabilities *through* the platform.

## 1. Verify the connection
1. `GET {base_url}/api/v1/whoami` — expect `{{agent, scopes, limits, capabilities_count}}`.
2. `GET {base_url}/api/v1/capabilities` — the capabilities this token is granted. Keep the
   returned `version_token`; call this route again and re-read the descriptors whenever the
   token changes. Never cache descriptors across a token change.

## 2. Discover a capability
`GET {base_url}/api/v1/capabilities/{{slug}}` returns the input/output schemas, the supported
modes (`sync`, `async`, `stream`), limits and examples. Validate your payload against
`input_schema` before calling: unknown parameters are rejected with `unsupported_parameter`.

## 3. Invoke
```
POST {base_url}/api/v1/invocations
Authorization: Bearer <your agent token>
{{"capability": "<slug>", "input": {{...}}, "options": {{"mode": "sync"}}}}
```
The answer is `{{request_id, invocation_id, status, result, artifacts[], usage, cost, error}}`.
`status` is one of `succeeded`, `uncertain`, `failed`. **`uncertain` means the upstream call
may have produced output** — report it to the user, do not silently retry a paid call.

Async capabilities answer `{{request_id, task_id, status: "queued", poll}}`; poll
`GET {base_url}/api/v1/tasks/{{task_id}}` and cancel with
`POST {base_url}/api/v1/tasks/{{task_id}}/cancel`.

## 4. Errors you must handle
`unauthenticated`, `invalid_token`, `token_revoked`, `token_expired`, `capability_not_granted`,
`capability_not_published`, `unsupported_parameter`, `input_schema_mismatch`,
`no_compatible_route`, `rate_limited`, `concurrency_limited`, `budget_exceeded`,
`upstream_timeout_uncertain`.

## 5. Tools that need live reasoning
Some SKILLs contain steps of kind `agent`. Those steps need the AragonAgent engine: the
platform does not simulate reasoning and will answer `409 engine_required`. If you are that
engine, run the step yourself; otherwise tell the user which step needs it.

## 6. MCP alternative
`POST {base_url}/api/mcp` (streamable HTTP, JSON-RPC 2.0, bearer token) exposes every granted
published capability as a tool named with dots replaced by `__`
(e.g. `text.summarize` → `text__summarize`).
"""


def _granted_capabilities(principal: Any) -> list[dict[str, Any]]:
    rows = db.query_all(
        """SELECT c.slug FROM capabilities c
            WHERE c.status = 'published'
              AND c.current_version_id IS NOT NULL
            ORDER BY c.slug ASC"""
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        slug = row["slug"]
        can_use = getattr(principal, "can_use", None)
        if callable(can_use) and not can_use(slug):
            continue
        descriptor = _capability_descriptor(slug)
        if descriptor is None or not descriptor.get("published"):
            continue
        out.append(
            {
                "slug": descriptor["slug"],
                "name": descriptor["name"],
                "description": descriptor["description"],
                "version": descriptor["version"],
                "modes": descriptor["modes"],
                "needs_live_agent": descriptor["needs_live_agent"],
                "input_schema": descriptor["input_schema"],
                "examples": descriptor["examples"],
            }
        )
    return out


def build_external_skill_bundle(principal: Any, *, base_url: str | None = None) -> dict[str, Any]:
    """The connection SKILL for an external agent. Never carries the token."""
    settings = get_settings()
    platform_url = (base_url or settings.public_base_url or settings.backend_base_url).rstrip("/")
    agent_name = getattr(principal, "agent_name", None) or "external agent"
    agent_id = getattr(principal, "agent_id", "") or None
    capabilities = _granted_capabilities(principal)
    version_token = hashlib.sha256(
        json.dumps(capabilities, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    descriptors_text = "\n".join(
        f"- `{item['slug']}` v{item['version']} — {item['name']}"
        + (" _(needs live agent reasoning)_" if item["needs_live_agent"] else "")
        for item in capabilities
    ) or "- No capability is granted to this token yet."
    bundle: dict[str, Any] = {
        "schema": EXTERNAL_SCHEMA,
        "generated_at": db.iso(),
        "agent": {"id": agent_id, "name": agent_name},
        "platform": {
            "base_url": platform_url,
            "api_base": "/api/v1",
            "mcp_url": "/api/mcp",
            "openapi_url": "/api/v1/openapi.json",
        },
        "version_token": version_token,
        "instructions": EXTERNAL_INSTRUCTIONS.format(agent_name=agent_name, base_url=platform_url),
        "capabilities": capabilities,
        "capability_index": descriptors_text,
        "limits": {
            "rate_limit_per_min": getattr(principal, "rate_limit_per_min", None),
            "max_concurrency": getattr(principal, "max_concurrency", None),
            "budget_usd": getattr(principal, "budget_usd", None),
        },
        "scopes": list(getattr(principal, "scopes", []) or []),
        "examples": {
            "install": [
                "npx skill-router-connect install --url " + platform_url + " --token <AGENT_TOKEN>",
                "Store the token in the agent's own configuration; never put it in this SKILL file.",
            ],
            "verify": [
                f"curl -s -H \"Authorization: Bearer <AGENT_TOKEN>\" {platform_url}/api/v1/whoami",
                f"curl -s -H \"Authorization: Bearer <AGENT_TOKEN>\" {platform_url}/api/v1/capabilities",
            ],
            "invoke": [
                f"curl -s -X POST {platform_url}/api/v1/invocations "
                '-H "Authorization: Bearer <AGENT_TOKEN>" -H "content-type: application/json" '
                '-d \'{"capability": "<slug>", "input": {}}\'',
            ],
            "mcp": [
                f'curl -s -X POST {platform_url}/api/mcp -H "Authorization: Bearer <AGENT_TOKEN>" '
                '-H "content-type: application/json" '
                '-d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\'',
            ],
        },
        "notes": [
            "The token is not part of this bundle. Pass it as an Authorization header.",
            "Refresh capabilities when version_token changes.",
            "Steps of kind 'agent' need the AragonAgent engine and are never simulated.",
        ],
        "includes_credentials": False,
    }
    redacted = _redact_document(bundle)
    content = _scrub_text(json.dumps(redacted, indent=2, ensure_ascii=False))
    assert_secret_free(content, context="external SKILL bundle")
    redacted["content"] = content
    redacted["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return redacted


def bundle_as_markdown(bundle: dict[str, Any]) -> str:
    """Render an external bundle as a single SKILL.md-style document."""
    front = {
        "name": f"skill-router-{bundle.get('agent', {}).get('name') or 'agent'}",
        "description": "Connection SKILL for the Skill Router platform.",
        "base_url": (bundle.get("platform") or {}).get("base_url"),
        "api_base": (bundle.get("platform") or {}).get("api_base"),
        "schema": EXTERNAL_SCHEMA,
        "version_token": bundle.get("version_token"),
        "capabilities": [item.get("slug") for item in bundle.get("capabilities", [])],
        "includes_credentials": False,
    }
    parts = [
        "---",
        yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip(),
        "---",
        "",
        str(bundle.get("instructions") or "").strip(),
        "",
        "## Granted capabilities",
        "",
        str(bundle.get("capability_index") or ""),
        "",
        "## Examples",
        "",
    ]
    for group, commands in (bundle.get("examples") or {}).items():
        parts.append(f"### {group}")
        parts.append("")
        for command in commands:
            parts.append("```")
            parts.append(command)
            parts.append("```")
        parts.append("")
    content = _scrub_text("\n".join(parts))
    assert_secret_free(content, context="external SKILL markdown")
    return content


def secret_free_summary(values: Iterable[Any]) -> bool:
    """Small helper for tests/callers: true when nothing secret-looking is present."""
    text = json.dumps(list(values), default=str)
    try:
        assert_secret_free(text)
    except ApiError:
        return False
    return True
