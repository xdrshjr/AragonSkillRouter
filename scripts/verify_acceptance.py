#!/usr/bin/env python
"""Skill Router acceptance verifier (live server).

Runs the read-only acceptance checks from docs/CONTRACT.md against a *running*
instance and prints a PASS/FAIL/SKIP table with the request id of every call, so
a failed check can be traced in the server log with the same number.

    python scripts/verify_acceptance.py --base-url http://127.0.0.1:8000 \\
        --token sr_live_xxx --admin-user admin --admin-password '...'

    # optional: call one capability through MCP (the only non-read step)
    python scripts/verify_acceptance.py ... --capability text.summarize \\
        --input '{"prompt": "hello"}'

Design rules
------------
* stdlib + httpx only.
* It creates and deletes **no** platform data. The only mutations are a login
  (a session row), and — only when ``--capability`` is passed — a single
  invocation the caller explicitly asked for.
* The CSRF proof posts an *empty* body to a create endpoint, which is rejected by
  validation before anything is written.
* Exit code 1 when any check FAILs; 0 otherwise (SKIPs are not failures).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

try:  # httpx is a runtime dependency of the backend, not of this script's caller
    import httpx
except ModuleNotFoundError:  # pragma: no cover - operator feedback
    print("This script needs httpx (it ships with the backend venv): pip install httpx", file=sys.stderr)
    raise SystemExit(2)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

ENVELOPE_KEYS = ("code", "message", "details", "request_id", "actions")


@dataclass
class Check:
    name: str
    result: str
    detail: str = ""
    request_id: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, result: str, detail: str = "", request_id: str = "") -> None:
        self.checks.append(Check(name=name, result=result, detail=detail, request_id=request_id))

    def ok(self, name: str, detail: str = "", request_id: str = "") -> None:
        self.record(name, PASS, detail, request_id)

    def fail(self, name: str, detail: str, request_id: str = "") -> None:
        self.record(name, FAIL, detail, request_id)

    def skip(self, name: str, detail: str) -> None:
        self.record(name, SKIP, detail)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.result == FAIL]

    def render(self) -> str:
        width = max([len("CHECK")] + [len(check.name) for check in self.checks] + [24])
        lines = [
            f"{'CHECK'.ljust(width)}  RESULT  DETAIL / REQUEST ID",
            f"{'-' * width}  ------  {'-' * 60}",
        ]
        for check in self.checks:
            detail = check.detail
            if check.request_id:
                detail = f"{detail} [{check.request_id}]" if detail else f"[{check.request_id}]"
            lines.append(f"{check.name.ljust(width)}  {check.result:6}  {detail}")
        counts = {key: sum(1 for c in self.checks if c.result == key) for key in (PASS, FAIL, SKIP)}
        lines.append("")
        lines.append(f"total={len(self.checks)}  pass={counts[PASS]}  fail={counts[FAIL]}  skip={counts[SKIP]}")
        return "\n".join(lines)


# ------------------------------------------------------------------ helpers ---
def request_id_of(response: Any, body: Any = None) -> str:
    header = (response.headers.get("x-request-id") or "").strip()
    if header:
        return header
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("request_id") or "")
        return str(body.get("request_id") or "")
    return ""


def parse_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is itself a finding
        return None


def check_envelope(report: Report, name: str, response: Any) -> dict[str, Any] | None:
    """Assert the uniform error envelope and return its `error` object."""
    body = parse_json(response)
    rid = request_id_of(response, body)
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        report.fail(name, f"no error envelope (HTTP {response.status_code})", rid)
        return None
    missing = [key for key in ENVELOPE_KEYS if key not in error]
    if missing:
        report.fail(name, f"envelope missing {missing} (HTTP {response.status_code})", rid)
        return None
    if not str(error.get("request_id", "")).startswith("req_"):
        report.fail(name, f"bad request_id {error.get('request_id')!r}", rid)
        return None
    return error


def list_envelope(name: str, payload: Any) -> str | None:
    """Validate `{items,total,limit,offset}` and return a problem description."""
    if not isinstance(payload, dict):
        return f"{name}: response is not a JSON object"
    missing = {"items", "total", "limit", "offset"} - set(payload)
    if missing:
        return f"{name}: list envelope missing {sorted(missing)}"
    if not isinstance(payload["items"], list):
        return f"{name}: items is not a list"
    return None


# ------------------------------------------------------------------- checks ---
def check_health(report: Report, client: httpx.Client) -> None:
    response = client.get("/api/health")
    body = parse_json(response) or {}
    if response.status_code == 200 and body.get("status") == "ok":
        report.ok("health", f"service={body.get('service')} version={body.get('version')}", request_id_of(response, body))
    else:
        report.fail("health", f"HTTP {response.status_code}: {response.text[:120]}", request_id_of(response, body))

    ready = client.get("/api/health/ready")
    ready_body = parse_json(ready) or {}
    checks = ready_body.get("checks") if isinstance(ready_body.get("checks"), dict) else {}
    summary = ", ".join(f"{key}={value.get('status')}" for key, value in checks.items() if isinstance(value, dict))
    name = "ready"
    if ready.status_code == 200 and ready_body.get("status") == "ready":
        report.ok(name, summary or "ready", request_id_of(ready, ready_body))
    elif ready.status_code == 503:
        report.fail(name, f"degraded: {summary}", request_id_of(ready, ready_body))
    else:
        report.fail(name, f"HTTP {ready.status_code}: {ready.text[:120]}", request_id_of(ready, ready_body))


def check_meta(report: Report, client: httpx.Client) -> None:
    response = client.get("/api/meta")
    body = parse_json(response) or {}
    if response.status_code == 200 and body.get("app") == "skill-router":
        report.ok(
            "meta",
            f"api={body.get('api_version')} env={body.get('environment')} initialized={body.get('initialized')}",
            request_id_of(response, body),
        )
    else:
        report.fail("meta", f"HTTP {response.status_code}: {response.text[:120]}", request_id_of(response, body))

    anonymous = client.get("/api/admin/overview")
    error = check_envelope(report, "anonymous admin call is 401 + envelope", anonymous)
    if error is not None:
        if anonymous.status_code == 401 and error.get("code") == "unauthenticated":
            report.ok("anonymous admin call is 401 + envelope", "code=unauthenticated", request_id_of(anonymous))
        else:
            report.fail(
                "anonymous admin call is 401 + envelope",
                f"HTTP {anonymous.status_code} code={error.get('code')}",
                request_id_of(anonymous),
            )


def check_admin_login(report: Report, client: httpx.Client, args: argparse.Namespace) -> str:
    """Returns the CSRF token ("" when the admin flow is unavailable)."""
    if not (args.admin_user and args.admin_password):
        report.skip("admin login + CSRF", "no --admin-user/--admin-password supplied")
        return ""

    login = client.post("/api/auth/login", json={"username": args.admin_user, "password": args.admin_password})
    body = parse_json(login) or {}
    rid = request_id_of(login, body)
    if login.status_code != 200:
        error = body.get("error") if isinstance(body, dict) else None
        report.fail(
            "admin login + CSRF",
            f"login HTTP {login.status_code} code={(error or {}).get('code')} (check the credentials)",
            rid,
        )
        return ""
    csrf = str(body.get("csrf_token") or "")
    if not csrf or "sr_session" not in login.cookies:
        report.fail("admin login + CSRF", "login answered without a session cookie / csrf token", rid)
        return ""

    session = client.get("/api/auth/session")
    session_body = parse_json(session) or {}
    if session.status_code != 200:
        report.fail("admin login + CSRF", f"session probe HTTP {session.status_code}", request_id_of(session, session_body))
        return csrf
    must_change = bool(session_body.get("must_change_password"))
    user = (session_body.get("user") or {}).get("username")
    report.ok(
        "admin login + CSRF",
        f"user={user} must_change_password={must_change}",
        request_id_of(session, session_body),
    )
    if must_change:
        report.skip("admin console checks", "the admin must change the initial password before the console opens")
        return ""

    # CSRF enforcement, proven with an empty body that validation rejects before
    # anything is written: no data is created either way.
    blocked = client.post("/api/admin/skills", json={})
    error = check_envelope(report, "mutation without x-csrf-token is refused", blocked)
    if error is not None:
        if blocked.status_code == 403 and error.get("code") == "csrf_failed":
            report.ok("mutation without x-csrf-token is refused", "code=csrf_failed", request_id_of(blocked))
        else:
            report.fail(
                "mutation without x-csrf-token is refused",
                f"HTTP {blocked.status_code} code={error.get('code')}",
                request_id_of(blocked),
            )

    with_token = client.post("/api/admin/skills", json={}, headers={"x-csrf-token": csrf})
    with_body = parse_json(with_token) or {}
    with_error = with_body.get("error") if isinstance(with_body, dict) else None
    if with_token.status_code == 403 and (with_error or {}).get("code") == "csrf_failed":
        report.fail(
            "mutation with x-csrf-token passes the middleware",
            "the valid page token was rejected",
            request_id_of(with_token, with_body),
        )
    elif with_token.status_code in (201, 200):
        report.fail(
            "mutation with x-csrf-token passes the middleware",
            "an empty body created a SKILL — expected validation to reject it (check the verifier)",
            request_id_of(with_token, with_body),
        )
    else:
        # 422 validation_failed is the expected, side-effect-free answer.
        report.ok(
            "mutation with x-csrf-token passes the middleware",
            f"reached the handler (HTTP {with_token.status_code} code={(with_error or {}).get('code')})",
            request_id_of(with_token, with_body),
        )

    foreign = client.post(
        "/api/admin/skills",
        json={},
        headers={"x-csrf-token": csrf, "origin": "https://verify-acceptance.invalid"},
    )
    error = check_envelope(report, "foreign Origin is refused", foreign)
    if error is not None:
        if foreign.status_code == 403 and error.get("code") == "origin_rejected":
            report.ok("foreign Origin is refused", "code=origin_rejected", request_id_of(foreign))
        else:
            report.fail(
                "foreign Origin is refused",
                f"HTTP {foreign.status_code} code={error.get('code')}",
                request_id_of(foreign),
            )
    return csrf


def check_admin_console(report: Report, client: httpx.Client, csrf: str) -> None:
    if not csrf:
        report.skip("overview", "admin session unavailable")
        report.skip("providers/credentials/capabilities listing", "admin session unavailable")
        return

    overview = client.get("/api/admin/overview")
    body = parse_json(overview) or {}
    if overview.status_code == 200 and isinstance(body.get("counts"), dict):
        counts = body["counts"]
        capabilities = (counts.get("capabilities") or {}).get("total")
        report.ok(
            "overview",
            f"capabilities={capabilities} providers={(counts.get('providers') or {}).get('total')} "
            f"problems={len(body.get('problems') or [])}",
            request_id_of(overview, body),
        )
    else:
        report.fail("overview", f"HTTP {overview.status_code}: {overview.text[:140]}", request_id_of(overview, body))

    for path, label in (
        ("/api/admin/providers", "provider listing"),
        ("/api/admin/credentials", "credential listing"),
        ("/api/admin/capabilities", "capability listing"),
    ):
        response = client.get(path)
        payload = parse_json(response)
        rid = request_id_of(response, payload)
        if response.status_code != 200:
            report.fail(label, f"HTTP {response.status_code}: {response.text[:120]}", rid)
            continue
        problem = list_envelope(label, payload)
        if problem:
            report.fail(label, problem, rid)
        else:
            report.ok(label, f"total={payload['total']} shown={len(payload['items'])}", rid)

    settings = client.get("/api/admin/settings")
    settings_body = parse_json(settings) or {}
    if settings.status_code == 200 and isinstance(settings_body.get("settings"), dict):
        engine = settings_body.get("engine") or {}
        report.ok(
            "settings",
            f"engine adapter={engine.get('adapter_version')} release={(engine.get('release') or {}).get('version')}",
            request_id_of(settings, settings_body),
        )
    else:
        report.fail("settings", f"HTTP {settings.status_code}: {settings.text[:140]}", request_id_of(settings, settings_body))


def check_external(report: Report, client: httpx.Client, args: argparse.Namespace) -> dict[str, str]:
    """whoami + capability discovery with the agent token. Returns {"capability": slug}."""
    headers = {"authorization": f"Bearer {args.token}"}

    # Probe anonymity with a cookie-less client: this process may already hold the
    # administrator session cookie, and app.deps.require_agent deliberately lets a
    # signed-in admin use the external routes from the UI (that is not a bypass).
    with httpx.Client(
        base_url=str(client.base_url),
        timeout=client.timeout,
        trust_env=client.trust_env,
        follow_redirects=False,
    ) as anonymous_client:
        anonymous = anonymous_client.get("/api/v1/whoami")
    if anonymous.status_code == 401:
        error = check_envelope(report, "external surface refuses anonymous calls", anonymous)
        if error is not None and error.get("code") == "unauthenticated":
            report.ok("external surface refuses anonymous calls", "code=unauthenticated", request_id_of(anonymous))
        elif error is not None:
            report.fail("external surface refuses anonymous calls", f"unexpected code {error.get('code')}", request_id_of(anonymous))
    else:
        report.fail(
            "external surface refuses anonymous calls",
            f"HTTP {anonymous.status_code} without a token",
            request_id_of(anonymous),
        )

    whoami = client.get("/api/v1/whoami", headers=headers)
    body = parse_json(whoami) or {}
    rid = request_id_of(whoami, body)
    if whoami.status_code == 200 and isinstance(body.get("agent"), dict):
        report.ok(
            "external whoami",
            f"agent={body['agent'].get('name')} scopes={len(body.get('scopes') or [])} "
            f"capabilities={body.get('capabilities_count')}",
            rid,
        )
    else:
        error = body.get("error") if isinstance(body, dict) else None
        report.fail("external whoami", f"HTTP {whoami.status_code} code={(error or {}).get('code')}", rid)
        return {}

    listing = client.get("/api/v1/capabilities", headers=headers)
    listing_body = parse_json(listing) or {}
    etag = listing.headers.get("etag", "")
    rid = request_id_of(listing, listing_body)
    capabilities = listing_body.get("capabilities") or []
    if listing.status_code == 200 and isinstance(capabilities, list):
        report.ok(
            "external capabilities",
            f"count={len(capabilities)} version_token={str(listing_body.get('version_token'))[:12]}...",
            rid,
        )
    else:
        report.fail("external capabilities", f"HTTP {listing.status_code}: {listing.text[:120]}", rid)

    if not etag:
        report.fail("capabilities ETag", "no etag header on GET /api/v1/capabilities", rid)
    else:
        again = client.get("/api/v1/capabilities", headers={**headers, "if-none-match": etag})
        if again.status_code == 304 and not again.content:
            report.ok("capabilities ETag", f"304 for {etag[:18]}...", request_id_of(again))
        else:
            report.fail(
                "capabilities ETag",
                f"expected 304, got HTTP {again.status_code} ({len(again.content)} bytes)",
                request_id_of(again),
            )

    skill = client.get("/api/v1/skill", headers=headers)
    skill_body = parse_json(skill) or {}
    rid = request_id_of(skill, skill_body)
    if skill.status_code == 200 and skill_body.get("includes_credentials") is False and skill_body.get("content"):
        report.ok("external SKILL bundle", f"sha256={str(skill_body.get('sha256'))[:12]}...", rid)
    else:
        report.fail("external SKILL bundle", f"HTTP {skill.status_code}: {skill.text[:140]}", rid)

    slugs = [item.get("slug") for item in capabilities if isinstance(item, dict) and item.get("slug")]
    return {"capability": args.capability or (slugs[0] if slugs else ""), "capabilities": ",".join(slugs)}


def check_mcp(report: Report, client: httpx.Client, args: argparse.Namespace, capability: str) -> None:
    headers = {"authorization": f"Bearer {args.token}", "accept": "application/json"}
    initialize = client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify_acceptance"}},
        },
    )
    body = parse_json(initialize) or {}
    rid = request_id_of(initialize, body)
    result = body.get("result") if isinstance(body, dict) else None
    session_id = initialize.headers.get("mcp-session-id", "")
    if initialize.status_code == 200 and isinstance(result, dict) and result.get("protocolVersion"):
        report.ok(
            "mcp initialize",
            f"protocol={result.get('protocolVersion')} server={(result.get('serverInfo') or {}).get('name')} "
            f"session={'yes' if session_id else 'no'}",
            rid,
        )
    else:
        error = (body.get("error") or {}) if isinstance(body, dict) else {}
        report.fail("mcp initialize", f"HTTP {initialize.status_code} error={error.get('message')}", rid)
        return

    mcp_headers = {**headers, **({"mcp-session-id": session_id} if session_id else {})}
    client.post("/api/mcp", headers=mcp_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = client.post("/api/mcp", headers=mcp_headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools_body = parse_json(tools) or {}
    rid = request_id_of(tools, tools_body)
    tool_list = ((tools_body.get("result") or {}) if isinstance(tools_body, dict) else {}).get("tools")
    if tools.status_code == 200 and isinstance(tool_list, list):
        names = [tool.get("name") for tool in tool_list if isinstance(tool, dict)]
        schema_ok = all(isinstance(tool.get("inputSchema"), dict) for tool in tool_list if isinstance(tool, dict))
        report.ok(
            "mcp tools/list",
            f"tools={len(names)} inputSchema={'ok' if schema_ok else 'MISSING'} names={','.join(names[:4])}"
            + ("..." if len(names) > 4 else ""),
            rid,
        )
    else:
        report.fail("mcp tools/list", f"HTTP {tools.status_code}: {tools.text[:140]}", rid)
        return

    if not args.capability and not capability:
        report.skip("mcp tools/call", "no --capability supplied (the only non-read check)")
        return
    tool_name = capability.replace(".", "__")
    arguments = json.loads(args.input) if args.input else {}
    if not arguments:
        # Build a minimal argument object from the capability's own input schema,
        # so this check exercises the transport instead of failing schema
        # validation for a missing required field.
        descriptors = (parse_json(client.get("/api/v1/capabilities", headers=headers)) or {}).get("capabilities") or []
        descriptor = next((item for item in descriptors if item.get("slug") == capability), {})
        schema = descriptor.get("input_schema") or {}
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            spec = properties.get(field) or {}
            kind = spec.get("type")
            if kind == "string":
                arguments[field] = spec.get("examples", ["acceptance check"])[0] if spec.get("examples") else "acceptance check"
            elif kind in {"number", "integer"}:
                arguments[field] = 1
            elif kind == "boolean":
                arguments[field] = True
            elif kind == "array":
                arguments[field] = []
            else:
                arguments[field] = {}
    call = client.post(
        "/api/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}},
    )
    call_body = parse_json(call) or {}
    rid = request_id_of(call, call_body)
    call_result = (call_body.get("result") or {}) if isinstance(call_body, dict) else {}
    if call.status_code != 200:
        report.fail("mcp tools/call", f"HTTP {call.status_code}: {call.text[:140]}", rid)
        return
    # An unknown/ungranted tool is a JSON-RPC *error*, not a result: without this
    # branch the check would report a bogus PASS.
    rpc_error = call_body.get("error") if isinstance(call_body, dict) else None
    if isinstance(rpc_error, dict):
        data = rpc_error.get("data") if isinstance(rpc_error.get("data"), dict) else {}
        report.fail(
            "mcp tools/call",
            f"json-rpc error {rpc_error.get('code')}: {str(rpc_error.get('message'))[:80]} "
            f"(capability code={(data or {}).get('code')})",
            rid,
        )
        return
    if not isinstance(call_body.get("result"), dict):
        report.fail("mcp tools/call", f"no result object: {call.text[:140]}", rid)
        return
    if call_result.get("isError") is True:
        structured = call_result.get("structuredContent") or {}
        error = (structured.get("error") or {}) if isinstance(structured, dict) else {}
        report.fail(
            "mcp tools/call",
            f"tool error code={error.get('code')} message={str(error.get('message'))[:100]}",
            error.get("request_id") or rid,
        )
        return
    structured = call_result.get("structuredContent") or {}
    status = structured.get("status") if isinstance(structured, dict) else None
    report.ok(
        "mcp tools/call",
        f"tool={tool_name} status={status} task_id={structured.get('task_id') if isinstance(structured, dict) else None}",
        rid,
    )


def check_tasks(report: Report, client: httpx.Client, args: argparse.Namespace) -> None:
    headers = {"authorization": f"Bearer {args.token}"}

    task_id = args.task_id
    if not task_id and args.admin_user and args.admin_password:
        listing = client.get("/api/admin/tasks", params={"limit": 1})
        if listing.status_code == 200:
            payload = parse_json(listing) or {}
            items = payload.get("items") or []
            if items and isinstance(items[0], dict):
                task_id = items[0].get("id")

    if not task_id:
        report.skip("task lifecycle read", "no task exists yet (and no --task-id given)")
        return

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    body = parse_json(detail) or {}
    rid = request_id_of(detail, body)
    if detail.status_code == 200 and isinstance(body, dict) and body.get("status"):
        report.ok(
            "task lifecycle read",
            f"task={task_id} status={body.get('status')} progress={body.get('progress')} "
            f"poll={body.get('poll')}",
            rid,
        )
        return
    if detail.status_code == 404:
        report.skip("task lifecycle read", f"task {task_id} is not visible to this agent token (ownership scope)")
        return
    error = (body.get("error") or {}) if isinstance(body, dict) else {}
    report.fail("task lifecycle read", f"HTTP {detail.status_code} code={error.get('code')}", rid)


# --------------------------------------------------------------------- main ---
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_acceptance",
        description="Run the Skill Router acceptance checks against a live server (read-only).",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Server root, e.g. http://127.0.0.1:8000")
    parser.add_argument("--token", help="Agent access token (sr_live_...) for the /api/v1 and /api/mcp checks")
    parser.add_argument("--admin-user", help="Administrator username (enables the console checks)")
    parser.add_argument("--admin-password", help="Administrator password")
    parser.add_argument("--capability", help="Capability slug to call once through MCP (e.g. text.summarize)")
    parser.add_argument("--input", help="JSON input for the MCP tools/call (default {})")
    parser.add_argument("--task-id", help="Async task id to read (default: the newest task, when admin creds are given)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds")
    parser.add_argument(
        "--use-env-proxy",
        action="store_true",
        help="Honour HTTP(S)_PROXY from the environment. Off by default: a local "
        "acceptance run must not be hijacked by a desktop proxy (which answers 502 for 127.0.0.1).",
    )
    parser.add_argument("--json", action="store_true", help="Also print the checks as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")
    report = Report()
    headers = {"user-agent": "skill-router-verify-acceptance/1.0"}

    try:
        with httpx.Client(
            base_url=base_url,
            timeout=args.timeout,
            headers=headers,
            follow_redirects=False,
            trust_env=args.use_env_proxy,
        ) as client:
            check_health(report, client)
            check_meta(report, client)
            csrf = check_admin_login(report, client, args)
            check_admin_console(report, client, csrf)
            if args.token:
                external = check_external(report, client, args)
                check_mcp(report, client, args, external.get("capability", ""))
                check_tasks(report, client, args)
            else:
                for name in ("external whoami", "external capabilities", "capabilities ETag", "external SKILL bundle",
                             "mcp initialize", "mcp tools/list", "mcp tools/call", "task lifecycle read"):
                    report.skip(name, "no --token supplied")
    except httpx.RequestError as exc:
        report.fail("connect", f"{type(exc).__name__}: {exc} - is the server running at {base_url}?")
    except json.JSONDecodeError as exc:
        report.fail("json", f"invalid JSON from the server: {exc}")

    print(report.render())
    if args.json:
        print()
        print(json.dumps([check.__dict__ for check in report.checks], indent=2))

    if report.failures:
        print(f"\n{len(report.failures)} check(s) FAILED:", file=sys.stderr)
        for check in report.failures:
            print(f"  - {check.name}: {check.detail} [{check.request_id}]", file=sys.stderr)
        return 1
    print("\nall acceptance checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
