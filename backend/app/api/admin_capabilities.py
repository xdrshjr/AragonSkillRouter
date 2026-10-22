"""Capabilities, versions, tests and manual calls.

Owner: backend-capabilities (docs/CONTRACT.md §5.4). The lifecycle is
`draft → validate → real test → publish`, and each of those states is reported
separately. A published version is immutable; the router refuses to edit one with
`409 version_immutable` and points at the new-version endpoint instead.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.services import audit, capabilities as capability_service, execution

router = APIRouter(prefix="/api/admin/capabilities", tags=["admin-capabilities"])

MAX_LIMIT = 200
CapabilityType = Literal["text", "image", "video", "speech", "transcription", "generic"]
CapabilityStatus = Literal["draft", "testing", "published", "disabled"]


# ------------------------------------------------------------------- models ---
class CapabilityCreate(BaseModel):
    """Body of `POST /api/admin/capabilities` — also creates draft v1."""

    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    type: CapabilityType = "generic"
    description: str = Field(default="", max_length=4000)
    provider_id: str | None = None
    credential_id: str | None = None
    needs_live_agent: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not all(ch.isalnum() or ch in "-_." for ch in text):
            raise ValueError("use lowercase letters, digits, '-', '_' or '.'")
        return text.lower()


class CapabilityUpdate(BaseModel):
    """Body of `PATCH /api/admin/capabilities/{id}` — partial."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    type: CapabilityType | None = None
    provider_id: str | None = None
    credential_id: str | None = None
    needs_live_agent: bool | None = None


class VersionCreate(BaseModel):
    """Body of `POST /api/admin/capabilities/{id}/versions`."""

    from_version_id: str | None = Field(default=None, description="Copy this version's config.")
    config: dict[str, Any] | None = Field(default=None, description="Explicit config for the new draft.")
    notes: str | None = Field(default=None, max_length=2000)


class VersionUpdate(BaseModel):
    """Body of `PATCH /api/admin/capabilities/{id}/versions/{version_id}`."""

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    status: Literal["draft", "testing"] | None = Field(
        default=None, description="Only draft ↔ testing transitions are allowed here."
    )


class TestRequest(BaseModel):
    """Body of `POST .../versions/{version_id}/test`."""

    input: dict[str, Any] = Field(default_factory=dict, description="Sample input for the real call.")
    mode: Literal["sync", "async", "stream"] = "sync"


class InvokeRequest(BaseModel):
    """Body of `POST /api/admin/capabilities/{id}/invoke` (drafts allowed)."""

    input: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["sync", "async", "stream"] | None = None
    version_id: str | None = None
    provider_id: str | None = None
    dry_run: bool = Field(default=False, description="Build the request without sending it.")
    timeout_s: float | None = Field(default=None, gt=0, le=600)


class RollbackRequest(BaseModel):
    version_id: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=1000)


class RoutePreviewRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["sync", "async", "stream"] = "sync"
    provider_id: str | None = None


# ----------------------------------------------------------------- helpers ----
def _capability_or_404(capability_id: str) -> dict[str, Any]:
    try:
        return capability_service.get_row(capability_id)
    except ApiError:
        raise not_found("Capability") from None


def _version_or_404(capability_id: str, version_id: str) -> dict[str, Any]:
    try:
        return capability_service.get_version_row(capability_id, version_id)
    except ApiError:
        raise not_found("Capability version") from None


def _immutable(capability: dict[str, Any], version: dict[str, Any]) -> ApiError:
    return conflict(
        "version_immutable",
        f"Version {version['version']} is {version['status']} and can no longer be edited.",
        details={
            "version_id": version["id"],
            "version": version["version"],
            "status": version["status"],
            "editable_statuses": ["draft", "testing"],
        },
        actions=[
            f"POST /api/admin/capabilities/{capability['id']}/versions with "
            f"from_version_id={version['id']} to branch a new draft.",
            f"Or POST /api/admin/capabilities/{capability['id']}/rollback to re-publish that config.",
        ],
    )


# ---------------------------------------------------------------- endpoints ---
@router.get("")
def list_capabilities(
    type: CapabilityType | None = None,  # noqa: A002 - matches the contract query name
    status: CapabilityStatus | None = None,
    provider_id: str | None = None,
    search: str | None = Query(default=None, description="Match name, slug or description."),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    items, total = capability_service.list_capabilities(
        type=type, status=status, provider_id=provider_id, search=search, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_capability(
    payload: CapabilityCreate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    created = capability_service.create_capability(
        name=payload.name,
        type=payload.type,
        description=payload.description,
        slug=payload.slug,
        provider_id=payload.provider_id,
        credential_id=payload.credential_id,
        needs_live_agent=payload.needs_live_agent,
        actor_id=user.id,
        ip=client_ip(request),
    )
    return created


@router.get("/{capability_id}")
def get_capability(capability_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    row = _capability_or_404(capability_id)
    payload = capability_service.serialize_capability(row, detail=True)
    payload["tests"] = capability_service.list_tests(capability_id, limit=5)[0]
    return payload


@router.patch("/{capability_id}")
def update_capability(
    capability_id: str,
    payload: CapabilityUpdate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    changes = payload.model_dump(exclude_unset=True)
    return capability_service.update_capability(
        capability_id, changes, actor_id=user.id, ip=client_ip(request)
    )


@router.delete("/{capability_id}")
def delete_capability(
    capability_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    return capability_service.delete_capability(capability_id, actor_id=user.id, ip=client_ip(request))


# ----------------------------------------------------------------- versions ---
@router.get("/{capability_id}/versions")
def list_versions(capability_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    _capability_or_404(capability_id)
    items = capability_service.list_versions(capability_id)
    return {"items": items, "total": len(items), "limit": MAX_LIMIT, "offset": 0}


@router.post("/{capability_id}/versions", status_code=201)
def create_version(
    capability_id: str,
    payload: VersionCreate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    if payload.from_version_id:
        _version_or_404(capability_id, payload.from_version_id)
    return capability_service.new_version(
        capability_id,
        from_version_id=payload.from_version_id,
        config=payload.config,
        notes=payload.notes,
        actor_id=user.id,
        ip=client_ip(request),
    )


# NOTE: the literal `diff` segment is declared before `{version_id}` so the
# parameterised route cannot swallow it.
@router.get("/{capability_id}/versions/diff")
def diff_versions(
    capability_id: str,
    from_version: str = Query(alias="from", description="Source version id."),
    to_version: str = Query(alias="to", description="Target version id."),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    if from_version == to_version:
        raise unprocessable(
            "same_version",
            "Pick two different versions to compare.",
            details={"version_id": from_version},
        )
    return capability_service.diff_versions(capability_id, from_version, to_version)


@router.get("/{capability_id}/versions/{version_id}")
def get_version(capability_id: str, version_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    _capability_or_404(capability_id)
    row = _version_or_404(capability_id, version_id)
    payload = capability_service.serialize_version(row, include_config=True)
    payload["raw_config"] = db.loads(row["config"], {})
    return payload


@router.patch("/{capability_id}/versions/{version_id}")
def update_version(
    capability_id: str,
    version_id: str,
    payload: VersionUpdate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    capability = _capability_or_404(capability_id)
    version = _version_or_404(capability_id, version_id)
    if version["status"] not in {"draft", "testing"}:
        raise _immutable(capability, version)
    return capability_service.update_version(
        capability_id,
        version_id,
        config=payload.config,
        notes=payload.notes,
        status=payload.status,
        actor_id=user.id,
        ip=client_ip(request),
    )


@router.post("/{capability_id}/versions/{version_id}/validate")
def validate_version(capability_id: str, version_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    _capability_or_404(capability_id)
    result = capability_service.validate(capability_id, version_id)
    result["actions"] = [] if not result["blocking"] else [
        f"Fix {result['errors']} blocking issue(s) before running a test.",
        "Use the test endpoint once validation is clean.",
    ]
    return result


@router.post("/{capability_id}/versions/{version_id}/test")
async def test_version(
    capability_id: str,
    version_id: str,
    payload: TestRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Make a real call and store the evidence. Never fakes a result."""
    capability = _capability_or_404(capability_id)
    version = _version_or_404(capability_id, version_id)
    caller_errors = {
        "unsupported_parameter",
        "input_schema_mismatch",
        "input_too_large",
        "invalid_input",
        "mode_not_supported",
    }
    try:
        result = await execution.test_version(
            capability_id=capability_id,
            version_id=version_id,
            payload=payload.input or {},
            actor_id=user.id,
            mode=payload.mode,
        )
    except ApiError as exc:
        # A malformed input is the caller's problem and stays an HTTP error. An
        # upstream/transport failure is a *test result*: it has to be recorded as
        # evidence, because that is exactly what the administrator must see.
        if exc.code in caller_errors or exc.status_code < 500:
            raise
        details = exc.details if isinstance(exc.details, dict) else {}
        result = {
            "status": "failed",
            "error": {"code": exc.code, "message": exc.message},
            "http_status": details.get("upstream_status"),
            "latency_ms": None,
            "request": {},
        }
    evidence = capability_service.record_test(
        capability_id=capability_id,
        version_id=version_id,
        result=result,
        actor_id=user.id,
    )
    refreshed = capability_service.get_row(capability_id)
    body: dict[str, Any] = {
        "capability_id": capability_id,
        "capability_slug": capability["slug"],
        "version_id": version_id,
        "version": version["version"],
        "status": evidence.get("status"),
        "result": result,
        "evidence": evidence,
        "saved": True,
        "test_passed": evidence.get("status") == "passed",
        "published_version": capability_service.verification_summary(refreshed)["published_version"],
        "callable": capability_service.callable_state(refreshed)["callable"],
        "real_call_attempted": True,
    }
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="capability.tested",
        object_type="capability_version",
        object_id=version_id,
        summary=f"{capability['slug']} v{version['version']} test -> {evidence.get('status')}",
        metadata={
            "capability_id": capability_id,
            "status": evidence.get("status"),
            "http_status": evidence.get("http_status"),
            "test_id": evidence.get("id"),
        },
        ip=client_ip(request),
    )
    if evidence.get("status") != "passed":
        body["actions"] = [
            "Read the request/response preview and the recorded error.",
            "Fix the version config (URL, body template, result path) or the credential.",
            "Run the test again — publishing stays blocked until a real call passes.",
        ]
    return body


@router.post("/{capability_id}/versions/{version_id}/publish")
def publish_version(
    capability_id: str,
    version_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    _version_or_404(capability_id, version_id)
    return capability_service.publish(
        capability_id, version_id, actor_id=user.id, ip=client_ip(request)
    )


@router.post("/{capability_id}/versions/{version_id}/deprecate")
def deprecate_version(
    capability_id: str,
    version_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    _version_or_404(capability_id, version_id)
    return capability_service.deprecate(
        capability_id, version_id, actor_id=user.id, ip=client_ip(request)
    )


@router.post("/{capability_id}/rollback")
def rollback(
    capability_id: str,
    payload: RollbackRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    _version_or_404(capability_id, payload.version_id)
    return capability_service.rollback(
        capability_id, payload.version_id, actor_id=user.id, ip=client_ip(request)
    )


@router.get("/{capability_id}/tests")
def list_tests(
    capability_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _capability_or_404(capability_id)
    items, total = capability_service.list_tests(capability_id, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/{capability_id}/invoke")
async def invoke_capability(
    capability_id: str,
    payload: InvokeRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Admin manual call. Draft versions are allowed; the surface is recorded."""
    capability = _capability_or_404(capability_id)
    principal = capability_service.admin_principal(user)
    options = execution.InvokeOptions(
        mode=payload.mode,
        surface="admin",
        allow_draft=True,
        version_id=payload.version_id,
        provider_id=payload.provider_id,
        dry_run=payload.dry_run,
        timeout_s=payload.timeout_s,
        actor_type="user",
        actor_id=user.id,
    )
    result = await execution.invoke(principal, capability_id, payload.input or {}, options)
    body = result.to_dict()
    body["surface"] = "admin"
    body["dry_run"] = payload.dry_run
    body["version_status"] = (
        _version_or_404(capability_id, payload.version_id)["status"]
        if payload.version_id
        else (capability_service.current_version_row(capability) or {}).get("status")
    )
    body["callable"] = capability_service.callable_state(capability)["callable"]
    if payload.dry_run:
        body["request"] = result.raw_request
        body["message"] = "Dry run: the request was built but nothing was sent."
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="capability.invoked",
        object_type="capability",
        object_id=capability_id,
        summary=f"admin manual call to {capability['slug']} ({'dry run' if payload.dry_run else body['status']})",
        metadata={
            "invocation_id": body.get("invocation_id"),
            "request_id": body.get("request_id"),
            "status": body.get("status"),
            "mode": body.get("mode"),
        },
        ip=client_ip(request),
    )
    return body


@router.post("/{capability_id}/route-preview")
def route_preview(
    capability_id: str,
    payload: RoutePreviewRequest,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Explain which route would be chosen — routing.candidates_for, no network."""
    from app.services import routing

    capability = _capability_or_404(capability_id)
    version = capability_service.current_version_row(capability)
    if version is None:
        raise not_found("Capability version")
    selection = routing.candidates_for(
        capability=capability,
        version=version,
        input_payload=payload.input or {},
        mode=payload.mode,
        preferred_provider_id=payload.provider_id,
    )
    explain = routing.explain_selection(selection)
    explain["capability_id"] = capability_id
    explain["capability_slug"] = capability["slug"]
    explain["version"] = version["version"]
    explain["version_status"] = version["status"]
    explain["mode"] = payload.mode
    explain["network_call"] = False
    return explain


__all__ = ["router"]
