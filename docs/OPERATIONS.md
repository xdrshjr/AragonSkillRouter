# Operations runbook

Day-two procedure for a running Skill Router. Each section says what you are about to
do, the exact commands, and how to tell it worked. Commands assume the repository root
and a Windows development host; the Linux equivalents are noted where they differ.

Conventions used below:

* `PY` = `.venv\Scripts\python.exe` from the repository root (Linux: `.venv/bin/python`).
* All CLI commands run from `backend\` so the `app` package resolves.
* Anything done through the UI is marked **UI:** with the page it lives on.
* `SMOKE` = `powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 -Password <pw> -Token <token>`

---

## 0. First 60 seconds of any incident

```bat
:: is the API alive and ready?
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/health/ready

:: is the engine alive?
curl -s http://127.0.0.1:7317/health

:: is the box healthy overall?
cd backend
..\.venv\Scripts\python -m app.cli doctor
```

`/api/health/ready` answers `503` with `status: degraded` and a `checks` block that
names the failing part (database, bootstrap, queue). `doctor` adds the keyring,
credential decryption and cookie settings. Neither leaks a secret.

| Symptom | First move |
|---|---|
| UI loads, everything 500s | `/api/health/ready` → likely database or disk full (§8) |
| Everyone is logged out | session table purged, or `SR_SESSION_*` changed; sign in again |
| Agent calls fail with `no_compatible_route` | no healthy provider/credential: **UI: Providers** |
| Agent calls fail with `rate_limited` | token/budget limits (§3) |
| Agent panel says `engine_unavailable` | sidecar down or token mismatch (§9) |
| A call hangs and the tab shows nothing | SSE is being buffered by a proxy (§10) |

---

## 1. Rotate the master key

**When:** scheduled rotation, or any suspicion that `master.keyring.json` leaked.

```bat
cd backend
..\.venv\Scripts\python -m app.cli rotate-key
```

What it does: adds the next free key id (e.g. `k2`), makes it active, re-encrypts
every credential's ciphertext, prints
`{new_active_key, rewrapped, failed, note}`. Old keys stay in the ring so an older
backup can still be decrypted.

**UI equivalent:** Providers → a credential card → *Rotate master key*
(`POST /api/admin/credentials/rotate-key`), which also writes an audit row.

Then:

1. Copy the **new** keyring somewhere safe, immediately.
2. Restart the API and the worker (`scripts\dev.ps1 -Restart`; compose:
   `docker compose restart api worker`). Both cache settings at boot.
3. Verify: `doctor` shows `master_keyring` ok with the new active id and
   `credentials_decryptable` ok; run `SMOKE`; make one real capability call.
4. Only after a successful restore test, drop the retired key from the ring. Removing
   a key that still decrypts something is unrecoverable.

If `SR_MASTER_KEYS` supplies the keys, the command refuses and prints what to do
(append to the variable, set `SR_MASTER_KEY_ACTIVE`, restart, re-run).

**If a credential fails to decrypt after rotation:** you rotated the ring but not the
ciphertexts, or you restored an old database. `doctor` reports
`credentials_decryptable: false`. Restore the matching keyring — do not delete
credentials, that is what makes the loss permanent.

---

## 2. Reset the admin password

**When:** locked out, forgotten password, or a departed operator.

```bat
cd backend
..\.venv\Scripts\python -m app.cli reset-admin-password --generate
```

Prints a temporary password and **revokes every session**. The account is flagged
`must_change_password`, so the next sign-in must change it. To choose the password
yourself:

```bat
..\.venv\Scripts\python -m app.cli reset-admin-password --password "…"
```

The password policy is enforced (`weak_password` if it fails). There is no e-mail
reset and no "admin cannot log in" API path: this CLI is the only recovery route, and
it must run on the host that holds the database.

Sign-in throttling: 5 failures per 15 minutes → `429 too_many_attempts` with
`retry_after_seconds`. Wait it out, or lower `SR_LOGIN_MAX_ATTEMPTS` temporarily— do
not disable it.

---

## 3. Revoke an agent token

**UI:** Access → Tokens → the token → *Revoke*.

API (admin session + `x-csrf-token`):

```bat
curl -X POST http://127.0.0.1:8000/api/admin/tokens/%TOKEN_ID%/revoke ^
  -H "content-type: application/json" -H "x-csrf-token: %CSRF%" ^
  -b cookies.txt -d "{\"reason\":\"suspected leak\"}"
```

Effect: the very next `/api/v1` or `/api/mcp` call fails with `token_revoked`. The
records of what that token already did stay in `invocations` (they are not deleted, on
purpose).

To find the token id: **UI: Access → Tokens** (search by name), or
`GET /api/admin/tokens?agent_id=agent_…`. The token value itself is not recoverable
from the platform — only a hash is stored, and it is shown once at creation.

**Deprovisioning an agent** = revoke all its tokens (`GET
/api/admin/agents/{id}/tokens`, revoke each) and then disable the agent
(`PATCH /api/admin/agents/{id}` with `status: "disabled"`). Granting is per token
(`scopes`), so a disabled agent's still-valid tokens are the risk — revoke first.

---

## 4. Reconcile a stuck task

**When:** an async capability call stays `queued`/`running`, or a provider finished
but the platform did not see it.

```bat
:: look at it first
curl -s -b cookies.txt http://127.0.0.1:8000/api/admin/tasks/%TASK_ID%
curl -s -b cookies.txt http://127.0.0.1:8000/api/admin/tasks/%TASK_ID%/events

:: force one upstream status poll (audited)
curl -s -X POST -b cookies.txt -H "x-csrf-token: %CSRF%" ^
  http://127.0.0.1:8000/api/admin/tasks/%TASK_ID%/reconcile
```

`reconcile` bypasses the poll schedule and fetches the provider's status once. It is a
no-op for a task already in a terminal state, and it never invents a result: if the
provider says nothing, the task stays as it was.

What the worker does on its own (so you usually do not need to intervene):

* an active task past its deadline → **`uncertain`** with `task_timeout`, never
  `failed`, because the provider may still be working and may still bill;
* a task whose lease expired because a worker died → requeued immediately
  (`SR_WORKER_LEASE_SECONDS`, default 120);
* a permanent config error (e.g. a broken `async.status` URL) → `failed` with the
  exact field named.

**If cancel appears to do nothing:** cancellation is truthful by design. A provider
that accepted the cancel but kept working answers
`{supported: true, canceled: false, cancel_requested: true, upstream_may_continue:
true}`. The task will settle on the next poll. Do not re-submit a paid job before
checking `GET /api/v1/tasks/{id}` once more.

---

## 5. Restore a backup

```bat
:: 1. stop the writers (the restore refuses without them stopped)
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Stop
::    compose: docker compose stop api worker

:: 2. restore (asks for confirmation, keeps the current DB aside)
powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 ^
  -Backup  D:\backups\skillrouter\skillrouter-20261008T091500Z.db ^
  -Keyring E:\secrets\master.keyring.20261008T091500Z.json

:: 3. start again
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

The script ends with `doctor`. Green = done. `credentials_decryptable: false` = the
keyring does not match that database; restore the keyring that was taken with it.

Under the hood: `app.cli restore` checks integrity and that the file looks like a
Skill Router database, renames the current database to
`*.pre-restore-<timestamp>.db` (never deletes it), copies the backup in and re-applies
migrations. If the previous database is still there afterwards, you can always go
back.

---

## 6. Investigate a failed call by request id

The `request_id` (`req_…`) appears in the error envelope, the UI toast, the invocation
record and the audit row — it is the join key across all of them.

1. **Find it.** UI: Records → search the request id. API:

   ```bat
   curl -s -b cookies.txt "http://127.0.0.1:8000/api/admin/invocations?search=req_9f2c1b6a8d3f4e10"
   ```

2. **Read the record.** `GET /api/admin/invocations/{invocation_id}` — status, mode,
   surface (`http`/`mcp`/`stream`), attempts, provider, route reason, HTTP status,
   latency, the error code and the redacted request/response previews.
3. **Read the agent's own view** (only its own calls, and only if you are the token
   holder): `GET /api/v1/invocations/{request_id}`.
4. **Cross-check the audit trail:** `GET /api/admin/audit?search=req_…` shows the
   operator/agent actions around it (`auth.login`, `token.created`, `task.cancel`,
   `agent.tool_called`, …).
5. **Interpret the status:**

   | Status | Means | What to do |
   |---|---|---|
   | `succeeded` | the provider answered and the result validated | nothing |
   | `failed` | refused, or the provider answered with an error | read `error.code` / `error.action` |
   | `uncertain` | the request left the machine and the outcome is unknown | **do not blind-retry a paid call**; check the provider dashboard, then reconcile |
   | `rate_limited` / `concurrency_limited` / `budget_exceeded` | platform limits, nothing was sent upstream | raise the limit or wait |
   | `no_compatible_route` | no healthy provider/credential for that capability | check Providers, credentials status and route health |

6. **Logs vs audit.** They are different things:

   * `GET /api/admin/logs` — process log lines (level, logger, message), for "what did
     the software do". Rotates with the process; no retention guarantee.
   * `GET /api/admin/audit` — the durable, append-only record of *state changes* with
     actor, object, summary, metadata and IP. This is what you show an auditor or use
     after a key rotation.

   Redaction runs before anything is persisted on either path, so a provider key
   cannot appear in a log line or an audit row. If you think you see one, treat it as
   an incident (rotate, then report).

---

## 7. Disk, retention and growth

| Path | Grows with | Control |
|---|---|---|
| `/data/skillrouter.db` | invocations, audit, agent events | archive + `VACUUM` offline |
| `/data/uploads` | user uploads | `SR_MAX_UPLOAD_BYTES`; delete by hand |
| `/data/artifacts` | generated files | `SR_FILE_RETENTION_DAYS` + `app.cli purge` |
| `/data/backups` | every backup | your own retention |
| `/data/engine` | upgrade scratch builds | safe to delete when no upgrade is running |

Daily maintenance (cron / Task Scheduler):

```bat
cd backend
..\.venv\Scripts\python -m app.cli purge
```

It removes expired sessions and artifacts past retention and prints what it removed.

**Disk full is an outage, not a warning:** SQLite cannot write, so the API answers
`500` on mutating routes and the worker stops making progress. Free space first (old
backups, artifacts), then `doctor`. Alert at 80 %.

VACUUM (services stopped, it rewrites the whole file):

```bat
sqlite3 data\skillrouter.db "VACUUM;"
```

---

## 8. Engine (AragonAgent) upgrade and rollback

**UI:** Settings → Engine. API: `POST /api/admin/engine/check`,
`POST /api/admin/engine/releases/{id}/validate`,
`POST /api/admin/engine/releases/{id}/activate`, `POST /api/admin/engine/rollback`.

Rules that matter during an incident:

* A **skipped** validation check (npm registry unreachable, sidecar down, npm missing)
  is recorded as skipped with a reason — never as passed. Do not read a green row as
  "everything was tested".
* Activation is refused (`engine_activation_refused`) when the running sidecar reports
  a different version than the release: the platform will not claim a version it is
  not running.
* Rollback moves the pointer to `previous_release_id`. If the sidecar process was not
  itself restarted onto the old package, you get a warning that the running engine did
  not follow the pointer — follow it up, the pointer and the process disagree.
* A failed candidate **never** touches the active release.

Manual fallback: `sidecar/package.json` pins `@aragon-agent/core`; change it, `npm ci`,
`npm run build`, restart the sidecar, then `curl :7317/health` and confirm
`engine.version` is what you expect.

**Known mismatch (reported, unfixed by the delivery owner):**
`SR_ARAGON_ALIGNED_CORE_VERSION` is `0.2.46` while the sidecar pins and reports
`0.2.50`, so `/api/admin/engine` shows a difference. The API surface is identical;
update the setting when convenient.

---

## 9. Agent panel / sidecar problems

| Symptom | Cause | Fix |
|---|---|---|
| `engine_unavailable` in `/api/admin/agent/health` | sidecar not running | start it; the state is reported, not thrown |
| `401 unauthenticated` from the sidecar | `SR_ARAGON_SIDECAR_TOKEN` differs between API and sidecar | set the same value, restart both |
| `409 engine_model_unconfigured` | no `agent.model` setting and no ACTIVE LLM credential with `config.model` | add an LLM credential, or PATCH the setting the error names |
| run status `failed` with `provider_error` | the model endpoint refused | check the provider base URL and key; the message is the provider's |
| run `failed` with `engine_stream_ended` | stream closed with no terminal event | read `agent_runs.error`; the outcome is genuinely unknown |
| stop button reports `settled: false` | the loop did not go idle within the ceiling | the run is still doing something; read `may_continue_upstream` |

The tool surface is a fixed allowlist of 12 `platform.*` tools. `bash`, file, code-exec
and web tools cannot be requested at all — a request naming one is refused with
`tool_not_allowed` before any engine is constructed. Mutating tools additionally
require `agent.auto_execute.enabled` and are budgeted per run
(`max_calls`/`max_minutes`/`max_cost_usd`).

---

## 10. Streaming and proxying

Agent runs and streamed capability calls are Server-Sent Events. If the UI shows a
spinner forever while the API log shows a completed run, something is buffering:

* nginx: `proxy_buffering off` for `/api/stream/`, `/api/mcp` and
  `/api/v1/invocations`; `gzip off`; `proxy_read_timeout` ≥ the longest run
  (`docker/nginx.conf` uses 3600 s).
* The API already sends `x-accel-buffering: no`.
* Cloud load balancers have their own idle timeouts (often 60 s). Raise them, or the
  connection dies silently mid-run.
* `Last-Event-ID` resumption is supported on `/api/stream/agent/{run_id}`
  (`?since=` also works). A reconnect after a dropped connection should replay, not
  restart.

---

## 11. Disaster scenarios

| Scenario | Response |
|---|---|
| **Database file lost, keyring intact** | Restore the newest backup (§5). Calls made after the backup are gone; audit rows included. Tell the operators whose calls vanished. |
| **Keyring lost, database intact** | Credentials are unreadable — permanently. `doctor` shows `credentials_decryptable: false`. Create the keyring anew (`python -m app.cli init` will make one), then **re-enter every provider credential by hand**. Rotate the provider keys at the vendor too, since you cannot prove the old ones were not exposed. |
| **Both lost** | Rebuild: fresh `init`, re-add providers/credentials/capabilities/agents/tokens. Accepted agent tokens are gone, so every consumer must re-run `skill-router-connect install`. |
| **Master key leaked** | Rotate the ring (§1), then rotate the *provider* keys at each vendor (the platform keyring only protects what is at rest). Revoke agent tokens issued before the rotation. |
| **Agent token leaked** | Revoke it (§3), then read `invocations` for that agent since the leak window: `GET /api/admin/invocations?agent_id=…`. Check spend, not just errors. |
| **Provider key leaked** | Disable the credential first (**UI: Providers → credential → Disable**), which stops the bleed, then rotate at the vendor, then add the new secret and re-enable. `GET /api/admin/credentials/{id}/impact` lists the capabilities and active tasks that depend on it. |
| **Disk full** | §7. Free space, then check whether the worker lost leases (`doctor` queue depth). |
| **Corrupt database** | `sqlite3 db "PRAGMA integrity_check"`. If it is not `ok`, restore a backup; recovery of a corrupt SQLite file is not a runbook step, it is a data-loss event. |
| **Compromised host** | Assume both the database and the keyring are gone. Rotate every provider key at the vendor, re-build from the repository, restore only code (not data), and re-issue agent tokens. |
| **Lost admin password with no shell access** | There is no API path by design. Get shell access; §2 is the only route. |

---

## 12. Periodic checks

| Cadence | Check |
|---|---|
| Daily | `app.cli purge`; volume usage < 80 %; `/api/health/ready` green |
| Weekly | `doctor`; `SMOKE` with a real token; failed invocations in Records trending? |
| Monthly | Restore the newest backup into a scratch directory and run `doctor` against it |
| Quarterly | Rotate the master key (§1); rotate provider keys; review tokens/agents that are no longer used |
| On every deploy | `SMOKE`; one real capability call; `doctor` |
