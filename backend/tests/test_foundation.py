"""Foundation tests: crypto, passwords, bootstrap idempotency, auth, redaction,
network policy and capability-config validation.

These run against a throw-away SQLite file and keyring, never the real data dir.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point the app at a temp data dir + fresh keyring for every test."""
    from app.core import config

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SR_DATA_DIR", str(data))
    monkeypatch.setenv("SR_ENVIRONMENT", "test")
    monkeypatch.setenv("SR_DATABASE_PATH", str(data / "test.db"))
    monkeypatch.setenv("SR_MASTER_KEY_FILE", str(data / "keys" / "master.keyring.json"))
    monkeypatch.setenv("SR_INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SR_INITIAL_ADMIN_PASSWORD", "adminjr")
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


# --------------------------------------------------------------- crypto -----
def test_credential_encryption_roundtrip_and_envelope(isolated):
    from app.security import crypto

    envelope, key_id = crypto.encrypt_map({"api_key": "sk-secret-value-123456"})
    assert key_id == "k1"
    assert "sk-secret-value" not in envelope, "ciphertext must not contain plaintext"
    assert crypto.decrypt_map(envelope) == {"api_key": "sk-secret-value-123456"}


def test_encryption_is_randomised(isolated):
    from app.security import crypto

    first, _ = crypto.encrypt_map({"token": "abc"})
    second, _ = crypto.encrypt_map({"token": "abc"})
    assert first != second, "each encryption must use a fresh nonce"


def test_wrong_key_cannot_decrypt(isolated):
    from app.security import crypto

    envelope, _ = crypto.encrypt_map({"api_key": "value"})
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_map(envelope, ring=crypto.Keyring(active="k9", keys={"k9": crypto._generate_key()}))


def test_key_rotation_rewraps_and_old_key_still_reads(isolated):
    from app.security import crypto
    from app.core.config import get_settings

    settings = get_settings()
    envelope, _ = crypto.encrypt_map({"api_key": "rotate-me"})
    old_ring = crypto.load_keyring()
    new_key = crypto._generate_key()
    new_ring = crypto.Keyring(active="k2", keys={**old_ring.keys, "k2": new_key})
    crypto.write_keyring(new_ring, settings.keyring_path)

    rewrapped, key_id, changed = crypto.rewrap(envelope)
    assert changed is True and key_id == "k2"
    assert crypto.decrypt_map(rewrapped) == {"api_key": "rotate-me"}
    # Rollback path: the previous key stays valid in the ring.
    assert crypto.decrypt_map(envelope, ring=old_ring) == {"api_key": "rotate-me"}


def test_tampered_ciphertext_is_rejected(isolated):
    from app.security import crypto

    envelope, _ = crypto.encrypt_map({"api_key": "value"})
    version, key_id, nonce, data = envelope.split(".")
    tampered = ".".join([version, key_id, nonce, data[:-4] + ("AAAA" if data[-4:] != "AAAA" else "BBBB")])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_map(tampered)


# ------------------------------------------------------------- passwords ----
def test_password_hashing_and_policy():
    from app.security import passwords

    digest = passwords.hash_password("Str0ng-Passw0rd!")
    assert digest.startswith("$argon2id$")
    assert passwords.verify_password(digest, "Str0ng-Passw0rd!")
    assert not passwords.verify_password(digest, "wrong")

    with pytest.raises(passwords.PasswordPolicyError):
        passwords.check_password_policy("adminjr")
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.check_password_policy("short1!")
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.check_password_policy("alllowercase123")
    passwords.check_password_policy("NewPassw0rd!23")


# --------------------------------------------------------------- redaction --
def test_redaction_scrubs_shapes_and_known_values():
    from app.security.redact import Redactor, preview

    redactor = Redactor(["opaque-unknown-secret-42"])
    text = (
        "authorization: Bearer abcdefghijklmnop "
        "key sk-abcdefghijklmnopqrstuvwxyz123456 "
        "google AIzaSyABCDEFGHIJKLMNOPQRSTUVWX12345 "
        "value opaque-unknown-secret-42"
    )
    cleaned = redactor.text(text)
    assert "abcdefghijklmnop" not in cleaned
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in cleaned
    assert "AIzaSy" not in cleaned
    assert "opaque-unknown-secret-42" not in cleaned

    payload = {"api_key": "abc", "model": "gpt", "nested": {"password": "p"}, "capability_id": "cap_1"}
    scrubbed = preview(payload)
    assert "abc" not in scrubbed
    assert "gpt" in scrubbed
    assert "cap_1" in scrubbed


# -------------------------------------------------------------- bootstrap ---
def test_bootstrap_creates_admin_once_and_never_resets(isolated):
    from app.core import db
    from app.security import passwords
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    users = db.query_all("SELECT * FROM users")
    assert len(users) == 1
    assert users[0]["username"] == "admin"
    assert users[0]["must_change_password"] == 1
    assert passwords.verify_password(users[0]["password_hash"], "adminjr")

    # Simulate the operator choosing a real password, then a restart/upgrade.
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (passwords.hash_password("MyNewPassw0rd!"), users[0]["id"]),
    )
    bootstrap.ensure_bootstrap()
    bootstrap.ensure_bootstrap()
    after = db.query_all("SELECT * FROM users")
    assert len(after) == 1, "restart must not create a second admin"
    assert passwords.verify_password(after[0]["password_hash"], "MyNewPassw0rd!"), "restart must not reset the password"
    assert not passwords.verify_password(after[0]["password_hash"], "adminjr")


def test_bootstrap_uses_configured_credentials(isolated, monkeypatch):
    from app.core import config, db
    from app.security import passwords
    from app.services import bootstrap

    monkeypatch.setenv("SR_INITIAL_ADMIN_USERNAME", "root")
    monkeypatch.setenv("SR_INITIAL_ADMIN_PASSWORD", "Sup3rSecret!init")
    config.reset_settings_cache()
    bootstrap.ensure_bootstrap()
    row = db.query_one("SELECT * FROM users")
    assert row["username"] == "root"
    assert passwords.verify_password(row["password_hash"], "Sup3rSecret!init")


# --------------------------------------------------------------- sessions ---
def test_session_expiry_and_revocation(isolated):
    from app.core import db
    from app.security import sessions

    user_id = db.new_id("usr")
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, must_change_password, disabled, created_at, updated_at) "
        "VALUES (?, 'u1', 'x', 'admin', 0, 0, ?, ?)",
        (user_id, db.iso(), db.iso()),
    )
    issued = sessions.create_session(user_id, ttl_minutes=1)
    assert sessions.get_session(issued.token) is not None
    # Already-expired session is refused and removed.
    db.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (db.iso(db.utcnow() - __import__("datetime").timedelta(minutes=1)), issued.record.id))
    assert sessions.get_session(issued.token) is None
    assert db.query_scalar("SELECT COUNT(*) FROM sessions") == 0


def test_csrf_token_must_match(isolated):
    from app.core import db
    from app.security import sessions

    user_id = db.new_id("usr")
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, must_change_password, disabled, created_at, updated_at) "
        "VALUES (?, 'u2', 'x', 'admin', 0, 0, ?, ?)",
        (user_id, db.iso(), db.iso()),
    )
    issued = sessions.create_session(user_id)
    record = sessions.get_session(issued.token)
    assert record is not None
    assert sessions.verify_csrf(record, issued.csrf_token)
    assert not sessions.verify_csrf(record, "wrong-token")


# ------------------------------------------------------------ network -------
def test_network_policy_blocks_internal_and_metadata_targets(isolated):
    from app.services.network import NetworkPolicyError, validate_url

    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1:8000/api",
        "http://localhost/admin",
        "file:///etc/passwd",
    ):
        with pytest.raises(NetworkPolicyError):
            validate_url(url)


def test_network_policy_allows_public_and_allowlisted(isolated, monkeypatch):
    from app.core import config
    from app.services.network import validate_url

    # This host resolves every name through a fake-IP proxy, which the policy
    # permits only with the explicit opt-in.
    monkeypatch.setenv("SR_OUTBOUND_ALLOW_FAKE_IP", "true")
    monkeypatch.setenv("SR_OUTBOUND_ALLOWED_HOSTS", "internal.example.com")
    config.reset_settings_cache()
    validate_url("https://example.com/v1/models")
    validate_url("http://internal.example.com/health")  # allowlisted private target

    # Without the opt-in the fake-IP range is refused.
    monkeypatch.setenv("SR_OUTBOUND_ALLOW_FAKE_IP", "false")
    config.reset_settings_cache()
    import pytest

    from app.services.network import NetworkPolicyError

    with pytest.raises(NetworkPolicyError):
        validate_url("https://example.com/v1/models")


# ------------------------------------------------- capability config --------
def test_config_validation_reports_blocking_errors():
    from app.services.capability_config import default_config, has_blocking_issues, validate_config

    cfg = default_config("text")
    issues = validate_config(cfg, capability_type="text")
    assert has_blocking_issues(issues)

    good = default_config("text")
    good["request"]["url"] = "https://api.example.com/v1/chat"
    good["request"]["body_template"] = {"model": "{{ model }}", "input": "{{ input.prompt }}"}
    good["input_schema"] = {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}
    good["response"]["extract"]["result"] = "$.choices[0].message.content"
    good["stream"]["delta_path"] = "$.choices[0].delta.content"
    good["examples"] = [{"input": {"prompt": "hello"}}]
    issues = validate_config(good, capability_type="text")
    assert not has_blocking_issues(issues), issues


def test_static_authorization_header_is_rejected():
    from app.services.capability_config import default_config, validate_config

    cfg = default_config("text")
    cfg["request"]["url"] = "https://api.example.com/v1/chat"
    cfg["request"]["headers"]["authorization"] = "Bearer hardcoded"
    cfg["request"]["body_template"] = {"x": 1}
    cfg["input_schema"] = {"type": "object", "properties": {}}
    cfg["response"]["extract"]["result"] = "$.ok"
    issues = validate_config(cfg)
    assert any(i["field"].endswith("authorization") and i["severity"] == "error" for i in issues)


def test_template_render_preserves_types_and_jsonpath():
    from app.services.capability_config import jsonpath_get, render

    ctx = {"input": {"prompt": "hi", "max": 12, "flag": True}, "model": "m1"}
    assert render("{{ input.max }}", ctx) == 12
    assert render("model={{ model }}", ctx) == "model=m1"
    assert render({"a": "{{ input.flag }}", "b": ["{{ input.prompt }}"]}, ctx) == {"a": True, "b": ["hi"]}
    assert render("{{ input.missing }}", ctx) is None

    body = {"choices": [{"message": {"content": "answer"}}], "usage": {"prompt_tokens": 9}}
    assert jsonpath_get(body, "$.choices[0].message.content") == "answer"
    assert jsonpath_get(body, "$.usage.prompt_tokens") == 9
    assert jsonpath_get(body, "$.choices[5].message") is None


# ----------------------------------------------------------------- auth -----
@pytest.fixture()
def client(isolated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import auth
    from app.core.errors import install_error_handlers
    from app.core.request_context import set_request_id
    from app.services import bootstrap

    bootstrap.ensure_bootstrap()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(auth.public_router)

    @app.middleware("http")
    async def rid(request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(None)
        return await call_next(request)

    return TestClient(app)


def test_login_rejects_bad_password_and_sets_cookies_for_good_one(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"

    response = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["must_change_password"] is True
    assert body["csrf_token"]
    assert "sr_session" in response.cookies
    assert "sr_csrf" in response.cookies


def test_login_is_throttled_after_repeated_failures(isolated, client):
    from app.core import config

    config.get_settings().login_max_attempts = 3
    for _ in range(3):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"


def test_password_change_clears_the_gate(client):
    from app.core import db

    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"}).json()
    csrf = login["csrf_token"]
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "adminjr", "new_password": "FreshPassw0rd!9"},
        headers={"x-csrf-token": csrf},
    )
    assert changed.status_code == 200, changed.text
    row = db.query_one("SELECT must_change_password FROM users WHERE username = 'admin'")
    assert row["must_change_password"] == 0
    # Old password no longer works; new one does.
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "FreshPassw0rd!9"}).status_code == 200


def test_weak_password_change_is_rejected(client):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminjr"}).json()
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "adminjr", "new_password": "adminjr"},
        headers={"x-csrf-token": login["csrf_token"]},
    )
    assert response.status_code in (400, 403)
    assert response.json()["error"]["code"] in {"weak_password", "invalid_current_password", "password_unchanged"}


def test_protected_route_requires_session(client):
    response = client.get("/api/auth/session")
    assert response.status_code == 401
    assert response.json()["error"]["request_id"].startswith("req_")
