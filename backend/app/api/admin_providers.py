"""Providers: the upstream services a capability can call.

Owner: backend-capabilities (docs/CONTRACT.md §5.2). Every write is audited and
every delete is guarded — a provider that still has credentials or capabilities
is refused with `409 provider_in_use` so an administrator never silently orphans
a working configuration.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import db
from app.core.errors import conflict, not_found
from app.deps import AuthUser, client_ip, require_admin
from app.services import audit, capabilities as capability_service, credentials as credential_service
from app.security.tokens import slugify

router = APIRouter(prefix="/api/admin/providers", tags=["admin-providers"])

ProviderCategory = Literal["llm", "image", "video", "speech", "transcription", "generic"]
ProviderEnvironment = Literal["production", "staging", "development"]
ProviderStatus = Literal["active", "disabled"]

MAX_LIMIT = 200


# ------------------------------------------------------------------- models ---
def _check_http_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be an http:// or https:// URL (or left empty)")
    return text


class ProviderCreate(BaseModel):
    """Body of `POST /api/admin/providers` (CONTRACT §5.2)."""

    name: str = Field(min_length=1, max_length=120, description="Name shown in the UI and pickers.")
    category: ProviderCategory = Field(default="llm", description="What the provider offers.")
    base_url: str | None = Field(default=None, description="API root, e.g. https://api.example.com/v1.")
    docs_url: str | None = None
    tags: list[str] = Field(default_factory=list, description="Free-form labels for filtering.")
    environment: ProviderEnvironment = "production"
    notes: str | None = Field(default=None, max_length=4000)
    status: ProviderStatus = "active"

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("base_url", "docs_url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        return _check_http_url(value)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            text = str(tag).strip()
            if not text:
                continue
            if len(text) > 40:
                raise ValueError("each tag must be 40 characters or fewer")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned[:20]

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class ProviderUpdate(BaseModel):
    """Body of `PATCH /api/admin/providers/{id}` — every field optional."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: ProviderCategory | None = None
    base_url: str | None = None
    docs_url: str | None = None
    tags: list[str] | None = None
    environment: ProviderEnvironment | None = None
    notes: str | None = Field(default=None, max_length=4000)
    status: ProviderStatus | None = None

    @field_validator("base_url", "docs_url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        return _check_http_url(value)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for tag in value:
            text = str(tag).strip()
            if not text:
                continue
            if len(text) > 40:
                raise ValueError("each tag must be 40 characters or fewer")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned[:20]

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


# ------------------------------------------------------------- serialisation --
def _counts(provider_id: str) -> dict[str, int]:
    return {
        "credentials": int(
            db.query_scalar("SELECT COUNT(*) FROM credentials WHERE provider_id = ?", (provider_id,)) or 0
        ),
        "capabilities": int(
            db.query_scalar("SELECT COUNT(*) FROM capabilities WHERE provider_id = ?", (provider_id,)) or 0
        ),
        "routes": int(db.query_scalar("SELECT COUNT(*) FROM routes WHERE provider_id = ?", (provider_id,)) or 0),
    }


def serialize_provider(row: dict[str, Any]) -> dict[str, Any]:
    counts = _counts(row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "category": row["category"],
        "base_url": row.get("base_url"),
        "docs_url": row.get("docs_url"),
        "tags": db.loads(row.get("tags"), []),
        "environment": row["environment"],
        "notes": row.get("notes"),
        "status": row["status"],
        "counts": counts,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    payload = serialize_provider(row)
    credential_rows, credential_total = credential_service.list_credentials(provider_id=row["id"], limit=MAX_LIMIT)
    payload["credentials"] = credential_rows
    payload["counts"]["credentials"] = credential_total
    capability_rows = db.query_all(
        """SELECT id, slug, name, type, status, current_version_id FROM capabilities
            WHERE provider_id = ? ORDER BY name ASC LIMIT ?""",
        (row["id"], MAX_LIMIT),
    )
    payload["capabilities"] = [
        {
            "id": item["id"],
            "slug": item["slug"],
            "name": item["name"],
            "type": item["type"],
            "status": item["status"],
            "callable": capability_service.callable_state(capability_service.get_row(item["id"]))["callable"],
        }
        for item in capability_rows
    ]
    payload["routes"] = db.query_all(
        """SELECT r.id, r.capability_id, c.slug AS capability_slug, r.capability_version_id,
                  r.enabled, r.health_status, r.last_health_at
             FROM routes r JOIN capabilities c ON c.id = r.capability_id
            WHERE r.provider_id = ? ORDER BY c.slug ASC LIMIT ?""",
        (row["id"], MAX_LIMIT),
    )
    for route in payload["routes"]:
        route["enabled"] = bool(route["enabled"])
    return payload


def _get_provider(provider_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
    if not row:
        raise not_found("Provider")
    return row


def _slug(provider_name: str, *, exclude_id: str | None = None) -> str:
    base = slugify(provider_name, fallback="provider")
    exists = lambda candidate: db.query_scalar(  # noqa: E731
        "SELECT 1 FROM providers WHERE slug = ?" + (" AND id != ?" if exclude_id else ""),
        (candidate, exclude_id) if exclude_id else (candidate,),
    )
    if not exists(base):
        return base
    counter = 2
    while exists(f"{base}-{counter}"):
        counter += 1
    return f"{base}-{counter}"


# ---------------------------------------------------------------- endpoints ---
@router.get("")
def list_providers(
    search: str | None = Query(default=None, description="Match name, slug, notes or tag."),
    category: ProviderCategory | None = None,
    environment: ProviderEnvironment | None = None,
    status: ProviderStatus | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    if search:
        where.append("(name LIKE ? OR slug LIKE ? OR COALESCE(notes,'') LIKE ? OR tags LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if category:
        where.append("category = ?")
        params.append(category)
    if environment:
        where.append("environment = ?")
        params.append(environment)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM providers WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM providers WHERE {clause} ORDER BY name ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return {"items": [serialize_provider(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_provider(
    payload: ProviderCreate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    name = payload.name
    if db.query_scalar("SELECT 1 FROM providers WHERE LOWER(name) = LOWER(?)", (name,)):
        raise conflict(
            "provider_exists",
            f"A provider named {name!r} already exists.",
            details={"name": name},
            actions=["Edit the existing provider instead of creating a duplicate."],
        )
    now = db.iso()
    provider_id = db.new_id("prov")
    db.insert(
        "providers",
        {
            "id": provider_id,
            "name": name,
            "slug": _slug(name),
            "category": payload.category,
            "base_url": payload.base_url,
            "docs_url": payload.docs_url,
            "tags": db.dumps(payload.tags),
            "environment": payload.environment,
            "notes": payload.notes,
            "status": payload.status,
            "created_at": now,
            "updated_at": now,
        },
    )
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="provider.created",
        object_type="provider",
        object_id=provider_id,
        summary=f"{name} ({payload.category}, {payload.environment})",
        metadata={
            "category": payload.category,
            "environment": payload.environment,
            "base_url": payload.base_url,
            "status": payload.status,
        },
        ip=client_ip(request),
    )
    return _detail(_get_provider(provider_id))


@router.get("/{provider_id}")
def get_provider(provider_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    return _detail(_get_provider(provider_id))


@router.patch("/{provider_id}")
def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = _get_provider(provider_id)
    changes = payload.model_dump(exclude_unset=True)
    values: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "tags":
            values["tags"] = db.dumps(value or [])
        elif field in {"base_url", "docs_url", "notes"}:
            values[field] = value or None
        elif value is not None:
            values[field] = value
    if "name" in values:
        clash = db.query_one(
            "SELECT id FROM providers WHERE LOWER(name) = LOWER(?) AND id != ?", (values["name"], provider_id)
        )
        if clash:
            raise conflict(
                "provider_exists",
                f"Another provider is already named {values['name']!r}.",
                details={"conflicting_id": clash["id"]},
            )
        values["slug"] = _slug(values["name"], exclude_id=provider_id)
    if values:
        values["updated_at"] = db.iso()
        db.update("providers", provider_id, values)
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="provider.updated",
        object_type="provider",
        object_id=provider_id,
        summary=f"{row['name']} updated ({', '.join(sorted(values)) or 'no changes'})",
        metadata={"fields_changed": sorted(values.keys())},
        ip=client_ip(request),
    )
    return _detail(_get_provider(provider_id))


@router.delete("/{provider_id}")
def delete_provider(
    provider_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = _get_provider(provider_id)
    counts = _counts(provider_id)
    if counts["credentials"] or counts["capabilities"]:
        raise conflict(
            "provider_in_use",
            "This provider still has credentials or capabilities attached.",
            details={"provider_id": provider_id, "counts": counts},
            actions=[
                "Delete or re-point the provider's credentials first.",
                "Delete or re-point the capabilities that use it.",
                "Or disable the provider instead of deleting it.",
            ],
        )
    db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="provider.deleted",
        object_type="provider",
        object_id=provider_id,
        summary=f"{row['name']} deleted",
        metadata={"counts": counts},
        ip=client_ip(request),
    )
    return {"deleted": True, "id": provider_id, "slug": row["slug"], "counts": counts}


@router.post("/{provider_id}/models")
async def list_provider_models(
    provider_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Ask the provider which model ids this account can use.

    Without this, naming a model for the agent is guesswork: the user has to know the
    exact id the provider expects (`glm-5.3-flash`, `gpt-4o-mini`, …). The probe uses
    the provider's own base_url and its first active credential, so the answer is what
    this account would actually call — and it goes through the same outbound guard as
    a real capability call.
    """
    from app.services.network import NetworkPolicyError, classify_network_error, guarded_request

    provider = _get_provider(provider_id)
    base = (provider.get("base_url") or "").rstrip("/")
    if not base:
        raise conflict(
            "provider_has_no_base_url",
            "Set the provider's service address first; the model list is fetched from it.",
            details={"provider_id": provider_id},
            actions=["Edit the provider and add the base URL, then retry."],
        )

    credential = db.query_one(
        "SELECT id FROM credentials WHERE provider_id = ? AND status = 'active' ORDER BY created_at LIMIT 1",
        (provider_id,),
    )
    if not credential:
        raise conflict(
            "provider_has_no_credential",
            "A credential is required to list the models this account can use.",
            details={"provider_id": provider_id},
            actions=["Add an active credential for this provider, then retry."],
        )

    row = credential_service.get_row(credential["id"])
    secrets = credential_service.get_secret_map(credential["id"])
    headers = credential_service.build_auth_headers(row, secrets)
    headers.setdefault("accept", "application/json")

    # The protocol decides the path: an Anthropic-compatible base_url serves
    # /v1/models, everything else (OpenAI gateways, GLM's native API) serves /models.
    candidates: list[str] = []
    if "/anthropic" in base:
        candidates.append(f"{base}/v1/models")
    candidates.append(f"{base}/models")

    redactor = secrets.redactor()
    attempts: list[dict[str, Any]] = []
    for url in candidates:
        try:
            response = await guarded_request("GET", url, headers=headers, timeout_s=20)
        except NetworkPolicyError as exc:
            attempts.append({"url": url, "error": exc.code, "message": redactor.text(str(exc))})
            continue
        except Exception as exc:  # noqa: BLE001 - transport problems are data here
            code, message = classify_network_error(exc)
            attempts.append({"url": url, "error": code, "message": redactor.text(message)})
            continue
        if response.status_code >= 400:
            attempts.append({"url": url, "error": f"http_{response.status_code}"})
            continue
        payload = response.json()
        ids = _model_ids(payload)
        if not ids:
            attempts.append({"url": url, "error": "no_model_ids_in_response"})
            continue
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="provider.models_listed",
            object_type="provider",
            object_id=provider_id,
            summary=f"{len(ids)} model(s) reported by {url}",
            metadata={"url": url, "count": len(ids)},
            ip=client_ip(request),
        )
        return {
            "provider_id": provider_id,
            "source_url": url,
            "items": ids,
            "count": len(ids),
            "attempts": attempts,
        }

    raise conflict(
        "models_unavailable",
        "The provider did not return a model list at a path this build knows.",
        details={"base_url": base, "attempts": attempts},
        actions=[
            "Type the model id exactly as the provider documents it.",
            "Check that the credential is valid (Providers → Credentials → Verify).",
        ],
    )


def _model_ids(payload: Any) -> list[str]:
    """Extract model ids from the shapes providers actually return.

    OpenAI gateways and GLM's native API: ``{"data":[{"id": …}]}``.
    Anthropic: ``{"data":[{"id": …}]}`` as well, sometimes with ``display_name``.
    Gemini: ``{"models":[{"name":"models/gemini-2.5-flash"}]}``.
    """
    ids: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        if text.startswith("models/"):
            text = text[len("models/") :]
        if text not in ids:
            ids.append(text)

    if not isinstance(payload, dict):
        return ids
    for key in ("data", "models", "items"):
        entries = payload.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    for field in ("id", "name", "model"):
                        if field in entry:
                            add(entry[field])
                            break
                elif isinstance(entry, str):
                    add(entry)
    return ids


__all__ = ["router"]
