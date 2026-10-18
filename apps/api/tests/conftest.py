"""pytest 公共 fixture：临时 DB、TestClient、认证、假上游（§8.2 / §15.1）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "api"))

_TMP = Path(tempfile.mkdtemp(prefix="sr-test-"))
os.environ["SKILLROUTER_DATA_DIR"] = str(_TMP)
os.environ["SKILLROUTER_DB_PATH"] = str(_TMP / "test.db")
os.environ["SKILLROUTER_ENV"] = "development"
os.environ["SKILLROUTER_ADMIN_USER"] = "admin"
os.environ["SKILLROUTER_ADMIN_PASSWORD"] = "adminjr"
os.environ["SKILLROUTER_PAYLOAD_POLICY"] = "redacted_preview"

from fastapi.testclient import TestClient  # noqa: E402

from skillrouter_api import deps  # noqa: E402
from skillrouter_api.main import app  # noqa: E402
from skillrouter_api.services import bootstrap as bootstrap_service  # noqa: E402
from tests.fixtures.builders import FAKE_BASE, capability_payload  # noqa: E402,F401

NEW_PASSWORD = "SkillRouter-Test-2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap() -> None:
    bootstrap_service.bootstrap(reason="pytest")
    deps.mark_initialized()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin(client: TestClient) -> dict:
    """已登录并完成强制改密的客户端 + CSRF 头。"""
    r = client.post("/api/admin/auth/login", json={"username": "admin", "password": "adminjr"})
    if r.status_code == 401:
        r = client.post(
            "/api/admin/auth/login", json={"username": "admin", "password": NEW_PASSWORD}
        )
        assert r.status_code == 200, r.text
        return {"headers": {"X-CSRF-Token": r.json()["csrf_token"]}, "csrf": r.json()["csrf_token"]}
    assert r.status_code == 200, r.text
    token = r.json()["csrf_token"]
    if r.json()["admin"]["must_change_password"]:
        r = client.post(
            "/api/admin/auth/change-password",
            headers={"X-CSRF-Token": token},
            json={"current_password": "adminjr", "new_password": NEW_PASSWORD},
        )
        assert r.status_code == 200, r.text
        token = r.json()["csrf_token"]
    return {"headers": {"X-CSRF-Token": token}, "csrf": token}


@pytest.fixture()
def provider(client: TestClient, admin: dict) -> dict:
    slug = f"pytest-{os.urandom(3).hex()}"
    r = client.post(
        "/api/admin/providers",
        headers=admin["headers"],
        json={"slug": slug, "name": f"Pytest {slug}", "category": "llm", "base_url": FAKE_BASE},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def credential(client: TestClient, admin: dict, provider: dict) -> dict:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={
            "name": "pytest 凭证",
            "kind": "llm",
            "auth_type": "bearer",
            "secret": "sk-pytest-secret-0123456789",
            "meta": {},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def make_capability(client: TestClient, admin: dict, provider: dict, credential: dict):
    def _make(key: str | None = None, *, modes=None, routes=None) -> dict:
        cap_key = key or f"text.generate.{os.urandom(3).hex()}"
        definition = capability_payload(provider["id"], credential["id"], key=cap_key, modes=modes)
        if routes is not None:
            definition["routes"] = routes
        r = client.post(
            "/api/admin/capabilities",
            headers=admin["headers"],
            json={
                "key": cap_key,
                "name": definition["name"],
                "kind": definition["kind"],
                "definition": definition,
                "tags": ["chat"],
            },
        )
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest.fixture()
def make_token(client: TestClient, admin: dict):
    counter = {"n": 0}

    def _make(*, scopes=None, allowed_modes=None, **limits) -> dict:
        counter["n"] += 1
        r = client.post(
            "/api/admin/access/agents",
            headers=admin["headers"],
            json={"name": f"test-agent-{counter['n']}-{os.urandom(2).hex()}"},
        )
        assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        body = {
            "name": f"token-{counter['n']}",
            "scopes": scopes or ["*"],
            "allowed_modes": allowed_modes or ["sync", "async", "stream"],
            **limits,
        }
        r = client.post(
            f"/api/admin/access/agents/{agent_id}/tokens", headers=admin["headers"], json=body
        )
        assert r.status_code == 201, r.text
        data = r.json()
        return {
            "token": data["token"],
            "token_id": data["token_id"],
            "agent_id": agent_id,
            "headers": {"Authorization": f"Bearer {data['token']}"},
        }

    return _make


def publish_capability(client: TestClient, admin: dict, capability_id: str, *, version="1.0.0") -> dict:
    """用一条真实（假上游）测试记录作为发布证据。"""
    r = client.post(
        f"/api/admin/capabilities/{capability_id}/publish",
        headers=admin["headers"],
        json={"version": version, "changelog": "pytest"},
    )
    return {"status": r.status_code, "body": r.json()}
