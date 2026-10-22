# Skill Router — frozen interface contract

This document is the single source of truth shared by every module. Backend
routers, the Next.js app, the npm installer and the MCP adapter all code against
this. If an implementation needs to deviate, change this file in the same commit.

Version: contract v1 (2026-10).
Audience: implementers on this repo. Not user documentation.

---

## 1. Repository layout and file ownership

```
backend/
  app/core/            config.py db.py errors.py request_context.py        (owner: platform core)
  app/security/        crypto.py passwords.py sessions.py tokens.py
                       throttle.py redact.py                              (owner: platform core)
  app/deps.py          auth dependencies                                   (owner: platform core)
  app/services/
    bootstrap.py audit.py credentials.py execution.py routing.py usage.py
    files.py capability_config.py network.py                              (owner: platform core)
    capabilities.py                                                       (owner: backend-capabilities)
    tasks.py worker.py                                                     (owner: backend-runtime)
    skills.py skill_import.py export.py                                    (owner: backend-access)
    agent_adapter.py agent_runtime.py engine_upgrade.py                    (owner: agent-engine)
  app/api/
    auth.py health.py                                                      (owner: platform core)
    admin_providers.py admin_credentials.py admin_capabilities.py
    admin_routing.py                                                       (owner: backend-capabilities)
    admin_tasks.py admin_files.py                                          (owner: backend-runtime)
    admin_skills.py admin_tokens.py admin_usage.py admin_overview.py        (owner: backend-access)
    external.py mcp.py                                                      (owner: external-surface)
    admin_agent.py admin_settings.py stream.py                             (owner: agent-engine)
  migrations/*.sql                                                          (owner: platform core)
  tests/*.py
frontend/              Next.js app (see §9)
sidecar/               Node service embedding @aragon-agent/core (see §10)
packages/installer/    npm package "skill-router-connect" (see §11)
docs/                  product/ops/API documentation
scripts/               verification and maintenance scripts
```

Never edit a file another owner holds. Add new files instead of widening scope.

---

## 2. Backend conventions

### 2.1 Request flow

1. `app/main.py` assigns a request id (`req_…`), enforces origin + CSRF on
   mutating `/api` calls that carry a session cookie, and stamps security
   headers.
2. Router dependency `require_admin` (see `app/deps.py`) authenticates the
   session cookie and blocks business endpoints while
   `users.must_change_password = 1` (error code `password_change_required`).
3. Errors funnel through `ApiError` → uniform envelope:

```json
{"error": {"code": "upstream_auth_failed", "message": "…", "details": {},
           "request_id": "req_…", "actions": ["…"]}}
```

4. List endpoints accept `limit` (default 50, max 200), `offset`, `search`,
   plus the documented filters, and answer
   `{"items": [...], "total": 123, "limit": 50, "offset": 0}`.
5. Every state change writes an audit row via `app.services.audit.record(...)`
   with `actor_type`/`actor_id` taken from the current user.
6. Any string derived from upstream responses or user input that is persisted or
   returned must pass through `app.security.redact`. Use
   `Redactor(known_secret_values).text(...)` where the secrets are known.

### 2.2 Database access

Use only these helpers (they own transactions and pragmas):

```python
from app.core import db
db.query_one(sql, params)      # dict | None
db.query_all(sql, params)      # list[dict]
db.query_scalar(sql, params)   # scalar | None
db.execute(sql, params)        # rowcount
db.insert(table, {...})        # INSERT
db.update(table, id, {...})
db.dumps(obj) / db.loads(text, default)
db.iso() / db.parse_iso(s) / db.new_id("cap")
db.transaction()               # BEGIN IMMEDIATE … COMMIT/ROLLBACK
```
Timestamps are ISO-8601 UTC strings. JSON columns are TEXT. Never store a
secret outside `credentials.secret_ciphertext`.

### 2.3 Auth dependencies

```python
from app.deps import AuthUser, require_admin, require_csrf, require_agent, AgentPrincipal
```

* Browser routes: `user: AuthUser = Depends(require_admin)`.
* External routes (`/api/v1`, `/api/mcp`): `principal = Depends(require_agent)`,
  then `principal.can_use(slug)` before doing anything.
* Mutating browser routes are already covered by the CSRF middleware; do not
  weaken it.

---

## 3. Existing services you must call (do not reimplement)

### 3.1 Credentials — `app.services.credentials`

```python
list_credentials(provider_id=None, status=None, search=None, limit=100, offset=0) -> (rows, total)  # masked
get_row(credential_id) -> dict                      # full row incl. ciphertext; internal only
mask_payload(row) -> dict                            # the API shape
create_credential(*, provider_id, name, auth_type, secrets: dict, config=None, expires_at=None,
                  actor_id=None, ip=None, status="active") -> dict
update_credential(credential_id, *, name=None, config=None, expires_at=None, status=None,
                  secrets=None, actor_id=None, ip=None) -> dict
delete_credential(credential_id, *, actor_id=None, ip=None, force=False) -> dict
impact_report(credential_id) -> {"capabilities": [...], "routes": n, "active_tasks": n, "blocking": bool}
get_secret_map(credential_id) -> CredentialSecrets(values: dict)   # raises if disabled
build_auth_headers(credential_row, secrets) -> dict                  # send-time only
async verify_credential(credential_id, *, probe_url=None, method="GET", actor_id=None, ip=None) -> dict
rotate_master_key(*, actor_id=None, ip=None) -> dict
```
`config` accepts `header_name`, `prefix`, `value_template`, `test_url`, `username` and **`model`** (the default model id for that credential; `resolve_model()` falls back to it).

`auth_type ∈ {api_key, bearer, basic, custom_header}`. Secret field names:
`api_key | token | username+password | value`.

### 3.2 Capability config — `app.services.capability_config`

```python
default_config(capability_type) -> dict
merge_defaults(config, capability_type) -> dict
validate_config(config, *, capability_type) -> [{"severity":"error|warning","field","message"}]
has_blocking_issues(issues) -> bool
config_checksum(config) -> str
check_unknown_inputs(payload, config) -> [names]
jsonpath_get(data, "$.a[0].b", default=None)
render(template, ctx)                 # ctx = {"input", "model", "mode"}
mask_config_for_export(config) -> dict
```

`capability_versions.config` shape — every key optional except `request`:

```jsonc
{
  "model": "gemini-2.5-flash",
  "request": {
    "method": "POST",
    "url": "https://host/v1/models/{model}:generateContent",   // {{ }} templates allowed
    "headers": {"content-type": "application/json"},            // never secrets
    "query": [{"name": "alt", "value": "sse", "when_mode": "stream"}],
    "encoding": "json|form|multipart|none",
    "form_fields": {"field": "{{ input.x }}"},
    "multipart_file_field": "file",
    "body_template": {"contents": [{"parts": [{"text": "{{ input.prompt }}"}]}]}
  },
  "auth": {"mode": "credential|none"},
  "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
  "output_schema": {"type": "object", "properties": {}},
  "response": {
    "kind": "json|text",
    "extract": {"result": "$.path", "usage_in": "$.usage.in", "usage_out": "$.usage.out",
                 "usage_total": "$.usage.total", "cost": "$.cost"},
    "artifact": {"kind": "image|audio|video|file", "path": "$.…", "encoding": "base64|url",
                  "mime": "audio/mpeg", "mime_path": "$.…", "filename": "out.mp3"}
  },
  "async": {
    "enabled": true,
    "submit_extract": {"task_id": "$.id"},
    "status": {"url": "https://host/jobs/{task_id}", "method": "GET",
                "extract": {"status": "$.status", "progress": "$.progress"},
                "map": {"queued": [], "running": [], "succeeded": [], "failed": [], "cancelled": []},
                "poll_interval_s": 5, "result_extract": {"result": "$.output"}},
    "cancel": {"url": "https://host/jobs/{task_id}/cancel", "method": "POST", "extract": {"status": "$.status"}}
  },
  "stream": {"enabled": true, "protocol": "sse|ndjson", "delta_path": "$.…", "done_marker": "[DONE]"},
  "errors": {"rules": [{"match": {"http_status": [401], "body_path": "$.error.code",
                                   "equals": "x", "contains": "y"},
                          "code": "…", "message": "…", "action": "…", "retryable": false}],
              "default_code": "upstream_error"},
  "limits": {"timeout_s": 60, "max_output_bytes": 8388608, "max_input_bytes": 262144,
              "file_input": false, "max_file_mb": 10, "allowed_mime": [], "max_duration_s": null},
  "modes": {"sync": true, "async": false, "stream": false},
  "provider_params": ["temperature"],
  "retry": {"max_attempts": 2, "backoff_ms": [500, 1500],
             "retry_on": ["connect_timeout", "read_timeout", "upstream_5xx", "rate_limited"]},
  "pricing": {"per_call": 0, "input_per_1k": 0, "output_per_1k": 0, "per_second": 0, "currency": "USD"},
  "dedupe_window_s": 0,
  "examples": [{"input": {...}, "note": "…"}]
}
```

### 3.3 Execution — `app.services.execution`

```python
InvokeOptions(mode=None, idempotency_key=None, timeout_s=None, provider_id=None,
              surface="http", allow_draft=False, version_id=None, dry_run=False,
              max_retries=None, save_bodies=None, actor_type="agent", actor_id=None)

await invoke(principal, capability_ref, payload, options) -> InvokeResult
InvokeResult.to_dict()  # request_id, invocation_id, status, mode, capability, version,
                        # task_id, result, artifacts[], usage{}, cost{}, provider{},
                        # route_reason, error{}, http_status, latency_ms, attempts,
                        # deduplicated, schema_valid
await test_version(*, capability_id, version_id, payload, actor_id=None, mode="sync",
                   save_bodies=True) -> {"request": {...}, "status":…, "response":…, …}
stream_invocation(principal, capability_ref, payload, options) -> AsyncIterator[dict]
await fetch_status(*, config, upstream_task_id, input_payload, credential_id) -> {...}
await request_cancel(*, config, upstream_task_id, input_payload, credential_id) -> {...}
get_capability(ref) -> row ; get_version(capability, *, version_id=None, published_only=True) -> row
```
`InvokeResult.status ∈ {succeeded, uncertain, failed, preview}`. Async submission
is handled by the caller (`services/tasks.py`): it calls `invoke(...)` with
`mode="async"`, then persists the returned upstream task id.

### 3.4 Routing — `app.services.routing`

```python
candidates_for(*, capability, version, input_payload, mode, preferred_provider_id=None,
               allow_unhealthy=False) -> RouteSelection(candidates, reason, rejected)
explain_selection(selection) -> {"reason", "chosen_route", "candidates": [...], "rejected": [...]}
mark_success(route_id) / mark_failure(route_id, degraded=True) / update_health(route_id, status)
```

### 3.5 Usage — `app.services.usage`

```python
assert_within_limits(principal, capability_id, provider_id)     # raises ApiError 429
check_agent_limits(principal) -> LimitDecision
check_capability_limits(capability_id, provider_id) -> LimitDecision
usage_overview(days=7) -> {...totals + by_capability/by_provider/by_agent}
current_counters(agent_id=None) -> dict
```

### 3.6 Files — `app.services.files`

```python
save_bytes(content, *, filename, content_type, purpose="upload", owner_type="user",
           owner_id=None, task_id=None, invocation_id=None, retention_days=None) -> {"id", …, "url"}
get_file(id) / read_bytes(id) / delete_file(id)
list_files(*, purpose=None, task_id=None, limit=50, offset=0) -> (rows, total)
cleanup_expired() -> {"removed": n, "bytes_freed": n}
content_disposition(filename, inline=False)
```

### 3.7 Network — `app.services.network`

```python
validate_url(url)                       # SSRF guard; raises NetworkPolicyError
await guarded_request(method, url, *, headers=None, params=None, json_body=None,
                      data=None, files=None, content=None, timeout_s=None,
                      max_bytes=None, allow_private=None) -> GuardedResponse
GuardedResponse(status_code, headers, content, url, elapsed_ms, redirects).json()/.text()/.content_type
classify_network_error(exc) -> (code, message)
```

### 3.8 Bootstrap — `app.services.bootstrap`

`get_status()`, `onboarding_summary()`, `password_change_required(user_id)`.
Admin creation is idempotent and must never be duplicated by a new module.

---

## 4. Status vocabulary (use these exact strings)

| Entity | Statuses |
|---|---|
| capability | `draft` `testing` `published` `disabled` |
| capability_version | `draft` `testing` `published` `deprecated` |
| credential | `active` `disabled` `expired` |
| verification (`last_verify_status`) | `valid` `invalid` `unreachable` `rate_limited` `blocked` `unknown` |
| route health | `unknown` `healthy` `degraded` `unhealthy` |
| invocation | `queued` `running` `succeeded` `failed` `cancelled` `uncertain` `rejected` |
| task | `queued` `running` `succeeded` `failed` `cancel_requested` `cancelled` `uncertain` |
| skill | `draft` `published` `disabled` |
| skill_import | `pending` `verified` `rejected` `failed` `imported` |
| engine_release | `detected` `validating` `verified` `failed` `pending_approval` `active` `rolled_back` `superseded` |
| agent run | `queued` `running` `succeeded` `failed` `stopped` |

Four distinct states must never be conflated in the UI or API: *saved* (config
persisted), *test passed* (a real call succeeded), *published* (immutable version
is live), *callable* (external call allowed with a valid credential + route).

---

## 5. Admin API (`/api/admin/...`, cookie session + CSRF header)

All responses are JSON. All list responses use the envelope in §2.1.

### 5.1 Overview — owner backend-access
| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/overview` | dashboard payload (see below) |
| GET | `/api/admin/overview/problems` | actionable issues list |

```jsonc
{"counts": {"capabilities": {"total":4,"published":3,"draft":1},
            "providers": {"total":2,"active":2},
            "credentials": {"total":3,"active":2,"expiring_soon":1,"invalid":1},
            "skills": {"total":2,"published":1},
            "agents": {"total":1,"active":1},
            "tasks": {"queued":0,"running":1,"failed":1,"uncertain":0}},
 "health": {"success_rate_24h": 0.97, "calls_24h": 120, "avg_latency_ms_24h": 840,
            "failed_tasks_24h": 2, "engine": {"status": "active", "version": "0.2.46",
            "adapter": "1.0.0", "channel": "stable"}},
 "problems": [{"id":"credential-invalid:cred_1","severity":"error","title":"…",
               "detail":"…","href":"/providers?credential=cred_1","action":"Verify credential"}],
 "recent_invocations": [ … maximal 8, same shape as §5.7 … ],
 "onboarding": {"steps": {...}, "agent_configured": true}}
```

### 5.2 Providers — owner backend-capabilities
| Method | Path | Body / query |
|---|---|---|
| GET | `/api/admin/providers` | `?search=&category=&environment=&limit=&offset=` |
| POST | `/api/admin/providers` | `{name, category, base_url?, docs_url?, tags[], environment, notes?, status}` |
| GET | `/api/admin/providers/{id}` | detail + `credentials[]`, `capabilities[]`, `routes[]` counts |
| PATCH | `/api/admin/providers/{id}` | partial of POST body |
| DELETE | `/api/admin/providers/{id}` | `409 provider_in_use` when referenced |
| POST | `/api/admin/providers/{id}/models` | Ask the provider which model ids the account can use; `{provider_id, source_url, items[], count, attempts[]}`. `409 models_unavailable` lists every attempted URL |

Provider row: `{id, name, slug, category, base_url, docs_url, tags[], environment, notes, status, created_at, updated_at, counts:{credentials,capabilities}}`.

### 5.3 Credentials — owner backend-capabilities
| Method | Path | Body / query |
|---|---|---|
| GET | `/api/admin/credentials` | `?provider_id=&status=&search=` |
| POST | `/api/admin/credentials` | `{provider_id, name, auth_type, secret: {…}, config: {header_name?, prefix?, value_template?, test_url?, username?}, expires_at?}` |
| GET | `/api/admin/credentials/{id}` | masked row + `impact` |
| PATCH | `/api/admin/credentials/{id}` | `{name?, config?, expires_at?, status?, secret?}` |
| DELETE | `/api/admin/credentials/{id}` | `?force=true` allowed |
| POST | `/api/admin/credentials/{id}/verify` | `{probe_url?, method?}` → verification result |
| POST | `/api/admin/credentials/{id}/test-call` | delegates to a capability test when `capability_id` given |
| GET | `/api/admin/credentials/{id}/impact` | `impact_report` |
| POST | `/api/admin/credentials/rotate-key` | re-wrap all credentials (also audited) |

`secret` is write-only. Responses only ever contain `hint`.

### 5.4 Capabilities, versions, tests — owner backend-capabilities
| Method | Path | Body / query |
|---|---|---|
| GET | `/api/admin/capabilities` | `?type=&status=&provider_id=&search=` |
| POST | `/api/admin/capabilities` | `{name, slug?, type, description, provider_id?, credential_id?}` → creates v1 draft |
| GET | `/api/admin/capabilities/{id}` | capability + versions[] + `verification` summary + `route` info |
| PATCH | `/api/admin/capabilities/{id}` | `{name?, description?, type?, provider_id?, credential_id?, needs_live_agent?}` |
| DELETE | `/api/admin/capabilities/{id}` | `409 capability_in_use` if routes/skills reference a published version |
| GET | `/api/admin/capabilities/{id}/versions` | list |
| POST | `/api/admin/capabilities/{id}/versions` | `{from_version_id?, config?}` → new draft version N+1 |
| GET | `/api/admin/capabilities/{id}/versions/{version_id}` | full config |
| PATCH | `/api/admin/capabilities/{id}/versions/{version_id}` | `{config?, notes?}` — only while `draft`/`testing` |
| POST | `/api/admin/capabilities/{id}/versions/{version_id}/validate` | schema lint → `{issues[], blocking}` |
| POST | `/api/admin/capabilities/{id}/versions/{version_id}/test` | `{input, mode}` → `execution.test_version(...)` result, stored in `capability_tests` |
| POST | `/api/admin/capabilities/{id}/versions/{version_id}/publish` | requires a passing test row for that version; makes `capabilities.current_version_id` this version, status `published` |
| POST | `/api/admin/capabilities/{id}/versions/{version_id}/deprecate` | |
| GET | `/api/admin/capabilities/{id}/versions/diff?from=&to=` | structured config diff |
| POST | `/api/admin/capabilities/{id}/rollback` | `{version_id}` → publish that version again as a new version |
| GET | `/api/admin/capabilities/{id}/tests` | test history |
| POST | `/api/admin/capabilities/{id}/invoke` | admin manual call (draft allowed) → `InvokeResult` |

Publish must refuse with `409 test_evidence_missing` when the newest test for
that version is not `passed`, and `422 validation_failed` when
`has_blocking_issues`.

Async test semantics (implemented): testing a draft `async` version submits the
job, verifies the task id, **persists a real `tasks` row** and lets the worker
poll it — so a test does not strand a paid upstream job and the publish decision
is backed by a tracked lifecycle. `POST /{id}/invoke` with `allow_draft` uses the
capability's own provider/credential when no explicit route exists yet.

### 5.5 Routing — owner backend-capabilities
| Method | Path | Body |
|---|---|---|
| GET | `/api/admin/capabilities/{id}/routes` | list with provider/credential names |
| POST | `/api/admin/capabilities/{id}/routes` | `{capability_version_id, provider_id, credential_id?, strategy, priority, weight, enabled}` |
| PATCH | `/api/admin/routes/{route_id}` | partial |
| DELETE | `/api/admin/routes/{route_id}` | |
| POST | `/api/admin/routes/{route_id}/health-check` | real probe → `{status, latency_ms, message}` |
| POST | `/api/admin/capabilities/{id}/route-preview` | `{input, mode, provider_id?}` → `explain_selection` |

### 5.6 Tasks & invocations — owner backend-runtime
| Method | Path | Query / body |
|---|---|---|
| GET | `/api/admin/invocations` | `?status=&capability=&agent=&surface=&search=&from=&to=` |
| GET | `/api/admin/invocations/{id}` | full record incl. redacted request/response previews |
| GET | `/api/admin/tasks` | `?status=&capability=&agent=` |
| GET | `/api/admin/tasks/{id}` | task + `events[]` timeline + artifacts |
| POST | `/api/admin/tasks/{id}/cancel` | `{reason?}` → truthful result (`cancel_requested` vs `unsupported`) |
| POST | `/api/admin/tasks/{id}/reconcile` | re-query upstream and update status |
| GET | `/api/admin/files` | `?purpose=&task_id=` |
| GET | `/api/admin/files/{id}` | metadata |
| GET | `/api/files/{id}` | binary download (session **or** agent token; scope-checked) |
| DELETE | `/api/admin/files/{id}` | |

Invocation row (public shape):
```jsonc
{"id":"inv_…","request_id":"req_…","caller_type":"agent","agent":{"id","name"},"surface":"mcp",
 "capability":{"slug","name"},"version":2,"provider":{"id","name"},"mode":"async","status":"succeeded",
 "http_status":200,"latency_ms":840,"retries":0,"task_id":null,
 "usage":{"input_tokens":12,"output_tokens":30,"source":"provider"},
 "cost":{"estimated_usd":0.0001,"actual_usd":null,"currency":"USD"},
 "error":{"code","message","action"},"created_at":"…","finished_at":"…"}
```

Task row: `{id, request_id, capability{slug,name}, provider{…}, status, progress, progress_note,
upstream_task_id, attempts, created_at, updated_at, finished_at, result_summary, artifacts[], error{}}`.

### 5.7 SKILLs — owner backend-access
| Method | Path | Body / query |
|---|---|---|
| GET | `/api/admin/skills` | `?status=&category=&search=` |
| POST | `/api/admin/skills` | `{name, slug?, description, category}` → v1 draft |
| GET | `/api/admin/skills/{id}` | skill + versions + dependency impact |
| PATCH | `/api/admin/skills/{id}` | `{name?, description?, category?, status?}` |
| DELETE | `/api/admin/skills/{id}` | |
| POST | `/api/admin/skills/{id}/versions` | `{manifest, version?}` |
| PATCH | `/api/admin/skills/{id}/versions/{version_id}` | `{manifest}` (drafts only) |
| POST | `/api/admin/skills/{id}/versions/{version_id}/publish` | validates dependencies resolve to published capability versions |
| POST | `/api/admin/skills/{id}/test` | `{version_id, input}` → executes the plan through `execution.invoke` step by step |
| POST | `/api/admin/skills/import/preview` | `{source_type: git|web|file, uri|file_id}` → staged review |
| POST | `/api/admin/skills/import` | `{preview_id, confirm_permissions: bool}` |
| GET | `/api/admin/skills/imports` | history |
| GET | `/api/admin/skills/{id}/export` | `?format=skill|json` → bundle (no secrets) |

SKILL manifest:
```jsonc
{"name","description","when_to_use","inputs":{json schema},"outputs":{json schema},
 "dependencies":[{"capability":"slug","version":2,"required":true}],
 "permissions":{"network":["api.example.com"],"files":["read","write"],"credentials":["cred_…"]},
 "instructions":"markdown for the external agent",
 "plan":[{"id":"step1","kind":"capability","capability":"slug","input":{"prompt":"{{ input.topic }}"},
           "save_as":"draft","on_error":"stop|continue","note":"…"},
          {"id":"step2","kind":"agent","instruction":"…","save_as":"final"}],
 "examples":[{"input":{…},"note":"…"}],
 "sources":[{"type":"git","uri":"…","commit":"…"}]}
```
`kind:"agent"` steps are the only ones that need live agent reasoning and must be
flagged as such in the UI and the external bundle.

### 5.8 Agents, tokens, budgets — owner backend-access
| Method | Path | Body |
|---|---|---|
| GET | `/api/admin/agents` | list with token counts |
| POST | `/api/admin/agents` | `{name, description}` |
| PATCH | `/api/admin/agents/{id}` | `{name?, description?, status?}` |
| DELETE | `/api/admin/agents/{id}` | revokes tokens |
| GET | `/api/admin/agents/{id}/tokens` | token list (never the secret) |
| POST | `/api/admin/agents/{id}/tokens` | `{name, scopes[], expires_in_days?, rate_limit_per_min?, max_concurrency?, budget_usd?}` → **only** response containing `token` |
| POST | `/api/admin/tokens/{token_id}/revoke` | immediate |
| GET | `/api/admin/tokens` | all tokens with filters |
| GET | `/api/admin/budgets` / POST / PATCH / DELETE | budget policies per §(schema) |

### 5.9 Usage & audit — owner backend-access
| Method | Path | Query |
|---|---|---|
| GET | `/api/admin/usage` | `?days=7` → `usage_overview` |
| GET | `/api/admin/usage/limits` | current counters + policies |
| GET | `/api/admin/audit` | `?object_type=&object_id=&action=&search=` |
| GET | `/api/admin/logs` | operational log tail (ring buffer), distinct from audit |

### 5.10 Settings & engine — owner agent-engine
| Method | Path | Body |
|---|---|---|
| GET | `/api/admin/settings` | effective settings (no secrets) + engine state |
| PATCH | `/api/admin/settings` | `{logging:{store_request_bodies,store_response_bodies}, files:{retention_days}, security:{…}, agent:{upgrade_policy, upgrade_channel, track_branch}}` |
| GET | `/api/admin/engine` | current release, candidate, history, capability matrix |
| POST | `/api/admin/engine/check` | detect upstream ref (network) → candidate release row |
| POST | `/api/admin/engine/releases/{id}/validate` | isolated build + compatibility + regression |
| POST | `/api/admin/engine/releases/{id}/activate` | switch + health check + rollback on failure |
| POST | `/api/admin/engine/rollback` | back to `previous_release_id` |
| GET | `/api/admin/engine/releases` | history |
| POST | `/api/admin/agent/sessions` | `{title?}` |
| GET | `/api/admin/agent/sessions` | list |
| GET | `/api/admin/agent/sessions/{id}` | messages + runs |
| POST | `/api/admin/agent/sessions/{id}/messages` | `{text, attachments[]}` → creates run |
| POST | `/api/admin/agent/runs/{run_id}/stop` | truthful stop report |
| GET | `/api/admin/agent/runs/{run_id}/events` | recorded event replay |
| GET | `/api/stream/agent/{run_id}` | SSE live events (`event:` names below) |
| GET | `/api/admin/agent/health` | sidecar status, model configured, capability matrix |

SSE event names (agent): `run.started`, `message.accepted`, `message.queued`,
`assistant.delta`, `tool.call`, `tool.result`, `todo.update`, `plan.proposed`,
`change.applied`, `test.result`, `run.progress`, `run.stopped`, `run.failed`,
`run.finished`, `keepalive`. Every event carries `{seq, run_id, at, type}` plus
type-specific fields. A `Last-Event-ID` header resumes from `seq`.

---

## 6. External API `/api/v1` (bearer agent token) — owner external-surface

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/whoami` | `{agent:{id,name}, scopes[], limits{}, capabilities_count}` — connectivity check |
| GET | `/api/v1/capabilities` | granted, published only; `{capabilities[], version_token, generated_at}` |
| GET | `/api/v1/capabilities/{slug}` | full descriptor incl. schemas, examples, limits, modes |
| POST | `/api/v1/invocations` | `{capability, version?, input{}, options{mode?, idempotency_key?, timeout_s?, stream?}}` |
| GET | `/api/v1/invocations/{request_id}` | replay a recorded call (own calls only) |
| GET | `/api/v1/tasks/{task_id}` | async status |
| POST | `/api/v1/tasks/{task_id}/cancel` | truthful cancel result |
| GET | `/api/v1/files/{file_id}` | artefact download, scope-checked |
| GET | `/api/v1/skill` | installable SKILL bundle (instructions + capability list, no secrets) |
| GET | `/api/v1/openapi.json` | external-only OpenAPI document |

Invocation response:
```jsonc
{"request_id":"req_…","invocation_id":"inv_…","status":"succeeded",
 "capability":"text.summarize","version":2,
 "result":{…},"artifacts":[{"id":"file_…","url":"/api/v1/files/file_…","content_type":"audio/mpeg","size":1234}],
 "usage":{…},"cost":{…},"provider":{"id","name"},"route_reason":"…",
 "error":null,"http_status":200,"latency_ms":840,"deduplicated":false}
```
Async: `{"request_id","task_id","status":"queued","poll":"/api/v1/tasks/{id}"}`.
Streaming (`options.stream: true` or `Accept: text/event-stream`): SSE with
`event: start|delta|usage|done|error`.

Error codes the client must handle: `unauthenticated`, `invalid_token`,
`token_revoked`, `token_expired`, `capability_not_granted`, `capability_not_published`,
`unsupported_parameter`, `input_schema_mismatch`, `no_compatible_route`,
`rate_limited`, `concurrency_limited`, `budget_exceeded`, `upstream_timeout_uncertain`.

## 7. MCP adapter `/api/mcp` — owner external-surface

Streamable HTTP, JSON-RPC 2.0, protocol versions `2025-06-18` (preferred) and
`2024-11-05` (accepted). Bearer token required. Methods:
`initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`,
`resources/list` (empty), `prompts/list` (empty).
Each granted published capability becomes one tool:
`name = capability slug with dots replaced by "__"`, `description`, `inputSchema`
= capability input schema. `tools/call` returns
`{"content":[{"type":"text","text":"<json result>"}], "structuredContent": {...}, "isError": bool}`.
Async capabilities return the task id in `structuredContent` with a note. All
calls go through `execution.invoke` so records, limits and scopes match HTTP.

MCP responses: `Content-Type: application/json` for plain requests,
`text/event-stream` when the client sends `Accept: text/event-stream`; support
`Mcp-Session-Id` echoing `initialize` (generate if absent) and `DELETE` for
session teardown.

## 8. Capability discovery & caching

`GET /api/v1/capabilities` returns `version_token` = sha256 of the granted
capability descriptors. Clients refresh when the token changes; the SKILL bundle
documents the refresh call. Capability updates therefore never require
reinstalling the package, and a breaking change ships as a new capability
version with a note in the descriptor.

---

## 9. Frontend contract (`frontend/`)

* Next.js 15 App Router + TypeScript strict + Tailwind 3.4 + lucide-react +
  `@tanstack/react-query` + `react-hook-form` + `zod`.
* All requests go to relative `/api/...`; Next rewrites `/api/:path*` to the
  backend. Cookies and CSRF therefore behave as same-origin.
* `lib/api.ts` exposes `api.get/post/patch/del`, attaches `x-csrf-token` from the
  `sr_csrf` cookie for mutations, normalises errors into
  `{code, message, details, requestId, actions}` and surfaces `requestId` in the
  error toast/banner.
* Routes: `/login`, `/` (overview), `/providers`, `/providers/[id]`,
  `/capabilities`, `/capabilities/[id]`, `/skills`, `/skills/[id]`, `/access`
  (agents + tokens), `/records` (invocations & tasks), `/records/[id]`,
  `/settings`, `/settings/engine`, `/account`.
* Shell: fixed left navigation (Overview, Providers, Capabilities, SKILLs,
  Access, Records, Settings), top bar with page title + primary action + account
  menu, content max-width 1180px, `--sr-*` design tokens in `globals.css`.
* Visual language: 6px control radius / 8px card radius, 1px neutral borders,
  no gradients, shadows only on overlays, one accent colour, 13/14/16/20/24
  type scale, 8px spacing grid.
* Every list: search, filter, sort, pagination, empty state, loading skeleton,
  error state with retry and request id.
* Every workflow shows the four states separately: saved / test passed /
  published / callable.
* Floating agent panel: fixed bottom-right button (never overlapping the primary
  action), 420px right drawer, drag-and-drop upload with preview, Enter to send,
  Shift+Enter newline, streaming output, progress steps, stop with an explicit
  "what stopped / what may still run upstream" report, retry, and a session
  list. Panel state (open, draft, scroll, active run) survives route changes.

---

## 10. AragonAgent sidecar (`sidecar/`) — owner agent-engine

Node ≥ 18 ESM service embedding `@aragon-agent/core` (verified upstream
`Agent`/`AragonAgent`: `systemPrompt`, `model{providerId,modelId,baseUrl}`,
`tools`, `providerRegistry`, `getApiKey`; `prompt()`, `abort()`, `waitForIdle()`,
`subscribe(listener)`, `state`, `steer()`, `followUp()`, `pauseIdleWatchdog()`).
`defineTool`, `textResult`, `errorResult` build tools; `initProviders` +
`getProviderRegistry` supply providers.

Endpoints (bearer `SR_ARAGON_SIDECAR_TOKEN`, bind 127.0.0.1):
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{ok, engine:{package,version,commit,adapter_version}, model_configured}` |
| GET | `/info` | tools, providers, features, limits |
| POST | `/sessions` | `{session_id, model, api_key_ref, system_prompt, tools[], platform{base_url,token}}` → create/resume |
| POST | `/sessions/{id}/messages` | `{text}` → 202 + SSE stream on `/sessions/{id}/events` |
| GET | `/sessions/{id}/events` | SSE: normalised `AgentEvent` union (§5.10 names) |
| POST | `/sessions/{id}/stop` | abort → `{stopped_steps[], may_continue_upstream[], reason}` |
| GET | `/sessions/{id}` | transcript snapshot |
| DELETE | `/sessions/{id}` | dispose |
| GET | `/releases/check` | upstream tag/commit detection for the upgrade flow |

The adapter (`backend/app/services/agent_adapter.py`) is the ONLY place the
backend talks to this service. Platform tools are declared in
`agent_runtime.py` and executed through HTTP callbacks to a scoped, short-lived
tool token, never by handing the engine platform credentials.

## 11. npm installer `skill-router-connect` (`packages/installer`) — owner delivery

CLI: `npx skill-router-connect install|verify|update|uninstall`.
`install` writes a SKILL file (or connection config) for the target agent,
stores the platform URL + token in the agent's own config, and `verify` performs
`GET /api/v1/whoami` + `GET /api/v1/capabilities`. Support matrix must list only
verified targets. Package must ship a local tarball (`npm pack`) until published.

---

## 12. Testing requirements

* `backend/tests/test_foundation.py` — security core (already green).
* `backend/tests/test_api_contract.py` — every admin route rejects anonymous
  calls and returns the error envelope; happy paths for providers/credentials/
  capabilities/tokens.
* `backend/tests/test_execution.py` — sync success/failure, unsupported
  parameter, idempotency dedupe, timeout → `uncertain`, no failover after output.
* `backend/tests/test_tasks.py` — lease recovery, cancel truthfulness, restart.
* `backend/tests/test_skills.py` — import validation, dependency impact, export
  contains no secrets.
* `backend/tests/test_engine_upgrade.py` — candidate validation failure keeps
  the current release; failed activation rolls back.
* `frontend` — `npm run build` and `npm run lint` must pass.
