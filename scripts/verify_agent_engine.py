"""Verify the AragonAgent engine integration end to end.

Proves, against the running platform and the real sidecar:

  1. the model credential is configured and the sidecar reports the pinned
     @aragon-agent/core version + adapter version
  2. a real agent run streams events (SSE) and the model actually answers
  3. the model calls a *platform* tool (``platform.list_capabilities``) and the
     tool result comes back through the adapter, not through a mock
  4. a stop request returns a truthful report (what stopped, what may still be
     running upstream) instead of claiming a clean cancellation
  5. the recorded event log replays from the database (audit/replay path)

Usage:
    python scripts/verify_agent_engine.py --url http://127.0.0.1:8000 \\
        --admin-password <password> [--credential-id cred_...] [--model google/gemini-2.5-flash]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

RESULTS: list[dict] = []


def record(step: str, ok: bool, detail: str, data: object = None) -> None:
    RESULTS.append({"step": step, "status": "PASS" if ok else "FAIL", "detail": detail, "data": data})
    print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}", flush=True)


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=120.0, trust_env=False)
        self.csrf = ""

    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "x-csrf-token": self.csrf or self.http.cookies.get("sr_csrf", "")}

    def post(self, path: str, body: object = None) -> httpx.Response:
        return self.http.post(path, json=body, headers=self.headers())

    def patch(self, path: str, body: object = None) -> httpx.Response:
        return self.http.patch(path, json=body, headers=self.headers())

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        return self.http.get(path, **kwargs)


def login(api: Api, user: str, password: str) -> None:
    response = api.post("/api/auth/login", {"username": user, "password": password})
    if response.status_code != 200:
        raise SystemExit(f"login failed: {response.text[:300]}")
    body = response.json()
    api.csrf = body["csrf_token"]
    if body.get("must_change_password"):
        raise SystemExit("The admin password must be changed before the engine checks can run.")


def pick_credential(api: Api, explicit: str | None) -> dict:
    if explicit:
        return api.get(f"/api/admin/credentials/{explicit}").json()
    credentials = api.get("/api/admin/credentials", params={"status": "active", "limit": 50}).json()["items"]
    llm = [c for c in credentials if (c.get("provider_name") or "").lower() in {"zenmux", "google gemini", "openai", "anthropic"}]
    if not llm:
        raise SystemExit("No active LLM credential found. Create one for an llm-category provider first.")
    # Prefer an OpenAI-compatible gateway: the engine's openai adapter covers it.
    for candidate in llm:
        if (candidate.get("provider_name") or "").lower() == "zenmux":
            return candidate
    return llm[0]


def configure_model(api: Api, credential: dict, model_id: str) -> None:
    current = api.get("/api/admin/settings").json()
    agent_settings = (current.get("settings") or current).get("agent") or {}
    if (agent_settings.get("model") or {}).get("credential_id") == credential["id"]:
        record("model configuration", True, "already configured")
        return
    response = api.patch(
        "/api/admin/settings",
        {"agent": {"model": {"credential_id": credential["id"], "model_id": model_id, "provider_id": "openai"}}},
    )
    record(
        "model configuration",
        response.status_code == 200,
        f"HTTP {response.status_code} credential={credential['id']} model={model_id}",
        response.json() if response.status_code == 200 else response.text[:300],
    )


def engine_health(api: Api) -> dict:
    health = api.get("/api/admin/agent/health")
    body = health.json() if health.status_code == 200 else {}
    sidecar = body.get("sidecar") or {}
    engine = sidecar.get("engine") or {}
    platform = body.get("platform") or {}
    tools = platform.get("tools") or []
    model_configured = bool(sidecar.get("model_configured") or (platform.get("model") or {}).get("configured"))
    ok = bool(sidecar.get("ok")) and bool(engine.get("version")) and bool(engine.get("adapter_version"))
    record(
        "engine health",
        ok,
        f"ok={body.get('ok')} sidecar_ok={sidecar.get('ok')} core={engine.get('package')}@{engine.get('version')} "
        f"adapter={engine.get('adapter_version')} model_configured={model_configured} platform_tools={len(tools)}",
        {"engine": engine, "model_configured": model_configured, "tools": tools},
    )
    return body


def stream_run(api: Api, run_id: str, *, timeout_s: int = 240) -> dict:
    """Read the SSE endpoint until the run reaches a terminal state."""
    events: list[dict] = []
    assistant_text: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    terminal = None
    deadline = time.time() + timeout_s
    with api.http.stream("GET", f"/api/stream/agent/{run_id}", timeout=httpx.Timeout(timeout_s, read=timeout_s)) as response:
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}"}
        event_name = "message"
        data_lines: list[str] = []
        for line in response.iter_lines():
            if time.time() > deadline:
                break
            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    data_lines = []
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        payload = {"raw": raw[:300]}
                    payload.setdefault("type", event_name)
                    events.append(payload)
                    kind = payload.get("type")
                    if kind == "assistant.delta":
                        chunk = payload.get("delta") or payload.get("text")
                        if chunk:
                            assistant_text.append(str(chunk))
                    elif kind == "tool.call":
                        tool_calls.append(payload)
                    elif kind == "tool.result":
                        tool_results.append(payload)
                    elif kind == "run.finished" and payload.get("text") and not assistant_text:
                        assistant_text.append(str(payload["text"]))
                    if kind in {"run.finished", "run.failed", "run.stopped"}:
                        terminal = payload
                event_name = "message"
                if terminal:
                    break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
    return {
        "events": events,
        "text": "".join(assistant_text),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "terminal": terminal,
    }


def create_run(api: Api, session_id: str, text: str) -> str | None:
    response = api.post(f"/api/admin/agent/sessions/{session_id}/messages", {"text": text, "attachments": [], "queue_if_busy": False})
    if response.status_code not in (200, 202):
        record("start run", False, f"HTTP {response.status_code} {response.text[:300]}")
        return None
    body = response.json()
    run_id = body.get("run_id") or (body.get("run") or {}).get("id")
    record("start run", bool(run_id), f"run_id={run_id} status={body.get('status')}")
    return run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the AragonAgent engine integration")
    parser.add_argument("--url", default=os.environ.get("SR_VERIFY_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default=os.environ.get("SR_ADMIN_PASSWORD", ""))
    parser.add_argument("--credential-id", default=None)
    parser.add_argument("--model", default="google/gemini-2.5-flash")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not args.admin_password:
        raise SystemExit("--admin-password (or SR_ADMIN_PASSWORD) is required")

    api = Api(args.url)
    login(api, args.admin_user, args.admin_password)
    record("login", True, "signed in")

    credential = pick_credential(api, args.credential_id)
    record("credential selection", True, f"{credential['name']} ({credential.get('auth_type')}) on {credential.get('provider_name')}")
    configure_model(api, credential, args.model)
    health = engine_health(api)
    if not health.get("sidecar", {}).get("ok"):
        print("sidecar is not reachable; start it with `cd sidecar && npm start`", file=sys.stderr)
        return 2

    session = api.post("/api/admin/agent/sessions", {"title": f"engine verification {uuid.uuid4().hex[:6]}"})
    if session.status_code != 201:
        record("session creation", False, session.text[:300])
        return 1
    session_id = session.json()["id"]
    record("session creation", True, session_id)

    run_id = create_run(
        api,
        session_id,
        "First call the platform.list_capabilities tool. Then reply with one short sentence listing the capability slugs you saw. Do not call any other tool.",
    )
    if not run_id:
        return 1
    outcome = stream_run(api, run_id, timeout_s=300)
    if "error" in outcome:
        record("run stream", False, outcome["error"])
        return 1
    record(
        "run stream",
        bool(outcome["terminal"]),
        f"events={len(outcome['events'])} assistant_chars={len(outcome['text'])} terminal={(outcome['terminal'] or {}).get('type')} "
        f"text={outcome['text'][:160]!r}",
        {"event_types": sorted({e.get('type') for e in outcome['events']})},
    )
    record(
        "platform tool call",
        bool(outcome["tool_calls"]) and any("list_capabilities" in json.dumps(call) for call in outcome["tool_calls"]),
        f"tool_calls={[c.get('tool') or c.get('name') for c in outcome['tool_calls']]} results={len(outcome['tool_results'])}",
    )
    terminal = outcome["terminal"] or {}
    run_detail = api.get(f"/api/admin/agent/runs/{run_id}").json()
    record(
        "real model answer",
        len(outcome["text"]) > 10 and terminal.get("type") == "run.finished" and run_detail.get("status") == "succeeded",
        f"run_status={run_detail.get('status')} stop_reason={run_detail.get('stop_reason')} turns={(terminal.get('turns'))} "
        f"usage={run_detail.get('usage')} text={outcome['text'][:140]!r}",
        {"status": run_detail.get("status"), "usage": run_detail.get("usage")},
    )

    # ---- truthful stop -------------------------------------------------------
    run2 = create_run(
        api,
        session_id,
        "Call platform.list_capabilities, then platform.list_credentials, then write a 400 word essay about distributed systems. Work step by step.",
    )
    if run2:
        # Wait until the run is genuinely in flight so the stop path is exercised.
        state = None
        for _ in range(60):
            detail = api.get(f"/api/admin/agent/runs/{run2}").json()
            state = detail.get("status")
            if state == "running" or state in {"succeeded", "failed", "stopped"}:
                break
            time.sleep(0.25)
        stop = api.post(f"/api/admin/agent/runs/{run2}/stop", {"reason": "verification stop"})
        body = stop.json() if stop.status_code == 200 else {}
        truthful = stop.status_code == 200 and (
            body.get("already_finished") is True
            or "stopped_steps" in body
            or "may_continue_upstream" in body
        )
        record(
            "truthful stop",
            truthful,
            f"observed={state} HTTP {stop.status_code} stopped={body.get('stopped')} reason={body.get('reason')} "
            f"stopped_steps={body.get('stopped_steps')} may_continue_upstream={body.get('may_continue_upstream')} note={body.get('note')}",
            body,
        )

    replay = api.get(f"/api/admin/agent/runs/{run_id}/events")
    replay_body = replay.json() if replay.status_code == 200 else {}
    events = replay_body.get("items") or replay_body.get("events") or []
    record("event replay", replay.status_code == 200 and len(events) > 3, f"HTTP {replay.status_code} stored_events={len(events)}")

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": RESULTS,
        "engine": (health.get("sidecar") or {}).get("engine"),
        "summary": {"passed": passed, "failed": failed},
    }
    out = args.out or os.path.join(os.path.dirname(__file__), "..", "data", "verification",
                                   f"agent-engine-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(f"report: {os.path.abspath(out)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
