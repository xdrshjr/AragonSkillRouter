# API reference

Two surfaces, one platform:

* **`/api/v1`** — the external API for agents. Bearer token. This is what
  `skill-router-connect` talks to.
* **`/api/mcp`** — the same capabilities as MCP tools (Streamable HTTP, JSON-RPC 2.0).
  Same bearer token, same limits, same records.
* **`/api/admin/...`** and **`/api/auth`** — the browser surface (session cookie +
  CSRF). Summarised in §1.2; the machine-readable version is the OpenAPI document (§8).

Examples below were produced against a real instance of this API and are edited only
for length. Ids, slugs and numbers are illustrative.

---

## 1. Authentication

### 1.1 Agent token (external surfaces)

```
Authorization: Bearer sr_live_<random>
```

* Issued once in **Access → Tokens** (`POST /api/admin/agents/{agent_id}/tokens`). The
  plaintext is shown a single time; only a hash is stored.
* A token carries an explicit `scopes` list of capability slugs. An empty list or `"*"`
  means "every published capability granted to this agent". A capability that is not
  granted is reported exactly like a missing one (`404`), so the surface cannot be used
  to enumerate what exists.
* Optional per-token limits: `rate_limit_per_min`, `max_concurrency`, `budget_usd`,
  `expires_in_days`. See `GET /api/v1/whoami`.
* Failure is a **plain HTTP 401 with the normal error envelope**, not a JSON-RPC error
  (even on `/api/mcp`, because at that point the platform does not know which client is
  talking).
* The token is never echoed back — not in `whoami`, not in errors, not in the SKILL
  bundle.

### 1.2 Browser session + CSRF (admin surface)

1. `POST /api/auth/login` with `{"username","password"}` sets two cookies:
   `sr_session` (HttpOnly) and `sr_csrf` (readable by JS).
2. Every **mutating** request must send `x-csrf-token: <value of sr_csrf>`.
   A missing/expired token answers `403 csrf_failed`.
3. A mutating request whose `Origin` header is not in `SR_TRUSTED_ORIGINS` answers
   `403 origin_rejected`.
4. While `must_change_password` is set, every business route answers
   `403 password_change_required`. Only `/api/auth/*`, `/api/health*`, `/api/meta` and
   `/api/v1/*` (token-authenticated) work.

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H "content-type: application/json" \
  -d '{"username":"admin","password":"…"}'
# -> {"user":{...},"csrf_token":"…","must_change_password":true}

curl -b cookies.txt http://127.0.0.1:8000/api/admin/overview
curl -b cookies.txt -X POST http://127.0.0.1:8000/api/admin/agents \
  -H "content-type: application/json" -H "x-csrf-token: <csrf_token>" \
  -d '{"name":"research-bot"}'
```

There is no API-key path on the admin surface, no OAuth and no public signup.

### 1.3 Error envelope

Every failure — both surfaces, both auth kinds — uses one shape:

```json
{
  "error": {
    "code": "rate_limited",
    "message": "This token is limited to 60 calls per minute.",
    "details": {"limit": 60, "used": 60, "window": "1m"},
    "request_id": "req_9f2c1b6a8d3f4e10",
    "actions": ["Retry after a short wait.", "Ask the administrator to raise the limit."]
  }
}
```

`request_id` is the same string stored on the invocation row, written to the audit log
and returned in the `x-request-id` response header. Quote it in any support request.

---

## 2. `GET /api/v1` — route index (no auth)

A discovery aid for installers: what this surface offers, without authenticating.

```bash
curl -s http://127.0.0.1:8000/api/v1
```

```json
{
  "service": "skill-router",
  "api_version": "1",
  "auth": "Authorization: Bearer <agent token>",
  "routes": [{"method": "GET", "path": "/api/v1/whoami", "summary": "Agent identity, scopes and limits (connectivity check)."}],
  "mcp": {"url": "/api/mcp", "transport": "streamable-http", "protocol_versions": ["2025-06-18", "2024-11-05"]},
  "content_types": {"invocations": ["application/json", "text/event-stream"]},
  "error_envelope": {"error": {"code": "string", "message": "string", "details": {}, "request_id": "req_…", "actions": []}}
}
```

---

## 3. `GET /api/v1/whoami` — connectivity check

```bash
curl -s -H "Authorization: Bearer $SR_AGENT_TOKEN" http://127.0.0.1:8000/api/v1/whoami
```

```json
{
  "service": "skill-router",
  "api_version": "1",
  "agent": {"id": "agent_2b7c", "name": "research-bot"},
  "scopes": ["text.summarize", "image.generate"],
  "limits": {"rate_limit_per_min": 60, "max_concurrency": 4, "budget_usd": 25.0},
  "counters": {"agent_id": "agent_2b7c", "calls_last_minute": 3, "concurrency": 1},
  "capabilities_count": 2,
  "capabilities_url": "/api/v1/capabilities",
  "mcp_url": "/api/mcp",
  "server_time": "2026-10-08T09:14:05.123456Z"
}
```

`limits` values may be `null` — that means "no limit configured for this token", not
zero. Use it as the health check: if this answers `200`, the token is valid.

---

## 4. `GET /api/v1/capabilities`

Granted **and** published capabilities only.

```bash
curl -s -H "Authorization: Bearer $SR_AGENT_TOKEN" http://127.0.0.1:8000/api/v1/capabilities
```

```json
{
  "capabilities": [
    {
      "slug": "text.summarize",
      "name": "Summarize",
      "type": "text",
      "description": "Condense a document into a short summary.",
      "versions": {"current": 2, "available": [1, 2]},
      "modes": {"sync": true, "async": false, "stream": true},
      "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "max_words": {"type": "integer"}},
        "required": ["prompt"],
        "additionalProperties": false
      },
      "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
      "limits": {"timeout_s": 60, "max_input_bytes": 262144, "max_output_bytes": 8388608,
                 "file_input": false, "max_file_mb": 10, "max_duration_s": null},
      "examples": [{"input": {"prompt": "…", "max_words": 60}, "note": "Short summary."}],
      "needs_live_agent": false,
      "updated_at": "2026-10-07T18:22:41.000000Z",
      "version_token": "b0f7…"
    }
  ],
  "count": 1,
  "version_token": "3c9a…",
  "generated_at": "2026-10-08T09:14:05.123456Z",
  "refresh_url": "/api/v1/capabilities?refresh=1",
  "notes": ["Refresh when version_token changes; a breaking change ships as a new version."]
}
```

* `version_token` is the sha256 of the granted descriptors. **Refresh when it
  changes**; never cache descriptors across a token change.
* The response sends an `ETag` and honours `If-None-Match` → `304 Not Modified`, so
  polling is cheap. `?refresh=1` recomputes and skips the conditional.
* `Cache-Control: private, max-age=0, must-revalidate`, `Vary: authorization`.

### 4.1 `GET /api/v1/capabilities/{slug}`

The same descriptor for one capability, plus `mcp_tool` and `invoke_url`:

```json
{
  "slug": "text.summarize",
  "mcp_tool": "text__summarize",
  "invoke_url": "/api/v1/invocations",
  "…": "everything from the list entry"
}
```

`404 not_found` for a slug that is unknown **or** not granted to this token — the two
cases are deliberately indistinguishable.

---

## 5. `POST /api/v1/invocations`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/invocations \
  -H "Authorization: Bearer $SR_AGENT_TOKEN" \
  -H "content-type: application/json" \
  -d '{
        "capability": "text.summarize",
        "version": 2,
        "input": {"prompt": "Summarise this contract in two sentences.", "max_words": 60},
        "options": {"mode": "sync", "idempotency_key": "summary-run-42", "timeout_s": 30}
      }'
```

Request body:

| Field | Type | Notes |
|---|---|---|
| `capability` | string | slug, required |
| `version` | int \| string | a published version number (or version id). Omit for current. A non-published version is `409 capability_not_published` |
| `input` | object | validated against the capability's `input_schema` |
| `options.mode` | `sync` \| `async` \| `stream` | default `sync`; unknown values → `422 mode_not_supported` |
| `options.idempotency_key` | string ≤ 200 | same key within the dedupe window returns the first result with `deduplicated: true` |
| `options.timeout_s` | 0 < n ≤ 600 | per-call upstream timeout |
| `options.stream` | bool | same as sending `Accept: text/event-stream` |

Success (`200`):

```json
{
  "request_id": "req_9f2c1b6a8d3f4e10",
  "invocation_id": "inv_5d1c0a7f0b2e4c33",
  "status": "succeeded",
  "mode": "sync",
  "capability": "text.summarize",
  "version": 2,
  "task_id": null,
  "result": {"summary": "Skill Router routes capability calls across providers."},
  "artifacts": [{"id": "file_1a2b3c4d", "url": "/api/v1/files/file_1a2b3c4d",
                 "content_type": "audio/mpeg", "size": 1234}],
  "usage": {"input_tokens": 128, "output_tokens": 42, "source": "provider"},
  "cost": {"estimated_usd": 0.00021, "actual_usd": null, "currency": "USD"},
  "provider": {"id": "prov_1", "name": "Acme AI"},
  "route_reason": "chose Acme AI via route route_1 (strategy=priority, health=healthy, priority=10); 0 fallback(s) available",
  "error": null,
  "http_status": 200,
  "latency_ms": 840,
  "attempts": 1,
  "deduplicated": false
}
```

`status` semantics — this is the part clients get wrong:

| `status` | Meaning | Correct client behaviour |
|---|---|---|
| `succeeded` | the provider answered; `result` validated against `output_schema` | use `result` |
| `failed` | refused before sending, or the provider returned an error | read `error.code` / `error.actions` |
| `uncertain` | the request may have reached the provider and may have produced (billed) output | **do not retry blindly**; report to the user, reconcile later |

Async capabilities answer immediately:

```json
{"request_id": "req_…", "task_id": "task_7a1f", "status": "queued", "poll": "/api/v1/tasks/task_7a1f"}
```

### 5.1 `GET /api/v1/invocations/{request_id}` — replay

Returns a recorded call made by this same token (`replay: true`), including `error`,
`attempts`, `created_at`, `finished_at`. `404` for someone else's call — ownership is
not disclosed.

---

## 6. Async tasks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/tasks/{task_id}` | status, progress, result, artifacts, error |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | truthful cancel result |

```json
{
  "id": "task_7a1f",
  "request_id": "req_9f2c1b6a8d3f4e10",
  "kind": "capability_async",
  "status": "running",
  "progress": 40,
  "progress_note": "Rendering frame 96/240.",
  "upstream_task_id": "job_8831",
  "attempts": 1,
  "capability": {"id": "cap_12", "slug": "video.render", "name": "Render video"},
  "provider": {"id": "prov_3", "name": "Acme Video"},
  "artifacts": [],
  "error": null,
  "poll": "/api/v1/tasks/task_7a1f",
  "cancel_url": "/api/v1/tasks/task_7a1f/cancel"
}
```

Lifecycle:

```
queued ──► running ──► succeeded
   │          │
   │          ├──► failed          (provider said it failed, or a permanent config error)
   │          ├──► uncertain       (past its deadline; the provider may still finish/bill)
   │          └──► cancel_requested ──► cancelled | succeeded | uncertain
   └──► failed (never dispatched)
```

* `cancel` never lies. If the provider accepted the request but is still working:

  ```json
  {"task_id": "task_7a1f", "status": "cancel_requested", "supported": true,
   "canceled": false, "cancel_requested": true, "upstream_may_continue": true,
   "message": "The provider accepted the cancellation request; the job is still running upstream."}
  ```

* `uncertain` is a real, permanent state, not a transient one: nothing will move it
  except a later successful poll. Treat it as "may have cost money".
* Operators can force one poll with `POST /api/admin/tasks/{id}/reconcile` (see
  `docs/OPERATIONS.md` §4).

---

## 7. Files

`GET /api/v1/files/{file_id}` streams an artifact produced by a call this token made.
Scope-checked: uploaded user files are never readable with an agent token, and another
agent's artifacts answer `404`. `Content-Disposition` is set from the stored filename.

### 7.1 `GET /api/v1/skill` — the installable connection bundle

The server-rendered SKILL that `skill-router-connect update` writes to disk. It
contains no credentials, and its content is redacted and asserted secret-free before it
is returned: if a secret shape survived (an `sk-…`, `AIza…`, `sr_live_…`, a JWT, an
AES-GCM envelope or a credential/file id) the route fails with
`500 export_secret_leak` instead of returning the document.

```bash
curl -s -H "Authorization: Bearer $SR_AGENT_TOKEN" http://127.0.0.1:8000/api/v1/skill
```

```json
{
  "schema": "skill-router.connection/v1",
  "format": "skill",
  "content_type": "text/markdown",
  "generated_at": "2026-10-08T09:14:05.123456Z",
  "agent": {"id": "agent_2b7c", "name": "research-bot"},
  "platform": {
    "base_url": "https://skillrouter.example.com",
    "api_base": "/api/v1",
    "mcp_url": "/api/mcp",
    "openapi_url": "/api/v1/openapi.json"
  },
  "version_token": "3c9a…",
  "content": "---\nname: skill-router-research-bot\n…\n## Granted capabilities\n- `text.summarize` v2 — Summarize\n…",
  "sha256": "9020eb238e37…",
  "document": {"schema": "skill-router/external-skill@1", "…": "the JSON variant of the same bundle"},
  "capabilities": [{"slug": "text.summarize", "name": "Summarize", "version": 2, "modes": {}, "input_schema": {}}],
  "capability_index": "- `text.summarize` v2 — Summarize",
  "limits": {"rate_limit_per_min": 60, "max_concurrency": 4, "budget_usd": 25.0},
  "scopes": ["text.summarize"],
  "examples": {
    "install": ["npx skill-router-connect install --url https://… --token <AGENT_TOKEN>"],
    "verify": ["curl -s -H \"Authorization: Bearer <AGENT_TOKEN>\" https://…/api/v1/whoami"],
    "invoke": ["curl -s -X POST https://…/api/v1/invocations …"],
    "mcp": ["curl -s -X POST https://…/api/mcp …"]
  },
  "notes": ["The token is not part of this bundle. Pass it as an Authorization header."],
  "includes_credentials": false
}
```

`base_url` is derived from the **request** (`request.base_url`), not from configuration,
so the document names the origin the caller actually reached — behind a proxy that is
the proxy, which is what an agent elsewhere needs. `sha256` is the digest of `content`;
`update` records it in `.skill-router.json` so you can tell whether the installed file
still matches the server.

---

## 8. Streaming (SSE)

Ask for it either way:

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/invocations \
  -H "Authorization: Bearer $SR_AGENT_TOKEN" \
  -H "accept: text/event-stream" \
  -H "content-type: application/json" \
  -d '{"capability":"text.summarize","input":{"prompt":"hello"}}'
```

```
event: start
data: {"request_id":"req_…","invocation_id":"inv_…","capability":"text.summarize"}

event: delta
data: {"text":"Skill Router "}

event: delta
data: {"text":"routes calls."}

event: usage
data: {"request_id":"req_…","usage":{...},"cost":{...},"latency_ms":840}

event: done
data: {"request_id":"req_…","status":"succeeded"}

```

* Event names: `start`, `delta`, `usage` (exactly once, before `done`), `done`, `error`.
* `error` carries the full envelope: `event: error` + `{"error": {...}}`.
* A comment frame `: keepalive` is sent after 15 s of quiet. Clients should ignore
  comment lines (they start with `:`).
* Response headers include `cache-control: no-cache, no-transform` and
  `x-accel-buffering: no`. **Any proxy in front must have buffering off** or the
  stream will look hung (`docker/nginx.conf` does this for `/api/v1/invocations`,
  `/api/mcp` and `/api/stream/`).
* The agent console has its own stream: `GET /api/stream/agent/{run_id}`, resumable
  with `Last-Event-ID` or `?since=<seq>`.

---

## 9. Error codes

All arrive inside the envelope of §1.3.

### 9.1 Authentication and authorisation

| Code | HTTP | Meaning |
|---|---|---|
| `unauthenticated` | 401 | no token / no session |
| `invalid_token` | 401 | token not recognised |
| `token_revoked` | 401 | token was revoked |
| `token_expired` | 401 | token passed its expiry |
| `csrf_failed` | 403 | mutating browser request without/with a stale `x-csrf-token` |
| `origin_rejected` | 403 | `Origin` not in `SR_TRUSTED_ORIGINS` |
| `password_change_required` | 403 | first-run password not changed yet |
| `forbidden` | 403 | authenticated but not allowed |

### 9.2 Capability and input

| Code | HTTP | Meaning |
|---|---|---|
| `capability_not_granted` | 404/409 | this token has no grant for that slug |
| `capability_not_published` | 409 | the requested version is not published |
| `input_schema_mismatch` | 422 | `input` does not satisfy `input_schema` |
| `unsupported_parameter` | 422 | a field the capability does not accept |
| `validation_failed` | 422 | request envelope itself is malformed (with per-field details) |
| `mode_not_supported` | 422 | `options.mode` is not sync/async/stream |

### 9.3 Routing, limits and upstream

| Code | HTTP | Meaning |
|---|---|---|
| `no_compatible_route` | 409 | no healthy provider/credential for that capability |
| `rate_limited` | 429 | per-token rate limit |
| `concurrency_limited` | 429 | per-token concurrency limit |
| `budget_exceeded` | 429 | token/agent budget exhausted |
| `too_many_attempts` | 429 | sign-in throttling |
| `upstream_timeout_uncertain` | 504 | the upstream may have received it → `uncertain`, not `failed` |
| `credential_leak_blocked` | 500 | a guard refused to return a payload containing a secret |

Anything not in these tables uses `http_error` with the HTTP status as the message, or
`internal_error` for an unhandled failure. The platform never returns a stack trace.

---

## 10. MCP endpoint `/api/mcp`

Streamable HTTP, JSON-RPC 2.0. Bearer token required. Each granted, published
capability becomes one tool named with dots replaced by `__`
(`text.summarize` → `text__summarize`).

| Property | Value |
|---|---|
| Transport | Streamable HTTP (`POST`), JSON-RPC 2.0 |
| Protocol versions | `2025-06-18` (preferred), `2024-11-05` (accepted) |
| Session | `Mcp-Session-Id` echoed from `initialize`; generated if absent; `DELETE` tears down (server-side sessions are stateless — teardown is acknowledged) |
| Response encoding | `application/json`, or `text/event-stream` when the client sends `Accept: text/event-stream` |
| Methods | `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`, `resources/list` (empty), `resources/templates/list` (empty), `prompts/list` (empty) |
| Auth failure | plain HTTP 401 + error envelope (**not** a JSON-RPC error) |
| `GET /api/mcp` | `405` with `Allow: POST, DELETE` (a standalone SSE stream is not offered) |
| Batches | JSON array of messages → JSON array of responses |
| Notifications | answered `204` |

### 10.1 `initialize`

```bash
curl -s -X POST http://127.0.0.1:8000/api/mcp \
  -H "Authorization: Bearer $SR_AGENT_TOKEN" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18",
                 "clientInfo":{"name":"my-client","version":"1.0.0"},
                 "capabilities":{}}}' -D headers.txt
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {"listChanged": false}, "resources": {}, "prompts": {}},
    "serverInfo": {"name": "skill-router", "version": "1.0.0"},
    "instructions": "Skill Router exposes each published capability granted to this token as one tool. Tool names are capability slugs with '.' replaced by '__' …"
  }
}
```

Response headers to keep: `Mcp-Session-Id: sess_…`, `Mcp-Protocol-Version: 2025-06-18`.
An unsupported requested version is negotiated down to `2025-06-18` rather than
rejected. Then send `notifications/initialized` (no `id`) — it answers `204`.

### 10.2 `tools/list`

```bash
curl -s -X POST http://127.0.0.1:8000/api/mcp \
  -H "Authorization: Bearer $SR_AGENT_TOKEN" \
  -H "Mcp-Session-Id: sess_…" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "text__summarize",
        "title": "Summarize",
        "description": "Summarize — text capability (slug `text.summarize`, version 2). Condense a document… Modes: sync, stream.",
        "inputSchema": {
          "type": "object",
          "properties": {"prompt": {"type": "string"}, "max_words": {"type": "integer"}},
          "required": ["prompt"],
          "additionalProperties": false
        },
        "annotations": {"readOnlyHint": false, "openWorldHint": true, "capability": "text.summarize"}
      }
    ],
    "nextCursor": null
  }
}
```

Paging: 50 tools per page; pass `params.cursor` (an opaque integer string) to get the
next page; `nextCursor: null` ends the list.

### 10.3 `tools/call`

```bash
curl -s -X POST http://127.0.0.1:8000/api/mcp \
  -H "Authorization: Bearer $SR_AGENT_TOKEN" \
  -H "Mcp-Session-Id: sess_…" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"text__summarize","arguments":{"prompt":"hello","max_words":20}}}'
```

Synchronous result:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"summary\": \"…\"}"}],
    "structuredContent": {
      "request_id": "req_…", "invocation_id": "inv_…", "status": "succeeded",
      "result": {"summary": "…"}, "artifacts": [], "usage": {}, "cost": {}, "error": null
    },
    "isError": false
  }
}
```

Async capability result — the task id is what you act on:

```json
{"content":[{"type":"text","text":"{\"task_id\":\"task_7a1f\",\"status\":\"queued\",\"mode\":\"async\",\"poll\":\"/api/v1/tasks/task_7a1f\",\"note\":\"This capability runs asynchronously; poll the task id for the result.\"}"}],
 "structuredContent":{"task_id":"task_7a1f","status":"queued","mode":"async","poll":"/api/v1/tasks/task_7a1f"},
 "isError":false}
```

Tool-level failure — still JSON-RPC *success*, with `isError: true`:

```json
{"content":[{"type":"text","text":"{\"error\": {\"code\": \"no_compatible_route\", \"message\": \"…\", \"request_id\": \"req_…\", \"actions\": [\"…\"]}}"}],
 "structuredContent":{"error":{"code":"no_compatible_route"},"request_id":"req_…","isError":true},
 "isError":true}
```

`tools/call` for an unknown tool or an ungranted capability is a JSON-RPC **error**
`-32602` with `data.code = tool_not_found` / `capability_not_granted`. `isError` is
reserved for calls that were dispatched and failed.

### 10.4 JSON-RPC error codes

| Code | When |
|---|---|
| `-32700` | body is not valid JSON |
| `-32600` | not a JSON-RPC 2.0 message, empty batch |
| `-32601` | method not supported |
| `-32602` | invalid params, bad cursor, unknown tool, arguments not an object |
| `-32603` | internal error |

Every `tools/call` goes through the same `execution.invoke` as `/api/v1`, so records,
scopes, rate limits, budgets and idempotency behave identically. There is no "MCP
ratchet": a call made over MCP is not more permissive than the same call over HTTP.

---

## 11. Where the OpenAPI documents live

| Document | Path | Contents |
|---|---|---|
| External API | `GET /api/v1/openapi.json` | OpenAPI 3.1 for `/api/v1` only, with `bearerAuth` security scheme and request/response examples. No auth required to fetch; it describes no secrets |
| Whole application | `GET /api/openapi.json` | every router (auth, admin, external, mcp, health, stream) |
| Interactive docs | `GET /api/docs` | Swagger UI for the whole application (development convenience; put it behind your proxy or disable it in production if your policy requires) |

The external document is generated from the live router objects, so it cannot drift
from the code. It carries examples for `whoami`, `capabilities`, `invocations`,
`tasks`, `cancel` and `skill`, and a `429` example for every operation.

---

## 12. Quick client recipes

```bash
# verify a token
curl -s -H "Authorization: Bearer $T" $BASE/api/v1/whoami | jq '.agent.name'

# list what it can do
curl -s -H "Authorization: Bearer $T" $BASE/api/v1/capabilities | jq -r '.capabilities[].slug'

# make a call
curl -s -X POST $BASE/api/v1/invocations -H "Authorization: Bearer $T" \
  -H "content-type: application/json" \
  -d '{"capability":"text.summarize","input":{"prompt":"hi"}}' | jq '.status, .result'

# poll an async task
curl -s -H "Authorization: Bearer $T" $BASE/api/v1/tasks/$TASK | jq '.status, .progress'

# cancel it honestly
curl -s -X POST -H "Authorization: Bearer $T" $BASE/api/v1/tasks/$TASK/cancel | jq '.canceled, .upstream_may_continue'

# MCP: what tools does this token get?
curl -s -X POST $BASE/api/mcp -H "Authorization: Bearer $T" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

All of these were exercised against a real instance; see `docs/VERIFICATION.md` for the
recorded results.
