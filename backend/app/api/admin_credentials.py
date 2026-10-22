"""Credentials: write-only secrets with a visible lifecycle.

Owner: backend-capabilities (docs/CONTRACT.md §5.3). All cryptography, masking and
impact analysis lives in `app.services.credentials`; this module only validates
shapes, maps errors onto the uniform envelope and audits.

Rules enforced here:
  * `secret` is write-only. It is accepted, encrypted by the service and never
    echoed — responses only ever contain `hint`.
  * A test-call never claims a call happened unless one did. Without a tested
    capability the best we can do is build the request (`dry_run`), and the
    response says exactly that.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.deps import AuthUser, client_ip, require_admin
from app.services import audit, capabilities as capability_service, credentials as credential_service

router = APIRouter(prefix="/api/admin/credentials", tags=["admin-credentials"])

MAX_LIMIT = 200
AuthType = Literal["api_key", "bearer", "basic", "custom_header"]
CredentialStatus = Literal["active", "disabled", "expired"]

SECRET_FIELDS = credential_service.SECRET_FIELDS


# ------------------------------------------------------------------- models ---
class CredentialConfig(BaseModel):
    """Non-secret auth shaping (header name, scheme prefix, test URL) and the
    model this credential speaks to.

    `model` matters because `agent_runtime.resolve_model()` falls back to it when no
    `agent.model` setting names a model — it used to be readable by the resolver but
    rejected here with 422, so that documented fallback could never be configured.
    """

    model_config = ConfigDict(extra="forbid")

    header_name: str | None = Field(default=None, max_length=120)
    prefix: str | None = Field(default=None, max_length=60)
    value_template: str | None = Field(default=None, max_length=400)
    test_url: str | None = Field(default=None, max_length=2000)
    username: str | None = Field(default=None, max_length=200)
    #: Default model id for this credential (e.g. "glm-5.3-flash", "gpt-4o-mini").
    model: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=120)

    @field_validator("header_name")
    @classmethod
    def _header_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if any(ch in text for ch in " \t\r\n:") or not text.replace("-", "").replace("_", "").isalnum():
            raise ValueError("must be a plain HTTP header name (letters, digits, - and _)")
        return text


class SecretPayload(BaseModel):
    """Write-only secret material. Field names depend on `auth_type`."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    value: str | None = None

    def to_map(self) -> dict[str, str]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class CredentialCreate(BaseModel):
    """Body of `POST /api/admin/credentials` (CONTRACT §5.3)."""

    provider_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    auth_type: AuthType = "api_key"
    secret: SecretPayload = Field(description="Write-only secret fields for the auth type.")
    config: CredentialConfig | None = None
    expires_at: str | None = Field(default=None, description="ISO-8601 timestamp, optional.")
    status: CredentialStatus = "active"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("expires_at")
    @classmethod
    def _expiry(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = db.parse_iso(value)
        if parsed is None:
            raise ValueError("must be an ISO-8601 timestamp such as 2026-01-31T00:00:00Z")
        return db.iso(parsed)


class CredentialUpdate(BaseModel):
    """Body of `PATCH /api/admin/credentials/{id}` — all fields optional."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: CredentialConfig | None = None
    expires_at: str | None = None
    status: CredentialStatus | None = None
    secret: SecretPayload | None = Field(default=None, description="Replaces the stored secret when sent.")


class VerifyRequest(BaseModel):
    probe_url: str | None = Field(default=None, max_length=2000)
    method: Literal["GET", "HEAD", "POST"] = "GET"


class TestCallRequest(BaseModel):
    """Body of `POST /api/admin/credentials/{id}/test-call`."""

    capability_id: str | None = Field(default=None, description="Run the capability's tested version.")
    input: dict[str, Any] = Field(default_factory=dict, description="Sample input for the call.")
    mode: Literal["sync", "async", "stream"] = "sync"


class RotateKeyRequest(BaseModel):
    """Confirmation body for the owner-only key rotation."""

    confirm: bool = Field(default=False, description="Must be true: rotation re-wraps every credential.")


# ----------------------------------------------------------------- helpers ----
def _require_secret_fields(auth_type: str, secrets: dict[str, str]) -> None:
    expected = SECRET_FIELDS[auth_type]
    missing = [field for field in expected if not str(secrets.get(field, "")).strip()]
    if missing:
        raise unprocessable(
            "missing_secret_field",
            f"The {auth_type} authentication type needs: {', '.join(expected)}.",
            details={"missing": missing, "fields": expected},
            actions=[f"Send secret = {{{', '.join(f'{name!r}: ...' for name in expected)}}}."],
        )
    unexpected = sorted(set(secrets) - set(expected))
    if unexpected:
        raise unprocessable(
            "unexpected_secret_field",
            f"{auth_type} does not use {', '.join(unexpected)}.",
            details={"unexpected": unexpected, "fields": expected},
            actions=[f"Send only: {', '.join(expected)}."],
        )


def _config_map(config: CredentialConfig | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {key: value for key, value in config.model_dump(exclude_none=True).items()}


def _get_or_404(credential_id: str) -> dict[str, Any]:
    try:
        return credential_service.get_row(credential_id)
    except ApiError:
        raise not_found("Credential") from None


# ---------------------------------------------------------------- endpoints ---
@router.get("")
def list_credentials(
    provider_id: str | None = None,
    status: CredentialStatus | None = None,
    search: str | None = Query(default=None, description="Match credential, provider name or hint."),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    items, total = credential_service.list_credentials(
        provider_id=provider_id, status=status, search=search, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_credential(
    payload: CredentialCreate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    secrets = payload.secret.to_map()
    _require_secret_fields(payload.auth_type, secrets)
    created = credential_service.create_credential(
        provider_id=payload.provider_id,
        name=payload.name,
        auth_type=payload.auth_type,
        secrets=secrets,
        config=_config_map(payload.config),
        expires_at=payload.expires_at,
        actor_id=user.id,
        ip=client_ip(request),
        status=payload.status,
    )
    return created


# NOTE: FastAPI resolves routes in declaration order, so the literal
# `/rotate-key` POST is declared here, before any parameterised POST under the
# same prefix could shadow it.
@router.post("/rotate-key")
def rotate_key(
    payload: RotateKeyRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Re-wrap every stored secret under the active master key (owner-only path)."""
    if not payload.confirm:
        raise conflict(
            "confirmation_required",
            "Key rotation rewrites every credential; confirm the action explicitly.",
            details={"field": "confirm"},
            actions=['Resend with {"confirm": true} once the keyring backup exists.'],
        )
    result = credential_service.rotate_master_key(actor_id=user.id, ip=client_ip(request))
    return {**result, "confirmed_by": user.username}


@router.get("/{credential_id}")
def get_credential(credential_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    row = _get_or_404(credential_id)
    payload = credential_service.mask_payload(row)
    payload["impact"] = credential_service.impact_report(credential_id)
    return payload


@router.patch("/{credential_id}")
def update_credential(
    credential_id: str,
    payload: CredentialUpdate,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    row = _get_or_404(credential_id)
    secrets = payload.secret.to_map() if payload.secret is not None else None
    if secrets is not None:
        _require_secret_fields(row["auth_type"], secrets)
    updated = credential_service.update_credential(
        credential_id,
        name=payload.name,
        config=_config_map(payload.config),
        expires_at=payload.expires_at,
        status=payload.status,
        secrets=secrets,
        actor_id=user.id,
        ip=client_ip(request),
    )
    if payload.config is not None or payload.expires_at is not None:
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="credential.updated",
            object_type="credential",
            object_id=credential_id,
            summary=f"{row['name']} configuration updated",
            metadata={"fields_changed": ["config", "expires_at"]},
            ip=client_ip(request),
        )
    return updated


@router.delete("/{credential_id}")
def delete_credential(
    credential_id: str,
    request: Request,
    force: bool = Query(default=False, description="Detach capabilities and delete anyway."),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    return credential_service.delete_credential(
        credential_id, actor_id=user.id, ip=client_ip(request), force=force
    )


@router.get("/{credential_id}/impact")
def credential_impact(credential_id: str, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    _get_or_404(credential_id)
    return credential_service.impact_report(credential_id)


@router.post("/{credential_id}/verify")
async def verify_credential(
    credential_id: str,
    payload: VerifyRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    _get_or_404(credential_id)
    result = await credential_service.verify_credential(
        credential_id,
        probe_url=payload.probe_url,
        method=payload.method,
        actor_id=user.id,
        ip=client_ip(request),
    )
    result["credential_id"] = credential_id
    result["truthful"] = True
    return result


@router.post("/{credential_id}/test-call")
async def test_call(
    credential_id: str,
    payload: TestCallRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Prove what this credential can actually do — and say what it cannot.

    The executor injects a credential from the *capability* it configures, so a
    credential override is out of scope. What we can honestly report is:
      * which capabilities this credential belongs to and whether any of them
        has a passing test / is callable;
      * a `dry_run` build of the request the chosen capability version would
        send (no network, no claim of success);
      * which secret fields are present and when the credential was last
        verified.
    """
    from app.services import execution

    row = _get_or_404(credential_id)
    linked = db.query_all(
        """SELECT id, slug, name, status, current_version_id FROM capabilities
            WHERE credential_id = ? ORDER BY created_at ASC""",
        (credential_id,),
    )
    target: dict[str, Any] | None = None
    if payload.capability_id:
        target = next((item for item in linked if item["id"] == payload.capability_id), None)
        if target is None:
            raise unprocessable(
                "capability_not_linked",
                "That capability does not use this credential.",
                details={"credential_id": credential_id, "capability_id": payload.capability_id},
                actions=["Pick one of the capabilities listed in `candidate_capabilities`."],
            )
    elif linked:
        target = next(
            (
                item
                for item in linked
                if capability_service.latest_test(capability_id=item["id"], version_id=item["current_version_id"])
            ),
            linked[0],
        )

    verified_fields = db.loads(row["field_names"], [])
    response: dict[str, Any] = {
        "credential_id": credential_id,
        "credential_name": row["name"],
        "auth_type": row["auth_type"],
        "hint": row["hint"],
        "status": row["status"],
        "secret_fields": verified_fields,
        "last_verified_at": row.get("last_verified_at"),
        "last_verify_status": row.get("last_verify_status"),
        "candidate_capabilities": [
            {
                "id": item["id"],
                "slug": item["slug"],
                "name": item["name"],
                "status": item["status"],
                "last_test_status": (
                    capability_service.latest_test(
                        capability_id=item["id"], version_id=item["current_version_id"]
                    ) or {}
                ).get("status"),
            }
            for item in linked
        ],
        "real_call_performed": False,
        "message": "",
        "next_steps": [],
    }

    if target is None:
        response["message"] = (
            "This credential is not attached to any capability, so no request could be built. "
            "No call was made."
        )
        response["next_steps"] = [
            "Attach the credential to a capability (PATCH /api/admin/capabilities/{id}).",
            "Then POST /api/admin/capabilities/{id}/versions/{version_id}/test with a sample input.",
        ]
        return response

    version = capability_service.current_version_row(
        db.query_one("SELECT * FROM capabilities WHERE id = ?", (target["id"],)) or {}
    )
    if version is None:
        response["message"] = "The linked capability has no version, so no request could be built."
        response["next_steps"] = ["Create a version, then run a capability test."]
        return response

    principal = capability_service.admin_principal(user)
    response["capability"] = {"id": target["id"], "slug": target["slug"], "name": target["name"]}
    response["version"] = version["version"]

    latest = capability_service.latest_test(capability_id=target["id"], version_id=version["id"])
    if latest is None:
        latest = capability_service.latest_test(capability_id=target["id"])
    if latest is None:
        response["message"] = (
            "No call was made. This credential's capability has never been tested, so there is no "
            "evidence to report — and the executor takes its credential from the capability, not from "
            "this endpoint, so a credential override is out of scope."
        )
        response["next_steps"] = [
            f"POST /api/admin/capabilities/{target['id']}/versions/{version['id']}/test with a sample "
            "input to make a real call and record the evidence.",
            f"Or POST /api/admin/credentials/{credential_id}/verify to probe the credential directly.",
        ]
        return response

    response["last_capability_test"] = {
        "id": latest["id"],
        "status": latest["status"],
        "version_id": latest.get("version_id"),
        "created_at": latest["created_at"],
    }
    try:
        preview_result = await execution.invoke(
            principal,
            target["id"],
            payload.input or {},
            execution.InvokeOptions(
                mode=payload.mode,
                surface="admin",
                allow_draft=True,
                version_id=version["id"],
                dry_run=True,
                actor_type="user",
                actor_id=user.id,
            ),
        )
    except ApiError as exc:
        response["dry_run"] = {"ok": False, "code": exc.code, "message": exc.message, "actions": exc.actions}
        response["message"] = (
            "No call was made and no request could be built: the capability is not yet callable "
            f"({exc.code}). {exc.message}"
        )
        response["next_steps"] = exc.actions or [
            "Validate and test the capability version.",
            "Publish it so a route can be selected.",
        ]
        audit.record(
            actor_type="user",
            actor_id=user.id,
            action="credential.test_call_build_failed",
            object_type="credential",
            object_id=credential_id,
            summary=f"request build failed for {target['slug']} ({exc.code})",
            metadata={"code": exc.code, "capability_id": target["id"]},
            ip=client_ip(request),
        )
        return response

    response["dry_run"] = {
        "ok": True,
        "request": preview_result.raw_request,
        "mode": preview_result.mode,
        "provider": preview_result.provider,
        "route_reason": preview_result.route_reason,
    }
    response["message"] = (
        "Dry run only: the request the capability would send was built successfully and nothing was "
        "sent over the network. A full call requires a capability test, because the executor takes the "
        "credential from the capability rather than from this endpoint."
    )
    response["next_steps"] = [
        f"POST /api/admin/capabilities/{target['id']}/versions/{version['id']}/test with a sample input "
        "to make a real call and record the evidence.",
        "Publish the version once that test passes.",
        f"Or POST /api/admin/credentials/{credential_id}/verify to probe the credential directly.",
    ]
    audit.record(
        actor_type="user",
        actor_id=user.id,
        action="credential.test_call_dry_run",
        object_type="credential",
        object_id=credential_id,
        summary=f"dry-run request build for {target['slug']} v{version['version']}",
        metadata={"capability_id": target["id"], "version_id": version["id"]},
        ip=client_ip(request),
    )
    return response


__all__ = ["router"]
