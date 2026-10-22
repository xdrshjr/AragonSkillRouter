/**
 * Sidecar configuration.
 *
 * Everything is read from the environment exactly once at boot so a running
 * process cannot drift from what the operator (or the platform's `.env`) said.
 * Missing *required* values throw here rather than surfacing as a broken request
 * later: a sidecar that starts without a bearer token is a sidecar that silently
 * accepts unauthenticated traffic.
 */

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

export interface SidecarConfig {
  /** Bind address. Loopback only — this service is never exposed. */
  host: string;
  port: number;
  /** Shared secret for `Authorization: Bearer …` on every non-/health call. */
  token: string;

  /** npm package + version of the embedded engine (reported by /health). */
  enginePackage: string;
  engineVersion: string;
  /** Repo URL used by /releases/check (`git ls-remote`). */
  repoUrl: string;
  /** `stable` (tags) | `branch` (head of `trackBranch`). */
  channel: string;
  trackBranch: string;
  /** Commit this build was pinned to, when the deploy knows it. */
  engineCommit: string | null;

  /** Session/template fallbacks used when a create request omits a model. */
  defaultProviderId: string;
  defaultModelId: string;
  defaultBaseUrl: string | null;

  /** Limits reported by /info and enforced per session. */
  maxSessions: number;
  eventBufferSize: number;
  toolTimeoutMs: number;
  llmCallTimeoutMs: number;
  idleTimeoutMs: number;
  maxTokens: number | null;
  maxToolOutputChars: number;
  stopSettleMs: number;
  releaseCheckTimeoutMs: number;
  keepaliveMs: number;
  bodyLimitBytes: number;
}

const PROVIDER_IDS = new Set(["anthropic", "openai", "google"]);

function str(env: NodeJS.ProcessEnv, name: string, fallback: string): string {
  const raw = env[name];
  if (raw === undefined) return fallback;
  return raw.trim();
}

function optStr(env: NodeJS.ProcessEnv, name: string): string | null {
  const raw = env[name];
  if (raw === undefined) return null;
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

function int(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${name} must be an integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

function intOrNull(env: NodeJS.ProcessEnv, name: string): number | null {
  const raw = env[name];
  if (raw === undefined || raw.trim() === "") return null;
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${name} must be an integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

/**
 * Read the engine version from the installed package instead of hard-coding it.
 * A version string in source would be a second source of truth and the first
 * thing to go stale.
 *
 * Resolution has to go through ESM, not CJS. `@aragon-agent/core` is ESM-only and
 * its `exports` map has no `require` condition, so BOTH
 * `require.resolve(pkg)` and `require.resolve(pkg + '/package.json')` throw
 * (`ERR_PACKAGE_PATH_NOT_EXPORTED`) and the version silently degraded to
 * "unknown". `import.meta.resolve` uses the ESM conditions and returns the real
 * entry point; walking up from there to the owning package.json gives the version
 * without depending on the package exposing that path.
 */
export function detectEngineVersion(enginePackage: string): string {
  try {
    const entry = import.meta.resolve(enginePackage);
    let dir = path.dirname(fileURLToPath(entry));
    for (let depth = 0; depth < 8; depth += 1) {
      const candidate = path.join(dir, "package.json");
      if (existsSync(candidate)) {
        const parsed = JSON.parse(readFileSync(candidate, "utf8")) as {
          name?: string;
          version?: string;
        };
        if (parsed.name === enginePackage && parsed.version) return parsed.version;
      }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
    return "unknown";
  } catch {
    // A missing/broken install is reported as 'unknown' rather than crashing the
    // boot: /health is exactly where an operator looks to discover this.
    return "unknown";
  }
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): SidecarConfig {
  const token = optStr(env, "SR_ARAGON_SIDECAR_TOKEN");
  if (!token) {
    throw new Error(
      "SR_ARAGON_SIDECAR_TOKEN is not set. The sidecar binds a loopback port and " +
        "speaks the platform tool contract; starting without a bearer token would " +
        "expose that to every local process. Set SR_ARAGON_SIDECAR_TOKEN to a random " +
        "32+ byte secret and give the backend the same value.",
    );
  }
  const host = str(env, "SR_ARAGON_SIDECAR_HOST", "127.0.0.1");
  if (host !== "127.0.0.1" && host !== "localhost" && host !== "::1") {
    throw new Error(
      `SR_ARAGON_SIDECAR_HOST=${host} is refused. This service must stay on loopback; ` +
        "put a reverse proxy in front if you really need remote access.",
    );
  }
  const enginePackage = str(env, "SR_ARAGON_NPM_PACKAGE", "@aragon-agent/core");
  const channel = str(env, "SR_ARAGON_UPGRADE_CHANNEL", "stable");
  if (channel !== "stable" && channel !== "branch") {
    throw new Error(`SR_ARAGON_UPGRADE_CHANNEL must be 'stable' or 'branch', got '${channel}'`);
  }

  return {
    host,
    port: int(env, "SR_ARAGON_SIDECAR_PORT", 7317),
    token,
    enginePackage,
    engineVersion: detectEngineVersion(enginePackage),
    repoUrl: str(env, "SR_ARAGON_REPO_URL", "https://github.com/xdrshjr/AragonAgent.git"),
    channel,
    trackBranch: str(env, "SR_ARAGON_TRACK_BRANCH", "main"),
    engineCommit: optStr(env, "SR_ARAGON_ENGINE_COMMIT"),
    defaultProviderId: str(env, "SR_ARAGON_DEFAULT_PROVIDER_ID", ""),
    defaultModelId: str(env, "SR_ARAGON_DEFAULT_MODEL_ID", ""),
    defaultBaseUrl: optStr(env, "SR_ARAGON_DEFAULT_BASE_URL"),
    maxSessions: int(env, "SR_ARAGON_MAX_SESSIONS", 32),
    eventBufferSize: int(env, "SR_ARAGON_EVENT_BUFFER", 2000),
    toolTimeoutMs: int(env, "SR_ARAGON_TOOL_TIMEOUT_MS", 120_000),
    llmCallTimeoutMs: int(env, "SR_ARAGON_LLM_TIMEOUT_MS", 300_000),
    idleTimeoutMs: int(env, "SR_ARAGON_IDLE_TIMEOUT_MS", 300_000),
    maxTokens: intOrNull(env, "SR_ARAGON_MAX_TOKENS"),
    maxToolOutputChars: int(env, "SR_ARAGON_MAX_TOOL_OUTPUT_CHARS", 4000),
    stopSettleMs: int(env, "SR_ARAGON_STOP_SETTLE_MS", 10_000),
    releaseCheckTimeoutMs: int(env, "SR_ARAGON_RELEASE_TIMEOUT_MS", 15_000),
    keepaliveMs: int(env, "SR_ARAGON_SSE_KEEPALIVE_MS", 15_000),
    bodyLimitBytes: int(env, "SR_ARAGON_BODY_LIMIT_BYTES", 8 * 1024 * 1024),
  };
}

/** True when the env supplies a complete default model template. */
export function defaultModelConfigured(config: SidecarConfig): boolean {
  return (
    config.defaultProviderId !== "" &&
    config.defaultModelId !== "" &&
    PROVIDER_IDS.has(config.defaultProviderId)
  );
}

export function supportedProviders(): string[] {
  return [...PROVIDER_IDS];
}
