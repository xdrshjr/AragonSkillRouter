# `agents/aragon-gateway` — Node sidecar gateway (AragonAgent host)

Node 22 + Fastify sidecar that hosts the **AragonAgent engine** for the Skill Router
platform. It is the only process that touches the engine: the Python API talks to it over
loopback HTTP + SSE, authenticated with `X-Internal-Token` (spec §4.2, §7.12, §9.5).

```
Python api / worker ──HTTP+SSE──▶ gateway (127.0.0.1:8500) ──child process──▶ engine-host ──▶ @aragon-agent/core@0.2.34
                                          │
                                          └──HTTP──▶ platform internal API (credentials, capabilities, audit)
```

## Pinned engine

| Item | Value |
|---|---|
| Package | `@aragon-agent/core` |
| Version | **`0.2.34`** (exact pin, no `^`) |
| Upstream commit | `0a119a64890717b0e3017edbaaf00944babf19a8` (= tag `core-v0.2.34` = `main`) |
| License | MIT |
| Module format | ESM-only (`exports` has only `types` + `import` conditions — never `require()`) |
| Node | ≥ 18 (measured on v22.18.0) |
| Public exports | 86, frozen by `test/conformance.test.ts` |
| Machine contract | `schemaVersion: 1` |

## Adapter boundary (what we depend on, and nothing else)

From `packages/core` we use only: `AragonAgent` (same class as `Agent`), `defineTool`,
`initProviders`, and the structural types `AgentTool`, `ModelRef`, `AgentEvent`.

`new AragonAgent({systemPrompt, model, tools, providerRegistry, getApiKey, thinkingLevel,
maxTokens, timeouts})`, then `prompt()`, `subscribe()`, `abort()`, `steer()`,
`waitForIdle()`, `setTools()`, `setModel()`, `setSystemPrompt()`, `replaceMessages()`,
`state`.

Facts that shaped this implementation (measured, `docs/plans/skill-router/aragonagent-upstream.md`):

- **`tools` is the whole permission surface.** Anything not in the array is invisible to
  the model, so the platform tool list *is* the security boundary.
- **`getApiKey(providerId)` is synchronous** and is the only place a provider secret can
  enter the engine. The gateway therefore materializes the credential *before* prompting
  (`POST /internal/credentials/materialize`, TTL ≤ 60s), keeps it in process memory only,
  and refreshes it once at half-life for long turns.
- **No persistence.** The platform's `agent_sessions`/`agent_turns` are authoritative;
  each turn replays its history through `replaceMessages()` (tail-clipped to
  `AGENT_REPLAY_TURNS`, default 20 turns).
- **No abort signal, no `maxTurns`, no event callbacks.** Cancellation is `abort()`; turn
  budgets (`max_turns`, `max_duration_ms`) are enforced by counting `turn_start` events
  and by a wall-clock timer that calls `abort()`.
- **No filesystem sandbox** upstream, which is why `bash`/`read_file`/`write_file` and the
  `skill_*` builtins are never registered here.

## Tool surface (spec §12.3) — the agent's only capabilities

| Tool | Params (summary) | Returns | Timeout |
|---|---|---|---|
| `sr_capabilities_list` | `query?, kind?, mode?, limit?` | capability list (key/name/kind/mode/status) | 20s |
| `sr_capability_get` | `key` | capability detail (schema/limits/examples) | 20s |
| `sr_fetch_source` | `url, purpose` | truncated text + `truncated` + `bytes` (via platform `fetch_guard`) | 20s |
| `sr_parse_document` | `file_id` | structured document summary | 20s |
| `sr_propose_capability_draft` | `key,name,kind,modes[],file_io,input_schema,output_schema,examples[],routes[],evidence[]` | `{draft_id, issues[]}` | 20s |
| `sr_update_capability_draft` | `draft_id, patch` | `{draft_id, issues[]}` | 20s |
| `sr_test_capability` | `draft_id, input, route_id?, save_payload?` | `{ok,http_status,latency_ms,response_preview,error_code,used_route}` | **60s** |
| `sr_sample_invoke` | `capability_key, version?, input` | minimal redacted result | **60s** |
| `sr_create_skill_draft` | `name, description, instructions, execution_plan?, permissions?` | `{skill_id, issues[]}` | 20s |
| `sr_validate_skill` | `skill_id` | `{ok, issues[]}` | 20s |
| `sr_record_finding` | `kind, confidence, text, targets?` | `{finding_id}` | 20s |
| `sr_request_approval` | `action, target, impact, requested_scope` | `{approval_id, decision}` | 20s |

Every tool is built by `buildTool()` in `src/tools/index.ts`, which enforces in one place:

1. ajv validation of the JSON Schema, with `additionalProperties: false` at the top level
   (unknown arguments are rejected before anything leaves the process);
2. a **real** timeout race — upstream's executor only *signals* a timeout and then awaits
   the tool promise, so a ceiling that is not raced here is inert;
3. one audit entry per call (including rejected calls) to `POST /internal/audit`;
4. redaction of secret-shaped fields plus a hard 20 000-character clip on what the model
   sees.

Upstream builtin tools (`bash`, `read_file`, `write_file`, `edit_file`, `skill_install`,
`skill_create`, …) are listed in `src/policy.ts` as `UPSTREAM_BUILTIN_TOOL_NAMES` and can
never be registered: the policy file may only name the 12 `sr_*` tools, and any *new*
upstream tool is default-off (asserted by `test/policy.test.ts`).

## Internal endpoints implemented here (spec §9.5)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/healthz` | none | `{ok, engine:{package,version,commit,schema_version,status,detail?}, tools[], active_turns, uptime_s}`. No secrets; used by the container healthcheck and §12.4's "3 consecutive healthy" gate. |
| POST | `/internal/turns` | token | `{session_id, turn_id, messages[]?, system_prompt?, model?, budget?, input?, credential_refs?}` → `{accepted:true, ipc_stream, turn_id}` |
| GET | `/internal/turns/:turnId/events` | token | SSE, supports `Last-Event-ID` / `?last_event_id=` replay |
| POST | `/internal/turns/:turnId/interrupt` | token | → `StopReport{stopped_steps[], external_pending[]}` |
| POST | `/internal/turns/:turnId/steer` | token | queue a user message into the running turn |
| POST | `/internal/admin/reload` | token | 4-step engine hot switch → `{ok, engine_version, stage, detail}` |

Outbound calls (all with `X-Internal-Token`): `POST /internal/credentials/materialize`,
`GET /internal/capabilities`, `GET /internal/capabilities/{key}`,
`POST /internal/audit`, and `POST /internal/tools/{tool_name}` for the tool bodies
(see "Assumptions" below).

### Engine events (§7.12, exactly 13 names)

`session.started`, `turn.started`, `assistant.delta`, `thinking.delta`, `tool.started`,
`tool.progress`, `tool.finished`, `todo.updated`, `turn.queued`, `turn.finished`,
`turn.stopped`, `turn.failed`, `engine.notice`.

Upstream → platform mapping (`src/engine.ts::onUpstreamEvent`):

| Upstream `AgentEvent` | Platform event |
|---|---|
| `agent_start` / `turn_start` | `turn.started` (+ iteration counting for `budget.max_turns`) |
| `message_update.streamEvent.text_delta` | `assistant.delta` |
| `message_update.streamEvent.thinking_delta` | `thinking.delta` |
| `tool_execution_start` | `tool.started` (with `args_summary`) |
| `tool_execution_end` | `tool.finished` (with `duration_ms`, `result_summary`) |
| `message_update.streamEvent.usage` / `turn_end` | accumulated into `turn.finished.usage` |
| `steering_accepted`, `retry_*`, `stream_restart`, `compaction_*` | `engine.notice` |
| `code_execution_*` | `engine.notice` (`CODE_EXECUTION_UNSUPPORTED` — no sandbox) |
| anything unknown, `team*`, `fast_review` | `engine.notice` (`UNSUPPORTED_UPSTREAM_EVENT` / `NEEDS_ADAPTER`) |

`tool.progress` is emitted when a tool reports progress. There is no `todo` event in
`@aragon-agent/core@0.2.34`'s `AgentEvent` union (only the CLI's NDJSON stream has it), so
`todo.updated` is wired for a future SDK addition but not produced today — this is stated
rather than faked.

## Engine modes and hot reload (§7.12-6)

`AGENT_GATEWAY_ENGINE_MODE` selects the backend:

| Mode | Behaviour |
|---|---|
| `host` (recommended, used by the image) | the engine runs in a child process (`node dist/src/engine-host.js --pin <version> --home var/aragon-home/engines/<version>`); `POST /internal/admin/reload` can switch versions without restarting anything |
| `inline` | the engine runs inside the gateway process. **Hot reload is impossible** (`import()` is cached) and the endpoint honestly returns `{ok:false, stage:"rejected"}` |
| `auto` (default in dev) | tries `host`, falls back to `inline` with a `degradedNote` reported in `/healthz`'s `engine.detail` |

`HostSupervisor.reload(target_version)` performs the four required steps and never leaves
the platform with a half-switched engine:

1. spawn a child on the target pin (`var/aragon-home/engines/<version>/`, isolated per
   version so rollback reuses its own directory);
2. wait for `{type:'ready', engine:{version}, tools:[…]}` on stdout NDJSON, and verify the
   reported version equals the target;
3. drain the old child: `abort` every live turn, wait for idle with a **60s** ceiling;
4. close the old child (child exits itself after a `shutdown` frame; the parent arms a
   bounded timeout) and only then report the new version.

Any failure (including a pin whose package is not installed) closes the *new* child, keeps
the old child serving and returns `{ok:false, stage, detail}` with **the pin unchanged**.
No signals are sent to the engine (upstream measured Windows signal differences: only
Ctrl-C yields a final `result`), no Docker socket, no container restart.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `AGENT_GATEWAY_BIND` | `127.0.0.1` | non-loopback needs `AGENT_GATEWAY_ALLOW_PUBLIC_BIND=1` |
| `AGENT_GATEWAY_PORT` | `8500` | |
| `AGENT_GATEWAY_SHARED_SECRET` | — | **required**, ≥ 32 chars, sent as `X-Internal-Token` |
| `SKILLROUTER_INTERNAL_URL` | `http://127.0.0.1:8000` | Python internal API base |
| `AGENT_POLICY_PATH` | `var/agent-policy.json` | tool allowlist written by the settings page |
| `ENGINE_PIN` | `0.2.34` | must be an exact installed version |
| `AGENT_GATEWAY_ENGINE_MODE` | `auto` | `host` \| `inline` \| `auto` |
| `AGENT_ENGINE_HOME_ROOT` | `var/aragon-home/engines` | per-version `ARAGON_HOME` |
| `AGENT_MODEL_PROVIDER` / `AGENT_MODEL_ID` | `anthropic` / — | fallback when a turn omits `model` |
| `AGENT_MODEL_BASE_URL` | — | OpenAI-compatible endpoints |
| `AGENT_MODEL_MAX_TOKENS`, `AGENT_THINKING_LEVEL` | — / `off` | |
| `AGENT_LLM_CALL_TIMEOUT_MS`, `AGENT_IDLE_TIMEOUT_MS`, `AGENT_TOOL_TIMEOUT_MS`, `AGENT_TURN_TIMEOUT_MS` | 300000 / 60000 / 120000 / 900000 | |
| `AGENT_SSE_KEEPALIVE_MS` | `15000` | spec §9.4 requires 15s |
| `AGENT_SESSION_DIR`, `AGENT_REPLAY_TURNS`, `AGENT_PLATFORM_TIMEOUT_MS`, `AGENT_GATEWAY_LOG_LEVEL` | `var/agent-sessions`, `20`, `30000`, `info` | |

No provider API key is ever read from the environment: secrets arrive only via
`materialize()` and live in process memory.

## Running

```cmd
cd agents\aragon-gateway
npm install --no-audit --no-fund --include=dev
set AGENT_GATEWAY_SHARED_SECRET=<64 random chars>
npm run dev            :: tsx watch src/main.ts
:: or
npm run build
npm start
```

Health check:

```cmd
curl -s http://127.0.0.1:8500/healthz
```

### Running without the SDK installed

`src/engine.ts` loads `@aragon-agent/core` with a **dynamic** `import()`, so the module
graph (and therefore the whole test suite) stays importable when the dependency is absent.
In that situation the gateway still boots and reports the truth:

- `GET /healthz` → `{"ok":false,"engine":{…,"status":"unavailable","detail":"… is not importable …"}}`
- `POST /internal/turns` → `503 {"code":"ENGINE_UNAVAILABLE","detail":…}`
- `POST /internal/admin/reload` → `{ok:false,…}` with the same reason

Nothing is simulated: there is no fake engine, no stub assistant text, and no "ok:true"
without a real engine behind it.

### Docker

```cmd
docker build -t skillrouter/gateway:dev .
docker run --rm -p 127.0.0.1:8500:8500 -e AGENT_GATEWAY_SHARED_SECRET=... skillrouter/gateway:dev
```

Multi-stage: the build stage installs devDependencies and compiles, the runtime stage runs
`npm ci --omit=dev` and copies only `dist/`. Non-root (`node`) user, writable `var/` only.

## Tests

```cmd
npx tsc --noEmit      :: strict, NodeNext, no explicit `any`
npx vitest run
```

- `test/conformance.test.ts` — 86 frozen upstream exports (removal/rename fails, addition
  warns), 13 frozen event names, 12 frozen tool names, `defineTool` acceptance, and a real
  `new AragonAgent({...})` construction with the platform toolset.
- `test/tools.test.ts` — schema validation, URL guard, timeout races, external abort,
  audit-per-call, redaction, output clipping, budget pass-through.
- `test/policy.test.ts` — allowlist only narrows, upstream builtins rejected, unknown/new
  tools default-off.

## Upgrade notes (§12.4)

1. Bump the pin in `package.json` (`--save-exact`), `ENGINE_PIN`, and the table above.
2. `npx vitest run` — the conformance suite is the gate: a removed/renamed export
   **fails**, an added export is logged as `needs_adapter_review`.
3. Re-check the mapping table above against upstream `dist/types.d.ts` (`AgentEvent` union,
   `StreamEvent` members). New `StreamEvent`s fall through to `engine.notice`, so they
   degrade visibly instead of being dropped silently.
4. Verify `HOST_TOOL_NAMES`-style additions do not appear in the platform tool list
   (`policy.test.ts` asserts this).
5. Deploy, then `POST /internal/admin/reload {target_version}`; `/healthz` must report the
   new version three times in a row. On failure nothing changed — fix and retry.
6. `var/aragon-home/engines/<version>/` is kept per version for rollback; do not reuse a
   directory across versions.

## Assumptions / open points

- **Tool bodies**: §9.5 defines `/internal/credentials/materialize`, `/internal/capabilities`
  and `/internal/audit`, but not where the *write* tools post their payloads. The gateway
  forwards them to `POST /internal/tools/{tool_name}` with
  `{tool, arguments, session_id, turn_id}` and lets the platform own validation, budgets,
  persistence and `fetch_guard`. If the Python side chooses different paths, only
  `tools/index.ts::PlatformClient.invokeTool` and the two capability tools need to change.
- **`/healthz` is unauthenticated** (loopback bind, no secrets in the body) so an
  orchestrator can probe it. Everything else requires `X-Internal-Token`.
- **Turn prompting**: the platform may send `input` (spec §7.12) or only `messages[]`
  (spec §9.5); in the latter case the last user message is used as the prompt *and excluded
  from the replayed history* so it is not duplicated.
- **`ENGINE_COMMIT`** is the only source for `engine.commit`; it is `null` unless the
  upgrade pipeline exports the tag's commit (`core-v0.2.34` → `0a119a6…`). No value is
  invented.
- **`schema_version`** is reported as `1` (measured across `aragon info --json` and
  `exec --output-format stream-json`); a change to it must be caught in review.
