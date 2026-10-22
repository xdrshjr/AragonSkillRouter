# Deployment

How to run Skill Router somewhere real: prerequisites, configuration, TLS, volumes,
upgrades, backup/restore and key rotation. Verified commands are marked; anything
that could not be executed on the build machine says so.

> **Status of the Docker path.** Docker was not installed on the machine that
> produced this repository. The images were never built and `docker compose config`
> was never run. Everything below marked *(not validated)* is a reviewed design, not
> a recorded result. See `docs/VERIFICATION.md` §7.

---

## 1. Prerequisites

| Component | Version | Notes |
|---|---|---|
| Python | 3.11 | `backend/requirements.txt` pins every runtime dependency |
| Node.js | 22 (≥ 18 works) | frontend, sidecar, installer |
| SQLite | bundled with Python | WAL mode, no server to install |
| Disk | ~500 MB + data | `data/` grows with uploads/artifacts; see §8 |
| OS | Windows 10/11 or Linux | verified on Windows 10.0.26200 |
| Docker (optional) | 24+ with compose v2 | **not validated here** |

Two processes are mandatory (`api`, `worker`); the frontend and the AragonAgent
sidecar are optional but the UI needs the frontend and the agent panel needs the
sidecar.

Ports:

| Port | Process | Exposure |
|---|---|---|
| 8000 | API (FastAPI/uvicorn) | behind the reverse proxy; never public directly |
| 3000 | web (Next.js) | behind the reverse proxy |
| 7317 | sidecar (AragonAgent) | **loopback only, by design** (`sidecar/src/config.ts` refuses any other bind) |
| 8080 / 443 | nginx (edge) | the only public listener |

---

## 2. Docker deployment

### 2.1 Files

| File | What it builds |
|---|---|
| `docker/Dockerfile.backend` | `python:3.11-slim-bookworm`, two stages, non-root uid 10001. CMD runs `python -m app.cli init` (migrations + keyring + first admin) and then uvicorn. |
| `docker/Dockerfile.frontend` | `node:22-bookworm-slim`, three stages, standalone Next output, non-root uid 10002, `node server.js`. |
| `docker/Dockerfile.sidecar` | `node:22-bookworm-slim`, compiled `dist/`, production-only `node_modules`, non-root uid 10003. |
| `docker/docker-compose.yml` | services `api`, `worker`, `web`, `sidecar`, `edge`; named volume `sr-data`; `env_file: ../.env`; healthchecks; `restart: unless-stopped`. |
| `docker/nginx.conf` | edge proxy: frontend + `/api`, SSE-safe for `/api/stream/`, `/api/mcp`, `/api/v1/invocations`. |
| `.dockerignore` | keeps `data/`, `.env`, `node_modules`, `.venv`, `packages/` and secrets out of the build context. |

Base images are pinned **by tag**, not by digest. To pin a digest as well:

```bash
docker buildx imagetools inspect python:3.11-slim-bookworm   # copy the digest
# then: FROM python:3.11-slim-bookworm@sha256:...
```

### 2.2 First run

```bash
cp .env.example .env
# mandatory edits: SR_ENVIRONMENT=production, SR_PUBLIC_BASE_URL, SR_TRUSTED_ORIGINS,
#                  SR_INITIAL_ADMIN_PASSWORD, SR_ARAGON_SIDECAR_TOKEN
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps
curl -fsS http://localhost:8080/healthz
curl -fsS http://localhost:8080/api/health/ready
```

*(not validated)* — review these before trusting them: the `next build` stage
(`SR_API_ORIGIN` build arg), the `sidecar` service's shared network namespace, and
the `worker` healthcheck command.

### 2.3 Why the sidecar shares the API's network namespace

`sidecar/src/config.ts` refuses to start unless `SR_ARAGON_SIDECAR_HOST` is
`127.0.0.1`, `localhost` or `::1`:

```
SR_ARAGON_SIDECAR_HOST=0.0.0.0 is refused. This service must stay on loopback;
put a reverse proxy in front if you really need remote access.
```

A container that binds `127.0.0.1` cannot be reached from a peer container, so the
compose file attaches the sidecar with `network_mode: "service:api"`. The sidecar
binds loopback inside the API's namespace, the API reaches it at
`http://127.0.0.1:7317`, and **port 7317 is not published anywhere**. Do not "fix"
this by binding `0.0.0.0`: the process will refuse to start, and if that guard were
removed the engine would be reachable with a bearer token from the whole network.

### 2.4 Offline validation that *was* performed

```bash
python -c "import yaml;yaml.safe_load(open('docker/docker-compose.yml'));print('yaml ok')"
```

plus a hand review of every `${VAR}`, every service key (`env_file`, `volumes`,
`healthcheck`, `restart`, `depends_on`), a secret scan of all delivery files, and a
brace/`location`-block review of `nginx.conf`. `nginx -t` was **not** run (no nginx
binary). Run it yourself before exposing the edge:

```bash
docker run --rm -v "$PWD/docker/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.27-bookworm nginx -t
```

---

## 3. Environment matrix

Every setting is read from the environment or a `.env` file (prefix `SR_`), defined in
`backend/app/core/config.py`. Secrets are never read from the database.

### 3.1 Deployment identity

| Variable | Default | Meaning |
|---|---|---|
| `SR_APP_NAME` | `Skill Router` | Title used in metadata |
| `SR_ENVIRONMENT` | `development` | `development` \| `staging` \| `production`. Drives secure cookies and the `doctor` production checks |
| `SR_PUBLIC_BASE_URL` | `http://localhost:3000` | The URL users and agents reach (behind your TLS proxy). Appears in exported SKILL bundles and examples |
| `SR_BACKEND_BASE_URL` | `http://localhost:8000` | Where the API listens; used for internal links only |

### 3.2 Storage

| Variable | Default | Meaning |
|---|---|---|
| `SR_DATA_DIR` | `<repo>/data` | Persistent root: database, uploads, artifacts, backups, engine workdir |
| `SR_DATABASE_PATH` | `<data>/skillrouter.db` | SQLite file |
| `SR_MASTER_KEY_FILE` | `<data>/keys/master.keyring.json` | Keyring file. Must be outside the database and backed up separately |
| `SR_MASTER_KEYS` | *(unset)* | Container-friendly alternative: `k1:<b64url32>,k2:<b64url32>`. When set, the file is ignored |
| `SR_MASTER_KEY_ACTIVE` | *(unset)* | Key id used for new encryptions; with `SR_MASTER_KEYS` only |

### 3.3 Bootstrap and sessions

| Variable | Default | Meaning |
|---|---|---|
| `SR_INITIAL_ADMIN_USERNAME` | `admin` | Created once, only while no user exists |
| `SR_INITIAL_ADMIN_PASSWORD` | `adminjr` | The first sign-in must change it (`403 password_change_required` until then). **Change it before first boot** |
| `SR_SESSION_COOKIE_NAME` | `sr_session` | HTTP-only session cookie |
| `SR_CSRF_COOKIE_NAME` | `sr_csrf` | Readable cookie the SPA echoes in `x-csrf-token` |
| `SR_SESSION_TTL_MINUTES` | `480` | Absolute session lifetime |
| `SR_SESSION_IDLE_TIMEOUT_MINUTES` | `120` | Idle timeout |
| `SR_COOKIE_SECURE` | *(auto)* | `true` in production. Set explicitly if you terminate TLS elsewhere |
| `SR_COOKIE_DOMAIN` | *(unset)* | Set only for a shared parent domain |
| `SR_TRUSTED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Origin allowlist. Mutating `/api` calls from any other origin are rejected with `origin_rejected`. **Set this to your public URL in production** |

### 3.4 Login throttling

| Variable | Default | Meaning |
|---|---|---|
| `SR_LOGIN_MAX_ATTEMPTS` | `5` | Failures per window before a lockout |
| `SR_LOGIN_WINDOW_MINUTES` | `15` | Window length |
| `SR_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration (`429 too_many_attempts`) |

### 3.5 Outbound network policy

| Variable | Default | Meaning |
|---|---|---|
| `SR_OUTBOUND_ALLOWED_HOSTS` | *(empty)* | Comma list of explicitly allowed hosts (needed for private/internal upstreams) |
| `SR_OUTBOUND_ALLOW_PRIVATE` | `false` | Allow private/loopback upstreams generally. Leave `false` in production |
| `SR_OUTBOUND_FAKE_IP_MODE` | `auto` | How to treat the `198.18.0.0/15` range that fake-IP proxies (Clash/Surge) return for *every* hostname. `auto` detects it at startup and exempts only that range; `allow` always exempts; `deny` never does. Loopback, private and metadata addresses stay blocked in all three modes. |
| `SR_OUTBOUND_ALLOW_FAKE_IP` | *(unset)* | Hard override for the range (`true`/`false`); wins over the mode when set |
| `SR_OUTBOUND_TIMEOUT_SECONDS` | `20` | Outbound connect/read timeout |
| `SR_OUTBOUND_MAX_REDIRECTS` | `3` | Redirect ceiling |
| `SR_OUTBOUND_MAX_RESPONSE_BYTES` | `8388608` | Response size ceiling |
| `SR_OUTBOUND_CONCURRENCY` | `8` | Parallel outbound calls |

### 3.6 Uploads, retention and the worker

| Variable | Default | Meaning |
|---|---|---|
| `SR_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Upload ceiling. Keep `client_max_body_size` in `nginx.conf` in step |
| `SR_FILE_RETENTION_DAYS` | `30` | Age at which `python -m app.cli purge` removes artifacts |
| `SR_WORKER_CONCURRENCY` | `4` | Items the worker processes concurrently |
| `SR_WORKER_LEASE_SECONDS` | `120` | Lease length; a dead worker's items are recovered after this |
| `SR_WORKER_POLL_SECONDS` | `1.0` | Idle poll interval |
| `SR_TASK_MAX_ATTEMPTS` | `3` | Async task retry ceiling |
| `SR_TASK_DEFAULT_TIMEOUT_SECONDS` | `300` | Default async task timeout |

### 3.7 AragonAgent engine

| Variable | Default | Meaning |
|---|---|---|
| `SR_ARAGON_SIDECAR_URL` | `http://127.0.0.1:7317` | Where the API finds the sidecar. In compose: `http://127.0.0.1:7317` (shared namespace) |
| `SR_ARAGON_SIDECAR_TOKEN` | *(empty)* | Shared bearer secret. **Required by the sidecar**; the same value in both processes |
| `SR_ARAGON_REPO_URL` | `https://github.com/xdrshjr/AragonAgent.git` | Upstream repo used by `/releases/check` |
| `SR_ARAGON_NPM_PACKAGE` | `@aragon-agent/core` | The embedded engine package |
| `SR_ARAGON_CLI_PACKAGE` | `@aragon-agent/cli` | CLI package tracked for the upgrade console |
| `SR_ARAGON_UPGRADE_CHANNEL` | `stable` | `stable` (tags) \| `branch` (head of `SR_ARAGON_TRACK_BRANCH`) |
| `SR_ARAGON_TRACK_BRANCH` | `main` | Branch used when the channel is `branch` |
| `SR_ARAGON_ALIGNED_COMMIT` | `c8b0e3df…` | Commit the adapter was written against |
| `SR_ARAGON_ALIGNED_CLI_VERSION` | `0.6.34` | CLI version in the compatibility matrix |
| `SR_ARAGON_ALIGNED_CORE_VERSION` | `0.2.46` | Core version in the matrix. **Known mismatch**: the sidecar pins `0.2.50`, so `/api/admin/engine` shows a difference until this moves (reported, not fixed) |
| `SR_ARAGON_UPGRADE_POLICY` | `manual` | `manual` \| `auto` |
| `SR_ARAGON_WORKDIR`, `SR_ARAGON_ENGINE_HOME` | *(unset)* | Override the engine scratch/install directories |

Sidecar-only variables (`sidecar/.env.example`, read by `sidecar/src/config.ts`):
`SR_ARAGON_SIDECAR_HOST`, `SR_ARAGON_SIDECAR_PORT`, `SR_ARAGON_ENGINE_COMMIT`,
`SR_ARAGON_DEFAULT_PROVIDER_ID`, `SR_ARAGON_DEFAULT_MODEL_ID`,
`SR_ARAGON_DEFAULT_BASE_URL`, `SR_ARAGON_MAX_SESSIONS`, `SR_ARAGON_EVENT_BUFFER`,
`SR_ARAGON_TOOL_TIMEOUT_MS`, `SR_ARAGON_LLM_TIMEOUT_MS`, `SR_ARAGON_IDLE_TIMEOUT_MS`,
`SR_ARAGON_MAX_TOKENS`, `SR_ARAGON_MAX_TOOL_OUTPUT_CHARS`, `SR_ARAGON_STOP_SETTLE_MS`,
`SR_ARAGON_RELEASE_TIMEOUT_MS`, `SR_ARAGON_SSE_KEEPALIVE_MS`,
`SR_ARAGON_BODY_LIMIT_BYTES`.

Frontend build-time variable: `SR_API_ORIGIN` (default `http://127.0.0.1:8000`) — read
by `next.config.mjs` when the `/api/:path*` rewrite is built.

### 3.8 Misc

| Variable | Default | Meaning |
|---|---|---|
| `SR_LOG_LEVEL` | `info` | `debug` \| `info` \| `warning` \| `error` |
| `SR_REQUIRE_INITIALIZED` | `true` | Require bootstrap state before serving business routes |

---

## 4. TLS and the reverse proxy

`docker/nginx.conf` already does the hard parts:

* `/api/stream/` — `proxy_buffering off`, `gzip off`, `proxy_read_timeout 3600s`.
  A buffered event stream is indistinguishable from a hung agent run.
* `/api/mcp` — the same treatment: a JSON-RPC POST may answer with SSE.
* `/api/v1/invocations` — explicitly unbuffered, because `Accept: text/event-stream`
  turns that route into a stream.
* `X-Forwarded-For / -Proto / -Host / -Port` and `X-Request-Id` are always set; uvicorn
  runs with `--proxy-headers --forwarded-allow-ips='*'`, so the API builds absolute
  URLs (SKILL bundles, MCP links) from the public origin rather than from the
  container's own host.
* `client_max_body_size 25m` matches `SR_MAX_UPLOAD_BYTES`.

### 4.1 Terminating TLS here

Add to the `http {}` block in `docker/nginx.conf`:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name skillrouter.example.com;

    ssl_certificate     /etc/nginx/tls/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;

    # HSTS only once the certificate chain is correct and you are sure about it.
    # add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # ... the same location blocks as the :8080 server, or use `include`.
}

server {
    listen 8080;
    server_name _;
    return 301 https://$host$request_uri;
}
```

Mount the certificate directory read-only (`- ./tls:/etc/nginx/tls:ro`) and never bake
a key into an image.

### 4.2 Production settings checklist

```dotenv
SR_ENVIRONMENT=production
SR_PUBLIC_BASE_URL=https://skillrouter.example.com
SR_TRUSTED_ORIGINS=https://skillrouter.example.com
SR_COOKIE_SECURE=true
SR_INITIAL_ADMIN_PASSWORD=<strong, one-time>
SR_ARAGON_SIDECAR_TOKEN=<32+ random bytes>
```

`python -m app.cli doctor` fails in production when `SR_COOKIE_SECURE` is not true.

---

## 5. Persistent volumes

Everything stateful lives under `SR_DATA_DIR` (`/data` in the containers):

```
/data/skillrouter.db            the database (WAL: also -wal and -shm)
/data/keys/master.keyring.json  the master keyring  ← back this up separately
/data/uploads/                  user uploads
/data/artifacts/                generated files served through /api/v1/files/{id}
/data/backups/                  `app.cli backup` output
/data/engine/                   engine upgrade scratch space
```

In compose this is the named volume `sr-data` (override the name with
`SR_DATA_VOLUME`). On a single host, a bind mount is usually better because it makes
backup and inspection obvious:

```yaml
volumes:
  - /srv/skillrouter/data:/data
```

Ownership must be uid/gid `10001` (the API/worker user) in the Docker deployment.

---

## 6. First boot

1. `docker compose up -d` (or `scripts/dev.ps1`). The API's `CMD` runs
   `python -m app.cli init` first: migrations → keyring → first admin. It is
   idempotent, so restarts are safe.
2. `GET /api/health/ready` must answer `200` with `status: ready`.
3. Sign in and change the password. Until then every business route answers
   `403 password_change_required` — expected, not a bug.
4. `python -m app.cli doctor` must be green (`initial_password_changed` is the one
   check that legitimately reports false before step 3).

---

## 7. Healthchecks

| Endpoint | What it proves | Auth |
|---|---|---|
| `GET /api/health` | process is up | none |
| `GET /api/health/ready` | database readable, bootstrap done, queue depth reported; `503` when degraded | none |
| `GET /healthz` (nginx) | edge is up | none |
| `GET /:7317/health` (sidecar) | engine loaded, version reported, `model_configured` | none — deliberately the only unauthenticated sidecar route |

Compose healthchecks: `api` uses `/api/health/ready`, `worker` runs
`python -c "from app.core import db; db.query_scalar('SELECT 1')"`, `web` fetches
`/login`, `sidecar` fetches `/health`, `edge` fetches `/healthz`.

---

## 8. Disk and retention

* Artifacts and uploads are the only unbounded growth. `SR_FILE_RETENTION_DAYS`
  (default 30) is enforced by `python -m app.cli purge`, which also drops expired
  sessions. Run it daily (cron / Task Scheduler):

  ```bat
  cd backend && ..\.venv\Scripts\python -m app.cli purge
  ```

* The SQLite file grows with `invocations`, `audit_logs` and `agent_events`. There is
  no built-in vacuum; after a large purge, `VACUUM` offline (services stopped) to
  reclaim space.
* Backups accumulate in `/data/backups`; prune them on a schedule you control.
* Alert at 80 % volume usage. A full volume makes SQLite fail writes, and the API
  then answers `500` on every mutating route.

---

## 9. Upgrade procedure

### 9.1 Database migration and rollback

Migrations are SQL files in `backend/migrations/`, applied by
`db.apply_migrations()` (recorded in `schema_migrations`). `python -m app.cli init`
and both process entrypoints apply them, so a normal restart migrates.

```bash
# 1. Back up first (always)
python -m app.cli backup --out /data/backups
cp /data/keys/master.keyring.json /safe/place/master.keyring.json

# 2. Record the schema version you are on
sqlite3 /data/skillrouter.db "SELECT version, name FROM schema_migrations ORDER BY version;"

# 3. Deploy the new code and restart (compose example)
docker compose -f docker/docker-compose.yml up -d --build api worker web
```

Rollback is a **database restore**, not a down-migration — there are no down
migrations. Older code against a newer schema is unsupported:

```bash
docker compose stop api worker
python -m app.cli restore /data/backups/skillrouter-<timestamp>.db --force
cp /safe/place/master.keyring.json /data/keys/master.keyring.json
docker compose up -d api worker
```

Verified locally: `python -m app.cli restore <file> --force` keeps the previous
database as `*.pre-restore-<timestamp>.db` and re-applies migrations.

### 9.2 Engine (AragonAgent) upgrade

Use the UI (`/settings/engine`) rather than editing `package.json`:

1. `POST /api/admin/engine/check` — detects the upstream tag/commit
   (`sidecar GET /releases/check`, a real network call; a failure is a `502`, never a
   silent fallback).
2. `POST /api/admin/engine/releases/{id}/validate` — runs the check matrix
   (`release_ref`, `adapter_compatibility`, `npm_available`, `sidecar_reachable`,
   `isolated_build`, `regression_tests`). A **skipped** check is recorded as skipped
   with a reason, never as passed.
3. `POST /api/admin/engine/releases/{id}/activate` — refuses if the running sidecar
   reports a different version than the release.
4. `POST /api/admin/engine/rollback` — returns to `previous_release_id` and warns when
   the running sidecar did not follow the pointer.

An installed engine version is what the sidecar reports at `GET /health`; it is read
from the installed package, never hard-coded.

---

## 10. Backup and restore

```bat
:: backup (database + keyring)
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Out D:\backups\skillrouter -KeyringOut E:\secrets

:: restore (services STOPPED, or the API/worker will fight you)
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Stop
powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -Backup D:\backups\skillrouter\skillrouter-20261008T091500Z.db -Keyring E:\secrets\master.keyring.20261008T091500Z.json
```

Facts that matter:

* `app.cli backup` uses SQLite's online backup API, so it is safe while the platform
  runs. It writes a `.manifest.json` with the migration list.
* **A database without its keyring is unreadable.** Every provider credential is
  AES-GCM encrypted with a key from the ring. Restoring a database against a
  different keyring leaves credentials undecryptable — `doctor` reports
  `credentials_decryptable: false`.
* Uploads and artifacts are not in the backup. Copy `/data/uploads` and
  `/data/artifacts` yourself if you need them.
* Test a restore into a scratch directory once per quarter. An untested backup is a
  hope, not a backup.

---

## 11. Key rotation

Rotating re-encrypts every stored credential under a new key; old keys stay in the
ring so a rollback still works.

**File-based keyring** (default):

```bat
cd backend
..\.venv\Scripts\python -m app.cli rotate-key
```

The command adds the next free key id (e.g. `k2`), makes it active, re-wraps all
credentials and prints a JSON summary. Then:

1. Back up the **new** `master.keyring.json` immediately — it now holds both keys.
2. Restart the API and worker (they cache the settings at boot).
3. Verify: `python -m app.cli doctor` (`master_keyring` and
   `credentials_decryptable` must both be ok) and run `scripts\smoke.ps1`.
4. Retire the old key only after a successful restore test: remove it from
   `keys` in the keyring file, keeping the active one. Removing a key that still
   decrypts a credential makes those credentials unreadable — there is no recovery
   without the key.

**Environment-supplied keys** (`SR_MASTER_KEYS`): `rotate-key` refuses and tells you
what to do — append the new key to the variable, set `SR_MASTER_KEY_ACTIVE` to it,
restart, and run the command again.

The web API equivalent is `POST /api/admin/credentials/rotate-key` (admin session +
CSRF), which does the same re-wrap and writes an audit row.

---

## 12. Scaling notes

The design is a single API + single worker on SQLite. That is deliberate: it is
enough for the documented 10-client target (see `docs/VERIFICATION.md` §8) and
removes a whole class of consistency bugs. What it does not do:

* Multiple API replicas are fine for reads, but SQLite on a network filesystem is
  **not** supported.
* The worker is safe to restart at any time (leases recover), but two workers on the
  same database double the upstream concurrency — set `SR_WORKER_CONCURRENCY`
  accordingly.
* Beyond that, the honest answer is "replace SQLite with a server database first";
  the code isolates SQL behind `app/core/db.py` for that reason.
