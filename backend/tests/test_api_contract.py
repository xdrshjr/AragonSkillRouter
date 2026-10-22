"""Acceptance: the HTTP contract surface (CONTRACT §2.1, §5, §6, §7).

Scope note: this is an **engine-level** proof. Every request is answered by the
real FastAPI application (`app.main.create_app()`), the real routers and the real
middleware stack, against a throw-away SQLite file. No external provider, sidecar
or MCP client is involved.

What this file locks down, from the route table itself rather than from a
hand-written list, so a *future* router cannot quietly ship without auth:

  * every `/api/**` route that is not explicitly public answers an anonymous
    request with HTTP 401 and the uniform error envelope
    `{error: {code, message, details, request_id, actions}}`;
  * every mutating `/api` route rejects a request that carries a session cookie
    but no `x-csrf-token` (403 `csrf_failed`);
  * a mutating `/api` request whose `Origin` is outside `SR_TRUSTED_ORIGINS` is
    rejected (403 `origin_rejected`) even when the CSRF token is valid.

The one thing that is *not* proven here: that the answer for an authorised
caller is right — that lives in the per-surface suites (`test_external_api.py`,
`test_mcp.py`, `test_capabilities_api.py`, `test_skills.py`, ...).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

NEW_PASSWORD = "Acceptance-Passw0rd!9"
TRUSTED_ORIGIN = "http://localhost:3000"
FOREIGN_ORIGIN = "https://evil.example"

#: Mutating `/api` prefixes the middleware deliberately exempts (CONTRACT §2.1):
#: the login itself cannot carry a page token, and the external/MCP surfaces
#: authenticate with a bearer token instead of a cookie.
CSRF_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/health",
    "/api/v1/",
    "/api/mcp",
    "/api/agent/stream",
)

#: Routes that are public *by contract*. Every entry carries the reason, so a
#: reader can tell a deliberate exception from an accidental hole.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/login"): "sign-in must work before a session exists",
    ("POST", "/api/auth/logout"): "idempotent sign-out; discloses nothing",
    ("GET", "/api/health"): "liveness probe",
    ("GET", "/api/health/ready"): "readiness probe",
    ("GET", "/api/bootstrap/status"): "first-run status for the sign-in page",
    ("GET", "/api/meta"): "non-secret build facts (CONTRACT §2.1)",
    ("GET", "/api/docs"): "FastAPI reference UI",
    ("GET", "/api/docs/oauth2-redirect"): "FastAPI reference UI asset",
    ("GET", "/api/openapi.json"): "the admin OpenAPI document",
    # /api/v1 and its OpenAPI document are discovery aids for the installer: they
    # describe the surface and carry no tenant data ("without auth" in
    # app/api/external.py). Everything else under /api/v1 requires the token.
    ("GET", "/api/v1"): "external route index, no data",
    ("GET", "/api/v1/openapi.json"): "external OpenAPI document, no data",
    # MCP is a JSON-RPC transport: auth is answered for POST only. GET is a
    # documented 405 and DELETE is a stateless session acknowledgement.
    ("GET", "/api/mcp"): "transport answer 405 by design (see app/api/mcp.py)",
    ("DELETE", "/api/mcp"): "stateless session teardown by design",
}

ENVELOPE_KEYS = {"code", "message", "details", "request_id", "actions"}

#: The sidecar's tool callback is authenticated by a *run-scoped bearer token*
#: (CONTRACT §10), not by a cookie. An unknown tool name answers 404 before the
#: token is inspected, so the probe uses a real tool name: that way the request
#: reaches the token check and must answer 401.
PROBE_OVERRIDES: dict[str, str] = {"/api/admin/agent/tools/{tool_name}": "__first_tool__"}


# ---------------------------------------------------------------- fixtures ---
@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Temp data dir + fresh keyring; never the real data directory."""
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.setenv("SR_INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SR_INITIAL_ADMIN_PASSWORD", "adminjr")
    monkeypatch.setenv("SR_TRUSTED_ORIGINS", TRUSTED_ORIGIN)
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


@pytest.fixture()
def app_client(isolated):
    """The production app object (``app.main.app`` is ``create_app()``)."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    app = create_app()
    # raise_server_exceptions=False: an unhandled 500 must show up as a status
    # code in the failure message instead of aborting the whole sweep.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


# ------------------------------------------------------------------ helpers ---
def _api_routes(app) -> list[tuple[str, str]]:
    """(method, path) for every /api route the app actually serves."""
    from fastapi.routing import APIRoute

    found: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not str(route.path).startswith("/api"):
            continue
        for method in sorted(set(route.methods or set()) - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return sorted(found)


def _fill(path: str) -> str:
    """Replace every path parameter with a plausible-but-unknown id."""
    out = []
    depth = 0
    for char in path:
        if char == "{":
            if depth == 0:
                out.append("sr_contract_probe")
            depth += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def _probe_url(path: str) -> str:
    """Concrete URL for a route template, honouring the documented overrides."""
    override = PROBE_OVERRIDES.get(path)
    if override is None:
        return _fill(path)
    if override == "__first_tool__":
        from app.services import agent_runtime

        tool_name = sorted(agent_runtime.TOOL_HANDLERS)[0]
        return path.replace("{tool_name}", tool_name)
    return path.replace("{tool_name}", override)


def _body_for(method: str) -> dict[str, Any] | None:
    return {} if method in {"POST", "PUT", "PATCH"} else None


def _assert_envelope(response, *, context: str) -> dict[str, Any]:
    body = response.json()
    assert isinstance(body, dict) and "error" in body, f"{context}: no error envelope: {response.text[:300]}"
    error = body["error"]
    missing = ENVELOPE_KEYS - set(error)
    assert not missing, f"{context}: envelope missing {sorted(missing)}: {response.text[:300]}"
    assert error["code"], f"{context}: empty error code"
    assert isinstance(error["actions"], list), f"{context}: actions must be a list"
    assert error["request_id"].startswith("req_"), f"{context}: request_id {error['request_id']!r} is not a request id"
    return error


def _login(client) -> str:
    """Sign in and clear the first-run password gate. Returns the CSRF token."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "adminjr", "new_password": NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert changed.status_code == 200, changed.text
    return changed.json()["csrf_token"]


# -------------------------------------------------------------------- tests ---
def test_anonymous_calls_to_every_protected_route_return_401_envelope(app_client):
    """Every non-public /api route refuses an anonymous caller, uniformly.

    Fails with the offending `METHOD /path` list, which is the guard against a
    router (or a new endpoint) being added without an auth dependency.
    """
    app, client = app_client
    routes = _api_routes(app)
    protected = [(method, path) for method, path in routes if (method, path) not in PUBLIC_ROUTES]

    assert len(protected) >= 60, f"route enumeration looks broken: only {len(protected)} protected routes"
    assert any(path.startswith("/api/admin/") for _, path in protected), "no /api/admin routes were enumerated"

    not_401: list[str] = []
    bad_envelope: list[str] = []
    for method, path in protected:
        url = _probe_url(path)
        response = client.request(method, url, json=_body_for(method))
        context = f"{method} {path} -> {response.status_code}"
        if response.status_code != 401:
            not_401.append(f"{context} ({response.text[:120]})")
            continue
        try:
            _assert_envelope(response, context=context)
        except AssertionError as exc:  # collected, reported together below
            bad_envelope.append(str(exc))

    assert not not_401, "these routes did not reject an anonymous caller with 401:\n" + "\n".join(not_401)
    assert not bad_envelope, "these routes answered without the uniform envelope:\n" + "\n".join(bad_envelope)


def test_public_routes_are_reachable_without_a_session(app_client):
    """The allow-list above must describe real, working public routes."""
    _app, client = app_client

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/ready").status_code == 200
    assert client.get("/api/bootstrap/status").status_code == 200
    meta = client.get("/api/meta")
    assert meta.status_code == 200 and meta.json()["app"] == "skill-router"
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/v1").status_code == 200
    assert client.get("/api/v1/openapi.json").status_code == 200
    assert client.get("/api/mcp").status_code == 405
    assert client.delete("/api/mcp").status_code == 204
    assert client.post("/api/auth/logout").status_code == 200


def test_mutating_admin_routes_require_the_csrf_header(app_client):
    """A session cookie alone is never enough for a state change."""
    app, client = app_client
    csrf = _login(client)
    assert csrf

    mutating = [
        (method, path)
        for method, path in _api_routes(app)
        if method in {"POST", "PUT", "PATCH", "DELETE"}
        and not path.startswith(CSRF_EXEMPT_PREFIXES)
        and (method, path) not in PUBLIC_ROUTES
    ]
    assert len(mutating) >= 40, f"only {len(mutating)} mutating routes enumerated"

    offenders: list[str] = []
    for method, path in mutating:
        response = client.request(method, _probe_url(path), json=_body_for(method))
        context = f"{method} {path} -> {response.status_code}"
        if response.status_code != 403:
            offenders.append(f"{context} (expected 403 csrf_failed, body {response.text[:120]})")
            continue
        error = _assert_envelope(response, context=context)
        if error["code"] != "csrf_failed":
            offenders.append(f"{context} (code {error['code']!r})")
    assert not offenders, "mutating routes accepted a request without x-csrf-token:\n" + "\n".join(offenders)


def test_csrf_header_is_enough_to_pass_the_middleware(app_client):
    """The complementary case: with the header the request reaches the handler."""
    _app, client = app_client
    csrf = _login(client)

    ok = client.post("/api/admin/skills", json={"name": "Contract skill"}, headers={"x-csrf-token": csrf})
    assert ok.status_code == 201, ok.text
    # ... and the same call without the header is refused.
    blocked = client.post("/api/admin/skills", json={"name": "Nope"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "csrf_failed"


def test_foreign_origin_is_rejected_before_anything_else(app_client):
    """CONTRACT §2.1: origin is checked first, on every mutating /api call."""
    _app, client = app_client
    csrf = _login(client)
    body = {"name": "Origin skill"}

    foreign_with_token = client.post(
        "/api/admin/skills",
        json=body,
        headers={"origin": FOREIGN_ORIGIN, "x-csrf-token": csrf},
    )
    assert foreign_with_token.status_code == 403, foreign_with_token.text
    error = _assert_envelope(foreign_with_token, context="foreign origin + csrf")
    assert error["code"] == "origin_rejected"
    assert error["actions"], "an origin rejection must tell the user what to do"

    foreign_without_token = client.post("/api/admin/skills", json=body, headers={"origin": FOREIGN_ORIGIN})
    assert foreign_without_token.status_code == 403
    assert foreign_without_token.json()["error"]["code"] == "origin_rejected"

    trusted = client.post(
        "/api/admin/skills",
        json=body,
        headers={"origin": TRUSTED_ORIGIN, "x-csrf-token": csrf},
    )
    assert trusted.status_code == 201, trusted.text


def test_external_surfaces_use_bearer_tokens_and_cookie_fallback_is_the_admin(app_client):
    """`/api/v1` is token-scoped for machines; the *documented* exception in
    `app.deps.require_agent` lets a signed-in administrator use the same routes
    from the UI. That fallback must identify itself as an admin (no agent id, no
    token) instead of impersonating an agent, and anonymity must still be 401."""
    _app, client = app_client

    anonymous = client.get("/api/v1/whoami")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthenticated"

    _login(client)
    whoami = client.get("/api/v1/whoami")
    assert whoami.status_code == 200, whoami.text
    body = whoami.json()
    # The fallback principal is the administrator: no agent identity is invented.
    assert body["agent"]["id"] is None
    assert body["scopes"] == ["*"]
    assert body["limits"] == {"rate_limit_per_min": None, "max_concurrency": None, "budget_usd": None}

    # The MCP transport accepts the same fallback, and never echoes a token.
    handshake = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    )
    assert handshake.status_code == 200, handshake.text
    assert handshake.headers["mcp-session-id"]
    assert "sr_live_" not in handshake.text and "token_hash" not in handshake.text


def test_error_envelope_carries_a_matching_request_id(app_client):
    """The `request_id` in the body must equal the `x-request-id` header."""
    _app, client = app_client

    response = client.get("/api/admin/overview")
    assert response.status_code == 401
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]

    # A caller-supplied id is echoed, so a user-reported number is traceable.
    supplied = client.get("/api/admin/overview", headers={"x-request-id": "req_contract_probe"})
    assert supplied.json()["error"]["request_id"] == "req_contract_probe"
