# Skill Router — Desktop (Electron)

The same platform, packaged as a Windows desktop application: one installer, no
Docker, no terminal. It runs the identical FastAPI backend, the identical Next.js
UI, and the identical AragonAgent sidecar — the desktop layer only *supervises*
them and puts them behind a single local origin.

```
┌───────────────────────────── Skill Router.exe (Electron) ─────────────────────────────┐
│                                                                                       │
│  BrowserWindow  ──loads──▶  http://127.0.0.1:<shell>   (the only origin)               │
│                                  │                                                    │
│                                  ├─ /api/*        ──▶  FastAPI  127.0.0.1:<backend>    │
│                                  ├─ /__desktop/*  ──▶  shell metadata (token-gated)     │
│                                  └─ everything    ──▶  Next.js  127.0.0.1:<web>        │
│                                                                                       │
│  Supervisor owns three child processes and restarts/reports them:                      │
│    skillrouter-backend.exe   (PyInstaller build of backend/)                           │
│    node web/server.js        (Next standalone build of frontend/)                      │
│    node sidecar/main.js      (@aragon-agent/core, optional)                            │
└───────────────────────────────────────────────────────────────────────────────────────┘
        data → %APPDATA%\Skill Router\data      logs → %APPDATA%\Skill Router\logs
```

## Why a shell proxy instead of loading the UI server directly

The UI and the API are two servers on ports that are chosen at launch (so a second
instance or an unrelated service on 3000/8000 cannot collide). If the window loaded
the UI server's origin, every `/api` call would be cross-origin: the session cookie
would be third-party and the CSRF double-submit would weaken. The shell publishes
one origin, routes `/api` straight to FastAPI, and forwards everything else to
Next. That is also why the backend is told `SR_TRUSTED_ORIGINS=<shell origin>`:
its cookie/CSRF checks stay exactly as strict as in the Docker deployment.

A static export of the UI was evaluated and rejected: four routes are dynamic
(`/capabilities/[id]`, `/providers/[id]`, `/records/[id]`, `/skills/[id]`) and the
app is data-driven at runtime, so an export would have required a hand-rolled SPA
fallback — a second UI with different behaviour.

## Build

The one-command path, from the repository root:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-desktop.txt   # once per machine
.\build-desktop.ps1                                                       # everything
.\build-desktop.ps1 -Install -Verify                                      # + install + check
```

`build-desktop.ps1` runs preflight → `tsc` → shell tests → Next standalone →
PyInstaller freeze + sidecar → electron-builder, stops a running instance first
(it locks `dist-desktop`), and keeps one UTF-8 log per step in `build-logs\`.
Flags: `-Install`, `-Verify`, `-Portable`, `-Clean`, `-Fast`, `-SkipTests`,
`-AppDataDir <path>`; `Get-Help .\build-desktop.ps1 -Detailed` lists them all.

The equivalent manual steps, if you want them individually:

Prerequisites: Node ≥ 20, Python 3.11 with the project virtualenv at `.venv`,
and PyInstaller in that virtualenv.

```bat
:: once per machine
.venv\Scripts\python -m pip install -r backend\requirements-desktop.txt

:: full build: shell TS → UI runtime → Python runtime → installer
cd desktop
npm install
npm run dist
```

`npm run dist` produces:

```
dist-desktop\SkillRouter-Setup-1.0.0.exe     NSIS installer (per-user, x64)
dist-desktop\win-unpacked\Skill Router.exe   portable/unpacked build
```

Useful sub-steps:

| Command | What it does |
|---|---|
| `npm run build:main` | Type-checks and compiles the Electron main/preload (`src/*.ts` → `dist/`) |
| `npm run build:web` | Builds the Next.js standalone server into `resources/runtime/web` |
| `npm run build:backend` | Freezes FastAPI with PyInstaller into `resources/runtime/backend` and copies the sidecar |
| `npm run pack` | Everything, but produces an unpacked directory (fast, no installer) |
| `npm run smoke` | Starts the three real runtimes with an isolated data dir and checks 12 behaviours |
| `npm test` | Shell unit tests (proxy routing, SSE streaming, health polling, shell token) |
| `npm run dev` | Electron against `http://localhost:3000` (run `npm run dev` in `frontend/` too) |

The icons are generated from source by `python resources/make-icons.py` — no binary
assets are checked in that cannot be rebuilt.

## First launch

1. The shell reserves a port, runs `skillrouter-backend bootstrap` (migrations,
   keyring, admin account) and only then opens the window.
2. Sign in with `admin` / `adminjr` (or the override you set in
   `SR_INITIAL_ADMIN_PASSWORD`). The API refuses every business route with
   `403 password_change_required` until the password is changed — this is the same
   gate the server deployment enforces.
3. Settings → **Desktop app** shows live ports, the engine version, the data/log
   folders, and the tray/startup/engine preferences.

## Where data lives

`%APPDATA%\Skill Router\`

| Path | Contents |
|---|---|
| `data\skillrouter.db` | SQLite database (WAL) — capabilities, credentials, records |
| `data\keys\master.keyring.json` | Master keyring. **Back this up**: without it the encrypted credentials cannot be read |
| `data\uploads`, `data\artifacts` | User uploads and generated media |
| `logs\main.log`, `backend.log`, `web.log`, `sidecar.log` | Operational logs |
| `runtime.json` | Ports and the sidecar token for the last run |
| `desktop-settings.json` | Tray/startup/engine preferences |

Uninstalling keeps this directory (`deleteAppDataOnUninstall: false`), because it
holds the credentials. Delete it manually for a clean reinstall.

Set `SR_DESKTOP_DATA_DIR` to run a second, isolated instance (the smoke test does).

## Troubleshooting

### "No model is configured for the agent"

The agent needs three things before it can run: an **active credential**, on an
**active provider with category `llm`**, and a **model id**. The panel names which
one is missing.

1. **Pick the model in the app.** Settings → **Agent model** lists the active LLM
   credentials, offers **Fetch models** (it asks the provider, so the id you choose
   is one the provider actually serves), and saves through
   `PATCH /api/admin/settings`. The card then shows the resolved
   `provider / model` — that string is what runs.
2. **The protocol adapter is detected, not guessed.** The platform infers
   `anthropic` / `openai` / `google` from the base URL, because a provider's *name*
   does not reveal its protocol: Zhipu's `GLM` speaks the Anthropic Messages API at
   `…/api/anthropic` while its native API at `…/api/paas/v4` is OpenAI-shaped. You
   can override the adapter in the same form.
3. **Verify without the UI** (e.g. on a headless install):

   ```bat
   curl -s -X POST http://127.0.0.1:<shell port>/api/admin/providers/<id>/models ^
        -H "cookie: sr_session=…; sr_csrf=…" -H "x-csrf-token: …"
   ```

If the panel still reports no model after saving, the saved credential was disabled
between save and use — check Providers → Credentials → Verify.

### "resolves to an address inside a private or reserved range"

Outbound calls are guarded so a provider configuration cannot reach this machine,
the local network, or a cloud metadata endpoint. The message names the address it
saw, and there are two distinct situations:

1. **A fake-IP proxy (Clash / Surge / some corporate middleboxes).** These answer
   *every* hostname with an address inside `198.18.0.0/15`. The tell-tale is that
   unrelated public names all resolve into that one narrow range. Desktop builds
   default to `SR_OUTBOUND_FAKE_IP_MODE=auto`, which detects this at startup and
   exempts only that range — so a normal install needs no configuration. Confirm
   what the app decided in **Settings → Desktop app**, or from the API:

   ```bat
   curl -s http://127.0.0.1:<shell port>/api/meta | findstr network_policy
   ```

   `fake_ip_detected: true` and `fake_ip_exemption_active: true` means provider
   calls are allowed. If you need to force it:
   `set SR_OUTBOUND_FAKE_IP_MODE=allow` before launching (or `deny` to keep the
   range blocked regardless).

2. **A genuine internal address** (`10.x`, `192.168.x`, `172.16–31.x`, loopback).
   This is the guard doing its job. If the upstream really is inside your network,
   name it explicitly: `set SR_OUTBOUND_ALLOWED_HOSTS=llm.corp.example`. Never set
   `SR_OUTBOUND_ALLOW_PRIVATE=true` on a shared install.

Loopback, link-local and cloud-metadata addresses are refused in every mode,
including `allow`; there is no setting that opens them.

The same policy governs API capability calls, credential verification, web/repo
imports and route health checks — it is not specific to credentials.

## The agent panel

The floating panel is a fixed-height column, the way a chat client behaves:

```
┌ title ────────────────────────────── ✕ ┐
│ session tabs  [tab][tab][tab]  +  ⌄    │  ← fixed
│ health / session state (when relevant) │  ← fixed
│ run strip (status, elapsed, Stop)      │  ← fixed
│                                        │
│ transcript — the only scrolling area   │  ← scrolls
│                                        │
├────────────────────────────────────────┤
│ composer (Enter sends)                 │  ← fixed
└────────────────────────────────────────┘
```

* **Session tabs**, Cursor-style: each open session is a tab, `+` starts a new one,
  `×` archives it (with a confirmation that states what archiving does — the
  transcript is kept, the tab disappears), and `⌄` lists every session with its run
  count and last activity. `←`/`→`/`Home`/`End` move between tabs.
* **Markdown**: assistant answers render as formatted Markdown — headings, lists,
  tables, task lists, and fenced code through the same `CodeBlock` used elsewhere
  (with its copy button). Inline code is styled inline. Raw HTML in a model answer is
  shown as text, never rendered, and only `http(s)`, `mailto`, `tel` and site-relative
  links survive; `javascript:` and `data:` URLs are dropped.
* **The composer never scrolls away.** It lives in the drawer's footer, outside the
  transcript's scroll container, so long answers and long tool trails cannot push it
  off screen.

## Behaviour worth knowing

* **Closing the window keeps the app running in the tray** (default). The API keeps
  serving external agents. Quit from the tray menu or the app menu.
* **Single instance**: a second launch focuses the existing window instead of
  starting a second stack (two writers on one SQLite file would be a bad idea).
* **The agent engine is optional.** If the sidecar fails, the shell reports it and
  published capabilities keep working — that is a product guarantee, not an
  oversight. Turn it off entirely in Settings → Desktop app.
* **Failures are surfaced, not hidden.** A missing runtime binary, a failed
  bootstrap, a crashed backend (three times in a minute) or a lost renderer each
  produce a dialog with the log path; nothing fails silently into a blank window.
* **Security posture**: `contextIsolation`, `sandbox`, no `nodeIntegration`, no
  `ipcRenderer` passthrough, external links open in the real browser, navigation
  away from the shell origin is blocked, and the preload exposes only three
  actions plus status facts.

## Verification

`npm run smoke` starts the real Python and Node runtimes with a throwaway data
directory and checks twelve things, including: API self-initialises, UI serves
HTML, sign-in returns a session and CSRF token, a mutation without CSRF is refused,
business routes are gated until the password changes, a foreign `Origin` is
rejected, and API + sidecar agree on the engine version.

The shell's own behaviour (routing, SSE streaming, shell-token gating, health
polling, gzip/cookie forwarding) is covered by `npm test`; those tests start two
throw-away HTTP servers and need neither Electron nor Python.

The agent panel itself has two dedicated checks:

```bat
:: Markdown → real HTML elements, plus injection attempts (no browser needed)
node desktop\scripts\verify-markdown.mjs

:: The live panel: tabs, add/archive, and the composer staying pinned while the
:: transcript scrolls — measured from the running DOM inside Electron.
:: (Quit the app first: it is single-instance, so a second launch cannot start.)
npx electron desktop\scripts\verify-panel-ui.mjs

:: A real model answer rendered through the panel's component.
.venv\Scripts\python scripts\verify_markdown_live.py
```

`node scripts/verify-outbound.mjs` starts the **installed** app and reproduces the
"credential cannot reach the provider" report end to end: it creates a provider and
credential for `open.bigmodel.cn`, performs a real verification request, checks a
second unrelated host also works, and confirms loopback plus cloud-metadata targets
are still refused (reported as `blocked`, not `unreachable`).
