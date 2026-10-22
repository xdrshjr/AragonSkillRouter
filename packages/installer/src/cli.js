/**
 * `skill-router-connect` command line.
 *
 * Exit codes are part of the contract (docs/SKILL-INTEGRATION.md):
 *   0  the command did what it said
 *   1  the platform refused, could not be reached, or the file would be overwritten
 *   2  the invocation itself was wrong (bad flag, missing value, bad JSON)
 */

import { existsSync, mkdirSync, readdirSync, rmSync, rmdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

import { getCapabilities, getSkill, getWhoami, invokeCapability } from "./api.js";
import { assertInside, configPath, readConfig, redactedConfig, removeConfig, writeConfig } from "./config.js";
import { ConnectError, InstallerError, UsageError } from "./errors.js";
import { normaliseBaseUrl } from "./http.js";
import { INSTALLER_VERSION, renderFromServerDocument, renderSkillDocument, skillNameOf } from "./render.js";
import { buildArtifacts, detectInstalledAgent, mergeManagedBlock, readArtifact, resolveTarget, stripManagedBlock, TARGETS, TARGET_IDS } from "./targets.js";
import { maskToken, tokenShapeWarning } from "./token.js";

export const PROGRAM = "skill-router-connect";

const VALUE_FLAGS = new Set(["url", "token", "target", "dir", "capability", "input", "timeout-ms"]);
const BOOLEAN_FLAGS = new Set(["project", "user", "json", "force", "help", "version"]);

/**
 * @param {string[]} argv
 * @returns {{command: string, options: Record<string, any>, unknown: string[]}}
 */
export function parseArgs(argv) {
  /** @type {Record<string, any>} */
  const options = {};
  /** @type {string[]} */
  const positional = [];
  /** @type {string[]} */
  const unknown = [];
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "-h") {
      options.help = true;
      continue;
    }
    if (!arg.startsWith("--")) {
      positional.push(arg);
      continue;
    }
    const equals = arg.indexOf("=");
    const name = (equals === -1 ? arg.slice(2) : arg.slice(2, equals)).toLowerCase();
    if (VALUE_FLAGS.has(name)) {
      if (equals !== -1) {
        options[name] = arg.slice(equals + 1);
      } else {
        const next = argv[index + 1];
        if (next === undefined || next.startsWith("--")) {
          throw new UsageError(`--${name} needs a value.`, { actions: [`Example: --${name} <value>`] });
        }
        options[name] = next;
        index += 1;
      }
      continue;
    }
    if (BOOLEAN_FLAGS.has(name)) {
      options[name] = true;
      continue;
    }
    unknown.push(arg);
  }
  const command = positional[0] || (options.version ? "version" : options.help ? "help" : "");
  return { command, options, unknown: [...unknown, ...positional.slice(1).map(String)] };
}

/** @param {number} ms */
function connectionOf(baseUrl, token, ms) {
  const timeoutMs = Number(ms) > 0 ? Number(ms) : undefined;
  return { baseUrl: normaliseBaseUrl(baseUrl), token, timeoutMs };
}

/**
 * Base dir: --dir wins; --user means the target's user directory (Codex:
 * `~/.codex`, Claude Code: `~/.claude`); otherwise the cwd.
 *
 * `--user` is target-aware on purpose: writing `~/AGENTS.md` for Codex would put the
 * file where neither the CLI nor the app looks, and silently do nothing.
 *
 * @param {Record<string, any>} options
 * @param {import("./targets.js").TargetSpec | null} [spec]
 */
export function resolveBaseDir(options, spec = null) {
  if (options.user && options.project) {
    throw new UsageError("--user and --project are mutually exclusive.", {
      actions: ["Use --user for a user-scope install, or --project (the default) for the current directory."],
    });
  }
  if (options.dir) return path.resolve(String(options.dir));
  if (options.user) {
    return spec && spec.userSubdir ? path.join(homedir(), spec.userSubdir) : homedir();
  }
  return process.cwd();
}

/**
 * Find a stored config in user scope without being told the target.
 *
 * `install --user --target codex-skills` writes `~/.codex/.skill-router.json`, while
 * `verify --user` starts from the home directory and therefore finds nothing. Probing
 * the known user-scope directories solves the chicken-and-egg: the target is derived
 * from where a config actually exists, so `verify --user` needs no extra flag.
 *
 * @param {string[]} [known]
 * @returns {{config: any, spec: import("./targets.js").TargetSpec} | null}
 */
export function findUserScopeConfig(known = TARGET_IDS) {
  for (const id of known) {
    const spec = TARGETS[id];
    if (!spec.userSubdir) continue;
    const candidate = path.join(homedir(), spec.userSubdir);
    const config = readConfig(candidate);
    if (config) return { config, spec };
  }
  return null;
}

/**
 * Resolve the target id that applies: explicit flag, else the stored one, else the
 * default.
 * @param {Record<string, any>} options
 * @param {{target?: string} | null} existing
 * @returns {import("./targets.js").TargetSpec}
 */
export function resolveTargetFrom(options, existing) {
  return resolveTarget(String(options.target || (existing && existing.target) || "generic"));
}

/**
 * Work out (target, base dir, stored config) from the flags and what is on disk.
 *
 * One place, used by install/verify/update/uninstall, because getting this wrong is
 * how `verify --user` ended up looking in `$HOME` instead of `~/.codex`.
 *
 * @param {Record<string, any>} options
 * @returns {{spec: import("./targets.js").TargetSpec, baseDir: string, existing: any | null}}
 */
export function resolveScope(options) {
  const peekDir = resolveBaseDir(options);
  let stored = readConfig(peekDir);
  let spec = null;
  if (stored) {
    spec = resolveTargetFrom(options, stored);
  } else if (options.user && !options.target) {
    // User scope with no explicit target: adopt whichever user-scope install exists.
    const found = findUserScopeConfig();
    if (found) {
      stored = found.config;
      spec = options.target ? resolveTarget(String(options.target)) : found.spec;
    }
  }
  if (!spec) spec = resolveTargetFrom(options, stored);
  const baseDir = resolveBaseDir(options, spec);
  const existing = readConfig(baseDir) || stored || null;
  return { spec, baseDir, existing };
}

/**
 * @param {Record<string, any>} options
 * @param {any | null} existing
 */
function resolveConnection(options, existing) {
  const url = options.url || (existing ? existing.url : null);
  const token = options.token || (existing ? existing.token : null);
  if (!url) {
    throw new UsageError("No platform URL. Pass --url <platform>, or run `install` first so it is stored in .skill-router.json.", {
      actions: ["skill-router-connect install --url https://skillrouter.example.com --token <AGENT_TOKEN>"],
    });
  }
  if (!token) {
    throw new UsageError("No agent token. Pass --token <agent token>, or run `install` first so it is stored in .skill-router.json.", {
      actions: ["Issue a token on the platform: Access -> Tokens -> New token."],
    });
  }
  return connectionOf(url, String(token), options["timeout-ms"]);
}

/**
 * Directories that do not exist yet, deepest last - recorded so `uninstall` can
 * prune only what it created.
 * @param {string} baseDir
 * @param {string[]} relPaths
 * @returns {string[]}
 */
function missingDirsFor(baseDir, relPaths) {
  const missing = new Set();
  for (const relPath of relPaths) {
    let dir = path.dirname(path.join(baseDir, relPath));
    while (dir.startsWith(path.resolve(baseDir)) && dir !== path.resolve(baseDir)) {
      if (!existsSync(dir)) missing.add(dir);
      dir = path.dirname(dir);
    }
  }
  return [...missing].sort((a, b) => b.length - a.length);
}

/**
 * Decide what would be written, and refuse the whole command before any file is
 * touched when the answer is "that file is not ours and it would change".
 *
 * @param {string} baseDir
 * @param {{relPath: string, style: string, content: string}[]} artifacts
 * @param {{force?: boolean, allowOwnedOverwrite?: boolean}} [policy]
 * @returns {{relPath: string, fullPath: string, action: "written"|"unchanged", content: string, style: string}[]}
 */
function planWrites(baseDir, artifacts, policy = {}) {
  const force = Boolean(policy.force);
  const allowOwned = Boolean(policy.allowOwnedOverwrite);
  return artifacts.map((artifact) => {
    const fullPath = assertInside(baseDir, artifact.relPath);
    const current = readArtifact(baseDir, artifact.relPath);
    if (artifact.style === "managed-block") {
      const hasBlock = current !== null && current.includes("<!-- skill-router:begin -->");
      const merged = mergeManagedBlock(current, artifact.content);
      if (current === merged) return { relPath: artifact.relPath, fullPath, action: "unchanged", content: merged, style: artifact.style };
      const changed = hasBlock && extractBlock(current).trim() !== artifact.content.trim();
      if (changed && !force && !allowOwned) {
        throw new ConnectError(`${artifact.relPath} already contains a Skill Router block that differs from what the platform returns now.`, {
          code: "refusing_to_overwrite",
          details: { file: fullPath },
          actions: [`Re-run with --force to replace the block in ${artifact.relPath}.`, "Text outside the begin/end markers is never modified."],
        });
      }
      return { relPath: artifact.relPath, fullPath, action: "written", content: merged, style: artifact.style };
    }
    if (current === null) return { relPath: artifact.relPath, fullPath, action: "written", content: artifact.content, style: artifact.style };
    if (current === artifact.content) return { relPath: artifact.relPath, fullPath, action: "unchanged", content: current, style: artifact.style };
    const ours = current.includes("<!-- managed by skill-router-connect -->");
    // `update` may refresh a file this tool owns; `install` always needs --force,
    // because install is also the command that could surprise an operator.
    if (!force && !(allowOwned && ours)) {
      throw new ConnectError(`${artifact.relPath} already exists and differs from the SKILL the platform returns now.`, {
        code: "refusing_to_overwrite",
        details: { file: fullPath, previously_written_by_this_tool: ours },
        actions: [
          `Re-run with --force to replace ${artifact.relPath}.`,
          ours ? "The existing file was written by skill-router-connect; --force is safe." : "That file was not written by this tool - back it up before using --force.",
        ],
      });
    }
    return { relPath: artifact.relPath, fullPath, action: "written", content: artifact.content, style: artifact.style };
  });
}

/**
 * @param {string} text
 * @returns {string}
 */
function extractBlock(text) {
  const begin = text.indexOf("<!-- skill-router:begin -->");
  const end = text.indexOf("<!-- skill-router:end -->");
  if (begin === -1 || end === -1 || end <= begin) return "";
  return text.slice(begin + "<!-- skill-router:begin -->".length, end);
}

/**
 * @param {string} baseDir
 * @param {ReturnType<typeof planWrites>} plan
 * @returns {string[]} directories created
 */
function applyWrites(baseDir, plan) {
  const written = plan.filter((item) => item.action === "written");
  const created = missingDirsFor(baseDir, written.map((item) => item.relPath));
  for (const item of written) {
    mkdirSync(path.dirname(item.fullPath), { recursive: true });
    writeFileSync(item.fullPath, item.content, "utf8");
  }
  return created;
}

// ----------------------------------------------------------------- output ----
/** @param {string} label @param {string} value @param {number} [width] */
function row(label, value, width = 14) {
  return `${label.padEnd(width)}${value}`;
}

/** @param {string[]} lines @returns {number} */
function emit(lines) {
  process.stdout.write(`${lines.join("\n")}\n`);
  return 0;
}

/**
 * @param {any} payload
 * @param {string[]} humanLines
 * @param {boolean} json
 */
function finish(payload, humanLines, json) {
  if (json) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } else {
    process.stdout.write(`${humanLines.join("\n")}\n`);
  }
  return 0;
}

// ---------------------------------------------------------------- commands ---
export function helpText() {
  return [
    `${PROGRAM} ${INSTALLER_VERSION} - connect an external agent to a Skill Router platform`,
    "",
    "Usage:",
    `  ${PROGRAM} install   --url <platform> --token <agent token> [--target <t>] [--dir <path>] [--project|--user] [--force] [--json]`,
    `  ${PROGRAM} install-self --url <platform> --token <agent token>   # one command: detects the agent and installs`,
    `  ${PROGRAM} verify    [--url <platform>] [--token <token>] [--capability <slug> [--input <json>]] [--json]`,
    `  ${PROGRAM} update    [--url <platform>] [--token <token>] [--force] [--json]`,
    `  ${PROGRAM} uninstall [--dir <path>] [--json]`,
    `  ${PROGRAM} --help | --version`,
    "",
    "Options:",
    "  --url <platform>     Base URL of the platform, e.g. https://skillrouter.example.com",
    "  --token <token>      Agent token (sr_live_...). Stored in .skill-router.json, never printed in full.",
    `  --target <t>         ${TARGET_IDS.join(" | ")}   (default: generic)`,
    "  --dir <path>         Base directory to install into (default: current directory)",
    "  --project            Project scope (default): install into the current directory",
    "  --user               User scope: install into the home directory",
    "  --json               Machine-readable output (the token is still masked)",
    "  --force              Replace files that already exist and differ",
    "  --capability <slug>  verify: also make one real invocation",
    "  --input <json>       verify: input object for --capability (default: {})",
    "  --timeout-ms <n>     Per-request timeout (default 20000, invocations 60000)",
    "",
    "Exit codes: 0 ok, 1 platform/file failure, 2 bad usage.",
    "",
    "What it writes:",
    ...TARGET_IDS.map((id) => `  ${id.padEnd(12)} ${TARGETS[id].files.map((file) => file.relPath).join(", ")}`),
    "  plus .skill-router.json (mode 0600) holding the platform URL and token.",
  ].join("\n");
}

/**
 * @param {Record<string, any>} options
 * @returns {Promise<number>}
 */
async function commandInstall(options) {
  // The target decides where `--user` points, so it is resolved before the base dir.
  // `resolveScope` also adopts an existing user-scope install, so re-running install
  // keeps the same target without repeating the flag.
  const { spec, baseDir, existing } = resolveScope(options);
  const connection = resolveConnection(options, existing);
  const json = Boolean(options.json);
  const force = Boolean(options.force);

  const whoami = await getWhoami(connection);
  const catalogue = await getCapabilities(connection, {});
  const document = renderSkillDocument({
    baseUrl: connection.baseUrl,
    whoami,
    capabilities: catalogue.capabilities,
    versionToken: catalogue.version_token,
  });
  // The skill directory is named after the skill, so the folder and the frontmatter
  // `name` agree — some agents resolve one from the other.
  const skillName = skillNameOf(whoami);
  // planWrites throws before anything on disk is touched.
  const plan = planWrites(baseDir, buildArtifacts(spec, document, { skillName }), { force });
  const createdDirs = applyWrites(baseDir, plan);

  const now = new Date().toISOString();
  const config = {
    schema: "skill-router.connection/v1",
    url: connection.baseUrl,
    token: connection.token,
    target: spec.id,
    scope: options.user ? "user" : "project",
    base_dir: baseDir,
    agent: (whoami && whoami.agent) || null,
    capabilities_count: catalogue.capabilities.length,
    platform_version_token: catalogue.version_token,
    files: plan.map((item) => ({ path: item.relPath, style: item.style })),
    created_dirs: createdDirs,
    installer_version: INSTALLER_VERSION,
    installed_at: (existing && existing.installed_at) || now,
    updated_at: now,
  };
  const writtenConfig = writeConfig(baseDir, config);

  const nextSteps = [
    `1. Load ${plan.map((item) => item.relPath).join(", ")} in your agent (Claude Code: \`.claude/skills/\` is read automatically).`,
    `2. Check the connection: ${PROGRAM} verify`,
    `3. Keep the token out of the SKILL and out of version control: ${configPath(baseDir)} is a plaintext secret (mode 0600 where POSIX permissions exist).`,
    `4. Refresh after access changes: ${PROGRAM} update`,
    `5. Remove everything again: ${PROGRAM} uninstall`,
  ];
  const human = [
    `Skill Router connection ${plan.some((item) => item.action === "written") ? "installed" : "already up to date"}`,
    "",
    row("platform", connection.baseUrl),
    row("agent", `${(whoami.agent && whoami.agent.name) || "unknown"} (${(whoami.agent && whoami.agent.id) || "no id"})`),
    row("token", `${maskToken(connection.token)} (stored, never printed in full)`),
    row("target", `${spec.id} (${options.user ? "user" : "project"} scope)`),
    ...plan.map((item) => row(item.relPath === plan[0].relPath ? "skill" : "", `${item.fullPath}  [${item.action}]`)),
    row("config", `${writtenConfig.path}  [${writtenConfig.mode}]`),
    ...(writtenConfig.mode === "600" ? [] : [row("config perms", writtenConfig.mode_note)]),
    row("capabilities", `${catalogue.capabilities.length} granted (version token ${short(catalogue.version_token)})`),
    "",
    "Next steps",
    ...nextSteps.map((line) => `  ${line}`),
    "",
    `Note: the platform URL is ${connection.baseUrl}; the agent token lives only in ${configPath(baseDir)}.`,
  ];
  return finish(
    {
      ok: true,
      command: "install",
      platform: connection.baseUrl,
      agent: (whoami && whoami.agent) || null,
      token_masked: maskToken(connection.token),
      target: spec.id,
      scope: options.user ? "user" : "project",
      base_dir: baseDir,
      files: plan.map((item) => ({ path: item.relPath, full_path: item.fullPath, action: item.action, style: item.style })),
      config: { path: writtenConfig.path, mode: writtenConfig.mode, mode_note: writtenConfig.mode_note },
      capabilities: { count: catalogue.capabilities.length, version_token: catalogue.version_token },
      next_steps: nextSteps,
    },
    human,
    json,
  );
}

/** @param {string|null|undefined} token */
function short(token) {
  return token ? `${String(token).slice(0, 8)}...` : "(none)";
}

/**
 * @param {Record<string, any>} options
 * @returns {Promise<number>}
 */
async function commandVerify(options) {
  // Same scope resolution as install: `verify --user` must find the config that
  // `install --user --target codex-skills` wrote into ~/.codex.
  const { baseDir, existing } = resolveScope(options);
  const connection = resolveConnection(options, existing);
  const json = Boolean(options.json);
  /** @type {string[]} */
  const checks = [];
  /** @type {Record<string, any>} */
  const report = { ok: false, command: "verify", platform: connection.baseUrl, token_masked: maskToken(connection.token), checks: [] };

  const warn = tokenShapeWarning(connection.token);
  const whoami = await getWhoami(connection);
  report.agent = (whoami && whoami.agent) || null;
  /** @type {string[]} */
  const human = [
    "Skill Router connectivity check",
    "",
    row("platform", connection.baseUrl),
    row("agent", `${(whoami.agent && whoami.agent.name) || "unknown"} (${(whoami.agent && whoami.agent.id) || "no id"})`),
    row("token", maskToken(connection.token)),
    row("service", `${whoami.service || "skill-router"} api ${whoami.api_version || "?"}`),
    row("limits", describeLimits(whoami.limits)),
    row("scopes", (whoami.scopes || []).join(", ") || "(none listed)"),
  ];
  checks.push({ name: "whoami", ok: true, detail: `agent ${(whoami.agent && whoami.agent.name) || "unknown"}`, duration_ms: null });
  report.checks.push(checks[checks.length - 1]);

  const catalogue = await getCapabilities(connection, {});
  report.capabilities = { count: catalogue.capabilities.length, version_token: catalogue.version_token };
  checks.push({ name: "capabilities", ok: true, detail: `${catalogue.capabilities.length} granted (version token ${short(catalogue.version_token)})` });
  report.checks.push(checks[checks.length - 1]);
  human.push(row("capabilities", `${catalogue.capabilities.length} granted (version token ${short(catalogue.version_token)})`));
  for (const capability of catalogue.capabilities) {
    const modes = Object.keys(capability.modes || {}).filter((key) => capability.modes[key]);
    const version = capability.versions && capability.versions.current;
    human.push(row("", `  ${capability.slug}  v${version === undefined ? "?" : version}  ${modes.join(",") || "sync"}`));
  }

  if (options.capability) {
    const slug = String(options.capability);
    let input = {};
    if (options.input !== undefined) {
      try {
        input = JSON.parse(String(options.input));
      } catch (error) {
        throw new UsageError(`--input is not valid JSON: ${error instanceof Error ? error.message : String(error)}`, {
          actions: ['Example: --input \'{"prompt":"hello"}\''],
        });
      }
      if (!input || typeof input !== "object" || Array.isArray(input)) {
        throw new UsageError("--input must be a JSON object.", { actions: ['Example: --input \'{"prompt":"hello"}\''] });
      }
    }
    const started = Date.now();
    const result = await invokeCapability(connection, { capability: slug, input });
    const duration = Date.now() - started;
    const status = String(result.status || "unknown");
    const ok = status === "succeeded";
    report.invocation = {
      capability: slug,
      status,
      request_id: result.request_id || null,
      invocation_id: result.invocation_id || null,
      latency_ms: result.latency_ms === undefined ? duration : result.latency_ms,
      task_id: result.task_id || null,
      error: result.error || null,
      provider: result.provider || null,
    };
    checks.push({ name: "invocation", ok, detail: `${slug} -> ${status}`, request_id: result.request_id || null });
    report.checks.push(checks[checks.length - 1]);
    human.push("");
    human.push(row("invocation", `${slug} -> ${status}`));
    human.push(row("request id", String(result.request_id || "(none)")));
    human.push(row("latency", `${result.latency_ms === undefined ? duration : result.latency_ms} ms`));
    if (result.task_id) human.push(row("task", `${result.task_id} (poll ${result.poll || `/api/v1/tasks/${result.task_id}`})`));
    if (result.provider) human.push(row("provider", `${result.provider.name || result.provider.id || "?"}`));
    if (!ok) {
      const error = result.error || {};
      report.ok = false;
      throw new ConnectError(`Invocation of '${slug}' returned status '${status}'.`, {
        code: String(error.code || `invocation_${status}`),
        details: error.details === undefined ? null : error.details,
        actions: [
          error.message ? `Platform message: ${error.message}` : "The platform did not say why.",
          ...(Array.isArray(error.actions) ? error.actions : []),
          `Re-check the input against the schema: ${connection.baseUrl}/api/v1/capabilities/${slug}`,
        ],
      });
    }
  }

  if (warn) {
    human.push("");
    human.push(`note: ${warn}`);
    report.warning = warn;
  }
  human.push("");
  human.push(`Result: connection OK (${checks.length} check(s) passed)`);
  report.ok = true;

  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return 0;
  }
  process.stdout.write(`${human.join("\n")}\n`);
  return 0;
}

/** @param {any} limits */
function describeLimits(limits) {
  if (!limits || typeof limits !== "object") return "(not reported)";
  const parts = [];
  if (limits.rate_limit_per_min !== undefined && limits.rate_limit_per_min !== null) parts.push(`${limits.rate_limit_per_min}/min`);
  if (limits.max_concurrency !== undefined && limits.max_concurrency !== null) parts.push(`concurrency ${limits.max_concurrency}`);
  if (limits.budget_usd !== undefined && limits.budget_usd !== null) parts.push(`budget $${limits.budget_usd}`);
  // Null means "no limit configured for this token", which is worth saying out loud
  // rather than printing `null`.
  return parts.length ? parts.join(", ") : "no limit configured on this token";
}

/**
 * @param {Record<string, any>} options
 * @returns {Promise<number>}
 */
async function commandUpdate(options) {
  const { spec, baseDir, existing } = resolveScope(options);
  if (!existing) {
    throw new ConnectError(`Nothing to update: ${configPath(baseDir)} does not exist.`, {
      code: "not_installed",
      actions: [`Run \`${PROGRAM} install --url <platform> --token <token>\` first.`],
    });
  }
  const connection = resolveConnection(options, existing);
  const json = Boolean(options.json);

  const bundle = await getSkill(connection);
  const whoami = await getWhoami(connection);
  const catalogue = await getCapabilities(connection, {});
  const document = renderFromServerDocument({
    serverMarkdown: bundle.content,
    whoami,
    capabilities: catalogue.capabilities,
    versionToken: catalogue.version_token,
    baseUrl: connection.baseUrl,
  });
  // `update` may replace files this tool wrote without --force, but never a file
  // it did not write. planWrites enforces the marker check for foreign files.
  const artifacts = buildArtifacts(spec, document, { skillName: skillNameOf(whoami) });
  const plan = planWrites(baseDir, artifacts, { force: Boolean(options.force), allowOwnedOverwrite: true });
  applyWrites(baseDir, plan);

  const config = {
    ...existing,
    url: connection.baseUrl,
    token: connection.token,
    target: spec.id,
    agent: (whoami && whoami.agent) || existing.agent || null,
    capabilities_count: catalogue.capabilities.length,
    platform_version_token: catalogue.version_token,
    files: plan.map((item) => ({ path: item.relPath, style: item.style })),
    installer_version: INSTALLER_VERSION,
    updated_at: new Date().toISOString(),
  };
  if (bundle.sha256) config.skill_sha256 = bundle.sha256;
  const writtenConfig = writeConfig(baseDir, config);

  const changed = plan.filter((item) => item.action === "written");
  const human = [
    changed.length ? "SKILL refreshed from /api/v1/skill" : "SKILL already up to date",
    "",
    row("platform", connection.baseUrl),
    row("server sha256", String(bundle.sha256 || "(not returned)").slice(0, 16)),
    ...plan.map((item) => row("file", `${item.fullPath}  [${item.action}]`)),
    row("config", `${writtenConfig.path}  [${writtenConfig.mode}]`),
    ...(writtenConfig.mode === "600" ? [] : [row("config perms", writtenConfig.mode_note)]),
    row("capabilities", `${catalogue.capabilities.length} granted (version token ${short(catalogue.version_token)})`),
    "",
    `Next step: ${PROGRAM} verify`,
  ];
  return finish(
    {
      ok: true,
      command: "update",
      platform: connection.baseUrl,
      files: plan.map((item) => ({ path: item.relPath, full_path: item.fullPath, action: item.action })),
      config: { path: writtenConfig.path, mode: writtenConfig.mode, mode_note: writtenConfig.mode_note },
      server_sha256: bundle.sha256 || null,
      capabilities: { count: catalogue.capabilities.length, version_token: catalogue.version_token },
    },
    human,
    json,
  );
}

/**
 * @param {Record<string, any>} options
 * @returns {Promise<number>}
 */
async function commandUninstall(options) {
  // Uninstall must look where install wrote: `resolveScope` adopts the user-scope
  // install when `--user` is given without `--target`.
  const { spec, baseDir, existing } = resolveScope(options);
  const json = Boolean(options.json);
  if (!existing) {
    const message = `Nothing to uninstall: ${configPath(baseDir)} does not exist. (No files were removed.)`;
    return finish({ ok: true, command: "uninstall", removed: [], skipped: [], note: "not installed" }, [message], json);
  }
  /** @type {{path: string, result: string}[]} */
  const removed = [];
  /** @type {{path: string, reason: string}[]} */
  const skipped = [];
  const files = Array.isArray(existing.files) && existing.files.length
    ? existing.files
    : TARGETS[existing.target] ? TARGETS[existing.target].files : TARGETS.generic.files;

  for (const file of files) {
    const relPath = String(file.path || file.relPath);
    const fullPath = assertInside(baseDir, relPath); // never touch anything outside the base dir
    const current = readArtifact(baseDir, relPath);
    if (current === null) {
      skipped.push({ path: relPath, reason: "already absent" });
      continue;
    }
    if (file.style === "managed-block") {
      const stripped = stripManagedBlock(current);
      if (!stripped.found) {
        skipped.push({ path: relPath, reason: "no Skill Router block found; file left untouched" });
        continue;
      }
      if (stripped.content === null) {
        rmSync(fullPath, { force: true });
        removed.push({ path: relPath, result: "deleted (only the Skill Router block was in it)" });
      } else {
        writeFileSync(fullPath, stripped.content, "utf8");
        removed.push({ path: relPath, result: "block removed, surrounding content kept" });
      }
      continue;
    }
    if (!current.includes("<!-- managed by skill-router-connect -->")) {
      skipped.push({ path: relPath, reason: "not written by this tool; refused to delete" });
      continue;
    }
    rmSync(fullPath, { force: true });
    removed.push({ path: relPath, result: "deleted" });
  }

  // Prune only the directories the installer created, deepest first, and only when
  // they are empty (a file the operator added keeps the directory alive).
  const createdDirs = Array.isArray(existing.created_dirs) ? [...existing.created_dirs].sort((a, b) => b.length - a.length) : [];
  for (const dir of createdDirs) {
    try {
      if (existsSync(dir) && readdirSync(dir).length === 0) {
        rmdirSync(dir);
        removed.push({ path: path.relative(baseDir, dir).split(path.sep).join("/"), result: "empty directory removed" });
      }
    } catch {
      // A locked directory is not an error worth failing the uninstall for.
    }
  }

  const configRemoved = removeConfig(baseDir);
  const human = [
    "Skill Router connection removed",
    "",
    ...(removed.length ? removed.map((item) => row("removed", `${item.path}  (${item.result})`)) : [row("removed", "(nothing)")]),
    ...(skipped.map((item) => row("kept", `${item.path}  (${item.reason})`))),
    row("config", configRemoved ? `${configPath(baseDir)}  (deleted)` : `${configPath(baseDir)}  (already absent)`),
    "",
    "Files this tool did not write, and the platform-side agent/token, are untouched.",
  ];
  return finish(
    {
      ok: true,
      command: "uninstall",
      base_dir: baseDir,
      removed,
      skipped,
      config_removed: configRemoved,
      token_masked: maskToken(existing.token),
    },
    human,
    json,
  );
}

// -------------------------------------------------------------------- entry ---
/**
 * @param {string[]} argv
 * @returns {Promise<number>}
 */
/**
 * `install-self` — one command an agent can run against itself.
 *
 * The agent is told: "run this to install the Skill Router skill". It cannot be expected
 * to know whether it is Codex, Claude Code or something else, so this detects the agent
 * from the presence of its skill directory and installs there. Everything else is the
 * normal install path, so the resulting SKILL and config are identical to a targeted
 * install.
 *
 * @param {Record<string, any>} options
 * @returns {Promise<number>}
 */
async function commandInstallSelf(options) {
  const detected = detectInstalledAgent();
  const delegated = {
    ...options,
    target: detected.id,
    dir: detected.baseDir,
    user: false,
    project: false,
    force: true,
  };
  const code = await commandInstall(delegated);
  if (code !== 0) return code;
  // Tell the agent what it now has, in the terms it needs to act on: the folder, and the
  // fact that no further setup step exists.
  process.stdout.write(
    [
      "",
      `Detected agent: ${detected.id} (${detected.reason})`,
      `Installed into: ${detected.baseDir}`,
      "The skill is installed. Later, when the user asks about Skill Router capabilities,",
      "read the SKILL.md above and follow it — the token is already in .skill-router.json.",
      "",
    ].join("\n"),
  );
  return 0;
}

export async function run(argv) {
  let parsed;
  try {
    parsed = parseArgs(argv);
  } catch (error) {
    return fail(error, false);
  }
  const { command, options, unknown } = parsed;
  if (options.version) {
    process.stdout.write(`${INSTALLER_VERSION}\n`);
    return 0;
  }
  if (!command || command === "help" || options.help) {
    process.stdout.write(`${helpText()}\n`);
    return 0;
  }
  if (unknown.length) {
    return fail(new UsageError(`Unknown argument(s): ${unknown.join(" ")}.`, { actions: [`Run \`${PROGRAM} --help\` for the accepted flags.`] }), Boolean(options.json));
  }
  try {
    switch (command) {
      case "install":
        return await commandInstall(options);
      case "install-self":
      case "installself":
        return await commandInstallSelf(options);
      case "verify":
        return await commandVerify(options);
      case "update":
        return await commandUpdate(options);
      case "uninstall":
        return await commandUninstall(options);
      default:
        throw new UsageError(`Unknown command ${JSON.stringify(command)}.`, {
          actions: ["Commands: install, verify, update, uninstall, --help, --version"],
        });
    }
  } catch (error) {
    return fail(error, Boolean(options.json), command);
  }
}

/**
 * @param {unknown} error
 * @param {boolean} json
 * @param {string} [command]
 * @returns {number}
 */
function fail(error, json, command = "") {
  const known = error instanceof InstallerError;
  const code = known ? error.code : "unexpected_error";
  const message = error instanceof Error ? error.message : String(error);
  const actions = known ? error.actions : [];
  const exitCode = known ? error.exitCode : 1;
  if (json) {
    process.stdout.write(`${JSON.stringify({ ok: false, command, code, message, actions, details: known ? error.details : null }, null, 2)}\n`);
  }
  process.stderr.write(`${PROGRAM}: ${message}\n`);
  for (const action of actions) process.stderr.write(`  -> ${action}\n`);
  process.stderr.write(`  (exit ${exitCode}${code ? `, ${code}` : ""})\n`);
  return exitCode;
}

export { redactedConfig, skillNameOf };
