"""Browser-path smoke test: everything through the Next.js origin (:3000).

Proves the rewrite proxies /api, that cookies and the CSRF double-submit work
same-origin, and that the pages the UI calls return real data.
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:3000"
PASSWORD = sys.argv[1] if len(sys.argv) > 1 else ""

client = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False, follow_redirects=False)

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(("[PASS] " if ok else "[FAIL] ") + label)


login = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
check(login.status_code == 200, f"login through the Next origin: HTTP {login.status_code}")
csrf = login.json().get("csrf_token", "") if login.status_code == 200 else ""
check(bool(csrf), "csrf token issued in the response body")
check(bool(client.cookies.get("sr_session")), "session cookie set (HttpOnly)")

headers = {"x-csrf-token": csrf}
for path in (
    "/api/auth/session",
    "/api/admin/overview",
    "/api/admin/providers",
    "/api/admin/capabilities",
    "/api/admin/skills",
    "/api/admin/agents",
    "/api/admin/invocations",
    "/api/admin/tasks",
    "/api/admin/settings",
    "/api/admin/engine",
    "/api/admin/agent/health",
    "/api/meta",
):
    response = client.get(path)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    detail = ""
    if isinstance(body, dict):
        if "total" in body:
            detail = f"total={body['total']}"
        elif "items" in body:
            detail = f"items={len(body['items'])}"
        elif "counts" in body:
            detail = "overview payload"
        elif "sidecar" in body:
            detail = f"sidecar_ok={body.get('sidecar', {}).get('ok')}"
    check(response.status_code == 200, f"GET {path} → {response.status_code} {detail}")

# CSRF must be enforced through the proxy as well.
no_csrf = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
no_csrf.cookies.update(client.cookies)
blocked = no_csrf.post("/api/admin/providers", json={"name": "csrf-probe", "category": "generic"})
check(blocked.status_code == 403, f"mutation without CSRF through the proxy → HTTP {blocked.status_code}")

# A real mutation with the header must succeed, then be removed.
created = client.post(
    "/api/admin/providers",
    json={"name": "Browser smoke provider", "category": "generic", "environment": "development", "tags": ["smoke"], "status": "active"},
    headers=headers,
)
check(created.status_code == 201, f"create provider with CSRF → HTTP {created.status_code}")
if created.status_code == 201:
    provider_id = created.json()["id"]
    deleted = client.delete(f"/api/admin/providers/{provider_id}", headers=headers)
    check(deleted.status_code in (200, 204), f"delete provider → HTTP {deleted.status_code}")

# The agent panel talks to these; they are cookie+CSRF protected too.
session = client.post("/api/admin/agent/sessions", json={"title": "smoke"}, headers=headers)
check(session.status_code == 201, f"create agent session → HTTP {session.status_code}")
if session.status_code == 201:
    listed = client.get("/api/admin/agent/sessions")
    check(listed.status_code == 200 and listed.json()["total"] >= 1, "agent session list")

failed = [label for ok, label in results if not ok]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
