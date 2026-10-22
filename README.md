# Skill Router

One control plane for the AI capabilities an organisation already pays for.

Skill Router keeps the provider credentials, decides which upstream route a call
takes, enforces limits and budgets, records every call, and hands external agents a
**capability** surface — over plain HTTP (`/api/v1`) or MCP (`/api/mcp`) — plus an
installable SKILL file that tells an agent how to use it. It also embeds the
**AragonAgent** engine as an operator console that can inspect and change the
platform through a fixed, audited tool allowlist.

Nothing is simulated: a call either reaches an upstream provider and reports what
happened (`succeeded`, `failed`, or `uncertain` when the outcome is genuinely unknown)
or it is refused with a reason and a next step.

```
                            ┌──────────────────────────────────────────────┐
  browser ────────────────► │  Next.js 15 app (7 pages)                    │
  (session cookie + CSRF)   │  /login / /providers /capabilities /skills    │
                            │  /access /records /settings (+ /account)      │
                            └───────────────┬──────────────────────────────┘
                                            │ relative /api/*  (same origin)
                                            ▼
  external agent ──┐              ┌─────────────────────────────────────────┐
  (bearer token)   ├─────────────►│  FastAPI + SQLite (WAL)                 │
  MCP client ──────┘   /api/v1    │  app/api/{auth,admin_*,external,mcp,    │
                       /api/mcp   │            health,stream}.py            │
                                  │  app/services/{execution,routing,tasks, │
                                  │   credentials,capabilities,skills,      │
                                  │   export,agent_runtime,…}.py            │
                                  │  /data: skillrouter.db, uploads,        │
                                  │   artifacts, backups, master keyring    │
                                  └───────┬───────────────────┬─────────────┘
                                          │ queued work       │ only agent_adapter.py
                                          ▼                   ▼
                                  ┌───────────────┐   ┌──────────────────────────┐
                                  │ worker        │   │ sidecar  127.0.0.1:7317  │
                                  │ app.worker_main│  │ Node 22 + @aragon-agent/ │
                                  │ tasks + jobs  │   │ core (the only importer) │
                                  └───────┬───────┘   └────────────┬─────────────┘
                                          │                        │ platform.* tool
                                          ▼                        │ callbacks (HMAC
                                  ┌───────────────┐                │ run-scoped token)
                                  │ providers     │                └──────────► back
                                  │ (real HTTP)   │                             to FastAPI
                                  └───────────────┘
```

The browser never talks to a provider, and the engine never holds a platform or
provider credential: the API decrypts a provider key only for the duration of one
outbound request, and the engine gets a short-lived token that unlocks exactly one
tool URL for one run.

## The seven pages

| Page | Route | What it is for |
|---|---|---|
| Overview | `/` | Health, current problems, recent calls, onboarding steps, **and the one-command "Connect your agent" card**: pick a token (auto-filled, or create one) and copy the single command an agent runs to install the skill itself |
| Providers | `/providers`, `/providers/[id]` | Upstream services and their credentials |
| Capabilities | `/capabilities`, `/capabilities/[id]` | Config, test, publish, routes per capability |
| SKILLs | `/skills`, `/skills/[id]` | Composed plans (capability + agent steps) for agents to run |
| Access | `/access` | Agents, tokens, scopes, budgets |
| Records | `/records`, `/records/[id]` | Every invocation and async task, with request ids |
| Settings | `/settings`, `/settings/engine` | Runtime settings and the engine upgrade console |

The account page (`/account`) holds the password change that a fresh install
requires, and the floating agent panel (bottom right) runs an AragonAgent session.

## Quick start

### A. Desktop app (Windows installer)

The whole platform — API, UI and agent engine — packaged as one installer. No
Docker, no Python, no terminal.

**Build it with one command** (from the repository root):

```powershell
.\build-desktop.ps1
```

That runs preflight → TypeScript → shell tests → Next.js standalone → PyInstaller
freeze + agent engine → NSIS packaging, and writes
`dist-desktop\SkillRouter-Setup-1.0.0.exe`. Useful variations:

```powershell
.\build-desktop.ps1 -Install -Verify   # build, install it, then run the packaged app's
                                       # end-to-end check (isolated data dir) and stop it
.\build-desktop.ps1 -Portable          # unpacked app only (fast, no installer)
.\build-desktop.ps1 -Fast              # rebuild only what changed
.\build-desktop.ps1 -Clean             # discard previous runtime + output first
.\build-desktop.ps1 -SkipTests         # skip the shell unit tests
```

It stops a running Skill Router first (the portable build locks `dist-desktop`),
checks every tool it needs up front, and writes one UTF-8 log per step to
`build-logs\`. A failure prints the failing command and the tail of its log.

Then install:

```bat
dist-desktop\SkillRouter-Setup-1.0.0.exe
```

It installs to `%LOCALAPPDATA%\Programs\Skill Router`, keeps its data in
`%APPDATA%\Skill Router\` (database, keyring, logs), and starts on first launch
with the same `admin` / `adminjr` bootstrap account and the same
"change the password first" gate. Details, architecture and verification:
[`desktop/README.md`](desktop/README.md).

### B. Local development on Windows

These are the commands used to build and verify this repository (Node 22.18,
Python 3.11.7, npm 10.9.3).

```bat
:: 1. Python virtualenv + backend dependencies
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements.txt -r backend\requirements-dev.txt

:: 2. Frontend and sidecar dependencies
cd frontend && npm ci && cd ..
cd sidecar  && npm ci && npm run build && cd ..

:: 3. Configuration (optional for local use; development defaults are safe)
copy .env.example .env

:: 4. First-run: create the database, keyring and admin account
cd backend
..\.venv\Scripts\python -m app.cli init
..\.venv\Scripts\python -m app.cli doctor
cd ..

:: 5. Start everything (sidecar, API, worker, web) with one command
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1

:: 6. Stop everything it started
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Stop
```

Then open <http://localhost:3000>, sign in with `SR_INITIAL_ADMIN_USERNAME` /
`SR_INITIAL_ADMIN_PASSWORD` (default `admin` / `adminjr`) and change the password.

Smoke-test the running stack:

```bat
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 -Password <your password>
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 -Token <agent token>   :: adds /api/v1 + MCP checks
```

### C. Docker Compose

```bat
copy .env.example .env
:: edit .env: SR_ENVIRONMENT=production, SR_PUBLIC_BASE_URL, SR_TRUSTED_ORIGINS,
::            SR_INITIAL_ADMIN_PASSWORD, SR_ARAGON_SIDECAR_TOKEN (32+ random bytes)
docker compose --env-file .env -f docker\docker-compose.yml up -d --build
docker compose -f docker\docker-compose.yml ps
curl http://localhost:8080/api/health
```

**Docker was not available on the machine that produced this repository**, so the
images were never built and `docker compose config` was never run. The YAML was
parsed and every reference reviewed by hand — see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §2 and
[`docs/VERIFICATION.md`](docs/VERIFICATION.md) §7 for exactly what that does and
does not prove. Treat the Docker path as **unvalidated** until you have run it once.

## First run

1. `python -m app.cli init` applies migrations, creates the keyring and the first
   admin account, and prints the database/keyring paths.
2. Sign in at `/login`. The API blocks every business route with
   **`403 password_change_required`** until the initial password is changed — this is
   deliberate, and it is why a fresh install's Overview page looks empty.
3. Change the password at `/account`. All other sessions are revoked.
4. Add a provider (`/providers`) and a credential for it. The secret is encrypted with
   the master keyring and never returned again.
5. Create a capability (`/capabilities`), fill in the request/response config, run
   **Test** (a real call, recorded as evidence), then **Publish**.
6. Create an agent and an agent token (`/access`), granting the capabilities it may
   call. The token is shown once.
7. Connect an agent:

   ```bat
   npx skill-router-connect install --url https://skillrouter.example.com --token <AGENT_TOKEN> --target claude-code
   npx skill-router-connect verify
   ```

8. Optional: run an AragonAgent session from the floating panel. It needs
   `SR_ARAGON_SIDECAR_TOKEN` on both processes and an ACTIVE LLM credential
   (Settings → or a provider with `category=llm` and `config.model`).

### Preloaded state of this working copy

`data/skillrouter.db` in this checkout is **not empty** — it holds the records
created while verifying the platform against real providers:

* providers `Google Gemini` and `ZenMux`, each with one encrypted credential;
* five published, callable capabilities (text ×2, speech, image, async video);
* one external agent (`Verify external agent`) with a revocable token;
* real invocation/task records, including a completed video job and its mp4.

The admin password was reset through the operator CLI during verification, so it
is **`SkillRouter-Dev!2026` with `must_change_password = 1`**: the first sign-in
will ask you to change it, exactly like a fresh install. To start from a clean
slate instead, delete `data/skillrouter.db` (and its `-wal`/`-shm` files) and run
`python -m app.cli init` from `backend/`.

## Verification status

What was proven on the build machine, and what still needs credentials you must
supply. Details and commands: [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

| Area | Status on this machine |
|---|---|
| Backend test suite | **202 passed, 0 failed** (`python -m pytest backend/tests -q`, 78 s) — outbound provider HTTP is a fake in those tests |
| Sidecar build + tests | **43 passed, 0 failed** (`npm run build`, `node --test "test/*.test.mjs"`) |
| Installer tests | **35 passed, 0 failed**, plus a real `npm pack` → `skill-router-connect-1.0.0.tgz` installed in a clean directory |
| Installer against the real platform | **verified**: `install` / `verify` / `update` ran against a local build of this API (database copy, agent + token created through the admin API), token never present in the SKILL |
| Capability catalogue in the SKILL | **verified** against the real `/api/v1/capabilities` with one published capability inserted into a database copy |
| MCP transport | **verified live**: `initialize` → `2025-06-18`, `tools/list` → 200; protocol details covered by `backend/tests/test_mcp.py` |
| Frontend | `npm run lint` (0 warnings), `npm run typecheck` (clean), `npm run build` from a clean tree (15 routes, `.next/standalone/server.js`) — see VERIFICATION.md §4 for the build/render caveats |
| **Real provider calls (text, speech, image, async video)** | **verified**: Gemini + ZenMux text, Gemini TTS audio (WAV artifact), Gemini image, and a ZenMux async video job that went `queued → running → succeeded` and produced a 622 KB mp4. `scripts/verify_real_providers.py` → **37 passed, 0 failed** |
| **A real AragonAgent model turn** | **verified**: `@aragon-agent/core@0.2.50` through the sidecar, model `google/gemini-2.5-flash` (ZenMux), 14 streamed events, a real `platform.list_capabilities` tool call, run `succeeded`. `scripts/verify_agent_engine.py` → **12 passed, 0 failed** |
| **External agent closed loop** | **verified**: bearer token → `/api/v1/whoami` → discovery (ETag/304) → sync call → async task → artifact download → MCP `tools/call`. Included in the 37 above |
| **Performance (10 concurrent clients)** | **verified on this host**: management reads P95 41–342 ms (budget 500 ms); async submit platform overhead P95 774 ms (budget 1 s). `scripts/perf_check.py` |
| Docker Compose | **config validated** (`docker compose config --quiet` → exit 0, 5 services resolve, `network_mode` correct). Image **builds NOT run** — no Docker daemon on the build machine |
| A successful *mid-run* agent abort | **NOT verified** — the model finished in 1.7 s before the stop landed; the stop contract itself is unit-tested and the UI shows the honest result (`already_finished`) |
| Vendor CLIs loading the installed SKILL | **NOT verified** — the installed files were inspected; Claude Code / Codex were not launched |

## Repository layout

```
backend/     FastAPI app, migrations, tests (202 tests) + desktop_entry.py (frozen entry)
frontend/    Next.js 15 App Router, 7 pages + agent panel
desktop/     Electron shell, packaging scripts, installer config (12 shell tests)
sidecar/     Node service embedding @aragon-agent/core (43 tests)
packages/    installer/ → npm package `skill-router-connect` (35 tests)
docker/      Dockerfiles, docker-compose.yml, nginx.conf
scripts/     dev.ps1, backup.ps1, restore.ps1, smoke.ps1 + verification harnesses
             (verify_real_providers, verify_agent_engine, verify_browser_path,
             verify_acceptance, perf_check)
docs/        CONTRACT.md, ARAGON-INTEGRATION.md, and the docs below
data/        runtime state (database, keyring, uploads, artifacts, verification reports)
dist-desktop/  installer output (SkillRouter-Setup-<version>.exe + win-unpacked)
```

## Documentation

| Document | Read it for |
|---|---|
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Prerequisites, the full `SR_*` env matrix, TLS/reverse proxy, volumes, upgrades, backup/restore, key rotation |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Runbook: rotate key, reset a password, revoke a token, reconcile a task, restore, investigate by request id, disk/retention, disaster cases |
| [`docs/API.md`](docs/API.md) | Authentication, every `/api/v1` endpoint with examples, error codes, streaming, task lifecycle, MCP with JSON-RPC examples |
| [`docs/SKILL-INTEGRATION.md`](docs/SKILL-INTEGRATION.md) | Install/verify/update/uninstall per target, refresh procedure, breaking-change policy, MCP clients |
| [`docs/VERIFICATION.md`](docs/VERIFICATION.md) | Every acceptance check that exists, what it proves, and the 4 vCPU / 8 GB / 10-client performance procedure |
| [`docs/CONTRACT.md`](docs/CONTRACT.md) | The frozen internal interface contract (file ownership, statuses, route shapes) |
| [`docs/ARAGON-INTEGRATION.md`](docs/ARAGON-INTEGRATION.md) | How the engine sidecar works, its trust boundaries, and what was verified |
| [`packages/installer/README.md`](packages/installer/README.md) | The npm package: options, support matrix, tarball install |

## Licence

MIT (see the sidecar and installer package manifests).
