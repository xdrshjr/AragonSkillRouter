# Connecting an agent to Skill Router

Two ways, and you can use both at once:

1. **A SKILL file** — `skill-router-connect` writes a markdown document your agent
   reads, containing the endpoints, the capability catalogue with call examples and the
   refresh procedure. The agent calls the platform over HTTP.
2. **MCP** — point the client at `/api/mcp` and every granted capability appears as a
   tool. No local file, no installer.

This document covers both, per target, with what was actually verified.

---

## 1. Get a token first

**UI:** Access → Tokens → *New token*, or:

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8000/api/admin/agents/$AGENT_ID/tokens \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" \
  -d '{"name":"claude-code-laptop","scopes":["text.summarize","image.generate"],"expires_in_days":90}'
```

The token (`sr_live_…`) is displayed **once**. It is stored as a hash; there is no way
to read it back. Grant only the capabilities that agent needs — an empty `scopes` array
means "everything published".

---

## 2. Install the CLI

The package is **not published to npm**. Until it is, use the tarball built from this
repository:

```bash
cd packages/installer
npm pack                       # -> skill-router-connect-1.0.0.tgz
npx ./skill-router-connect-1.0.0.tgz --version
```

or install it into a project:

```bash
npm install /path/to/skill-router-connect-1.0.0.tgz
./node_modules/.bin/skill-router-connect --version
```

Requirements: Node ≥ 18. No runtime dependencies.

---

## 3. Supported targets

Only the targets below exist, and each entry says what was proven and what was not.

| Target | Writes | Verified | Not verified |
|---|---|---|---|
| `codex-skills` | `skills/<skill-name>/SKILL.md` (`--user`: `~/.codex/skills/<skill-name>/SKILL.md`) | layout, generated `SKILL.md`, idempotency and uninstall asserted by `test/codex-skills.test.js`; **end to end against a real platform** by `scripts/verify_codex_connection.py` — install → `verify` (whoami + catalogue) → `verify --capability` (a real invocation succeeded) → `update` twice (second writes nothing) → `uninstall` (directory pruned). Directory name matches the frontmatter `name` | that Codex loads it at startup: the skill file and its content are verified, the Codex process was not driven |
| `claude-code` | `.claude/skills/<skill-name>/SKILL.md` (`--user`: `~/.claude/…`) | file written at that path by the real CLI; frontmatter parses as YAML with `name` / `description` / `allowed-tools`; catalogue + call example + refresh section present; `install`, `verify`, `update` all ran against a **real local API**; `uninstall` removes it and prunes the directories it created | that Claude Code itself loads it (the CLI needs an interactive signed-in session; `claude --version` was `2.1.303` on the build machine) |
| `codex` | `AGENTS.md` (managed block) | block appended to an existing AGENTS.md with surrounding text byte-identical; a second install does not duplicate it; `update` refreshes in place; `uninstall` strips only the block and deletes the file only when the block was all it held | that Codex CLI follows the instructions (`codex-cli 0.154.0` was installed but not driven) |
| `generic` | `SKILL.md` | written next to `.skill-router.json`; `--dir` respected; idempotent; token never in the file | nothing target-specific — this is the "any agent that can read a file" case |

**Which Codex target?** A Codex build that ships a skills directory
(`~/.codex/skills/`, present in Codex 0.163) reads `skills/<name>/SKILL.md` — use
`codex-skills`. Older/AGENTS.md-only builds read instruction files — use `codex`.
`ls ~/.codex/skills` decides it.

Anything else (Cursor, Copilot, custom) → use `generic` and load `SKILL.md` however your
agent loads instructions, or use MCP (§7).

---

## 4. `install-self` — the one command

This is the path the product uses. The Overview card builds the command for you: you pick a
token (a new one is preselected when none exists) and press **Create & copy command**. The
command is on your clipboard immediately. Hand that one line to your agent:

```powershell
node "C:\…\resources\runtime\installer\bin\skill-router-connect.js" install-self --url http://127.0.0.1:59456 --token sr_live_…
```

The agent runs it and answers "installed". What the command does, in order:

1. calls `GET /api/v1/whoami` and `GET /api/v1/capabilities` so the skill describes the
   capabilities this token may actually use;
2. detects the agent it is running inside — `~/.codex` present → Codex, `~/.claude` →
   Claude Code, otherwise `SKILL.md` in the working directory;
3. writes `skills/<skill-name>/SKILL.md` and the connection config there;
4. prints the detected agent, the install path, and the fact that no further setup step
   exists.

`--target` and `--user` are not needed. If the skill already exists it is refreshed
(`install-self` forces an overwrite of its own file); the token never appears in the SKILL.

**Which token goes in the command?** The one you select in the card. A token's secret is
stored only as a hash, so selecting an existing token means the platform **re-issues its
secret** to place it in the command: the agent, scopes and expiry stay the same, and the
previous secret stops working. That is why the card warns before you click and why the
install must be re-run in the agent that was already using that token. Selecting
`Create a new token` avoids touching anything that already runs.

Later, when the user asks the agent about Skill Router capabilities, the agent re-reads
the SKILL and follows it: discover with `GET /api/v1/capabilities`, call with
`POST /api/v1/invocations`, using the token in the connection config.

## 4b. `install` — explicit target

When you want to place the skill somewhere specific (CI, a per-project install, a
non-standard agent directory), use `install` with explicit flags:

```bash
npx skill-router-connect install \
  --url https://skillrouter.example.com \
  --token sr_live_… \
  --target claude-code
```

| Option | Meaning |
|---|---|
| `--url` | platform base URL; `https://host`, `http://host:8000` and `…/api/v1` are all accepted and normalised |
| `--token` | agent token; stored in `.skill-router.json`, never printed in full |
| `--target` | `codex-skills` \| `claude-code` \| `codex` \| `generic` (default `generic`) |
| `--user` | user scope: `codex-skills` → `~/.codex`, `claude-code` → `~/.claude`, others → `$HOME` |
| `--dir` | base directory (default: current directory) |
| `--project` / `--user` | project scope (cwd, default) or user scope (home directory) |
| `--force` | replace a file that exists and differs |
| `--json` | machine-readable report (token still masked) |

What it does: `GET /api/v1/whoami` + `GET /api/v1/capabilities` with the token (so a bad
token fails here, not later), then writes the SKILL and the connection config.

**Idempotency:** running it again with unchanged platform state writes nothing and
reports `unchanged`, exit `0`. If the platform's capability set changed, it refuses and
tells you to add `--force` — deliberately, so an access change is never silently
rewritten while someone is debugging.

Example output (real run against a local API):

```
Skill Router connection installed

platform      http://127.0.0.1:8011
agent         live-verify-bot (agent_99e7c31b82964dceb498ec9ef56fe3ed)
token         sr_live_...iEHo (stored, never printed in full)
target        claude-code (project scope)
skill         …\agent-project\.claude\skills\skill-router\SKILL.md  [written]
config        …\agent-project\.skill-router.json  [windows-acl]
config perms  POSIX 0600 cannot be set on Windows; the file is protected by the user profile ACL. Treat it as a secret.
capabilities  1 granted (version token 2ad26981...)
```

### 4.1 The generated SKILL

Frontmatter, then instructions, then the catalogue:

```markdown
---
name: skill-router-research-bot
description: "Skill Router connection at https://skillrouter.example.com for agent \"research-bot\": call 2 granted capabilities over HTTP or MCP instead of holding provider keys."
allowed-tools: Bash(curl:*)
---

<!-- managed by skill-router-connect -->

# Skill Router - connection for `research-bot`
...
## 1. Verify the connection
## 2. Refresh the catalogue
## 3. Call a capability
## 4. Capability catalogue      <- one section per capability: schema + curl example + MCP tool name
## 5. Errors you must handle
## 6. Maintenance
```

The file contains **no token**. `allowed-tools: Bash(curl:*)` is our declaration that
the skill needs `curl`; the frontmatter was checked to parse as YAML (a `: ` inside a
plain scalar would break it, so `description` is quoted).

### 4.2 The connection config

`.skill-router.json` in the base directory holds the URL and the token:

```json
{
  "schema": "skill-router.connection/v1",
  "url": "https://skillrouter.example.com",
  "token": "sr_live_…",
  "target": "claude-code",
  "scope": "project",
  "files": [{"path": ".claude/skills/skill-router/SKILL.md", "style": "replace"}],
  "created_dirs": ["…/.claude/skills/skill-router"]
}
```

Written with mode `0600` where POSIX permissions exist. On Windows the CLI reports
`windows-acl` rather than claiming `0600`, because `chmod` there only toggles the
read-only flag. Either way:

> **It is a plaintext secret. Do not commit it.** Add `.skill-router.json` to your
> project's `.gitignore`. `dev.ps1`/the repository `.gitignore` already ignore it here.

---

## 5. `verify` — the connectivity check

```bash
npx skill-router-connect verify
npx skill-router-connect verify --capability text.summarize --input '{"prompt":"hello"}'
npx skill-router-connect verify --json
```

Real output (1 capability granted):

```
Skill Router connectivity check

platform      http://127.0.0.1:8011
agent         live-verify-bot (agent_99e7c31b82964dceb498ec9ef56fe3ed)
token         sr_live_...iEHo
service       skill-router api 1
limits        no limit configured on this token
scopes        (none listed)
capabilities  1 granted (version token 2ad26981...)
                text.summarize  v2  sync,stream

Result: connection OK (2 check(s) passed)
```

With `--capability` it makes one **real** call and prints the request id, latency, task
id (if async) and provider. It exits non-zero with the platform's own message and
actions when the call fails.

Exit codes: `0` ok · `1` platform refused / unreachable / a file would be overwritten ·
`2` bad usage (missing flag, invalid `--input` JSON).

### 5.1 Windows quoting for `--input`

`cmd.exe` strips the inner quotes, so this looks like broken JSON to the CLI:

```bat
:: cmd.exe: DO NOT do this
skill-router-connect verify --capability text.summarize --input '{"prompt":"hi"}'

:: cmd.exe: escape the quotes, or use PowerShell
skill-router-connect verify --capability text.summarize --input "{\"prompt\":\"hi\"}"
```

```powershell
# PowerShell: single quotes are literal
npx skill-router-connect verify --capability text.summarize --input '{"prompt":"hi"}'
```

The CLI fails with `--input is not valid JSON` and exit `2` rather than sending garbage.

---

## 6. `update` and `uninstall`

```bash
npx skill-router-connect update      # refresh from GET /api/v1/skill
npx skill-router-connect uninstall   # remove exactly what install wrote
```

`update` fetches the server-rendered document (`content` of `/api/v1/skill`), drops its
frontmatter and re-adds ours (`allowed-tools` is a local fact the server cannot know),
then writes it to the same target path. Files this tool wrote are refreshed without
`--force`; a file it did **not** write is refused.

`uninstall` removes only paths recorded in `.skill-router.json`, and only when they
carry our marker (or, for `AGENTS.md`, only the delimited block). It then prunes the
directories it created — and only if they are empty. Another file in that directory
keeps it alive, and a `SKILL.md` you wrote yourself is never deleted. Running it twice
is safe: the second run reports "Nothing to uninstall".

---

## 7. Refreshing when the platform changes

Capabilities are data, not code: an updated schema or a newly granted capability does
**not** require reinstalling the package.

```
agent start ──► GET /api/v1/capabilities ──► remember version_token
                        │
        (periodic, or before a call you have not made before)
                        ▼
             GET /api/v1/capabilities (If-None-Match: "<token>")
                        │
       304 Not Modified ┴─ keep using what you have
       200 with a new version_token ──► reload descriptors, then call
```

* `version_token` is the sha256 of the granted descriptors. Compare, don't diff.
* Then refresh the local SKILL so the file matches reality:

  ```bash
  npx skill-router-connect update
  ```

* Never cache a descriptor across a token change — the grant may have been narrowed,
  and a cached descriptor would make the agent ask for something it no longer has.

---

## 8. Breaking-change policy

| Change | What happens | What consumers must do |
|---|---|---|
| New capability, or a new grant | `version_token` changes; new entry in the catalogue | refresh, then call it |
| A compatible addition to an existing version's config | descriptor updated in place (`version` unchanged) | refresh; nothing breaks |
| **A breaking change** (renamed/required input field, different result shape) | ships as a **new version** with a note in the descriptor; the old version stays published until deprecated | pin `"version": <old>` in the request body while you migrate, then move |
| A version is deprecated | `versions.available` shrinks; `version: <deprecated>` answers `409 capability_not_published` | read the error, move to the current version |
| A grant is removed | the capability disappears from `/capabilities` and from MCP `tools/list`; calls answer `capability_not_granted`/`404` | remove the dependency; do not retry |
| A token is revoked or expires | `401 token_revoked` / `token_expired` | issue a new token, `install --force` (or update `.skill-router.json`) |

The rule the platform enforces: **the external surface never changes shape silently.**
`/api/v1` responses are additive; a new shape is a new capability version, not an edit
to a live one. The MCP `tools/list` output follows the same rule, since a tool is just a
capability.

The installer's own exit codes and flags are covered by its test suite; a flag is not
renamed without a major version bump of `skill-router-connect`.

---

## 9. MCP clients instead of a SKILL file

If your client speaks MCP, skip the installer entirely.

| Fact | Value |
|---|---|
| URL | `https://<platform>/api/mcp` |
| Transport | Streamable HTTP (`POST`), JSON-RPC 2.0 |
| Auth | `Authorization: Bearer sr_live_…` |
| Protocol versions | `2025-06-18` (preferred), `2024-11-05` (accepted) |
| Tool naming | capability slug with `.` → `__` (`text.summarize` → `text__summarize`) |
| Session | `Mcp-Session-Id` returned by `initialize`; echo it on later calls; `DELETE` to end |

A configuration in the shape most clients use (Claude Code example — **verify the exact
syntax against your client's documentation**, the shape below was not exercised against
a real client on the build machine):

```json
{
  "mcpServers": {
    "skill-router": {
      "type": "http",
      "url": "https://skillrouter.example.com/api/mcp",
      "headers": { "Authorization": "Bearer ${SKILL_ROUTER_TOKEN}" }
    }
  }
}
```

Prefer an environment variable over a literal token in a committed file — same rule as
`.skill-router.json`.

Verified against the live API (not through a client):

```bash
curl -s -X POST https://skillrouter.example.com/api/mcp \
  -H "Authorization: Bearer $T" -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"curl","version":"1"},"capabilities":{}}}'
# -> protocolVersion 2025-06-18, serverInfo {name: skill-router, version: 1.0.0}

curl -s -X POST https://skillrouter.example.com/api/mcp \
  -H "Authorization: Bearer $T" -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Notes that save an afternoon:

* A missing/revoked token on `/api/mcp` is a **plain HTTP 401** with the error envelope,
  not a JSON-RPC error. Do not parse it as one.
* `GET /api/mcp` is `405` — this endpoint is not a standalone SSE stream.
* Async capabilities return the task id in `structuredContent.task_id`; poll
  `GET /api/v1/tasks/{task_id}` with the same bearer token.
* Every `tools/call` writes the same invocation record as `/api/v1`, so Records shows
  MCP traffic too (`surface: "mcp"`).

Full protocol detail with request/response bodies: `docs/API.md` §10.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `install` exits 1 with `invalid_token` | wrong/copied-with-a-newline token | re-copy from Access → Tokens |
| `install` exits 1 `refusing_to_overwrite` | platform state changed since the last install | `--force` (the file was written by this tool; it says so) |
| `verify` exits 1 `Cannot reach …` | wrong URL, API down, or you used an internal Docker hostname from outside | use the public URL; `curl <url>/api/health` |
| `verify` exits 1 `password_change_required` | not an agent-token problem: the *admin* password is unchanged | unrelated to the installer; that error only appears on browser routes |
| `--input is not valid JSON` | shell quoting (see §5.1) | escape the quotes or use PowerShell |
| Agent ignores the SKILL | the file is not where the agent looks | Claude Code: `.claude/skills/<name>/SKILL.md`; Codex: `AGENTS.md`; otherwise tell the agent to read the file explicitly |
| MCP client sees no tools | token has no grants, or nothing is published | `GET /api/v1/capabilities` with the same token |
| Tool call returns `isError: true` with `capability_not_granted` | the grant changed | `tools/list` again after the grant is fixed |
