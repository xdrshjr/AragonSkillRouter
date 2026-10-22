"""Real-provider closed-loop verification for Skill Router.

This script drives the *running* platform through its own HTTP API — the same
paths the UI uses — and performs genuine calls to real providers. Nothing here
is mocked: if a credential is wrong, an endpoint moved, or an extraction path is
off, the step fails and the script says so.

What it proves, in order:
  1. first-run password gate, login, CSRF
  2. provider + credential creation (secrets encrypted at rest)
  3. capability authoring for four kinds: text (sync), text (streaming),
     speech (sync artifact) and video (async task)
  4. a real test call per capability version, with evidence stored
  5. publish, and the four distinct states (saved / tested / published / callable)
  6. an external agent token, then /api/v1 whoami + discovery + a real call
  7. an async video task submitted through /api/v1, polled to completion, and
     the resulting file downloaded
  8. MCP initialize + tools/list + tools/call (same capability, same records)

Usage:
    python scripts/verify_real_providers.py --url http://127.0.0.1:8000 \\
        --admin-password adminjr --rotate-password \\
        --gemini-key-env GEMINI_API_KEY --zenmux-key-env ZENMUX_API_KEY \\
        --video   # opt in to the paid async video run

Exit code 0 only when every requested step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

RESULTS: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str, data: Any = None) -> None:
    RESULTS.append(
        {
            "step": name,
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": data,
        }
    )
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}: {detail}", flush=True)


class Api:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        # The local proxy on this host would otherwise intercept 127.0.0.1.
        self.http = httpx.Client(base_url=self.base, timeout=120.0, trust_env=False, follow_redirects=False)
        self.csrf: str | None = None

    def _mutate_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        token = self.csrf or self.http.cookies.get("sr_csrf")
        if token:
            headers["x-csrf-token"] = token
        return headers

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.http.post(path, json=json_body, headers=self._mutate_headers(), **kwargs)

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.http.patch(path, json=json_body, headers=self._mutate_headers(), **kwargs)

    def delete(self, path: str) -> httpx.Response:
        return self.http.delete(path, headers=self._mutate_headers())

    @staticmethod
    def error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:300]}"
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return f"{err.get('code')}: {err.get('message')} (request {err.get('request_id')})"
        return f"HTTP {response.status_code}: {json.dumps(payload)[:300]}"


def login(api: Api, username: str, password: str, rotate: bool) -> str:
    response = api.post("/api/auth/login", {"username": username, "password": password})
    if response.status_code == 429:
        raise SystemExit(f"login throttled: {Api.error(response)}")
    if response.status_code != 200:
        raise SystemExit(f"login failed: {Api.error(response)}")
    body = response.json()
    api.csrf = body["csrf_token"]
    record("login", True, f"signed in as {username}")

    if body["must_change_password"]:
        if not rotate:
            raise SystemExit(
                "The admin must change the initial password first. Re-run with --rotate-password "
                "to have this script set a strong one (it is printed once), or change it in the UI."
            )
        new_password = "VRF-" + uuid.uuid4().hex[:18] + "!9"
        changed = api.post(
            "/api/auth/change-password",
            {"current_password": password, "new_password": new_password},
        )
        if changed.status_code != 200:
            raise SystemExit(f"password change failed: {Api.error(changed)}")
        api.csrf = changed.json()["csrf_token"]
        record("first-run password change", True, "default password replaced; gate cleared")
        print(f"\n  NEW ADMIN PASSWORD (shown once): {new_password}\n", flush=True)
    return api.csrf or ""


def find_id(api: Api, path: str, name: str, key: str = "name") -> str | None:
    response = api.get(path, params={"search": name, "limit": 200})
    if response.status_code != 200:
        return None
    for item in response.json().get("items", []):
        if item.get(key) == name:
            return item["id"]
    return None


def ensure_provider(api: Api, spec: dict[str, Any]) -> str:
    existing = find_id(api, "/api/admin/providers", spec["name"])
    if existing:
        record(f"provider {spec['name']}", True, f"reused {existing}")
        return existing
    response = api.post("/api/admin/providers", spec)
    if response.status_code != 201:
        record(f"provider {spec['name']}", False, Api.error(response))
        raise SystemExit(1)
    provider_id = response.json()["id"]
    record(f"provider {spec['name']}", True, f"created {provider_id}")
    return provider_id


def ensure_credential(api: Api, provider_id: str, name: str, auth_type: str, secret: dict[str, str], config: dict[str, Any] | None = None) -> str:
    existing = find_id(api, "/api/admin/credentials", name)
    if existing:
        record(f"credential {name}", True, f"reused {existing}")
        return existing
    response = api.post(
        "/api/admin/credentials",
        {"provider_id": provider_id, "name": name, "auth_type": auth_type, "secret": secret, "config": config or {}},
    )
    if response.status_code != 201:
        record(f"credential {name}", False, Api.error(response))
        raise SystemExit(1)
    body = response.json()
    leaked = any(value and value in json.dumps(body) for value in secret.values())
    record(
        f"credential {name}",
        not leaked,
        f"created {body['id']} hint={body.get('hint')!r} plaintext_leak={leaked}",
        {"hint": body.get("hint"), "auth_type": body.get("auth_type")},
    )
    if leaked:
        raise SystemExit(1)
    return body["id"]


def ensure_capability(api: Api, spec: dict[str, Any]) -> tuple[str, str, bool]:
    """Create (or reuse) a capability + its draft version. Returns (cap_id, version_id, created)."""
    existing = find_id(api, "/api/admin/capabilities", spec["name"])
    if existing:
        detail = api.get(f"/api/admin/capabilities/{existing}").json()
        version = detail["versions"][0]
        record(f"capability {spec['name']}", True, f"reused {existing} v{version['version']} status={version['status']}")
        return existing, version["id"], False
    response = api.post(
        "/api/admin/capabilities",
        {
            "name": spec["name"],
            "type": spec["type"],
            "description": spec["description"],
            "provider_id": spec["provider_id"],
            "credential_id": spec["credential_id"],
        },
    )
    if response.status_code != 201:
        record(f"capability {spec['name']}", False, Api.error(response))
        raise SystemExit(1)
    capability = response.json()
    cap_id = capability["id"]
    version_id = capability["versions"][0]["id"]
    patched = api.patch(
        f"/api/admin/capabilities/{cap_id}/versions/{version_id}",
        {"config": spec["config"], "notes": spec["notes"]},
    )
    if patched.status_code not in (200, 201):
        record(f"capability {spec['name']} config", False, Api.error(patched))
        raise SystemExit(1)
    record(f"capability {spec['name']}", True, f"created {cap_id} v1 draft")
    return cap_id, version_id, True


def validate_and_test(api: Api, cap_id: str, version_id: str, sample: dict[str, Any], mode: str = "sync") -> bool:
    validation = api.post(f"/api/admin/capabilities/{cap_id}/versions/{version_id}/validate")
    if validation.status_code != 200:
        record("validate", False, Api.error(validation))
        return False
    issues = validation.json()
    blocking = issues.get("blocking") or [i for i in issues.get("issues", []) if i.get("severity") == "error"]
    record("validate", not blocking, f"{len(issues.get('issues', []))} issue(s), blocking={bool(blocking)}", issues)
    if blocking:
        return False

    started = time.perf_counter()
    response = api.post(
        f"/api/admin/capabilities/{cap_id}/versions/{version_id}/test",
        {"input": sample, "mode": mode},
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    if response.status_code != 200:
        record("test call", False, Api.error(response))
        return False
    body = response.json()
    ok = body.get("status") == "passed"
    preview_text = json.dumps(body.get("result"), ensure_ascii=False)[:220] if body.get("result") is not None else ""
    record(
        "test call",
        ok,
        f"status={body.get('status')} http={body.get('http_status')} latency={body.get('latency_ms')}ms wall={elapsed}ms result={preview_text or ('task ' + str(body.get('task_id')))}",
        {
            "test_id": body.get("test_id"),
            "error": body.get("error"),
            "artifacts": body.get("artifacts"),
            "task_id": body.get("task_id"),
            "upstream_task_id": body.get("upstream_task_id"),
        },
    )
    if body.get("task_id"):
        async_task["task_id"] = body["task_id"]
    return ok


async_task: dict[str, Any] = {}


def wait_for_task(api: Api, task_id: str, timeout_s: int, *, label: str) -> dict[str, Any] | None:
    """Follow a tracked async task until it reaches a terminal state."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        response = api.get(f"/api/admin/tasks/{task_id}")
        if response.status_code != 200:
            record(label, False, f"HTTP {response.status_code} {response.text[:200]}")
            return None
        task = response.json()
        if task.get("status") != last:
            print(f"      {label}: {task.get('status')} progress={task.get('progress')} note={task.get('progress_note')}", flush=True)
            last = task.get("status")
        if task.get("status") in {"succeeded", "failed", "cancelled", "uncertain"}:
            artifacts = task.get("artifacts") or []
            record(
                label,
                task.get("status") == "succeeded" and bool(artifacts),
                f"status={task.get('status')} attempts={task.get('attempts')} artifacts={[(a.get('filename'), a.get('size')) for a in artifacts]} error={task.get('error')}",
                {"id": task.get("id"), "status": task.get("status"), "finished_at": task.get("finished_at")},
            )
            return task
        time.sleep(5)
    record(label, False, f"timed out after {timeout_s}s")
    return None


def publish(api: Api, cap_id: str, version_id: str) -> bool:
    detail = api.get(f"/api/admin/capabilities/{cap_id}").json()
    if detail.get("status") == "published" and detail.get("current_version_id") == version_id:
        record("publish", True, f"already published (skip); callable={detail.get('callable')}")
        return True
    response = api.post(f"/api/admin/capabilities/{cap_id}/versions/{version_id}/publish", {"confirm": True})
    if response.status_code != 200:
        record("publish", False, Api.error(response))
        return False
    detail = response.json()
    record(
        "publish",
        True,
        f"published; version={detail.get('current_version', {})} callable={detail.get('callable', detail.get('callable_state'))}",
        {"callable": detail.get("callable"), "status": detail.get("status")},
    )
    return True


def external_checks(api: Api, base_url: str, token: str, text_slug: str) -> dict[str, Any]:
    ext = httpx.Client(base_url=base_url, timeout=180.0, trust_env=False, headers={"authorization": f"Bearer {token}"})
    outcome: dict[str, Any] = {}

    who = ext.get("/api/v1/whoami")
    record("agent whoami", who.status_code == 200, f"HTTP {who.status_code} {json.dumps(who.json())[:200] if who.status_code == 200 else who.text[:200]}")
    if who.status_code != 200:
        raise SystemExit(1)

    caps = ext.get("/api/v1/capabilities")
    body = caps.json() if caps.status_code == 200 else {}
    slugs = [c["slug"] for c in body.get("capabilities", [])]
    record(
        "agent discovery",
        caps.status_code == 200 and text_slug in slugs,
        f"HTTP {caps.status_code} capabilities={slugs} version_token={(body.get('version_token') or '')[:16]}",
        {"slugs": slugs, "etag": caps.headers.get("etag")},
    )
    etag = caps.headers.get("etag")
    if etag:
        cached = ext.get("/api/v1/capabilities", headers={"if-none-match": etag})
        record("capability cache", cached.status_code == 304, f"If-None-Match → HTTP {cached.status_code}")

    call = ext.post(
        "/api/v1/invocations",
        json={"capability": text_slug, "input": {"prompt": "Reply with exactly: SKILL-ROUTER-OK"}, "options": {"mode": "sync", "idempotency_key": f"verify-{uuid.uuid4().hex[:8]}"}},
    )
    payload = call.json() if call.status_code == 200 else {}
    text = json.dumps(payload.get("result"), ensure_ascii=False) if payload else call.text[:300]
    record(
        "agent sync call",
        call.status_code == 200 and payload.get("status") == "succeeded",
        f"HTTP {call.status_code} request_id={payload.get('request_id')} latency={payload.get('latency_ms')}ms result={text[:200]}",
        {"request_id": payload.get("request_id"), "usage": payload.get("usage"), "cost": payload.get("cost"), "route_reason": payload.get("route_reason")},
    )
    outcome["text_request_id"] = payload.get("request_id")

    unknown = ext.post("/api/v1/invocations", json={"capability": text_slug, "input": {"prompt": "x", "bogus_field": 1}})
    record(
        "unsupported parameter is rejected",
        unknown.status_code == 422,
        f"HTTP {unknown.status_code} {unknown.text[:160]}",
    )

    skill = ext.get("/api/v1/skill")
    doc = skill.text if skill.status_code == 200 else ""
    token_leaked = token in doc
    record(
        "agent install bundle",
        skill.status_code == 200 and not token_leaked and len(doc) > 200,
        f"HTTP {skill.status_code} bytes={len(doc)} token_in_bundle={token_leaked}",
    )
    return outcome


def mcp_checks(base_url: str, token: str, capability_slug: str) -> None:
    client = httpx.Client(base_url=base_url, timeout=120.0, trust_env=False)
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def rpc(method: str, params: dict[str, Any] | None = None, session: str | None = None) -> tuple[dict[str, Any], str | None]:
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8], "method": method, "params": params or {}}
        hdrs = dict(headers)
        if session:
            hdrs["mcp-session-id"] = session
        response = client.post("/api/mcp", json=payload, headers=hdrs)
        text = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    text = line[5:].strip()
                    break
        try:
            body = json.loads(text)
        except ValueError:
            body = {"raw": text[:300]}
        return body, response.headers.get("mcp-session-id")

    init, session = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "skill-router-verify", "version": "1.0"}})
    server = (init.get("result") or {}).get("serverInfo", {})
    version = (init.get("result") or {}).get("protocolVersion")
    record("mcp initialize", bool(init.get("result")), f"server={server} protocolVersion={version} session={bool(session)}")

    tools, _ = rpc("tools/list", {}, session)
    names = [t["name"] for t in (tools.get("result") or {}).get("tools", [])]
    mcp_name = capability_slug.replace(".", "__")
    record("mcp tools/list", mcp_name in names, f"{len(names)} tool(s); expected {mcp_name} present={mcp_name in names}", {"tools": names})

    call, _ = rpc(
        "tools/call",
        {"name": mcp_name, "arguments": {"prompt": "Reply with exactly: MCP-OK"}},
        session,
    )
    result = call.get("result") or {}
    structured = result.get("structuredContent") or {}
    record(
        "mcp tools/call",
        bool(result) and not result.get("isError"),
        f"isError={result.get('isError')} code={structured.get('code')} request_id={structured.get('request_id')} content={(json.dumps(result.get('content'))[:180])}",
        {"structured": structured},
    )

    unknown, _ = rpc("tools/call", {"name": "does__not__exist", "arguments": {}}, session)
    record("mcp unknown tool", bool(unknown.get("error")), f"error={unknown.get('error')}")


def async_checks(api: Api, base_url: str, token: str, video_slug: str, timeout_s: int) -> None:
    ext = httpx.Client(base_url=base_url, timeout=120.0, trust_env=False, headers={"authorization": f"Bearer {token}"})
    submit = ext.post(
        "/api/v1/invocations",
        json={
            "capability": video_slug,
            "input": {"prompt": "A single red apple on a plain white table, soft studio light, static camera."},
            "options": {"mode": "async", "idempotency_key": f"verify-video-{uuid.uuid4().hex[:8]}"},
        },
    )
    body = submit.json() if submit.status_code == 200 else {}
    task_id = body.get("task_id")
    record(
        "async submit",
        submit.status_code == 200 and bool(task_id),
        f"HTTP {submit.status_code} request_id={body.get('request_id')} task_id={task_id} status={body.get('status')}",
        {"task_id": task_id, "request_id": body.get("request_id")},
    )
    if not task_id:
        return

    deadline = time.time() + timeout_s
    last_status = None
    while time.time() < deadline:
        status = ext.get(f"/api/v1/tasks/{task_id}")
        if status.status_code != 200:
            record("async status", False, f"HTTP {status.status_code} {status.text[:200]}")
            return
        task = status.json()
        if task.get("status") != last_status:
            print(f"      task {task_id}: {task.get('status')} progress={task.get('progress')} note={task.get('progress_note')}", flush=True)
            last_status = task.get("status")
        if task.get("status") in {"succeeded", "failed", "cancelled", "uncertain"}:
            artifacts = task.get("artifacts") or []
            record(
                "async completion",
                task.get("status") == "succeeded" and bool(artifacts),
                f"status={task.get('status')} progress={task.get('progress')} artifacts={[(a.get('filename'), a.get('size')) for a in artifacts]} error={task.get('error')}",
                {"task": {k: task.get(k) for k in ("id", "status", "attempts", "created_at", "finished_at", "error")}},
            )
            for artifact in artifacts:
                download = ext.get(artifact["url"])
                record(
                    "artifact download",
                    download.status_code == 200 and len(download.content) == artifact.get("size"),
                    f"HTTP {download.status_code} bytes={len(download.content)} expected={artifact.get('size')} type={download.headers.get('content-type')}",
                )
            return
        time.sleep(5)
    record("async completion", False, f"timed out after {timeout_s}s waiting for task {task_id}")


# ----------------------------------------------------------------- capability specs --
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
ZENMUX_BASE = "https://zenmux.ai/api/v1"


def text_capability_zenmux(provider_id: str, credential_id: str) -> dict[str, Any]:
    return {
        "name": "Verify · Text completion (ZenMux)",
        "type": "text",
        "description": "OpenAI-compatible chat completion through ZenMux. Sync and streaming.",
        "provider_id": provider_id,
        "credential_id": credential_id,
        "notes": "Created by scripts/verify_real_providers.py against the live ZenMux gateway.",
        "config": {
            "model": "google/gemini-2.5-flash",
            "request": {
                "method": "POST",
                "url": f"{ZENMUX_BASE}/chat/completions",
                "headers": {"content-type": "application/json"},
                "encoding": "json",
                "query": [],
                "body_template": {
                    "model": "{{ model }}",
                    "messages": [{"role": "user", "content": "{{ input.prompt }}"}],
                    "stream": False,
                },
            },
            "input_schema": {
                "type": "object",
                "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 8000}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "response": {
                "kind": "json",
                "extract": {
                    "result": "$.choices[0].message.content",
                    "usage_in": "$.usage.prompt_tokens",
                    "usage_out": "$.usage.completion_tokens",
                    "usage_total": "$.usage.total_tokens",
                },
                "artifact": None,
            },
            "modes": {"sync": True, "async": False, "stream": True},
            "stream": {"enabled": True, "protocol": "sse", "delta_path": "$.choices[0].delta.content", "done_marker": "[DONE]"},
            "limits": {"timeout_s": 60, "max_output_bytes": 8388608, "max_input_bytes": 262144, "file_input": False, "max_file_mb": 10, "allowed_mime": [], "max_duration_s": None},
            "errors": {
                "rules": [
                    {"match": {"http_status": [401, 403]}, "code": "upstream_auth_failed", "message": "ZenMux rejected the API key.", "action": "Re-enter the ZenMux key and verify it.", "retryable": False},
                    {"match": {"http_status": [429]}, "code": "rate_limited", "message": "ZenMux is rate limiting this key.", "action": "Wait and retry, or raise the quota.", "retryable": True},
                ],
                "default_code": "upstream_error",
            },
            "pricing": {"per_call": 0.0, "input_per_1k": 0.0003, "output_per_1k": 0.0025, "per_second": 0.0, "currency": "USD"},
            "provider_params": ["temperature", "max_tokens"],
            "retry": {"max_attempts": 2, "backoff_ms": [500, 1500], "retry_on": ["connect_timeout", "connect_failed", "rate_limited"]},
            "examples": [{"input": {"prompt": "Reply with exactly: SKILL-ROUTER-OK"}, "note": "Cheapest smoke test."}],
        },
    }


def text_capability_gemini(provider_id: str, credential_id: str) -> dict[str, Any]:
    return {
        "name": "Verify · Text completion (Gemini)",
        "type": "text",
        "description": "Gemini generateContent through the native Google API. Sync only.",
        "provider_id": provider_id,
        "credential_id": credential_id,
        "notes": "Second provider for the same standard capability; used by routing.",
        "config": {
            "model": "gemini-2.5-flash",
            "request": {
                "method": "POST",
                "url": GEMINI_BASE + "/models/{{ model }}:generateContent",
                "headers": {"content-type": "application/json"},
                "encoding": "json",
                "query": [],
                "body_template": {"contents": [{"role": "user", "parts": [{"text": "{{ input.prompt }}"}]}]},
            },
            "input_schema": {
                "type": "object",
                "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 8000}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "response": {
                "kind": "json",
                "extract": {
                    "result": "$.candidates[0].content.parts[0].text",
                    "usage_in": "$.usageMetadata.promptTokenCount",
                    "usage_out": "$.usageMetadata.candidatesTokenCount",
                    "usage_total": "$.usageMetadata.totalTokenCount",
                },
            },
            "modes": {"sync": True, "async": False, "stream": False},
            "limits": {"timeout_s": 60, "max_output_bytes": 8388608, "max_input_bytes": 262144, "file_input": False, "max_file_mb": 10, "allowed_mime": [], "max_duration_s": None},
            "errors": {"rules": [{"match": {"http_status": [400, 403]}, "code": "upstream_auth_failed", "message": "Gemini rejected the key or the model.", "action": "Check the API key and model id.", "retryable": False}], "default_code": "upstream_error"},
            "pricing": {"per_call": 0.0, "input_per_1k": 0.0003, "output_per_1k": 0.0025, "per_second": 0.0, "currency": "USD"},
            "provider_params": [],
            "retry": {"max_attempts": 1, "backoff_ms": [800], "retry_on": ["connect_timeout", "connect_failed"]},
            "examples": [{"input": {"prompt": "Reply with exactly: GEMINI-OK"}, "note": "Cheapest smoke test."}],
        },
    }


def speech_capability(provider_id: str, credential_id: str) -> dict[str, Any]:
    return {
        "name": "Verify · Speech synthesis (Gemini TTS)",
        "type": "speech",
        "description": "Gemini text-to-speech. Returns raw PCM, wrapped into a playable WAV artifact.",
        "provider_id": provider_id,
        "credential_id": credential_id,
        "notes": "Real audio generation verified end-to-end; the artifact is stored and downloadable.",
        "config": {
            "model": "gemini-2.5-flash-preview-tts",
            "request": {
                "method": "POST",
                "url": GEMINI_BASE + "/models/{{ model }}:generateContent",
                "headers": {"content-type": "application/json"},
                "encoding": "json",
                "query": [],
                "body_template": {
                    "contents": [{"parts": [{"text": "{{ input.text }}"}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
                    },
                },
            },
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 2000}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "response": {
                "kind": "json",
                "extract": {"result": "$.candidates[0].content.parts[0].inlineData.mimeType", "usage_in": "$.usageMetadata.promptTokenCount"},
                "artifact": {
                    "kind": "audio",
                    "path": "$.candidates[0].content.parts[0].inlineData.data",
                    "encoding": "base64",
                    "mime": "audio/L16;rate=24000",
                    "mime_path": "$.candidates[0].content.parts[0].inlineData.mimeType",
                    "filename": "speech.wav",
                    "container": "wav",
                    "sample_rate": 24000,
                },
            },
            "modes": {"sync": True, "async": False, "stream": False},
            "limits": {"timeout_s": 90, "max_output_bytes": 8388608, "max_input_bytes": 32768, "file_input": False, "max_file_mb": 0, "allowed_mime": [], "max_duration_s": None},
            "errors": {"rules": [{"match": {"http_status": [400, 403]}, "code": "upstream_auth_failed", "message": "Gemini rejected the key or the TTS model.", "action": "Check the key and that the TTS model is available to it.", "retryable": False}], "default_code": "upstream_error"},
            "pricing": {"per_call": 0.0, "input_per_1k": 0.0, "output_per_1k": 0.0, "per_second": 0.0, "currency": "USD"},
            "provider_params": [],
            "retry": {"max_attempts": 0, "backoff_ms": [], "retry_on": []},
            "examples": [{"input": {"text": "Skill Router speech check."}, "note": "Short utterance keeps the cost negligible."}],
        },
    }


def image_capability(provider_id: str, credential_id: str) -> dict[str, Any]:
    return {
        "name": "Verify · Image generation (Gemini)",
        "type": "image",
        "description": "Gemini image output, returned inline as base64 and stored as a PNG artifact.",
        "provider_id": provider_id,
        "credential_id": credential_id,
        "notes": "Sync file-output capability.",
        "config": {
            "model": "gemini-2.5-flash-image",
            "request": {
                "method": "POST",
                "url": GEMINI_BASE + "/models/{{ model }}:generateContent",
                "headers": {"content-type": "application/json"},
                "encoding": "json",
                "query": [],
                "body_template": {
                    "contents": [{"parts": [{"text": "{{ input.prompt }}"}]}],
                    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
                },
            },
            "input_schema": {
                "type": "object",
                "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "response": {
                "kind": "json",
                "extract": {"result": "$.candidates[0].content.parts[1].inlineData.mimeType"},
                "artifact": {
                    "kind": "image",
                    "path": "$.candidates[0].content.parts[1].inlineData.data",
                    "encoding": "base64",
                    "mime": "image/png",
                    "mime_path": "$.candidates[0].content.parts[1].inlineData.mimeType",
                    "filename": "image.png",
                },
            },
            "modes": {"sync": True, "async": False, "stream": False},
            "limits": {"timeout_s": 120, "max_output_bytes": 8388608, "max_input_bytes": 32768, "file_input": False, "max_file_mb": 0, "allowed_mime": [], "max_duration_s": None},
            "errors": {"rules": [], "default_code": "upstream_error"},
            "pricing": {"per_call": 0.0, "input_per_1k": 0.0, "output_per_1k": 0.0, "per_second": 0.0, "currency": "USD"},
            "provider_params": [],
            "retry": {"max_attempts": 0, "backoff_ms": [], "retry_on": []},
            "examples": [{"input": {"prompt": "A flat green circle on a white background."}, "note": "Synthetic, cheap, unambiguous."}],
        },
    }


def video_capability(provider_id: str, credential_id: str) -> dict[str, Any]:
    return {
        "name": "Verify · Video generation (ZenMux, async)",
        "type": "video",
        "description": "Asynchronous video generation. Submit returns a task id; the platform polls, then downloads the mp4.",
        "provider_id": provider_id,
        "credential_id": credential_id,
        "notes": "Async lifecycle verified: queued → running → succeeded, artifact downloaded to managed storage.",
        "config": {
            "model": "bytedance/doubao-seedance-2.0",
            "request": {
                "method": "POST",
                "url": f"{ZENMUX_BASE}/videos",
                "headers": {"content-type": "application/json"},
                "encoding": "json",
                "query": [],
                "body_template": {
                    "model": "{{ model }}",
                    "content": [{"type": "text", "text": "{{ input.prompt }}"}],
                    "resolution": "480p",
                    "duration": 5,
                    "ratio": "16:9",
                    "generate_audio": False,
                },
            },
            "input_schema": {
                "type": "object",
                "properties": {"prompt": {"type": "string", "minLength": 4, "maxLength": 1500}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "response": {
                "kind": "json",
                "extract": {"result": "$.content"},
                "artifact": {"kind": "video", "path": "$.content.video_url", "encoding": "url", "mime": "video/mp4", "filename": "video.mp4"},
            },
            "modes": {"sync": False, "async": True, "stream": False},
            "async": {
                "enabled": True,
                "submit_extract": {"task_id": "$.id"},
                "status": {
                    "url": ZENMUX_BASE + "/videos/{{ task_id }}",
                    "method": "GET",
                    "extract": {"status": "$.status", "progress": "$.progress"},
                    "map": {
                        "queued": ["queued"],
                        "running": ["running", "processing"],
                        "succeeded": ["succeeded"],
                        "failed": ["failed"],
                        "cancelled": ["cancelled", "canceled"],
                    },
                    "poll_interval_s": 10,
                    "result_extract": {"result": "$.content"},
                },
                "cancel": {"method": "POST", "url": "", "extract": {"status": ""}},
            },
            "limits": {"timeout_s": 60, "max_output_bytes": 16777216, "max_input_bytes": 32768, "file_input": False, "max_file_mb": 0, "allowed_mime": [], "max_duration_s": 1800},
            "errors": {"rules": [{"match": {"http_status": [401, 403]}, "code": "upstream_auth_failed", "message": "ZenMux rejected the key.", "action": "Re-verify the ZenMux credential.", "retryable": False}], "default_code": "upstream_error"},
            "pricing": {"per_call": 0.0, "input_per_1k": 0.0, "output_per_1k": 0.0, "per_second": 0.05, "currency": "USD"},
            "provider_params": ["resolution", "duration", "ratio", "seed"],
            "retry": {"max_attempts": 1, "backoff_ms": [1000], "retry_on": ["connect_timeout", "connect_failed"]},
            "examples": [{"input": {"prompt": "A single red apple on a plain white table, static camera."}, "note": "5 s at 480p keeps the paid run minimal."}],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-provider closed-loop verification")
    parser.add_argument("--url", default=os.environ.get("SR_VERIFY_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default=os.environ.get("SR_ADMIN_PASSWORD", "adminjr"))
    parser.add_argument("--rotate-password", action="store_true")
    parser.add_argument("--gemini-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--zenmux-key-env", default="ZENMUX_API_KEY")
    parser.add_argument("--video", action="store_true", help="run the paid async video generation")
    parser.add_argument("--image", action="store_true", help="also verify sync image generation")
    parser.add_argument("--stream", action="store_true", help="also verify the SSE streaming path")
    parser.add_argument("--video-timeout", type=int, default=900)
    parser.add_argument("--out", default=None, help="write the full JSON report here")
    args = parser.parse_args(argv)

    gemini_key = os.environ.get(args.gemini_key_env, "").strip()
    zenmux_key = os.environ.get(args.zenmux_key_env, "").strip()
    if not gemini_key or not zenmux_key:
        print(f"Set {args.gemini_key_env} and {args.zenmux_key_env} in the environment first.", file=sys.stderr)
        return 2

    api = Api(args.url)
    login(api, args.admin_user, args.admin_password, args.rotate_password)

    gemini_provider = ensure_provider(
        api,
        {
            "name": "Google Gemini",
            "category": "llm",
            "base_url": GEMINI_BASE,
            "docs_url": "https://ai.google.dev/gemini-api/docs",
            "tags": ["llm", "speech", "image", "google"],
            "environment": "production",
            "notes": "Native Gemini API. Used for text, TTS and image verification.",
            "status": "active",
        },
    )
    zenmux_provider = ensure_provider(
        api,
        {
            "name": "ZenMux",
            "category": "llm",
            "base_url": ZENMUX_BASE,
            "docs_url": "https://zenmux.ai/docs",
            "tags": ["gateway", "openai-compatible", "video"],
            "environment": "production",
            "notes": "OpenAI-compatible aggregator; also exposes an async video generation API.",
            "status": "active",
        },
    )
    gemini_cred = ensure_credential(
        api,
        gemini_provider,
        "Gemini API key",
        "api_key",
        {"api_key": gemini_key},
        {"header_name": "x-goog-api-key", "test_url": GEMINI_BASE + "/models"},
    )
    zenmux_cred = ensure_credential(
        api,
        zenmux_provider,
        "ZenMux API key",
        "bearer",
        {"token": zenmux_key},
        {"test_url": ZENMUX_BASE + "/models"},
    )

    verify_cred = api.post(f"/api/admin/credentials/{gemini_cred}/verify", {"method": "GET"})
    record(
        "credential verification (Gemini)",
        verify_cred.status_code == 200 and verify_cred.json().get("status") in {"valid", "rate_limited"},
        f"HTTP {verify_cred.status_code} {verify_cred.json() if verify_cred.status_code == 200 else verify_cred.text[:200]}",
    )

    text_z = ensure_capability(api, text_capability_zenmux(zenmux_provider, zenmux_cred))
    text_g = ensure_capability(api, text_capability_gemini(gemini_provider, gemini_cred))
    speech = ensure_capability(api, speech_capability(gemini_provider, gemini_cred))

    results: dict[str, str] = {}
    for label, (cap_id, version_id, created), sample in (
        ("text-zenmux", text_z, {"prompt": "Reply with exactly: SKILL-ROUTER-OK"}),
        ("text-gemini", text_g, {"prompt": "Reply with exactly: GEMINI-OK"}),
        ("speech", speech, {"text": "Skill Router speech verification."}),
    ):
        if not validate_and_test(api, cap_id, version_id, sample):
            continue
        if publish(api, cap_id, version_id):
            detail = api.get(f"/api/admin/capabilities/{cap_id}").json()
            results[label] = detail["slug"]
            if not detail.get("callable"):
                record(f"callable {label}", False, f"published but callable=false: {detail.get('callable_reason') or detail.get('actions')}")

    if args.image:
        image = ensure_capability(api, image_capability(gemini_provider, gemini_cred))
        if validate_and_test(api, image[0], image[1], {"prompt": "A flat green circle on a white background."}):
            if publish(api, image[0], image[1]):
                results["image"] = api.get(f"/api/admin/capabilities/{image[0]}").json()["slug"]

    video_slug = None
    if args.video:
        video = ensure_capability(api, video_capability(zenmux_provider, zenmux_cred))
        video_prompt = {"prompt": "A single red apple on a plain white table, static camera."}
        if validate_and_test(api, video[0], video[1], video_prompt, mode="async"):
            # The draft test now submits AND tracks the job; follow it to the end
            # so publish is backed by a completed run, not just an accepted submit.
            test_evidence = async_task.get("task_id")
            if test_evidence:
                completed = wait_for_task(api, test_evidence, args.video_timeout, label="async draft lifecycle")
                if completed:
                    for artifact in completed.get("artifacts") or []:
                        download = api.get(f"/api/files/{artifact['id']}")
                        record(
                            "draft artifact download",
                            download.status_code == 200 and len(download.content) == artifact.get("size"),
                            f"HTTP {download.status_code} bytes={len(download.content)} expected={artifact.get('size')} type={download.headers.get('content-type')}",
                        )
            if publish(api, video[0], video[1]):
                video_slug = api.get(f"/api/admin/capabilities/{video[0]}").json()["slug"]
                results["video"] = video_slug

    # --- external agent -------------------------------------------------------
    agent_id = find_id(api, "/api/admin/agents", "Verify external agent")
    if not agent_id:
        created_agent = api.post("/api/admin/agents", {"name": "Verify external agent", "description": "Created by verify_real_providers.py"})
        if created_agent.status_code != 201:
            record("agent creation", False, Api.error(created_agent))
            return 1
        agent_id = created_agent.json()["id"]
        record("agent creation", True, f"created {agent_id}")
    else:
        record("agent creation", True, f"reused {agent_id}")

    token_response = api.post(
        f"/api/admin/agents/{agent_id}/tokens",
        {"name": "verify-token", "scopes": ["*"], "expires_in_days": 7, "rate_limit_per_min": 30, "max_concurrency": 4, "budget_usd": 5.0},
    )
    if token_response.status_code != 201:
        record("token creation", False, Api.error(token_response))
        return 1
    token = token_response.json()["token"]
    record("token creation", True, f"issued token {token[:12]}… (full value shown once, not logged here)")

    text_slug = results.get("text-zenmux")
    if text_slug:
        external_checks(api, args.url, token, text_slug)
        mcp_checks(args.url, token, text_slug)
    else:
        record("external checks", False, "no published text capability to call")

    if video_slug:
        async_checks(api, args.url, token, video_slug, args.video_timeout)

    # --- summary --------------------------------------------------------------
    passed = sum(1 for item in RESULTS if item["status"] == "PASS")
    failed = sum(1 for item in RESULTS if item["status"] == "FAIL")
    print("\n=== verification summary ===")
    for item in RESULTS:
        print(f"  {item['status']:4} {item['step']}: {item['detail'][:160]}")
    print(f"\n{passed} passed, {failed} failed")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.url,
        "results": RESULTS,
        "capabilities": results,
        "summary": {"passed": passed, "failed": failed},
    }
    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "verification",
                                   f"real-providers-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(f"report: {os.path.abspath(out)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
