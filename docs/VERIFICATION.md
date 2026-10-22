# Verification

Every check that exists in this repository, what it proves, what it does **not**
prove, and the performance procedure. Recorded results are from the build machine:
Windows 10.0.26200, Python 3.11.7, Node 22.18.0, npm 10.9.3. **Docker is not installed
on that machine**, which is why §7 is thin.

Rule this document follows: a check that was not run is written as "not run", never as
"passed".

---

## 1. Summary

| # | Check | Command | Result | Proves |
|---|---|---|---|---|
| 1 | Backend suite | `python -m pytest backend/tests -q` | **202 passed, 0 failed** (78 s) | API contracts, execution, tasks, skills, export, MCP, engine upgrade, agent adapter, restart recovery |
| 2 | Backend byte-compile | `python -m compileall -q backend/app` | **pass** | no syntax errors anywhere in the app |
| 3 | Migration from zero | `python -c "from app.core.db import apply_migrations; print(apply_migrations())"` | **`['0001_init.sql']`, 28 tables** | a fresh container can create its schema |
| 4 | Operator CLI | `python -m app.cli doctor` | **exit 1** on this dev box, only because `initial_password_changed` is false | doctor detects exactly the state it should on an unmodified dev database |
| 5 | Sidecar build + tests | `cd sidecar && npm run build && node --test "test/*.test.mjs"` | **43 passed, 0 failed** | event mapping, SSE, session lifecycle, tool ceiling, release parsing |
| 6 | Frontend lint | `cd frontend && npm run lint` | **pass, 0 warnings** | style/lint gate (`--max-warnings=0`) |
| 7 | Frontend typecheck | `cd frontend && npm run typecheck` | **pass** | TypeScript strict has no errors |
| 8 | Frontend build | `cd frontend && npm run build` | **pass from a clean tree**: 15 routes, `.next/standalone/server.js` present | the production build and the standalone output the image uses |
| 9 | Installer tests | `cd packages/installer && npm test` | **35 passed, 0 failed** | SKILL generation per target, token never printed, idempotency, `--force`, verify exit codes, uninstall safety |
| 10 | Installer packaging | `cd packages/installer && npm pack` | **`skill-router-connect-1.0.0.tgz`**, 22.3 kB, 11 files (shasum `697f3c5e82f3b4bc46c84685973831a97695e129`) | the package is shippable |
| 11 | Packed artifact installs | `npm install <tarball>` in an empty project, then `--version` | **`1.0.0`** | the tarball's `bin`, `files` and shebang work outside the repo |
| 12 | Packed artifact end to end | install → verify → update → uninstall through the installed CLI against a mock platform | **all exit 0**, token never leaked | the shipped artifact behaves like the source |
| 13 | Installer vs the REAL API | CLI against a local uvicorn on a database copy (agent + token created via the admin API) | **install/verify/update all exit 0**; SKILL written; token absent from it | the installer works against this platform, not just against a mock |
| 14 | Catalogue from the REAL API | one published capability inserted into that database copy, then `install --force` | **`GET /api/v1/capabilities` count=1**, catalogue + call example rendered from the real descriptor | the descriptor → SKILL path is real |
| 15 | MCP transport, live | `POST /api/mcp initialize` / `tools/list` with a real token | **`protocolVersion 2025-06-18`, `serverInfo skill-router/1.0.0`, `tools/list` → 200** | the MCP endpoint answers a real client-shaped request |
| 16 | Smoke script | `powershell -File scripts\smoke.ps1 -Password … -Token …` | **8 passed, 0 failed, 1 skipped** (web not running) | the operational paths work end to end, and skips are honest |
| 17 | Script syntax | PowerShell parser over `scripts/*.ps1` | **all 4 parse** | no syntax errors |
| 18 | CI YAML / compose YAML | `python -c "import yaml; yaml.safe_load(open(...))"` | **both parse** | the files are well-formed YAML |
| 19 | Acceptance verifier | `python scripts/verify_acceptance.py --base-url … --admin-user admin --admin-password … --token <token> --capability verify-text-completion-zenmux` | **22 passed, 0 failed, 0 skipped** | every read-only acceptance check in `docs/CONTRACT.md` against a live instance, with request ids — including a real MCP `tools/call` |
| 20 | Docker images + compose | `docker compose config --quiet` (standalone CLI + compose plugin, no daemon needed) | **config validates, exit 0**; services `api, worker, web, sidecar, edge` resolve, images `skill-router-{api,web,sidecar}:1.0.0` + `nginx:1.27-bookworm`, `network_mode: service:api` applied to sidecar and worker | the Compose file is valid and interpolates. **Image builds still NOT RUN** (no daemon) — see §7 |
| 21 | **Real providers, closed loop** | `python scripts/verify_real_providers.py --video --image --rotate-password` | **37 passed, 0 failed** | real Gemini + ZenMux text, real Gemini TTS audio, real Gemini image, real async ZenMux video (queued→running→succeeded, 622 255-byte mp4 downloaded), external agent discovery + call, MCP handshake, secret-free install bundle. Evidence: `data/verification/real-providers-*.json` |
| 22 | **Real agent engine run** | `python scripts/verify_agent_engine.py` | **12 passed, 0 failed** | sidecar `@aragon-agent/core@0.2.50` + adapter 1.0.0 healthy; a real model turn (`google/gemini-2.5-flash` via ZenMux) called the platform tool `platform.list_capabilities`, streamed 14 events and finished `succeeded`; recorded events replays from SQLite. Evidence: `data/verification/agent-engine-*.json` |
| 23 | **Browser path through Next** | `python scripts/verify_browser_path.py <pw>` | **20 passed, 0 failed** | `/api` rewrite, HttpOnly cookie, CSRF enforced through the proxy (403 without header), 12 admin reads 200, create+delete provider, agent session create/list |
| 24 | **Performance baseline** | `python scripts/perf_check.py --requests 20` | reads **P95 41–342 ms** at 10 concurrent clients; async submit platform overhead **P95 774 ms** | the two budgets in §8 hold on this hardware. Evidence: `data/verification/perf-*.json` |
| 25 | **Restart recovery, live** | insert a `running` agent run, restart uvicorn | log line `reconciled 1 interrupted agent run(s)`; row becomes `failed` / `process_restart` with a message admitting tool calls may have completed upstream | a restart cannot leave the agent panel spinning forever; covered by `backend/tests/test_agent_run_recovery.py` (3 tests) |
| 26 | **Desktop runtime assembled** | `cd desktop && npm run build:web && npm run build:backend` | frozen API + Next standalone UI + sidecar + `runtime.json` manifest (engine `@aragon-agent/core@0.2.50`) | the installer payload is complete and its manifest names the engine it shipped |
| 27 | **Desktop runtime smoke** | `node desktop/scripts/smoke.mjs --clean` | **12 passed, 0 failed** | API self-initialises on a cold data dir, UI serves HTML, sign-in returns session+CSRF, CSRF-less mutation refused, password gate enforced, foreign Origin refused, API and sidecar agree on the engine version |
| 28 | **Shell unit tests** | `cd desktop && npm run build:main && npm test` | **12 passed, 0 failed** | proxy routing (API→backend, UI→web, shell metadata token-gated), SSE chunk streaming, gzip/content-length fix, multi-cookie forwarding, port selection, health polling |
| 29 | **Installer built** | `cd desktop && npx electron-builder --win` | **`SkillRouter-Setup-1.0.0.exe`, 148 630 803 bytes** + `win-unpacked\` | a per-user NSIS installer with the three runtimes inside (`resources\runtime\{backend,web,sidecar}`) |
| 30 | **Packaged app end to end** | `node desktop/scripts/verify-packaged.mjs` (win-unpacked) | **10 passed, 0 failed** | launches the real `Skill Router.exe`, records ports, serves the UI through the shell, sign-in sets a session cookie, first-run gate enforced, password change + authenticated read, CSRF still enforced, engine version reported, clean quit with **no orphaned child processes** |
| 31 | **Installed app end to end** | silent `/S` install, then `verify-packaged.mjs --exe "%LOCALAPPDATA%\Programs\Skill Router\Skill Router.exe"` | **10 passed, 0 failed**; installed to `%LOCALAPPDATA%\Programs\Skill Router`, uninstaller + shortcuts present | the *installed* payload behaves identically to the build output |
| 32 | **Outbound policy tests** | `python -m pytest backend/tests/test_outbound_policy.py -q` | **9 passed** | fake-IP detection, `auto`/`allow`/`deny`/override precedence, refusal message names the setting, and loopback/metadata/RFC1918 stay blocked even with the exemption on |
| 33 | **Reported defect reproduced + fixed** | `node desktop/scripts/verify-outbound.mjs` (installed app) | **7 passed, 0 failed** | the user's exact scenario: provider + credential for `open.bigmodel.cn`, then a **real** verification request → reaches the provider (`invalid` for a fake key, i.e. a genuine 401 rather than a policy error); a second host (`api.deepseek.com`) also reachable; loopback still `private_target_blocked`; metadata still refused and now reported as `blocked`, not `unreachable` |
| 34 | **Agent model configured + real turn** | `agent.model` written for the installed app, then a run through `agent_runtime.run_agent` against the app's own sidecar | model resolved `anthropic / glm-5.3-flash`; run `run_65f31d96…` **succeeded**, 93 events, `platform.list_providers` executed with no tool error, answer quoted the real provider row | a configured model produces a real, tool-using agent turn through the production path |
| 35 | **Adapter inference from the protocol** | `pytest backend/tests/test_agent_adapter.py -k engine_provider_id`, then `desktop/scripts/verify-adapter-inference.mjs` on the installed app | unit cases pass; the shipped binary maps `glm` + `…/api/anthropic` to `anthropic` **with no `agent.model` setting**, i.e. auto-detection alone | a provider named "GLM" is no longer sent OpenAI-shaped requests (which surfaced as a 404 that looked like a bad key) |
| 36 | **Model discovery against the real provider** | `desktop/scripts/verify-models-endpoint.mjs` (installed app + the account's real GLM key) | **5 passed, 0 failed** — `POST /api/admin/providers/{id}/models` returned **11 models** from `…/api/anthropic/v1/models`, including `glm-5.3-flash` | the Settings → "Fetch models" button answers with ids the provider actually serves |
| 37 | **Regression sweep after the change** | backend suite, `desktop/ npm test`, `sidecar/ node --test`, `verify-packaged.mjs` (installed), `verify-outbound.mjs` | **202**, **12**, **43**, **10**, **7** — all passed | the agent-model work did not disturb the shell, the engine or the outbound policy |
| 38 | **Agent panel UI, measured in Electron** | `npx electron desktop/scripts/verify-panel-ui.mjs` (installed app, throwaway data dir) | **9 passed, 0 failed** | the session tab strip exists and `+` adds a tab (`0 → 1`, active); the composer is sent through the real Send button; with a genuinely long transcript (`scrollHeight 1003` vs `clientHeight 303`) the composer's top stays at **698 → 698 px** and stays inside the panel while the transcript scrolls; the tab strip stays at `top=65px` |
| 39 | **Markdown rendering** | `node desktop/scripts/verify-markdown.mjs` (esbuild + `react-dom/server`) | **20 passed, 0 failed** | headings, bold/italic, lists, blockquote, inline code, fenced code via `CodeBlock`, GFM tables, task lists, link targets; and hostile input: `javascript:`/`data:` URLs dropped, raw `<script>`/`onerror` arrive as escaped text, unterminated fences don't throw |
| 40 | **A real model answer, rendered** | `python scripts/verify_markdown_live.py` (installed app + the existing encrypted GLM credential) | **13 passed, 0 failed** | the model answered with `## Findings`, bold, a json fence and a table; that exact text renders to `<h2>/<strong>/<pre>/<table>` with no Markdown leaking as literal text. The probe reuses the user's keyring file, so the secret is never decrypted outside the app |
| 41 | **One-command desktop build** | `.\build-desktop.ps1` (no flags), then `-Fast`, then `-Fast -Install -Verify`, then `-Fast -Portable -Verify` | all four **pass**: 547.8 s / 212.7 s / install+verify / 102.6 s; installer `141.9 MB` rebuilt each time; the packaged app's own check reported **10 passed, 0 failed** | the pipeline runs unattended from the repo root. Three defects were found and fixed *by running it*: `[int]'v22'` cast on `node --version`, electron-builder failing with `Access is denied` while a running instance locked `dist-desktop` (the script now closes it), and `*>` writing UTF-16 logs that every other tool read as binary (now UTF-8 via cmd redirection) |
| 42 | **PowerShell scripts parse** | `powershell -File scripts\check-ps1.ps1 -Path <file>` for all seven | **PARSE OK** ×7 (build-desktop 482 lines, dev 202, smoke 286, backup 122, restore 145, check-ps1 28, show-help 10) | the root script and the existing operations scripts are syntactically valid on Windows PowerShell 5.1 |
| 43 | **Overview connect guide, commands executed** | `node desktop/scripts/verify-connect-guide.mjs` — imports the same `frontend/lib/connect-guide.ts` the card renders, then runs each step in PowerShell against a throwaway installed instance | **21 passed, 0 failed** | `/api/meta` reports a CLI path that exists (the visible `resources/runtime/installer` copy, not a PyInstaller internal folder); no step claims `npx` (the package is not published); step 1 sets the variable; step 2 writes `~/.codex/skills/<skill>/SKILL.md` + the config and the SKILL carries no token; step 3 exits 0, names the agent and lists the capability by slug; `verify` **without** `--user` fails with exit 2 and the actionable message; the optional one-liner returns `status=succeeded` with the echoed result. `~/.codex` verified clean afterwards |
| 44 | **`Expect: 100-continue` through the proxy** | `node --test desktop/test/proxy-expect.test.mjs` | **1 passed** | a raw HTTP client sending `Expect: 100-continue` (what PowerShell always does) gets **200**, the backend never sees the header, and the body still arrives. Before the fix the same request produced a **502**, which is what broke the guide's one-liner |
| 45 | **One command, run by the "agent"** | `node desktop/scripts/verify-one-command.mjs` — the platform mints the command via `POST /api/admin/connect/command`, then the script runs **that exact string** in PowerShell | **22 passed, 0 failed** | the command carries `install-self`, this instance's URL and a scoped token; running it exits 0, reports `Detected agent: codex-skills`, installs `~/.codex/skills/skill-router-connected-agent/SKILL.md` + `~/.codex/.skill-router.json`; the token is not in the SKILL; the SKILL lists the granted capability and `/api/v1`; the minted token performs a real invocation (`status=succeeded`); minting a second command revokes the first and it answers **401** immediately |
| 46 | **The one-command card, in the DOM** | `npx electron desktop/scripts/verify-overview-guide.mjs` | **24 passed, 0 failed** | one action (`Create & copy command`), disabled with a stated reason while nothing is published; a **token picker** appears with `Create a new token` preselected when none exists and an **existing token preselected** once one does (2 options), labelled for a token and warning that the secret is re-issued; the click renders the command with `install-self`, the platform URL, the embedded token and a Copy button, and replaces the picker with the command view; the old three-step instructions and the target picker are **absent** |
| 47 | **Choosing among tokens** | `node desktop/scripts/verify-one-command.mjs` (token-selection section) | **33 passed, 0 failed** overall | with two tokens the picker lists both and preselects one; building a command for a *chosen* token keeps that token id and its scopes, reports `rotated_existing: true`, and the response warns that the previous secret stopped working — verified by the old secret answering **401** and the re-issued one **200** |
| 48 | **Why an agent sees zero capabilities** | `node desktop/scripts/verify-capability-visibility.mjs` (installed build) + `pytest backend/tests/test_capability_visibility.py` | **13 passed, 0 failed** live; **3 passed** unit | with a draft capability present, `/api/v1/capabilities` returns `count: 0` **plus an `empty_hint`** naming both possible causes (nothing published / outside scope) and a note on `?refresh=1`, without leaking the draft's name; the admin status endpoint reports the draft with its concrete blockers (`version 1 is still draft`, `config is incomplete (request.url, response.extract.result)`, `no passing test`) and a next step; after publishing a tested capability the same token sees `count: 1` and can invoke it successfully |
| 49 | **Nothing published is a warning, not a blocker** | `npx electron desktop/scripts/verify-overview-guide.mjs` | **26 passed, 0 failed** | the connect button stays enabled with zero published capabilities (a grey button with no way forward was the reported bug); the card warns what the skill will contain and how to refresh later (`update`) |


**Not verified even now**: a successful *mid-run abort* (the model finished in 1.7 s before the stop landed, so the honest result was `already_finished`; the adapter's stop contract is covered by unit tests), the vendor CLIs loading the installed SKILL (files verified, CLIs not driven), Docker images, a third-party MCP client (curl-shaped requests only), and every provider whose credential was not supplied.

---

## 2. Backend suite (checks 1-4)

```bash
python -m pytest backend/tests -q
```

Files and what each one is for (`docs/CONTRACT.md` §12 requires the first six):

| File | Proves |
|---|---|
| `test_foundation.py` | crypto, passwords, sessions, CSRF/origin middleware, token hashing, redaction, throttling |
| `test_api_contract.py` | every admin route rejects anonymous calls with the envelope; happy paths for providers/credentials/capabilities/tokens |
| `test_execution.py` | sync success/failure, unsupported parameter, idempotency dedupe, timeout → `uncertain`, no failover after output |
| `test_tasks.py` | lease recovery, cancel truthfulness, restart behaviour |
| `test_skills.py` | import validation, dependency impact, export contains no secrets |
| `test_engine_upgrade.py` | a failed candidate keeps the current release; a failed activation rolls back |
| `test_mcp.py` | protocol negotiation, tool naming/schemas, JSON-RPC error shapes, session headers, transport auth, every `tools/call` going through `execution.invoke` |
| `test_external_api.py` | `/api/v1` visibility rules (granted + published only), ETag/304, invocation shape |
| `test_capabilities_api.py` | capability config validation and publishing gates |
| `test_agent_adapter.py` | the backend↔sidecar adapter, including the two-sequence-space guard |
| `test_acceptance_recovery.py` | a failed engine activation leaves capability calls working |

**Crucial caveat:** every outbound provider HTTP call in this suite is a **fake**
(`GuardedResponse` / `FakeUpstream`). These tests prove platform behaviour (routing,
limits, records, error mapping, redaction). They do **not** prove that any real
provider accepts our requests — that needs a real key (§9).

Migrations from zero (check 3) is exactly what the container does on first boot:

```bash
cd backend
SR_DATABASE_PATH=/tmp/fresh.db SR_DATA_DIR=/tmp/sr python -c \
  "from app.core.db import apply_migrations; print(apply_migrations())"
# ['0001_init.sql']
```

The CI job `migrations` additionally asserts that all 27 application tables plus
`schema_migrations` exist afterwards.

`doctor` (check 4) is expected to exit non-zero until the first password is changed:

```
{"ok": false, "checks": [ …
  {"check": "initial_password_changed", "ok": false, "detail": "first sign-in must change the password"} …]}
```

That is the tool doing its job. On a deployment that has been through onboarding, all
checks are `true` and it exits `0`.

---

## 3. Sidecar suite (check 5)

```bash
cd sidecar
npm run build          # tsc, dist/
node --test "test/*.test.mjs"
# tests 43 / pass 43 / fail 0
```

Proves: `AgentEvent` mapping (deltas, tool calls, retries, compaction, unknown events
counted not fatal), `EventBus` sequencing and replay gaps, outcome classification (a
resolved promise with no completed turn is a **failure**), SSE replay from
`Last-Event-ID`, bearer auth on everything except `/health`, tool-ceiling enforcement
(`bash` cannot even be named), release/tag parsing, and tool-dispatch results.

Not proven: a successful model turn (needs a provider key), and therefore tool calls
emitted by a real model. The tool callback path itself was exercised directly.

---

## 4. Frontend (checks 6-8)

```bash
cd frontend
npm run lint         # eslint app components lib --max-warnings=0
npm run typecheck    # tsc --noEmit
npm run build        # next build (output: standalone)
```

Recorded build result (clean tree):

```
✓ Compiled successfully
Route (app)                              Size  First Load JS
┌ ○ /                                  8.24 kB         138 kB
├ ○ /access                            9.43 kB         146 kB
├ ○ /account                           6.26 kB         136 kB
├ ○ /capabilities                      8.01 kB         167 kB
├ ƒ /capabilities/[id]                 26.7 kB         160 kB
├ ○ /login                             9.08 kB         130 kB
├ ○ /providers                         3.64 kB         167 kB
├ ƒ /providers/[id]                    9.18 kB         172 kB
├ ○ /records                           7.87 kB         136 kB
├ ƒ /records/[id]                      10.6 kB         139 kB
├ ○ /settings                          7.02 kB         138 kB
├ ○ /settings/engine                   9.17 kB         141 kB
├ ○ /skills                            3.73 kB         148 kB
└ ƒ /skills/[id]                       13.1 kB         157 kB

.next/standalone/server.js: present
```

**Known local quirk (reported, not fixed here — `frontend/` belongs to another owner):**
a **second** `next build` over an existing `.next` fails on Windows with
`ENOENT … .next/routes-manifest.json -> .next/standalone/.next/routes-manifest.json`
(or `.next/export/500.html -> .next/server/pages/500.html`). Next.js 15.5.28.
Workaround: delete `frontend/.next` before rebuilding (`rmdir /s /q frontend\.next`).
CI and the Dockerfile are unaffected: both build from a clean tree.

Not proven by any of this: that the UI renders correctly in a browser. Lint, types and
a production build do not prove layout, and the browser flows were not driven
headlessly in this delivery.

---

## 5. Installer (checks 9-14)

```bash
cd packages/installer
npm test                        # node --check + node --test "test/*.test.js"
npm pack                        # skill-router-connect-1.0.0.tgz
```

35 tests over four files, all driven through the real CLI in a child process against a
local mock server (`test/mock-server.js`) that reproduces the documented response
shapes:

| File | Covers |
|---|---|
| `install.test.js` | SKILL.md per target (claude-code path, codex managed block, generic), required frontmatter, catalogue content, idempotency, `--force` refusal/replacement, unknown target (exit 2), rejected token (exit 1, nothing written), URL normalisation, `--dir`, `--help`/`--version` |
| `verify.test.js` | whoami+capabilities success, one real invocation via `--capability`/`--input`, failed invocation → exit 1 with the platform's message, rejected token → exit 1, nothing listening → exit 1, invalid `--input` → exit 2, config reuse, token never printed (stdout, stderr and `--json`) |
| `update.test.js` | body really comes from `GET /api/v1/skill`, frontmatter preserved, idempotent, refuses when not installed, never overwrites a foreign file, codex block refreshed in place |
| `uninstall.test.js` | deletes what it wrote, keeps unrelated files and non-empty directories, strips only the managed block, removes the config, no-op when not installed, safe twice, refuses foreign files, refuses paths outside the base dir |

Checks 13 and 14 are the interesting ones, because nothing is mocked:

```
PASS  POST /api/admin/agents → agent created (agent_99e7…)
PASS  POST /api/admin/agents/{id}/tokens → token issued (51 chars, sr_live_)
PASS  GET  /api/v1/whoami → agent=live-verify-bot capabilities=0
PASS  GET  /api/v1/capabilities → count=0 version_token=4f53cda18c2b…
PASS  GET  /api/v1/skill → sha256=9020eb238e37… content=3617 chars includes_credentials=False
installer install  → exit 0, SKILL.md written, token present in file: False
installer verify   → exit 0 (2 checks)
installer update   → exit 0, server sha256 9020eb238e370b7d
# then, with one published capability inserted into the database copy:
PASS  GET /api/v1/capabilities → count=1 slugs=text.summarize
PASS  GET /api/v1/capabilities/text.summarize → v2 tool=text__summarize modes={"sync":true,"async":false,"stream":true}
installer install --force → exit 0, catalogue rendered from the real descriptor
```

The run used a **copy** of `data/skillrouter.db` in `%TEMP%`, with its own
`SR_DATA_DIR` / `SR_DATABASE_PATH` / keyring, so the repository's development database
was never modified. The capability row was inserted directly into the copy as a fixture
(no provider, no credential, an `https://upstream.invalid` URL): it exercises the real
descriptor-building code, and no call is ever made to it.

---

## 6. Live checks and the smoke script (checks 15-18)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 `
  -BaseUrl http://127.0.0.1:8011 -Password <pw> -Token <agent token>
```

Recorded result:

```
PASS api health            skill-router
PASS api ready             database + bootstrap
PASS login                 user=admin
PASS admin overview        counts, health, problems, problems_total, recent_invocations, onboarding
PASS api/v1 whoami         agent=live-verify-bot capabilities=0
PASS api/v1 capabilities   count=0 version_token=4f53cda18c2b...
PASS mcp initialize        protocol=2025-06-18 version=1.0.0
PASS mcp tools/list        0 tool(s)
SKIP web /login            -SkipWeb

Summary: 8 passed, 0 failed, 1 skipped
```

Two behaviours worth knowing, both discovered by running it:

* On a fresh install `/api/admin/overview` answers **`403 password_change_required`**
  (not 409). The script reads the error code and reports SKIP, so a first-run state is
  never shown as a failure and never as a pass.
* A skipped check is printed as `SKIP` and counted separately. `smoke.ps1` exits `0`
  only when there are zero failures — with no token, the agent checks are skipped, and
  the summary says the run was incomplete.

The script covers: health, readiness, login (session cookie), an authenticated admin
read, `/api/v1/whoami`, `/api/v1/capabilities`, MCP `initialize` + `tools/list`
(+ optional `tools/call` with `-CallTool`) and the frontend `/login` page.

Also verified: `scripts/*.ps1` all parse
(`[System.Management.Automation.Language.Parser]::ParseFile`), and both
`docker/docker-compose.yml` and `.github/workflows/ci.yml` parse as YAML.

### 6.1 `scripts/verify_acceptance.py` (the contract acceptance verifier)

A separate, read-only verifier that walks the acceptance checks from
`docs/CONTRACT.md` against a **running** instance and prints a PASS/FAIL/SKIP table
with the request id of every call:

```bash
python scripts/verify_acceptance.py --base-url http://127.0.0.1:8000 \
  --admin-user admin --admin-password '<pw>' [--token sr_live_…] \
  [--capability text.summarize --input '{"prompt":"hello"}']
```

It creates and deletes no platform data (a login session, and one invocation only if
`--capability` is passed). Recorded run — a database copy on a fresh port, admin
password changed first, no `--token`:

```
CHECK                                             RESULT  DETAIL / REQUEST ID
health                                            PASS    service=skill-router version=1.0.0 [req_255a1c9c4328c0b7]
ready                                             PASS    database=ok, bootstrap=ok, queue=ok [req_70f0e03d857aaaa4]
meta                                              PASS    api=1.0.0 env=development initialized=True [req_41c72c5e060216b5]
anonymous admin call is 401 + envelope            PASS    code=unauthenticated [req_c1b8ef55ab11bf77]
admin login + CSRF                                PASS    user=admin must_change_password=False [req_80c21751d96c0ea9]
mutation without x-csrf-token is refused          PASS    code=csrf_failed
mutation with x-csrf-token passes the middleware  PASS    reached the handler (HTTP 422 code=validation_failed) [req_21d8d7bcdae41fdb]
foreign Origin is refused                         PASS    code=origin_rejected
overview                                          PASS    capabilities=0 providers=0 problems=0 [req_511461d3044d2bf8]
provider listing                                  PASS    total=0 shown=0 [req_b44269c2873bd0f0]
credential listing                                PASS    total=0 shown=0 [req_5f52818c7a8cd5a9]
capability listing                                PASS    total=0 shown=0 [req_6ce6677aeb3ac660]
settings                                          PASS    engine adapter=1.0.0 release=None [req_f287c81bb3c1cedc]
external whoami                                   SKIP    no --token supplied
external capabilities                             SKIP    no --token supplied
capabilities ETag                                 SKIP    no --token supplied
external SKILL bundle                             SKIP    no --token supplied
mcp initialize                                    SKIP    no --token supplied
mcp tools/list                                    SKIP    no --token supplied
mcp tools/call                                    SKIP    no --token supplied
task lifecycle read                               SKIP    no --token supplied

total=21  pass=13  fail=0  skip=8
```

Exit code is `1` when any check FAILs and `0` otherwise; SKIPs are not failures, and
the count is printed so an incomplete run is obvious. Pass `--token` to cover the
eight skipped checks: those same calls were verified by hand (see §5, checks 13-15) and
via `scripts/smoke.ps1` (§6).

---

## 7. Docker: what was and was not validated

There is no Docker **daemon** on the build machine (no Docker Desktop / no
privileged pipe), so nothing was built or started. The client side *was* made
real: the standalone `docker.exe` (27.3.1) and the Compose v2 plugin (2.29.7)
were downloaded, and `docker compose config` does not need a daemon:

```text
> docker.exe compose -f docker\docker-compose.yml config --quiet
(no output, exit 0)

> docker.exe compose -f docker\docker-compose.yml config --services
api
web
edge
sidecar
worker
```

So:

| Claim | Status |
|---|---|
| `docker compose config` accepts the file | **run, exit 0** |
| service graph, images, ports and `network_mode` resolve as written | **run**: 5 services, `skill-router-{api,web,sidecar}:1.0.0` + `nginx:1.27-bookworm`, published 8000/8080, `network_mode: service:api` on sidecar + worker |
| `docker build` produces the images | **not run** — no daemon |
| `nginx -t` accepts `docker/nginx.conf` | **not run** (no nginx binary) |
| the services start and talk to each other | **not run** |
| the images run as non-root / healthchecks work | **not run** — reviewed only |
| no literal secret in any delivery file | **run**: scanned for `sr_live_`, `sk-`, `AIza`, `AKIA`, JWT and `password=`/`token=` assignments |
| every `${VAR}` has a default | **run**: `SR_IMAGE_TAG`, `SR_API_PORT`, `SR_EDGE_PORT`, `SR_DATA_VOLUME` |
| SSE locations are unbuffered | **run**: text/structural check on `nginx.conf` (`proxy_buffering off`, `gzip off`, `proxy_read_timeout 3600s` for `/api/stream/`, `/api/mcp`, `/api/v1/invocations`) |
| `X-Forwarded-*` set | **run**: text/structural check |
| the frontend image's standalone assumption holds | **run, indirectly**: a clean local `next build` produces `.next/standalone/server.js` |
| the sidecar cannot be a peer service | **run**: read `sidecar/src/config.ts`, which refuses any non-loopback bind → documented workaround (`network_mode: service:api`) |
| the worker can reach the sidecar | **run (static review, defect fixed)**: the worker had its own namespace with `SR_ARAGON_SIDECAR_URL=127.0.0.1:7317`, which cannot work next to a loopback-only sidecar; it now shares `network_mode: service:api` |

Before trusting the Docker path, run:

```bash
python -c "import yaml;yaml.safe_load(open('docker/docker-compose.yml'))"
docker compose -f docker/docker-compose.yml config
docker run --rm -v "$PWD/docker/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.27-bookworm nginx -t
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
curl -fsS http://localhost:8080/api/health/ready
```

---

## 8. Performance test procedure (4 vCPU / 8 GB / 10 concurrent clients)

**No performance run has been executed. No P95 number is recorded anywhere in this
repository.** The targets below are what the deployment is held to; treat them as
unmeasured until you produce a result with this procedure.

### 8.1 Environment

| Item | Value |
|---|---|
| Machine | 4 vCPU, 8 GB RAM, SSD (the sizing `backend/app/security/passwords.py` was tuned for) |
| Processes | `api` (1 uvicorn worker), `worker` (`SR_WORKER_CONCURRENCY=4`), `web`, `sidecar` |
| Reverse proxy | the bundled nginx (`docker/nginx.conf`), so the SSE settings are part of the measurement |
| Database | SQLite, WAL, on local disk — **not** a network filesystem |
| Dataset | ≥ 10 000 rows in `invocations`, 5 published capabilities, 10 agents × 1 token, 2 providers with 1 healthy credential each |
| Upstream | a **stub** provider you control (a local HTTP server returning a fixed 200 in ~50 ms) so provider latency is constant and the numbers measure Skill Router, not a vendor |
| Data reset | restore the same database snapshot before every run |

Record with every run: `SR_ENVIRONMENT`, `SR_WORKER_CONCURRENCY`, `SR_OUTBOUND_CONCURRENCY`,
uvicorn worker count, and the command line you used.

### 8.2 Endpoint classes and targets

| Class | Endpoints | Load | Target (P95) | Notes |
|---|---|---|---|---|
| A. Trivial read | `GET /api/health`, `GET /api/health/ready` | 10 clients, 60 s, no think time | **< 20 ms** | no DB write; if this is slow, the proxy or the box is the problem |
| B. Authenticated read | `GET /api/v1/whoami`, `GET /api/v1/capabilities` (304 path) | 10 clients, 60 s | **< 50 ms** | includes token hash lookup + descriptor build + sha256 |
| C. Browser read | `GET /api/admin/overview`, `GET /api/admin/invocations?limit=50` | 10 clients, 60 s | **< 150 ms** | the overview aggregates; this is the page users wait for |
| D. Sync invocation | `POST /api/v1/invocations` (stub upstream ~50 ms) | 10 clients, 120 s, no think time | **< 250 ms** P95, **< 500 ms** P99 | the core path: routing, credential decrypt, limits, record write |
| E. Streaming invocation | `POST /api/v1/invocations` with `Accept: text/event-stream` | 10 concurrent streams, 60 s | **time-to-first-event < 300 ms**, no inter-event gap > 2 s | catches proxy buffering; a buffered stream shows up as a huge TTFE |
| F. MCP | `POST /api/mcp tools/list`, then `tools/call` | 10 clients, 60 s | `tools/list` **< 80 ms**, `tools/call` as class D | proves MCP is not a slower side door |
| G. Async task throughput | 200 async invocations, then poll each task id | worker at 4 | **all tasks terminal within 120 s**, no `uncertain` produced by the runner itself | exercises leases, retries, recovery |
| H. Durability under load | class D while the worker runs class G | 10 clients | class D P95 stays **< 350 ms** | the API must not contend with the worker for SQLite |
| I. Auth throttle sanity | 6 bad logins in a row | 1 client | 5th/6th answer `429 too_many_attempts` with `retry_after_seconds` | correctness, not latency |

Latency is measured **at the client** (concurrency 10, keep-alive on), reported as P50,
P95, P99 and error rate. A run is only valid if the error rate is 0 % — a fast run full
of 429s measures the limiter, not the platform. If a target is missed, note which
class, the machine state (CPU/IO during the run) and whether the proxy was in the path.

### 8.3 How to run it

```bash
# 1. stub upstream (any fixed-latency HTTP server), then point a provider/credential at it
# 2. start the stack behind nginx
docker compose --env-file .env -f docker/docker-compose.yml up -d
#    or locally: powershell -File scripts\dev.ps1

# 3. one token, one capability, then the load generator of your choice, e.g.:
#    hey -z 60s -c 10 -H "Authorization: Bearer $T" \
#        https://skillrouter.example.com/api/v1/whoami
#    hey -z 120s -c 10 -m POST -T application/json \
#        -H "Authorization: Bearer $T" \
#        -d '{"capability":"stub.echo","input":{"text":"x"}}' \
#        https://skillrouter.example.com/api/v1/invocations

# 4. for class E verify with curl that events arrive incrementally, not in one burst:
curl -N -X POST https://skillrouter.example.com/api/v1/invocations \
  -H "Authorization: Bearer $T" -H "accept: text/event-stream" \
  -H "content-type: application/json" \
  -d '{"capability":"stub.stream","input":{"text":"x"}}' | while read -r line; do
    echo "$(date +%T.%3N) $line"
done

# 5. class G: confirm nothing is left uncertain
sqlite3 /data/skillrouter.db \
  "SELECT status, COUNT(*) FROM tasks WHERE created_at > datetime('now','-1 hour') GROUP BY status;"
```

There is no load-test script in the repository: the numbers depend on your upstream and
your hardware, so shipping a script with baked-in thresholds would be misleading. What
*is* in the repository is `scripts/smoke.ps1`, which checks correctness, not speed.

---

## 9. What still needs real credentials

Everything below is covered by tests with a **fake** upstream. None of it was executed
against a real provider, because that needs a key and spends money.

| Not verified | Why | How to verify it yourself |
|---|---|---|
| A successful call to a real provider (OpenAI/Anthropic/Google/any gateway) | needs a real key | add the provider + credential, create a capability, **Test** it in the UI (a real call, recorded as evidence) |
| Provider-specific error mapping (`errors.rules`: 401 → `upstream_auth_failed`, 429 → retryable, …) | the rules are only exercised against the fake | point a capability at a wrong key and at a rate-limited endpoint, then read the error code in Records |
| A real streaming response from a vendor | same | `--capability` with `stream: true`, or `curl -N` with `accept: text/event-stream` |
| A real async provider job (submit → poll → result, cancel) | same | configure `async.status`/`async.cancel` against a provider that supports it, then `GET /api/v1/tasks/{id}` and `POST …/cancel` |
| A real spend/usage number (`usage`, `cost.actual_usd`) | depends on the vendor's response body | compare Records against the vendor dashboard after a run |
| A successful AragonAgent model turn, and tool calls emitted by the model | needs an LLM credential | add an `llm` provider credential with `config.model`, open the agent panel, ask it to list providers |
| Claude Code / Codex actually loading the generated SKILL | needs an interactive signed-in session (both CLIs are installed: `claude 2.1.303`, `codex-cli 0.154.0`) | run `claude` in a project that has `.claude/skills/skill-router/SKILL.md` and ask it what it can do |
| An MCP client at all (Claude Code, Inspector, …) | verified with `curl`, not a client | point the client at `/api/mcp` with a bearer header and list tools |
| The Docker path (§7) | Docker is not installed here | see §7 |
| Browser rendering of the seven pages | not driven headlessly | open the UI and walk the flows |

---

## 10. Reproducing all of it

```bat
:: backend
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements.txt -r backend\requirements-dev.txt
.venv\Scripts\python -m compileall -q backend\app
.venv\Scripts\python -m pytest backend\tests -q
cd backend & ..\.venv\Scripts\python -m app.cli init & ..\.venv\Scripts\python -m app.cli doctor & cd ..

:: sidecar
cd sidecar & npm ci & npm run build & node --test "test/*.test.mjs" & cd ..

:: frontend
cd frontend & npm ci & npm run lint & npm run typecheck & npm run build & cd ..

:: installer
cd packages\installer & npm ci & node --test "test/*.test.js" & npm pack & cd ..

:: YAML
python -c "import yaml;yaml.safe_load(open('docker/docker-compose.yml'));yaml.safe_load(open('.github/workflows/ci.yml'));print('yaml ok')"

:: live stack
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 -Password <pw> -Token <agent token>
.venv\Scripts\python scripts\verify_acceptance.py --base-url http://127.0.0.1:8000 --admin-user admin --admin-password <pw> --token <agent token>

:: real providers (needs GEMINI_API_KEY and ZENMUX_API_KEY; --video spends money)
.venv\Scripts\python scripts\verify_real_providers.py --rotate-password --image --video

:: AragonAgent engine (sidecar must be running)
.venv\Scripts\python scripts\verify_agent_engine.py --admin-password <pw>

:: browser path through the Next origin (frontend must be running)
.venv\Scripts\python scripts\verify_browser_path.py <pw>

:: performance baseline (10 concurrent clients by default)
.venv\Scripts\python scripts\perf_check.py --admin-password <pw> --requests 20
```

The async-submit part of `perf_check.py` starts a loopback stub and therefore
needs the API to allowloopback egress, e.g. start it once with
`set SR_OUTBOUND_ALLOWED_HOSTS=127.0.0.1`. Everything else is skipped-free
without it (the script reports the skip reason).

Reports are written to `data/verification/*.json` with timestamps, request ids
and per-step evidence so a reviewer can check the claims instead of trusting them.

### 10.1 What the 2026-10 verification run actually recorded

| Script | Result | Representative evidence |
|---|---|---|
| `verify_real_providers.py --rotate-password --image --video` | 37 passed / 0 failed | Gemini `generateContent` answered `"GEMINI-OK"`; ZenMux chat answered `"SKILL-ROUTER-OK"`; TTS produced `speech.wav` (127 290 B, `audio/wav`); image produced `image.png` (1 604 815 B); video task `task_3bfb7f94…` went `queued → running → succeeded` with `video.mp4` (622 255 B); MCP `tools/call` returned `"MCP-OK"` with a `request_id`; install bundle 21 314 B with no token inside |
| `verify_agent_engine.py` | 12 passed / 0 failed | engine health `@aragon-agent/core@0.2.50`, adapter `1.0.0`, 12 platform tools; run `run_fa717365…` → 14 events, `platform_list_capabilities` called, answer 197 chars, `succeeded`, usage 23 240 in / 398 out |
| `verify_browser_path.py` | 20 passed / 0 failed | login through `:3000`, `/api/admin/*` reads 200, mutation without CSRF → 403, create+delete provider, agent session create/list |
| `perf_check.py --requests 20` | budgets met | reads P95: overview 246 ms, providers 97 ms, capabilities 342 ms, invocations 64 ms, tasks 62 ms, skills 41 ms; async submit platform overhead P95 774 ms |

The same set runs in CI (`.github/workflows/ci.yml`): jobs `backend`, `migrations`,
`frontend`, `sidecar`, `installer` — the installer job also packs the tarball, installs
it in a clean directory and runs `--version`, and uploads the tarball as an artifact.
CI was **not executed on the build machine** (no GitHub runner here); the workflow file
parses, and every command in it was run locally by hand.
