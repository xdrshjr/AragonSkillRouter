/**
 * Shared types for the Skill Router AragonAgent gateway.
 *
 * Two contracts live here and they must not be confused:
 *
 * 1. The PLATFORM contract (spec §7.12 / §9.5): `AgentEvent` with the 13 frozen
 *    platform event names, `TurnRequest`, `StopReport`, `EngineHealth`.
 * 2. The UPSTREAM contract (spec §12.1, measured in aragonagent-upstream.md §3):
 *    `@aragon-agent/core`'s own `AgentEvent` union, which is *different* from the
 *    platform union. `engine.ts` maps the second onto the first.
 *
 * Nothing here imports `@aragon-agent/core`: the package is loaded lazily so this
 * module (and everything the tests touch) stays importable when the dependency is
 * missing.
 */

/** The 13 platform event names, exactly as frozen by spec §7.12 / §9.4. */
export const PLATFORM_EVENT_TYPES = [
  'session.started',
  'turn.started',
  'assistant.delta',
  'thinking.delta',
  'tool.started',
  'tool.progress',
  'tool.finished',
  'todo.updated',
  'turn.queued',
  'turn.finished',
  'turn.stopped',
  'turn.failed',
  'engine.notice',
] as const;

export type PlatformEventType = (typeof PLATFORM_EVENT_TYPES)[number];

/** One event as delivered on the internal SSE stream (spec §9.4 envelope). */
export interface PlatformEvent {
  /** Monotonic per-turn sequence number, starting at 1. */
  seq: number;
  turn_id: string;
  type: PlatformEventType;
  /** ISO-8601 UTC. */
  at: string;
  payload: Record<string, unknown>;
}

export type EngineAvailability = 'available' | 'unavailable' | 'starting';

export interface EngineIdentity {
  /** npm package name of the engine. */
  package: string;
  /** Exact pinned version, e.g. `0.2.34`. */
  version: string;
  /** Git commit the pin corresponds to (from the pin manifest, never invented). */
  commit: string | null;
  /** Upstream machine-contract version (`aragon exec` schemaVersion). */
  schema_version: number;
}

export interface EngineHealth {
  ok: boolean;
  engine: EngineIdentity & { status: EngineAvailability; detail?: string };
  tools: string[];
  active_turns: number;
  uptime_s: number;
}

/** A single message in a replayed conversation (upstream `Message` shape). */
export type TurnMessage =
  | { role: 'user'; content: string; timestamp?: number }
  | {
      role: 'assistant';
      content: Array<
        | { type: 'text'; text: string }
        | { type: 'thinking'; text: string; signature?: string }
        | { type: 'tool_call'; toolCallId: string; toolName: string; args: Record<string, unknown> }
      >;
    }
  | { role: 'tool_result'; toolCallId: string; content: string; isError?: boolean };

export interface TurnBudget {
  /** Hard ceiling on agent-loop iterations; the gateway aborts when exceeded. */
  max_turns?: number;
  /** Hard wall-clock ceiling for the turn in ms. */
  max_duration_ms?: number;
}

/** `POST /internal/turns` request body (spec §9.5). */
export interface TurnRequest {
  session_id: string;
  turn_id: string;
  /** History to replay into the engine before prompting (newest last). */
  messages?: TurnMessage[];
  system_prompt?: string;
  model?: AgentModelRef;
  budget?: TurnBudget;
  /**
   * The user text for this turn. Optional because spec §9.5 describes the body as
   * `{session_id, turn_id, messages[], system_prompt, model, budget}` — in that shape
   * the prompt IS the last user message of `messages[]`, and the gateway derives it
   * (and excludes it from the replay so it is not duplicated in history).
   */
  input?: string;
  /**
   * Credential references this turn may materialize, keyed by provider id.
   * The gateway never reads platform business credentials from a database; this
   * is the only way a secret reaches process memory (spec §4.3, §7.12-1).
   */
  credential_refs?: Record<string, string>;
}

export interface AgentModelRef {
  providerId: string;
  modelId: string;
  baseUrl?: string;
}

export interface StoppedStep {
  kind: 'tool' | 'llm' | 'queue';
  name?: string;
  tool_call_id?: string;
  status: 'completed' | 'aborted' | 'unknown';
}

/** An upstream request that was already dispatched and may still be running. */
export interface ExternalPending {
  tool: string;
  upstream_task_id: string;
  note: string;
}

/** `POST /internal/turns/:turnId/interrupt` response (spec §7.12-5). */
export interface StopReport {
  turn_id: string;
  stopped: boolean;
  reason: 'aborted' | 'not_running' | 'timeout' | 'unknown_turn';
  stopped_steps: StoppedStep[];
  external_pending: ExternalPending[];
  detail?: string;
}

/** Result of a `POST /internal/turns` call. */
export interface TurnAccepted {
  accepted: true;
  ipc_stream: string;
  turn_id: string;
}

/** `POST /internal/admin/reload` response (spec §7.12-6, §12.4-4). */
export interface ReloadResult {
  ok: boolean;
  engine_version: string;
  stage: 'start_child' | 'wait_ready' | 'drain_old' | 'close_old' | 'done' | 'rejected';
  detail: string;
}

/** NDJSON frames exchanged with `engine-host` (spec §7.12-6). */
export type HostToParentFrame =
  | { type: 'ready'; engine: { version: string; package: string; schema_version: number }; tools: string[] }
  | { type: 'event'; turn_id: string; event: PlatformEvent }
  | { type: 'turn_finished'; turn_id: string; ok: boolean; detail?: string }
  | { type: 'stopped'; turn_id: string; report: StopReport }
  | { type: 'notice'; level: 'info' | 'warn' | 'error'; message: string }
  | { type: 'fatal'; message: string };

export type ParentToHostFrame =
  | { type: 'turn'; request: TurnRequest; requestId?: string }
  | { type: 'abort'; turn_id: string }
  | { type: 'steer'; turn_id: string; message: string }
  | { type: 'shutdown' };

/**
 * Loosely-typed mirror of upstream `AgentEvent`.
 *
 * Deliberately structural rather than imported: a *new* upstream event type must
 * degrade into an `engine.notice` (spec §7.12, §12.2), not break compilation.
 */
export interface UpstreamEvent {
  type: string;
  [key: string]: unknown;
}

/** Correlation fields every tool invocation carries. */
export interface ToolCallContext {
  session_id: string;
  turn_id: string;
  request_id: string;
  tool_call_id: string;
}

/**
 * Structured logger (JSON lines, spec §7.14-ish: `ts, level, event, ...fields`).
 *
 * Declared here rather than imported from a module so every layer (tools, engine,
 * supervisor) can take one without pulling in `main.ts` (which boots Fastify).
 */
export interface Logger {
  debug(event: string, fields?: Record<string, unknown>): void;
  info(event: string, fields?: Record<string, unknown>): void;
  warn(event: string, fields?: Record<string, unknown>): void;
  error(event: string, fields?: Record<string, unknown>): void;
  child(bindings: Record<string, unknown>): Logger;
}

/** No-op logger for tests and for code paths that must not become chatty. */
export const NULL_LOGGER: Logger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
  child: () => NULL_LOGGER,
};

/** An audit entry posted to `POST /internal/audit`. */
export interface AuditEntry {
  actor: 'agent-gateway';
  action: string;
  object: string;
  result: 'ok' | 'error' | 'timeout' | 'rejected';
  detail: string;
  session_id: string;
  turn_id: string;
  request_id: string;
  duration_ms: number;
  at: string;
}
