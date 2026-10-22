# AragonAgent sidecar

The **only** process in Skill Router that imports `@aragon-agent/core`. The Python
backend never links the engine; it talks HTTP to this service through
`backend/app/services/agent_adapter.py`.

```
browser ──► FastAPI (/api/admin/agent/…, /api/stream/agent/…)
                     │  agent_adapter.py  (httpx, SSE parser)
                     ▼
             sidecar 127.0.0.1:7317   ← THIS PACKAGE
                     │  platform.* tool → HTTP POST
                     ▼
             FastAPI /api/admin/agent/tools/{name}  (scoped tool token)
```

## Pinned engine

| | |
|---|---|
| package | `@aragon-agent/core` |
| pinned version | **0.2.50** (`--save-exact`, lockfile committed) |
| resolved | `https://registry.npmjs.org/@aragon-agent/core/-/core-0.2.50.tgz` |
| licence | MIT, ESM-only, Node ≥ 18 |
| upstream repo | `https://github.com/xdrshjr/AragonAgent.git` |
| verified install | Node 22.18.0 · npm 10.9.3 |
| measured against | repo copy 0.2.46 @ `c8b0e3df60319b81b529310e4d4f8d545c70c525` |

The 0.2.50 typings were diffed against the 0.2.46 source after installing: the
`AgentEvent` and `StreamEvent` unions are **identical**, so the mapping table
below applies unchanged. `package-lock.json` is the source of truth; bump
`backend/app/core/config.py::aragon_aligned_core_version` in the same commit as
any change here.

> Note: `SR_ARAGON_ALIGNED_CORE_VERSION` still reads `0.2.46` in `config.py`
> (owned by platform core). The risk floor is low — the API surface is identical —
> but the upgrade flow compares the two and will report a mismatch until that
> setting moves to `0.2.50`.

## Run

```cmd
cd sidecar
npm install
npm run build
set SR_ARAGON_SIDECAR_TOKEN=<shared secret>
npm start
```

`npm test` builds and runs the `node:test` suite (33 tests, no network, no API
keys, no provider calls).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `SR_ARAGON_SIDECAR_HOST` | `127.0.0.1` | loopback only; anything else is refused at boot |
| `SR_ARAGON_SIDECAR_PORT` | `7317` | listener port |
| `SR_ARAGON_SIDECAR_TOKEN` | — | **required**; bearer for every non-`/health` call |
| `SR_ARAGON_REPO_URL` | upstream repo | `/releases/check` remote |
| `SR_ARAGON_UPGRADE_CHANNEL` | `stable` | `stable` = tags, `branch` = branch head |
| `SR_ARAGON_TRACK_BRANCH` | `main` | branch used when channel is `branch` |
| `SR_ARAGON_ENGINE_COMMIT` | — | commit this build was deployed from (reported by `/health`) |
| `SR_ARAGON_DEFAULT_PROVIDER_ID` / `_MODEL_ID` / `_BASE_URL` | — | model template used when a create request omits `model` |
| `SR_ARAGON_MAX_SESSIONS` | `32` | live sessions before creates are refused (429) |
| `SR_ARAGON_EVENT_BUFFER` | `2000` | events retained per session for replay |
| `SR_ARAGON_TOOL_TIMEOUT_MS` | `120000` | per tool call, local hard timer |
| `SR_ARAGON_LLM_TIMEOUT_MS` | `300000` | per LLM call |
| `SR_ARAGON_IDLE_TIMEOUT_MS` | `300000` | engine idle watchdog |
| `SR_ARAGON_MAX_TOKENS` | — | output ceiling per call |
| `SR_ARAGON_MAX_TOOL_OUTPUT_CHARS` | `4000` | tool result truncation in events |
| `SR_ARAGON_STOP_SETTLE_MS` | `10000` | how long stop waits for the loop to settle |
| `SR_ARAGON_RELEASE_TIMEOUT_MS` | `15000` | `git ls-remote` ceiling |
| `SR_ARAGON_SSE_KEEPALIVE_MS` | `15000` | keepalive comment cadence |

## Endpoints (CONTRACT §10)

All but `GET /health` require `Authorization: Bearer $SR_ARAGON_SIDECAR_TOKEN`.

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{ok, engine{package,version,commit,adapter_version}, model_configured, model_template}` |
| GET | `/info` | tools, providers, features, limits, `unknown_events` |
| POST | `/sessions` | create **or resume**; `201` new / `200` resumed |
| GET | `/sessions/{id}` | transcript snapshot + last 50 events |
| DELETE | `/sessions/{id}` | dispose (stops a live run first) |
| POST | `/sessions/{id}/messages` | `202 {run_id, seq}`; `409 session_busy` while running |
| GET | `/sessions/{id}/events` | SSE, `Last-Event-ID` / `?since=` resume |
| POST | `/sessions/{id}/stop` | truthful stop report |
| GET | `/releases/check` | `?channel=stable|branch`; `502` when git fails |

Errors use `{ok:false, code, message, details?}`. `/releases/check` answers `502`
on a git failure rather than `200 {ok:false}` so an HTTP client cannot mistake a
failed detection for a reading of the remote.

### Session create

```jsonc
POST /sessions
{"session_id":"ses_abc",                       // optional; upsert semantics
 "model":{"providerId":"openai",               // anthropic | openai | google
          "modelId":"gpt-4o-mini",
          "baseUrl":"https://zenmux.ai/api/v1"}, // OpenAI-compatible gateway
 "api_key":"sk-…",                             // memory only, never logged
 "api_key_ref":"cred_01H…",                    // non-secret label for logs
 "system_prompt":"…",
 "tools":[{"name":"platform.list_providers",
           "description":"…",
           "parameters":{"type":"object","properties":{}}}],
 "platform":{"base_url":"http://127.0.0.1:8000",
             "token":"<run-scoped tool token>"}}
```

Fail-fast rejections (`400`): `model_not_configured`, `unsupported_provider`,
`invalid_base_url`, `platform_not_configured`, `invalid_platform_url`,
`tool_not_allowed`, `invalid_session_id`.

`baseUrl` is passed straight to the provider adapter, which appends
`/chat/completions` — so give the API root (`…/api/v1`), not the full path.

## The permission ceiling

`PLATFORM_TOOL_NAMES` in `src/tools.ts` is the complete and only set of tools the
engine can ever hold:

```
platform.list_providers              platform.list_skills
platform.list_credentials            platform.save_skill_draft
platform.list_capabilities           platform.get_test_evidence
platform.get_capability              platform.validate_capability
platform.create_capability_draft     platform.test_capability
platform.update_capability_draft     platform.publish_capability
```

* Anything else in a create request → the **whole** request is rejected with
  `400 tool_not_allowed`, listing what was refused and the allowed set. Rejection
  is deliberate: the engine never registers a denied tool, so the model cannot see
  it or plan around it — dropping it silently would invite the model to try.
* `bash`, `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `list_dir`,
  `web_fetch`, `task`, `code_exec` are not merely denied — they are **unnameable**.
  No value in a request can produce them, and the sidecar never supplies a
  `sandbox`, so CodeAct cannot run either.
* Each tool does `POST {platform.base_url}/api/admin/agent/tools/{canonical}`
  with the run-scoped bearer token. The engine never receives a platform
  credential, a database handle or a filesystem path.

### Wire names

Provider APIs only accept `[A-Za-z0-9_-]{1,64}` tool names (Anthropic rejects a
dot with a 400), but the canonical names are dotted. So the model sees the
underscore form and the callback carries the dotted form:

| canonical (URL, body, audit) | wire name (model-facing) |
|---|---|
| `platform.list_providers` | `platform_list_providers` |
| `platform.publish_capability` | `platform_publish_capability` |

Only the **namespace** separator is converted — the tool's own words keep their
underscores (`platform.save_skill_draft`, not `platform.save.skill.draft`).

## Event mapping

The normaliser consumes upstream `AgentEvent` (0.2.50 typings) and emits the
CONTRACT §5.10 union. Every emitted event carries `{seq, run_id, session_id,
at, type}` plus type-specific fields.

| upstream event | emitted | fields |
|---|---|---|
| `agent_start` | *absorbed* | `run.started` is emitted by the runner so it can carry the model + tool list |
| `agent_end` | *absorbed* | terminal state is only knowable after `prompt()` settles |
| `turn_start` | `run.progress` | `phase:"turn_start"`, `turn` |
| `steering_accepted` | `run.progress` | `phase:"steering_accepted"`, `ids` |
| `turn_end` | `run.progress` | `phase:"turn_end"`, `stop_reason`, `usage` |
| `message_update` / `text_delta` | `assistant.delta` | `delta` |
| `message_update` / `thinking_start` | `run.progress` | `phase:"thinking_start"` |
| `message_update` / `thinking_delta` | `run.progress` | `phase:"thinking_delta"`, `delta` |
| `message_update` / `tool_call_start` | `run.progress` | `phase:"tool_call_start"`, `tool`, `tool_call_id` |
| `message_update` / `tool_call_delta` | *absorbed* | thousands of fragments per call; complete args arrive on `tool_execution_start` |
| `message_update` / `tool_call_end` | *absorbed* | duplicate of `tool_execution_start` args |
| `message_update` / `done` | `run.progress` | `phase:"llm_done"`, `stop_reason`, `usage` |
| `message_update` / `error` | `run.progress` | `phase:"llm_error"`, `message` — not terminal, the engine retries |
| `message_update` / `retry_scheduled` | `run.progress` | `phase`, `attempt`, `max_retries`, `delay_ms`, `resume_at`, `error_type`, `message` |
| `message_update` / `retry_attempt` | `run.progress` | `phase`, `attempt`, `max_retries` |
| `message_update` / `stream_restart` | `run.progress` | `phase`, `attempt`, `discarded_tool_call_ids` |
| `tool_execution_start` | `tool.call` | `tool`, `tool_call_id`, `args` |
| `tool_execution_end` | `tool.result` | `tool`, `tool_call_id`, `is_error`, `duration_ms`, `output` (truncated), `output_bytes`, `output_truncated` |
| `code_execution_start/end` | `run.progress` | mapped for completeness; unreachable — no sandbox is ever supplied |
| `compaction_start` | `run.progress` | `phase`, `trigger`, `message_count` |
| `compaction_end` | `run.progress` | `phase`, `applied`, `mode`, `reason`, token/message deltas, `duration_ms` (the summary body is **not** forwarded) |
| anything else | *dropped, counted* | `unknown_events` on `/info` |

Runner-emitted terminals (not derivable from `agent_end`):

| type | when |
|---|---|
| `run.started` | a run begins (`model`, `tools`, `api_key_ref`) |
| `run.finished` | the loop ended after at least one completed turn, and no stop was requested (`turns`, `usage`, `text`, `unknown_events`) |
| `run.stopped` | aborted (`reason`, `stopped_steps`, `may_continue_upstream`) |
| `run.failed` | a thrown error, a provider failure, or a loop that completed no turn (`code`, `message`, `retryable`, `error_type`) |

### Run outcome: resolved ≠ succeeded

`Agent.runLoopWithLifecycle` **catches a loop error, prints it to `console.error`
and resolves `prompt()`**. So "the promise resolved" does not mean "the run
worked", and a naive runner reports a provider outage as a green, empty success.

The outcome is therefore classified from the event stream, in this order:

1. a stop was requested → `stopped`;
2. `prompt()` threw → `failed`;
3. **no turn completed** (no `done`, no `turn_end`) → `failed`, with the last
   `retry_scheduled`/`error` message as the reason;
4. otherwise → `finished`.

This was found by pointing the engine at an unreachable base URL: the run came
back `finished` with no text while the sidecar log showed
`[Agent] loop error: LLMError: openai network error: fetch failed`. Cases (3) and
the raw-event tracking in `trackOutcomeEvidence` are what close that hole
(`test/outcome.test.mjs`).

`todo.update` is part of the platform union but upstream core emits no todo
events (the CLI synthesises them from its own tool). The sidecar therefore never
emits it; if a future engine adds a todo tool it maps here without a schema
change.

### Unknown events are counted, not fatal

`schemaVersion`-style forward compatibility is a contract: a consumer must ignore
event types it does not know. An unrecognised upstream event is counted in
`unknown_events` (aggregated on `/info`, per run in `run.finished`) and dropped.
A run never fails because the engine got better.

## SSE

`GET /sessions/{id}/events` is a real `text/event-stream`:

```
: connected session=ses_abc after=41 gap=0

id: 42
event: run.progress
data: {"seq":42,"type":"run.progress","run_id":"run_1","session_id":"ses_abc","at":"…","phase":"turn_start","turn":1}

: keepalive 2026-10-08T…
```

* `id:` is the session's monotonic `seq`. Reconnect with `Last-Event-ID: 42` (or
  `?since=42`) to resume exactly.
* The backlog is replayed first — the platform attaches *after* the `202` that
  accepted the message and would otherwise miss `run.started`.
* A bounded ring buffer means an old resume point may fall off the back. That is
  reported honestly: header `x-replay-gap: 1` plus a `: replay gap — oldest
  retained seq is N` comment, so the platform re-reads its own persisted events
  instead of believing it saw everything.
* `: keepalive` comments keep proxies and read timeouts from killing a quiet
  stream (a long tool call is quiet).
* The stream is session-scoped, not run-scoped: it stays open across turns and the
  client closes it after the terminal event for its run.

## Stop semantics

`POST /sessions/{id}/stop` aborts, waits for the loop to settle (bounded by
`SR_ARAGON_STOP_SETTLE_MS`), and returns:

```jsonc
{"stopped":true,"reason":"stop_requested","settled":true,"run_id":"run_1",
 "stopped_steps":[{"kind":"agent_loop","turn":1},
                  {"kind":"queued_follow_ups"},
                  {"kind":"tool_call_aborted_locally","tool":"platform.test_capability"}],
 "may_continue_upstream":[{"tool":"platform.test_capability","tool_call_id":"t1",
                           "dispatched_at":"…",
                           "note":"the platform received this request; it may still write invocation records"}]}
```

`may_continue_upstream` exists because aborting a local `await` does not cancel
work the platform already started. Reporting those as "stopped" would make the
cancel button a liar the first time an invocation record appeared afterwards.
`settled:false` means the loop did not go idle within the ceiling — the run is
still winding down.

A stop with nothing running returns `{stopped:false, reason:"no_active_run"}`
(never a fake success).

## `/releases/check`

```jsonc
{"ok":true,"channel":"stable","ref":"core-v0.2.34","version":"0.2.34","package":"core",
 "commit":"0a119a64890717b0e3017edbaaf00944babf19a8",
 "detected_at":"2026-10-08T…",
 "source":"git ls-remote --tags https://github.com/xdrshjr/AragonAgent.git",
 "candidates":[{"ref":"cli-v0.6.22","version":"0.6.22","commit":"…","package":"cli"}, …]}
```

* `stable` → the highest semver tag **for this build's package** from
  `git ls-remote --tags`; annotated tags use the peeled `^{}` commit (the real one).
  The upstream remote uses monorepo tags (`core-v0.2.34`, `cli-v0.6.13`), so both
  `vX.Y.Z` and `<package>-vX.Y.Z` are parsed and the `core` stream is followed — an
  upgrade must never silently track the CLI's version line.
* `branch` → head of `SR_ARAGON_TRACK_BRANCH`.
* No silent fallbacks: a missing git, a timeout or an unparseable remote gives
  `ok:false` + `code` + the exact command and exit code. A release row that claims
  a commit must mean the remote actually said so.
* Results are cached for 30 s to keep the UI's polling from hammering the remote.

Note: at the time of writing the newest **git tag** is `core-v0.2.34` while npm
publishes `0.2.50`. The two channels genuinely disagree — tags lag the npm
releases on this project — so the upgrade flow compares a tag candidate against
the npm registry during validation and says so rather than picking one silently.
