/**
 * Tools — shared registry, validation, timeout, audit and redaction plumbing.
 *
 * Every platform tool (§12.3) is built by `buildTool()` so that all four hard
 * requirements are enforced in ONE place instead of twelve:
 *
 * 1. JSON Schema validation with `additionalProperties: false` (ajv, strict off so
 *    the schemas stay portable to upstream's structural validator).
 * 2. A per-tool timeout — 20s default, 60s for `sr_test_capability`. Upstream's
 *    `ToolExecutor` only *signals* a timeout (it awaits the tool promise), so the
 *    ceiling has to be a real race here or it is inert (upstream `executor.d.ts`).
 * 3. One audit entry per call (`POST /internal/audit`), including rejected calls.
 * 4. Every return value passes through `redactSecrets()` and is length-clipped
 *    before it reaches the model's context.
 *
 * Note on `defineTool`: the tools exported from this directory are *structural*
 * `AgentTool`s (same shape, verified by `tools.test.ts`). `engine.ts` wraps them
 * with upstream's `defineTool()` while it loads the SDK, which keeps this whole
 * directory importable when `@aragon-agent/core` is not installed.
 */

import { Ajv, type ValidateFunction } from 'ajv';
import { PlatformError, redactSecrets, redactedSummary, type PlatformClient } from '../platform-client.js';
import { PLATFORM_TOOL_NAMES, type ToolPolicy } from '../policy.js';
import { NULL_LOGGER, type AuditEntry, type Logger, type ToolCallContext } from '../types.js';
import { createRequestApprovalTool } from './sr_approval.js';
import { createCapabilitiesListTool, createCapabilityGetTool } from './sr_capabilities.js';
import { createProposeDraftTool, createUpdateDraftTool } from './sr_draft.js';
import { createSkillDraftTool, createValidateSkillTool } from './sr_skill.js';
import { createFetchSourceTool, createParseDocumentTool } from './sr_sources.js';
import { createSampleInvokeTool, createTestCapabilityTool } from './sr_test.js';

/** Structural twin of upstream `JSONSchema7` (index signature included). */
export interface ToolSchema {
  type?: string | string[];
  description?: string;
  properties?: Record<string, ToolSchema>;
  required?: string[];
  items?: ToolSchema | ToolSchema[];
  enum?: unknown[];
  default?: unknown;
  additionalProperties?: boolean | ToolSchema;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  pattern?: string;
  oneOf?: ToolSchema[];
  anyOf?: ToolSchema[];
  [key: string]: unknown;
}

export interface ToolContent {
  type: 'text';
  text: string;
}

export interface ToolResult {
  content: ToolContent[];
  isError?: boolean;
}

/** Structural twin of upstream `ToolExecutionContext`. */
export interface ToolExecutionContext {
  signal?: AbortSignal;
  onProgress?: (update: { message?: string; percentage?: number; detail?: unknown }) => void;
  abortCause?: 'timeout' | 'external';
}

/** Structural twin of upstream `AgentTool`. */
export interface PlatformTool {
  name: string;
  label: string;
  description: string;
  parameters: ToolSchema;
  tags: string[];
  execute: (
    toolCallId: string,
    params: Record<string, unknown>,
    context: ToolExecutionContext,
  ) => Promise<ToolResult>;
}

export interface ToolDeps {
  platform: PlatformClient;
  policy: ToolPolicy;
  logger?: Logger;
  /** Bridge to the turn's SSE stream for `tool.progress`. */
  progress?: (toolName: string, update: { message?: string; percentage?: number; detail?: unknown }) => void;
  now?: () => Date;
  /**
   * Correlation for the turn that owns this toolset. Tools are rebuilt per turn
   * (cheap: schemas are compiled once) so audit rows always carry the right
   * session/turn/request ids without smuggling them through model-visible args.
   */
  binding?: { sessionId: string; turnId: string; requestId: string };
}

/** Default ceiling from spec §12.3. */
export const DEFAULT_TOOL_TIMEOUT_MS = 20_000;
/** `sr_test_capability` performs a real upstream request (spec §12.3). */
export const LONG_TOOL_TIMEOUT_MS = 60_000;
/** Hard clip on the JSON a tool hands back to the model. */
export const MAX_RESULT_CHARS = 20_000;

export class ToolTimeoutError extends Error {
  readonly toolName: string;
  readonly timeoutMs: number;
  constructor(toolName: string, timeoutMs: number) {
    super(`${toolName} exceeded its ${timeoutMs}ms timeout`);
    this.name = 'ToolTimeoutError';
    this.toolName = toolName;
    this.timeoutMs = timeoutMs;
  }
}

export interface ToolSpec<TArgs extends Record<string, unknown>> {
  name: string;
  label: string;
  description: string;
  parameters: ToolSchema;
  timeoutMs?: number;
  tags: string[];
  /** Business-rule checks that JSON Schema cannot express (return a message to reject). */
  extraValidate?: (args: TArgs) => string | null;
  /** Perform the platform call; the returned value is redacted before returning. */
  invoke: (args: TArgs, ctx: ToolCallContext, signal: AbortSignal) => Promise<unknown>;
}

const ajv = new Ajv({ allErrors: true, strict: false, coerceTypes: false, useDefaults: true, allowUnionTypes: true });
const validatorCache = new Map<string, ValidateFunction>();

function validatorFor(name: string, schema: ToolSchema): ValidateFunction {
  const cached = validatorCache.get(name);
  if (cached) return cached;
  const compiled = ajv.compile(schema);
  validatorCache.set(name, compiled);
  return compiled;
}

function formatAjvErrors(validate: ValidateFunction): string[] {
  return (validate.errors ?? []).map((err) => {
    const path = err.instancePath === '' ? '(root)' : err.instancePath;
    const extra = err.params && Object.keys(err.params).length > 0 ? ` ${JSON.stringify(err.params)}` : '';
    return `${path} ${err.message ?? 'is invalid'}${extra}`;
  });
}

function textResult(payload: unknown): ToolResult {
  return { content: [{ type: 'text', text: renderPayload(payload) }] };
}

function errorText(payload: unknown): ToolResult {
  return { content: [{ type: 'text', text: renderPayload(payload) }], isError: true };
}

function renderPayload(payload: unknown): string {
  let text: string;
  try {
    text = JSON.stringify(redactSecrets(payload));
  } catch {
    text = JSON.stringify({ ok: false, code: 'UNSERIALIZABLE', message: 'tool result was not serializable' });
  }
  if (text === undefined) text = 'null';
  return text.length > MAX_RESULT_CHARS ? `${text.slice(0, MAX_RESULT_CHARS)}…[truncated]` : text;
}

/** Build one validated, timed, audited platform tool. */
export function buildTool<TArgs extends Record<string, unknown>>(
  spec: ToolSpec<TArgs>,
  deps: ToolDeps,
): PlatformTool {
  const logger = deps.logger ?? NULL_LOGGER;
  const now = deps.now ?? (() => new Date());
  const timeoutMs = spec.timeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS;
  const validate = validatorFor(spec.name, spec.parameters);

  const audit = async (entry: Omit<AuditEntry, 'actor' | 'at' | 'action' | 'object'>): Promise<void> => {
    try {
      await deps.platform.audit({
        actor: 'agent-gateway',
        action: `tool.${spec.name}`,
        object: spec.name,
        at: now().toISOString(),
        ...entry,
      });
    } catch (err) {
      // Audit is best-effort: a successful tool call must not be reported as a
      // failure because the platform was momentarily unavailable. The failure is
      // logged (and surfaced by /healthz counters on the Python side).
      logger.warn('audit.write_failed', { tool: spec.name, error: (err as Error).message });
    }
  };

  return {
    name: spec.name,
    label: spec.label,
    description: spec.description,
    parameters: spec.parameters,
    tags: spec.tags,
    async execute(toolCallId, params, context) {
      const startedAt = Date.now();
      const call: ToolCallContext = {
        session_id: deps.binding?.sessionId ?? '',
        turn_id: deps.binding?.turnId ?? '',
        request_id: deps.binding?.requestId ?? '',
        tool_call_id: toolCallId,
      };

      if (!validate(params)) {
        const errors = formatAjvErrors(validate);
        await audit({
          result: 'rejected',
          detail: redactedSummary({ reason: 'schema', errors, args: params }),
          session_id: call.session_id,
          turn_id: call.turn_id,
          request_id: call.request_id,
          duration_ms: Date.now() - startedAt,
        });
        logger.warn('tool.invalid_arguments', { tool: spec.name, errors });
        return errorText({ ok: false, code: 'INVALID_ARGUMENTS', tool: spec.name, errors });
      }

      const args = params as TArgs;
      if (spec.extraValidate) {
        const problem = spec.extraValidate(args);
        if (problem !== null) {
          await audit({
            result: 'rejected',
            detail: redactedSummary({ reason: 'extra_validate', problem, args }),
            session_id: call.session_id,
            turn_id: call.turn_id,
            request_id: call.request_id,
            duration_ms: Date.now() - startedAt,
          });
          return errorText({ ok: false, code: 'INVALID_ARGUMENTS', tool: spec.name, errors: [problem] });
        }
      }

      const controller = new AbortController();
      const unlink = linkSignals(context.signal, controller);
      let timer: NodeJS.Timeout | undefined;
      const timeout = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new ToolTimeoutError(spec.name, timeoutMs));
        }, timeoutMs);
        timer.unref?.();
      });

      const progress = deps.progress;
      if (progress && context.onProgress === undefined) {
        context.onProgress = (update) => progress(spec.name, update);
      }

      try {
        const result = await Promise.race([spec.invoke(args, call, controller.signal), timeout]);
        await audit({
          result: 'ok',
          detail: redactedSummary({ args, result }),
          session_id: call.session_id,
          turn_id: call.turn_id,
          request_id: call.request_id,
          duration_ms: Date.now() - startedAt,
        });
        return textResult(result);
      } catch (err) {
        const timedOut = err instanceof ToolTimeoutError;
        const code = timedOut ? 'TIMEOUT' : errorCode(err);
        await audit({
          result: timedOut ? 'timeout' : 'error',
          detail: redactedSummary({ args, code, error: (err as Error).message }),
          session_id: call.session_id,
          turn_id: call.turn_id,
          request_id: call.request_id,
          duration_ms: Date.now() - startedAt,
        });
        logger.warn('tool.failed', { tool: spec.name, code, error: (err as Error).message });
        return errorText({
          ok: false,
          code,
          tool: spec.name,
          message: (err as Error).message,
          ...(timedOut ? { timeout_ms: timeoutMs } : {}),
        });
      } finally {
        if (timer) clearTimeout(timer);
        unlink();
      }
    },
  };
}

function linkSignals(external: AbortSignal | undefined, controller: AbortController): () => void {
  if (!external) return () => undefined;
  if (external.aborted) {
    controller.abort();
    return () => undefined;
  }
  const onAbort = (): void => controller.abort();
  external.addEventListener('abort', onAbort, { once: true });
  return () => external.removeEventListener('abort', onAbort);
}

function errorCode(err: unknown): string {
  if (err instanceof PlatformError) return err.code;
  return 'TOOL_ERROR';
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export interface ToolsetResult {
  tools: PlatformTool[];
  /** Platform tools that exist but are disabled by the policy document. */
  disabled: string[];
}

/**
 * Build the tool array handed to the engine, filtered by the policy allowlist.
 *
 * The returned order follows spec §12.3 so `/healthz`'s tool list and the audit
 * trail are stable across restarts. Every name is a `PLATFORM_TOOL_NAMES` member —
 * `policy.ts` already rejected anything else, and `policy.test.ts` asserts that a
 * newly added upstream tool never appears here.
 */
export function buildToolset(deps: ToolDeps): ToolsetResult {
  const factories: Record<string, (deps: ToolDeps) => PlatformTool> = {
    sr_capabilities_list: createCapabilitiesListTool,
    sr_capability_get: createCapabilityGetTool,
    sr_fetch_source: createFetchSourceTool,
    sr_parse_document: createParseDocumentTool,
    sr_propose_capability_draft: createProposeDraftTool,
    sr_update_capability_draft: createUpdateDraftTool,
    sr_test_capability: createTestCapabilityTool,
    sr_sample_invoke: createSampleInvokeTool,
    sr_create_skill_draft: createSkillDraftTool,
    sr_validate_skill: createValidateSkillTool,
    sr_record_finding: createRecordFindingTool,
    sr_request_approval: createRequestApprovalTool,
  };

  // Assert the registry and the §12.3 list agree in both directions; drift here is a
  // permission bug, not a cosmetic one.
  for (const name of PLATFORM_TOOL_NAMES) {
    if (!factories[name]) throw new Error(`tool registry is missing platform tool ${name}`);
  }
  for (const name of Object.keys(factories)) {
    if (!PLATFORM_TOOL_NAMES.includes(name as (typeof PLATFORM_TOOL_NAMES)[number])) {
      throw new Error(`tool registry contains non-platform tool ${name}`);
    }
  }

  const enabled = new Set(deps.policy.enabled);
  const tools: PlatformTool[] = [];
  const disabled: string[] = [];
  for (const name of PLATFORM_TOOL_NAMES) {
    if (!enabled.has(name)) {
      disabled.push(name);
      continue;
    }
    tools.push(factories[name]!(deps));
  }
  return { tools, disabled };
}

/** Names of all tools that could ever be registered (spec §12.3, for /healthz). */
export function registrableToolNames(): string[] {
  return [...PLATFORM_TOOL_NAMES];
}

/**
 * Wrap our structural tools with upstream's `defineTool()`.
 *
 * Kept as a separate, generic function so `engine.ts` can pass the loaded SDK module
 * in and this module stays free of a static `@aragon-agent/core` import (the package
 * must remain importable when the dependency is absent — see `engine.ts`).
 */
export function defineWithHost<TModule>(
  core: TModule,
  tools: PlatformTool[],
): PlatformTool[] {
  const define = (core as { defineTool?: (config: unknown) => unknown }).defineTool;
  if (typeof define !== 'function') {
    throw new Error('loaded engine module does not export defineTool; adapter contract broken');
  }
  return tools.map((tool) => define(tool) as PlatformTool);
}

import { createRecordFindingTool } from './sr_notes.js';
