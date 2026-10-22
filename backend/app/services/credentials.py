"""Credential lifecycle: store, mask, verify, rotate, delete-with-impact.

The cleartext secret map exists only inside this module and the executor. It is
never returned by an API response, never written to a log, and never included
in an exported SKILL or npm package.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.security import crypto
from app.security.redact import Redactor, preview
from app.services import audit

AUTH_TYPES = {"api_key", "bearer", "basic", "custom_header"}
SECRET_FIELDS: dict[str, list[str]] = {
    "api_key": ["api_key"],
    "bearer": ["token"],
    "basic": ["username", "password"],
    "custom_header": ["value"],
}

EXPIRING_SOON_DAYS = 14


@dataclass(frozen=True)
class CredentialSecrets:
    values: dict[str, str]

    def redactor(self) -> Redactor:
        return Redactor(self.values.values())


def _validate_auth_type(auth_type: str) -> str:
    if auth_type not in AUTH_TYPES:
        raise unprocessable(
            "invalid_auth_type",
            f"Authentication type {auth_type!r} is not supported.",
            actions=[f"Use one of: {', '.join(sorted(AUTH_TYPES))}."],
        )
    return auth_type


def _clean_secrets(auth_type: str, secrets: dict[str, str]) -> dict[str, str]:
    expected = SECRET_FIELDS[auth_type]
    missing = [field for field in expected if not str(secrets.get(field, "")).strip()]
    if missing:
        raise unprocessable(
            "missing_secret_field",
            f"The {auth_type} authentication type needs: {', '.join(expected)}.",
            details={"missing": missing},
        )
    return {field: str(secrets[field]).strip() for field in expected if field in secrets}


def mask_payload(row: dict[str, Any]) -> dict[str, Any]:
    """The only shape a credential is ever serialised in for the UI/API."""
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "provider_name": row.get("provider_name"),
        "name": row["name"],
        "auth_type": row["auth_type"],
        "hint": row["hint"],
        "field_names": db.loads(row["field_names"], []),
        "config": db.loads(row["config"], {}),
        "status": row["status"],
        "expires_at": row["expires_at"],
        "last_verified_at": row["last_verified_at"],
        "last_verify_status": row["last_verify_status"],
        "last_verify_message": row["last_verify_message"],
        "last_rotated_at": row["last_rotated_at"],
        "key_id": row["key_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "usage": row.get("usage"),
    }


def get_row(credential_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT c.*, p.name AS provider_name, p.category AS provider_category, p.base_url AS provider_base_url
             FROM credentials c LEFT JOIN providers p ON p.id = c.provider_id WHERE c.id = ?""",
        (credential_id,),
    )
    if not row:
        raise not_found("Credential")
    return row


def list_credentials(
    *,
    provider_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if provider_id:
        where.append("c.provider_id = ?")
        params.append(provider_id)
    if status:
        where.append("c.status = ?")
        params.append(status)
    if search:
        where.append("(c.name LIKE ? OR COALESCE(p.name,'') LIKE ? OR c.hint LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    clause = " AND ".join(where)
    total = db.query_scalar(
        f"SELECT COUNT(*) FROM credentials c LEFT JOIN providers p ON p.id = c.provider_id WHERE {clause}",
        params,
    )
    rows = db.query_all(
        f"""SELECT c.*, p.name AS provider_name,
                   (SELECT COUNT(*) FROM capabilities cap WHERE cap.credential_id = c.id) AS capability_count
              FROM credentials c LEFT JOIN providers p ON p.id = c.provider_id
             WHERE {clause}
             ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    out = []
    for row in rows:
        payload = mask_payload(row)
        payload["usage"] = {
            "capabilities": int(row["capability_count"] or 0),
        }
        payload["expiring_soon"] = _expiring_soon(row["expires_at"])
        out.append(payload)
    return out, int(total or 0)


def _expiring_soon(expires_at: str | None) -> bool:
    when = db.parse_iso(expires_at)
    if not when:
        return False
    return when <= db.utcnow() + timedelta(days=EXPIRING_SOON_DAYS)


def create_credential(
    *,
    provider_id: str,
    name: str,
    auth_type: str,
    secrets: dict[str, str],
    config: dict[str, Any] | None = None,
    expires_at: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    _validate_auth_type(auth_type)
    provider = db.query_one("SELECT id, name FROM providers WHERE id = ?", (provider_id,))
    if not provider:
        raise not_found("Provider")
    values = _clean_secrets(auth_type, secrets)
    cfg = config or {}
    if auth_type == "custom_header" and not cfg.get("header_name"):
        raise unprocessable(
            "missing_header_name",
            "A custom-header credential needs the header name to send.",
            details={"field": "config.header_name"},
        )
    envelope, key_id = crypto.encrypt_map(values)
    now = db.iso()
    credential_id = db.new_id("cred")
    db.insert(
        "credentials",
        {
            "id": credential_id,
            "provider_id": provider_id,
            "name": name.strip(),
            "auth_type": auth_type,
            "secret_ciphertext": envelope,
            "hint": crypto.build_hint(values),
            "field_names": db.dumps(list(values.keys())),
            "config": db.dumps(cfg),
            "key_id": key_id,
            "status": status,
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
        },
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="credential.created",
        object_type="credential",
        object_id=credential_id,
        summary=f"{name} ({auth_type}) for {provider['name']}",
        metadata={"auth_type": auth_type, "fields": list(values.keys()), "provider_id": provider_id},
        ip=ip,
    )
    return mask_payload(get_row(credential_id))


def update_credential(
    credential_id: str,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    expires_at: str | None = None,
    status: str | None = None,
    secrets: dict[str, str] | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    row = get_row(credential_id)
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name.strip()
    if config is not None:
        values["config"] = db.dumps(config)
    if expires_at is not None:
        values["expires_at"] = expires_at or None
    if status is not None:
        if status not in {"active", "disabled", "expired"}:
            raise unprocessable("invalid_status", "Credential status must be active, disabled or expired.")
        if status == "disabled":
            _ensure_disable_is_safe(credential_id)
        values["status"] = status
    if secrets:
        cleaned = _clean_secrets(row["auth_type"], secrets)
        envelope, key_id = crypto.encrypt_map(cleaned)
        values.update(
            {
                "secret_ciphertext": envelope,
                "key_id": key_id,
                "hint": crypto.build_hint(cleaned),
                "field_names": db.dumps(list(cleaned.keys())),
                "last_rotated_at": db.iso(),
                "last_verify_status": None,
                "last_verify_message": None,
            }
        )
    if values:
        values["updated_at"] = db.iso()
        db.update("credentials", credential_id, values)
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="credential.rotated" if secrets else "credential.updated",
        object_type="credential",
        object_id=credential_id,
        summary=f"{row['name']} updated" + (" with new secret material" if secrets else ""),
        metadata={"fields_changed": sorted(values.keys())},
        ip=ip,
    )
    return mask_payload(get_row(credential_id))


def _ensure_disable_is_safe(credential_id: str) -> None:
    """Warn-with-block: disabling a credential that a published capability
    depends on would silently break calls, so the caller must confirm."""
    in_use = db.query_all(
        """SELECT c.slug, c.status FROM capabilities c
            WHERE c.credential_id = ? AND c.status IN ('published','testing')""",
        (credential_id,),
    )
    if in_use:
        raise conflict(
            "credential_in_use",
            "This credential is used by published capabilities. Disabling it will break them.",
            details={"capabilities": [row["slug"] for row in in_use]},
            actions=[
                "Point those capabilities at another credential, or",
                "Confirm that calls using this credential may fail.",
            ],
        )


def delete_credential(credential_id: str, *, actor_id: str | None = None, ip: str | None = None, force: bool = False) -> dict[str, Any]:
    row = get_row(credential_id)
    impact = impact_report(credential_id)
    if impact["blocking"] and not force:
        raise conflict(
            "credential_referenced",
            "This credential is referenced by capabilities and cannot be deleted yet.",
            details=impact,
            actions=["Re-point or delete the affected capabilities first.", "Or disable the credential instead."],
        )
    db.execute("UPDATE capabilities SET credential_id = NULL, updated_at = ? WHERE credential_id = ?", (db.iso(), credential_id))
    db.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="credential.deleted",
        object_type="credential",
        object_id=credential_id,
        summary=f"{row['name']} deleted",
        metadata={"impact": impact},
        ip=ip,
    )
    return {"deleted": True, "impact": impact}


def impact_report(credential_id: str) -> dict[str, Any]:
    capabilities = db.query_all(
        "SELECT id, slug, name, status FROM capabilities WHERE credential_id = ?", (credential_id,)
    )
    routes = db.query_all("SELECT id, capability_id FROM routes WHERE credential_id = ?", (credential_id,))
    tasks = db.query_scalar(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','cancel_requested') AND capability_id IN "
        "(SELECT id FROM capabilities WHERE credential_id = ?)",
        (credential_id,),
    )
    blocking = any(row["status"] in {"published", "testing"} for row in capabilities)
    return {
        "capabilities": capabilities,
        "routes": len(routes),
        "active_tasks": int(tasks or 0),
        "blocking": blocking,
    }


# ------------------------------------------------------------------ secrets --
def get_secret_map(credential_id: str) -> CredentialSecrets:
    row = db.query_one("SELECT secret_ciphertext, key_id, status FROM credentials WHERE id = ?", (credential_id,))
    if not row:
        raise not_found("Credential")
    if row["status"] != "active":
        raise ApiError(
            "credential_inactive",
            "This credential is disabled; calls using it are refused.",
            status_code=409,
            actions=["Enable the credential in Providers → Credentials."],
        )
    return CredentialSecrets(values=crypto.decrypt_map(row["secret_ciphertext"]))


def build_auth_headers(row: dict[str, Any], secrets: CredentialSecrets) -> dict[str, str]:
    """Produce the wire headers for a credential. Called at send time only."""
    config = db.loads(row["config"], {})
    values = secrets.values
    auth_type = row["auth_type"]
    if auth_type == "api_key":
        header = (config.get("header_name") or "x-api-key").strip()
        prefix = (config.get("prefix") or "").strip()
        value = values.get("api_key", "")
        return {header: f"{prefix} {value}".strip() if prefix else value}
    if auth_type == "bearer":
        return {"authorization": f"Bearer {values.get('token', '')}"}
    if auth_type == "basic":
        raw = f"{values.get('username','')}:{values.get('password','')}".encode("utf-8")
        return {"authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    if auth_type == "custom_header":
        header = (config.get("header_name") or "x-custom-auth").strip()
        template = config.get("value_template") or "{value}"
        return {header: str(template).replace("{value}", values.get("value", ""))}
    raise ApiError("invalid_auth_type", f"Unsupported auth type {auth_type!r}.", status_code=500)


# -------------------------------------------------------------------- verify --
@dataclass
class VerifyResult:
    status: str  # valid|invalid|unreachable|rate_limited|blocked|unknown
    message: str
    http_status: int | None = None
    latency_ms: int | None = None


async def verify_credential(
    credential_id: str,
    *,
    probe_url: str | None = None,
    method: str = "GET",
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Actively check a credential against the provider.

    A provider rarely exposes a cheap "is this key valid" endpoint, so the probe
    interprets the auth-relevant status codes: 2xx → valid, 401/403 → invalid,
    429 → valid but throttled, other 4xx → reachable (endpoint shape unknown).
    """
    from app.services.network import NetworkPolicyError, classify_network_error, guarded_request

    row = get_row(credential_id)
    secrets = get_secret_map(credential_id)
    config = db.loads(row["config"], {})
    target = probe_url or config.get("test_url") or row.get("provider_base_url")
    redactor = secrets.redactor()
    if not target:
        result = VerifyResult("unknown", "No probe URL configured for this credential.")
    else:
        headers = build_auth_headers(row, secrets)
        headers.setdefault("accept", "application/json")
        try:
            response = await guarded_request(method, target, headers=headers, timeout_s=15)
            if 200 <= response.status_code < 300:
                result = VerifyResult("valid", "Provider accepted the credential.", response.status_code, response.elapsed_ms)
            elif response.status_code in (401, 403):
                result = VerifyResult("invalid", "Provider rejected the credential.", response.status_code, response.elapsed_ms)
            elif response.status_code == 429:
                result = VerifyResult("rate_limited", "Credential worked but the provider is rate limiting.", 429, response.elapsed_ms)
            elif response.status_code == 404:
                result = VerifyResult(
                    "unknown",
                    "The probe URL returned 404 — set a provider endpoint that requires authentication.",
                    404,
                    response.elapsed_ms,
                )
            else:
                result = VerifyResult(
                    "unknown",
                    f"Probe returned HTTP {response.status_code}; the credential was neither accepted nor rejected.",
                    response.status_code,
                    response.elapsed_ms,
                )
        except NetworkPolicyError as exc:
            # A refusal by our own outbound policy is not "the provider is down":
            # the request was never sent. Saying "unreachable" would send the user
            # looking for a network fault that does not exist.
            result = VerifyResult("blocked", redactor.text(str(exc)))
        except Exception as exc:  # noqa: BLE001 - transport errors are expected here
            _, message = classify_network_error(exc)
            result = VerifyResult("unreachable", redactor.text(message))

    db.execute(
        "UPDATE credentials SET last_verified_at = ?, last_verify_status = ?, last_verify_message = ?, updated_at = ? WHERE id = ?",
        (db.iso(), result.status, result.message[:500], db.iso(), credential_id),
    )
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="credential.verified",
        object_type="credential",
        object_id=credential_id,
        summary=f"verification → {result.status}",
        metadata={"http_status": result.http_status, "message": result.message},
        ip=ip,
    )
    return {
        "status": result.status,
        "message": result.message,
        "http_status": result.http_status,
        "latency_ms": result.latency_ms,
        "checked_at": db.iso(),
    }


# ------------------------------------------------------------------ rotation --
def rotate_master_key(*, actor_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
    """Re-wrap every stored secret under the active key id."""
    ring = crypto.load_keyring()
    rows = db.query_all("SELECT id, secret_ciphertext, key_id FROM credentials")
    rotated = 0
    for row in rows:
        envelope, key_id, changed = crypto.rewrap(row["secret_ciphertext"], ring=ring)
        if changed:
            db.execute(
                "UPDATE credentials SET secret_ciphertext = ?, key_id = ?, updated_at = ? WHERE id = ?",
                (envelope, key_id, db.iso(), row["id"]),
            )
            rotated += 1
    audit.record(
        actor_type="user",
        actor_id=actor_id,
        action="credential.key_rotation",
        object_type="system",
        object_id="keyring",
        summary=f"re-wrapped {rotated} credential(s) under key {ring.active}",
        metadata={"rotated": rotated, "active_key": ring.active},
        ip=ip,
    )
    return {"rotated": rotated, "active_key": ring.active, "total": len(rows)}


def redacted_preview(credential_id: str, payload: Any) -> str:
    try:
        secrets = get_secret_map(credential_id)
    except Exception:  # noqa: BLE001 - preview must never fail the request
        return preview(payload)
    return preview(payload, secrets=secrets.values.values())
