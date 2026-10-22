# skill-router-connect

Connects an external agent to a **Skill Router** platform: writes the SKILL file the
agent reads, stores the platform URL and agent token in a private connection
config, and verifies the connection against the live platform.

Zero runtime dependencies (Node built-ins and the global `fetch` only).

```
npx skill-router-connect install --url https://skillrouter.example.com --token $SR_AGENT_TOKEN
npx skill-router-connect verify
npx skill-router-connect update
npx skill-router-connect uninstall
```

## Commands

| Command | What it does |
|---|---|
| `install` | `GET /api/v1/whoami` + `GET /api/v1/capabilities` with your token, then writes the target's SKILL file and `.skill-router.json`. Refuses to overwrite a differing file unless `--force`; a second run with unchanged content writes nothing and exits 0. |
| `verify` | The connectivity check: whoami + capabilities, plus one real `POST /api/v1/invocations` when `--capability`/`--input` are given. Prints a table, exits non-zero with the platform's own advice on failure. |
| `update` | Refreshes the SKILL from `GET /api/v1/skill` (the server-rendered document), keeping the locally required frontmatter. |
| `uninstall` | Removes exactly what `install` wrote, and nothing else. |
| `--help`, `--version` | Usage and version. |

## Options

| Option | Meaning |
|---|---|
| `--url <platform>` | Platform base URL. `https://host`, `http://host:8000`, `…/api/v1` are all accepted and normalised. |
| `--token <token>` | Agent token (`sr_live_…`). Stored in `.skill-router.json`, never printed in full. |
| `--target <t>` | `codex-skills` \| `claude-code` \| `codex` \| `generic` (default `generic`). Not needed with `install-self`, which detects the agent itself. |
| `--dir <path>` | Base directory (default: current directory). |
| `--project` / `--user` | Project scope (current directory, default) or user scope (home directory). |
| `--json` | Machine-readable report on stdout. The token is still masked. |
| `--force` | Replace a file that exists and differs. |
| `--capability <slug>` / `--input <json>` | `verify`: make one real invocation. |
| `--timeout-ms <n>` | Per-request timeout (default 20000; invocations get at least 60000). |

Exit codes: `0` ok, `1` the platform refused / could not be reached / a file would
be overwritten, `2` bad usage.

## What gets written

| Target | SKILL location | Notes |
|---|---|---|
| `codex-skills` | `skills/<skill-name>/SKILL.md` (`--user`: `~/.codex/skills/…`) | For Codex builds that read a skills directory (Codex 0.163 ships `~/.codex/skills/`). The directory name equals the frontmatter `name`. |
| `claude-code` | `.claude/skills/<skill-name>/SKILL.md` (`--user`: `~/.claude/skills/…`) | Frontmatter `name` / `description` / `allowed-tools`, then the capability catalogue and the refresh procedure. |
| `codex` | `AGENTS.md` | A delimited block (`<!-- skill-router:begin -->` … `<!-- skill-router:end -->`). Text outside the markers is never modified. For AGENTS.md-only builds. |
| `generic` | `SKILL.md` | Plain markdown next to the config. |

Plus `.skill-router.json` in the same base directory:

```json
{
  "schema": "skill-router.connection/v1",
  "url": "https://skillrouter.example.com",
  "token": "sr_live_…",
  "target": "generic",
  "files": [{ "path": "SKILL.md", "style": "replace" }]
}
```

It is written with mode `0600` where POSIX permissions exist. On Windows `chmod`
only toggles the read-only flag, so the CLI reports `windows-acl` instead of
claiming `0600`: there the file is protected by your user profile ACL. **Either
way it is a plaintext secret - do not commit it.** Add `.skill-router.json` to the
agent project's `.gitignore`.

The token is never written into the SKILL file, never printed in full and never
included in `--json` output (`token_masked` only).

## Support matrix - what was actually verified

Verification here is against a **local mock server** (`test/mock-server.js`) that
answers the same routes with the same response shapes as
`backend/app/api/external.py`, driven through the real CLI in a child process
(`test/*.test.js`, 35 tests). The vendor CLIs were **not** driven: they need
interactive sign-in, so nothing below claims "Claude Code loaded it".

| Target | Layout | Verified | Not verified |
|---|---|---|---|
| `claude-code` | `.claude/skills/skill-router/SKILL.md` | file written at that path; frontmatter parses as YAML with `name`/`description`/`allowed-tools`; catalogue, call example and refresh section present; uninstall removes it and prunes empty dirs | that Claude Code itself discovers and loads the skill (needs a signed-in CLI session; `claude --version` was `2.1.303` on the build machine) |
| `codex` | `AGENTS.md` managed block | block appended to an existing AGENTS.md without touching surrounding text; a second install does not duplicate it; uninstall strips only the block and deletes the file only when the block was all it held | that Codex CLI follows the instructions (`codex-cli 0.154.0` was installed but not driven) |
| `generic` | `SKILL.md` | written next to `.skill-router.json`; `--dir` respected; idempotent | nothing target-specific |

Because the mock server reproduces the documented contract, not the vendor tools,
the **honest summary is: installer behaviour verified; agent-side loading not
verified.**

## Install from the local tarball

Published to npm: **no**. Until that happens the repository ships the tarball built by
`npm pack` (`packages/installer/skill-router-connect-1.0.0.tgz`; regenerate it after
changing this package — the name carries the version):

```
npm pack                                  # -> skill-router-connect-1.0.0.tgz
npx ./skill-router-connect-1.0.0.tgz install --url https://skillrouter.example.com --token <AGENT_TOKEN>
```

## Tests

```
npm test        # node --check + node --test "test/*.test.js"
```

The suite covers: SKILL generation for all three targets, token never printed in
full, idempotency, `--force`, `update` from `/api/v1/skill`, verify exit codes
(rejected token, unreachable platform, failed invocation, bad `--input`) and
uninstall safety (foreign files, outside-the-base-dir entries, double uninstall).

## See also

- `docs/SKILL-INTEGRATION.md` — per-target integration, refresh procedure, breaking-change policy, MCP alternative.
- `docs/API.md` — every `/api/v1` endpoint and the MCP transport.
