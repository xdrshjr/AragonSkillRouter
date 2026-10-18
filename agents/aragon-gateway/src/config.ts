/**
 * Environment configuration for the AragonAgent gateway (spec §4.2, §7.1, §9.5).
 *
 * Rules encoded here, all of them security-relevant:
 * - The gateway binds loopback only (`AGENT_GATEWAY_BIND` defaults to 127.0.0.1).
 *   A non-loopback bind needs an explicit, deliberately ugly opt-in flag.
 * - `AGENT_GATEWAY_SHARED_SECRET` is REQUIRED and must be at least 32 characters
 *   (spec §4.2: 32-byte random value carried in `X-Internal-Token`).
 * - The engine pin is validated as a semver-ish exact version, because the whole
 *   upgrade story (spec §12.4) depends on the pin being exact and rewritable.
 * - **The pointer wins over the environment** (spec §A.3.7-4): `AGENT_ENGINE_RUNTIME_DIR/current.json`
 *   is written by the Python platform on a successful switch, read here at boot, and echoed
 *   as `pin_source: "pointer" | "env"`. A broken pointer falls back to `ENGINE_PIN` and is
 *   *logged as an error* (`engine_pointer_invalid`) — never silently.
 * - No secret is ever read from the environment on behalf of upstream providers:
 *   provider keys arrive only through `platform-client.materialize()`.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

import { NULL_LOGGER, type Logger } from './types.js';

export interface ModelConfig {
  /** Default provider id when a turn does not specify one. `''` = no default. */
  providerId: string;
  /** Default model id when a turn does not specify one. `''` = no default. */
  modelId: string;
  /** Optional default base URL for OpenAI-compatible providers. */
  baseUrl?: string;
  /** Output token ceiling per LLM call (upstream `maxTokens`). */
  maxTokens?: number;
  thinkingLevel: 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh';
}

export interface TimeoutConfig {
  /** Single upstream LLM call ceiling (ms). */
  llmCallTimeout: number;
  /** Engine idle watchdog (ms). */
  idleTimeout: number;
  /** Per-tool execution ceiling (ms) — platform tools use their own too. */
  toolTimeout: number;
  /** Whole-turn wall clock ceiling (ms); used when the turn has no budget. */
  turnTimeout: number;
  /** SSE keep-alive interval (ms) — spec §9.4 requires 15s. */
  sseKeepAlive: number;
}

export type EngineMode = 'host' | 'inline' | 'auto';

/** Where the effective engine pin came from (spec §A.3.7-4). */
export type PinSource = 'pointer' | 'env';

/** Shape of `AGENT_ENGINE_RUNTIME_DIR/current.json`, written by the Python platform. */
export interface EnginePointer {
  version: string;
  pin?: string;
  schema_version?: number;
  provenance?: string;
  engine_home?: string;
}

export interface Config {
  nodeEnv: string;
  bind: string;
  port: number;
  sharedSecret: string;
  internalUrl: string;
  policyPath: string;
  /** Effective pin: the pointer's version when readable, else `ENGINE_PIN`. */
  enginePin: string;
  /** `ENGINE_PIN` as configured, kept for diagnostics. */
  enginePinEnv: string;
  /** Which of the two produced `enginePin`. */
  pinSource: PinSource;
  /** Why the pointer was rejected (broken JSON / missing version dir); `undefined` = healthy. */
  pointerError?: string;
  /** Directory holding the platform-written pointer (`current.json`). */
  engineRuntimeDir: string;
  engineMode: EngineMode;
  /** Root under which per-version engine homes live: `<root>/<version>/`. */
  engineHomeRoot: string;
  /** Where isolated build artifacts are written (must be the shared `/data` volume). */
  buildsDir: string;
  /** Optional maintenance marker file; its existence is reported by `/healthz`. */
  maintenanceFlagFile: string;
  model: ModelConfig;
  timeouts: TimeoutConfig;
  /** Directory for `turns.jsonl` session traces written by the gateway. */
  sessionDir: string;
  logLevel: 'debug' | 'info' | 'warn' | 'error';
  /** Ceiling on requests the gateway accepts from the platform, ms. */
  platformTimeout: number;
  /** Max turns replayed into a fresh engine via `replaceMessages()` (spec §7.12-4). */
  replayTurns: number;
}

export class ConfigError extends Error {
  readonly problems: string[];
  constructor(problems: string[]) {
    super(`invalid gateway configuration:\n- ${problems.join('\n- ')}`);
    this.name = 'ConfigError';
    this.problems = problems;
  }
}

const LOOPBACK = new Set(['127.0.0.1', '::1', 'localhost']);

function str(env: NodeJS.ProcessEnv, key: string, fallback?: string): string {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') {
    if (fallback !== undefined) return fallback;
    return '';
  }
  return raw.trim();
}

function intOr(
  env: NodeJS.ProcessEnv,
  key: string,
  fallback: number,
  problems: string[],
  min: number,
  max: number,
): number {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') return fallback;
  const n = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < min || n > max) {
    problems.push(`${key} must be an integer in [${min}, ${max}] (got ${JSON.stringify(raw)})`);
    return fallback;
  }
  return n;
}

function bool(env: NodeJS.ProcessEnv, key: string, fallback: boolean): boolean {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') return fallback;
  const v = raw.trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(v)) return true;
  if (['0', 'false', 'no', 'off'].includes(v)) return false;
  return fallback;
}

const SEMVERISH = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;

const THINKING_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh']);

/** Absolute or relative `<homeRoot>/<version>` for a pin. */
export function engineHomePath(homeRoot: string, version: string): string {
  const root = homeRoot.replace(/[\\/]+$/, '');
  return `${root}/${version}`;
}

/** Result of looking for the platform-written pointer. */
export interface PointerRead {
  pointer: EnginePointer | null;
  /** Human-readable reason the pointer was rejected (`null` when fine or absent). */
  error: string | null;
  path: string;
}

/**
 * Read `AGENT_ENGINE_RUNTIME_DIR/current.json`.
 *
 * The pointer is only trusted when **both** the JSON and the referenced version
 * directory exist: a pointer to a version that was never installed (or was garbage
 * collected) would make the gateway report a pin it cannot possibly run.
 */
export function readEnginePointer(runtimeDir: string, homeRoot: string): PointerRead {
  const path = join(runtimeDir, 'current.json');
  if (!existsSync(path)) return { pointer: null, error: null, path };
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { pointer: null, error: `current.json is not valid JSON: ${message}`, path };
  }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return { pointer: null, error: 'current.json is not a JSON object', path };
  }
  const record = raw as Record<string, unknown>;
  const version = record['version'];
  if (typeof version !== 'string' || !SEMVERISH.test(version)) {
    return { pointer: null, error: `current.json has no usable version (got ${JSON.stringify(version)})`, path };
  }
  const home = engineHomePath(homeRoot, version);
  if (!existsSync(home)) {
    return { pointer: null, error: `engine home for ${version} does not exist (${home})`, path };
  }
  const schemaVersion = record['schema_version'];
  return {
    pointer: {
      version,
      ...(typeof record['pin'] === 'string' ? { pin: record['pin'] } : {}),
      ...(typeof schemaVersion === 'number' ? { schema_version: schemaVersion } : {}),
      ...(typeof record['provenance'] === 'string' ? { provenance: record['provenance'] } : {}),
      ...(typeof record['engine_home'] === 'string' ? { engine_home: record['engine_home'] } : {}),
    },
    error: null,
    path,
  };
}

/** Load and validate configuration. Throws `ConfigError` listing every problem. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env, logger: Logger = NULL_LOGGER): Config {
  const problems: string[] = [];

  const bind = str(env, 'AGENT_GATEWAY_BIND', '127.0.0.1');
  if (!LOOPBACK.has(bind) && !bool(env, 'AGENT_GATEWAY_ALLOW_PUBLIC_BIND', false)) {
    problems.push(
      `AGENT_GATEWAY_BIND=${bind} is not a loopback address; the gateway must not be ` +
        'reachable from outside the platform domain. Set AGENT_GATEWAY_ALLOW_PUBLIC_BIND=1 ' +
        'only if a reverse proxy in front of it terminates authentication.',
    );
  }

  const port = intOr(env, 'AGENT_GATEWAY_PORT', 8500, problems, 1, 65535);

  const sharedSecret = str(env, 'AGENT_GATEWAY_SHARED_SECRET', '');
  if (sharedSecret === '') {
    problems.push('AGENT_GATEWAY_SHARED_SECRET is required (32-byte random value, sent as X-Internal-Token)');
  } else if (sharedSecret.length < 32) {
    problems.push(
      `AGENT_GATEWAY_SHARED_SECRET must be at least 32 characters (got ${sharedSecret.length}); ` +
        'spec §4.2 requires a 32-byte random value',
    );
  }

  const internalUrl = str(env, 'SKILLROUTER_INTERNAL_URL', 'http://127.0.0.1:8000');
  try {
    const u = new URL(internalUrl);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') {
      problems.push(`SKILLROUTER_INTERNAL_URL must be http(s) (got ${u.protocol})`);
    }
  } catch {
    problems.push(`SKILLROUTER_INTERNAL_URL is not a valid URL: ${JSON.stringify(internalUrl)}`);
  }

  const enginePinEnv = str(env, 'ENGINE_PIN', '0.2.34');
  if (!SEMVERISH.test(enginePinEnv)) {
    problems.push(`ENGINE_PIN must be an exact version like 0.2.34 (got ${JSON.stringify(enginePinEnv)})`);
  }

  // -- pointer overrides the env pin (spec §A.3.7-2 / R2-09) ------------------
  const engineHomeRoot = str(env, 'AGENT_ENGINE_HOME_ROOT', 'var/aragon-home/engines');
  const engineRuntimeDir = str(env, 'AGENT_ENGINE_RUNTIME_DIR', 'var/engine-runtime');
  let enginePin = enginePinEnv;
  let pinSource: PinSource = 'env';
  let pointerError: string | undefined;
  const pointer = readEnginePointer(engineRuntimeDir, engineHomeRoot);
  if (pointer.error !== null) {
    pointerError = pointer.error;
    logger.error('engine_pointer_invalid', {
      path: pointer.path,
      error: pointer.error,
      fallback_pin: enginePinEnv,
    });
  } else if (pointer.pointer !== null) {
    enginePin = pointer.pointer.version;
    pinSource = 'pointer';
  }

  const modeRaw = str(env, 'AGENT_GATEWAY_ENGINE_MODE', 'auto').toLowerCase();
  if (modeRaw !== 'host' && modeRaw !== 'inline' && modeRaw !== 'auto') {
    problems.push(`AGENT_GATEWAY_ENGINE_MODE must be host|inline|auto (got ${JSON.stringify(modeRaw)})`);
  }
  const engineMode: EngineMode = modeRaw === 'host' || modeRaw === 'inline' ? modeRaw : 'auto';

  const thinkingRaw = str(env, 'AGENT_THINKING_LEVEL', 'off').toLowerCase();
  if (!THINKING_LEVELS.has(thinkingRaw)) {
    problems.push(`AGENT_THINKING_LEVEL must be one of ${[...THINKING_LEVELS].join('|')}`);
  }

  const maxTokensRaw = str(env, 'AGENT_MODEL_MAX_TOKENS', '');
  let maxTokens: number | undefined;
  if (maxTokensRaw !== '') {
    const n = Number.parseInt(maxTokensRaw, 10);
    if (!Number.isInteger(n) || n < 1 || n > 1_000_000) {
      problems.push(`AGENT_MODEL_MAX_TOKENS must be an integer in [1, 1000000] (got ${maxTokensRaw})`);
    } else {
      maxTokens = n;
    }
  }

  const logLevelRaw = str(env, 'AGENT_GATEWAY_LOG_LEVEL', 'info').toLowerCase();
  if (!['debug', 'info', 'warn', 'error'].includes(logLevelRaw)) {
    problems.push('AGENT_GATEWAY_LOG_LEVEL must be debug|info|warn|error');
  }

  const baseUrlRaw = str(env, 'AGENT_MODEL_BASE_URL', '');

  // Collected BEFORE the throw below: `intOr` reports into `problems`, and a
  // numeric problem discovered while building the return value would otherwise
  // be silently dropped.
  const timeouts: TimeoutConfig = {
    llmCallTimeout: intOr(env, 'AGENT_LLM_CALL_TIMEOUT_MS', 300_000, problems, 1_000, 3_600_000),
    idleTimeout: intOr(env, 'AGENT_IDLE_TIMEOUT_MS', 60_000, problems, 1_000, 3_600_000),
    toolTimeout: intOr(env, 'AGENT_TOOL_TIMEOUT_MS', 120_000, problems, 1_000, 3_600_000),
    turnTimeout: intOr(env, 'AGENT_TURN_TIMEOUT_MS', 900_000, problems, 1_000, 7_200_000),
    sseKeepAlive: intOr(env, 'AGENT_SSE_KEEPALIVE_MS', 15_000, problems, 1_000, 120_000),
  };
  const sessionDir = str(env, 'AGENT_SESSION_DIR', 'var/agent-sessions');
  const platformTimeout = intOr(env, 'AGENT_PLATFORM_TIMEOUT_MS', 30_000, problems, 1_000, 600_000);
  const replayTurns = intOr(env, 'AGENT_REPLAY_TURNS', 20, problems, 0, 500);

  if (problems.length > 0) throw new ConfigError(problems);

  return {
    nodeEnv: str(env, 'NODE_ENV', 'development'),
    bind,
    port,
    sharedSecret,
    internalUrl: internalUrl.replace(/\/+$/, ''),
    policyPath: str(env, 'AGENT_POLICY_PATH', 'var/agent-policy.json'),
    enginePin,
    enginePinEnv,
    pinSource,
    ...(pointerError === undefined ? {} : { pointerError }),
    engineRuntimeDir,
    engineMode,
    engineHomeRoot,
    buildsDir: str(env, 'AGENT_ENGINE_BUILDS_DIR', join(dirname(engineRuntimeDir), 'engine-builds')),
    maintenanceFlagFile: str(env, 'AGENT_MAINTENANCE_FLAG_FILE', ''),
    model: {
      providerId: str(env, 'AGENT_MODEL_PROVIDER', 'anthropic'),
      modelId: str(env, 'AGENT_MODEL_ID', ''),
      ...(baseUrlRaw === '' ? {} : { baseUrl: baseUrlRaw }),
      ...(maxTokens === undefined ? {} : { maxTokens }),
      thinkingLevel: thinkingRaw as ModelConfig['thinkingLevel'],
    },
    timeouts,
    sessionDir,
    logLevel: logLevelRaw as Config['logLevel'],
    platformTimeout,
    replayTurns,
  };
}

/** Resolve the per-version engine home (`var/aragon-home/engines/<version>`). */
export function engineHome(config: Config, version: string): string {
  return engineHomePath(config.engineHomeRoot, version);
}

/** The installed package directory a pin actually resolves to (spec §A.5.3 `resolved_path`). */
export function engineResolvedPath(config: Config, version: string): string {
  return `${engineHome(config, version).replace(/[\\/]+$/, '')}/node_modules/@aragon-agent/core`;
}

/** `<homeRoot>/<version>/node_modules` — what the engine child must resolve from. */
export function engineNodeModules(config: Config, version: string): string {
  return `${engineHome(config, version).replace(/[\\/]+$/, '')}/node_modules`;
}

/** `true` when the platform wrote the maintenance marker (reported by `/healthz`). */
export function maintenanceEnabled(config: Config): boolean {
  return config.maintenanceFlagFile !== '' && existsSync(config.maintenanceFlagFile);
}
