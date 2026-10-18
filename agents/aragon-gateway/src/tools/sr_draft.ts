/**
 * `sr_propose_capability_draft` / `sr_update_capability_draft` — spec §12.3
 * (write-draft, never publish).
 *
 * Publishing is deliberately NOT a tool: it requires a real test run plus sample
 * evidence (spec §7.12, §12.4), and the gateway must not be able to short-circuit
 * that. The platform validates the draft body (`validate_draft()`) and owns the
 * artifact; the gateway only shapes and forwards it.
 */

import { buildTool, type PlatformTool, type ToolSchema, type ToolDeps } from './index.js';

/** Route-level request description (a subset of the standard layer, §7.6). */
const REQUEST_SCHEMA: ToolSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['method', 'path'],
  properties: {
    method: { type: 'string', enum: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] },
    path: {
      type: 'string',
      minLength: 1,
      maxLength: 1024,
      description: 'Path template, e.g. "/v1/videos/{model}". Relative to the provider base_url.',
    },
    headers: {
      type: 'object',
      additionalProperties: { type: 'string', maxLength: 1024 },
      description: 'Extra headers. Never put a secret here: credentials are injected by the proxy.',
    },
    body_template: {
      type: 'object',
      additionalProperties: true,
      description: 'Upstream JSON body template with {placeholders} resolved from param_map.',
    },
    content_type: { type: 'string', maxLength: 128 },
  },
};

const ROUTE_SCHEMA: ToolSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['provider_id', 'credential_id', 'priority', 'request'],
  properties: {
    provider_id: { type: 'string', minLength: 1, maxLength: 100 },
    credential_id: { type: 'string', minLength: 1, maxLength: 100 },
    priority: { type: 'integer', minimum: 0, maximum: 1000, description: 'Lower wins; 0 = primary route.' },
    request: REQUEST_SCHEMA,
    param_map: {
      type: 'object',
      additionalProperties: { type: 'string', maxLength: 300 },
      description: 'capability input field -> upstream template path.',
    },
    response_map: {
      type: 'object',
      additionalProperties: { type: 'string', maxLength: 300 },
      description: 'upstream JSON path -> standard output field.',
    },
    limits: {
      type: 'object',
      additionalProperties: false,
      properties: {
        timeout_ms: { type: 'integer', minimum: 100, maximum: 600_000 },
        max_requests_per_min: { type: 'integer', minimum: 1, maximum: 10_000 },
        max_response_bytes: { type: 'integer', minimum: 1024, maximum: 104_857_600 },
      },
    },
    errors: {
      type: 'array',
      maxItems: 50,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['status', 'code'],
        properties: {
          status: { type: 'integer', minimum: 100, maximum: 599 },
          code: { type: 'string', minLength: 1, maxLength: 100 },
          message: { type: 'string', maxLength: 500 },
        },
      },
    },
    stream: { type: 'boolean', description: 'True when the upstream endpoint streams (SSE).' },
    async: { type: 'boolean', description: 'True when the upstream endpoint is submit-then-poll.' },
  },
};

const EVIDENCE_SCHEMA: ToolSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['kind'],
  properties: {
    kind: { type: 'string', enum: ['document', 'test_run', 'sample_call', 'note', 'url'] },
    ref: { type: 'string', maxLength: 300 },
    note: { type: 'string', maxLength: 1000 },
  },
};

const PROPOSE_PARAMETERS: ToolSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['key', 'name', 'kind', 'modes', 'input_schema', 'output_schema', 'routes'],
  properties: {
    key: {
      type: 'string',
      minLength: 3,
      maxLength: 200,
      pattern: '^[a-z0-9]+([._-][a-z0-9]+)*$',
      description: 'Stable dotted key, e.g. "video.generate.kling".',
    },
    name: { type: 'string', minLength: 1, maxLength: 200 },
    kind: {
      type: 'string',
      enum: ['image', 'video', 'audio', 'text', 'embedding', 'rerank', 'ocr', 'other'],
    },
    modes: {
      type: 'array',
      minItems: 1,
      maxItems: 3,
      items: { type: 'string', enum: ['sync', 'async', 'stream'] },
    },
    file_io: {
      type: 'object',
      additionalProperties: false,
      properties: {
        input_files: { type: 'boolean' },
        output_files: { type: 'boolean' },
        max_input_bytes: { type: 'integer', minimum: 0, maximum: 2_147_483_648 },
      },
    },
    input_schema: {
      type: 'object',
      additionalProperties: true,
      description: 'Standard-layer JSON Schema for capability input (platform validates it).',
    },
    output_schema: {
      type: 'object',
      additionalProperties: true,
      description: 'Standard-layer JSON Schema for capability output.',
    },
    examples: {
      type: 'array',
      maxItems: 20,
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          title: { type: 'string', maxLength: 200 },
          input: { type: 'object', additionalProperties: true },
          note: { type: 'string', maxLength: 1000 },
        },
      },
    },
    routes: { type: 'array', minItems: 1, maxItems: 20, items: ROUTE_SCHEMA },
    evidence: { type: 'array', maxItems: 50, items: EVIDENCE_SCHEMA },
  },
};

const PATCH_PARAMETERS: ToolSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['draft_id', 'patch'],
  properties: {
    draft_id: { type: 'string', minLength: 3, maxLength: 200 },
    patch: {
      type: 'object',
      additionalProperties: false,
      description: 'Only these draft fields may be patched; the platform re-validates the result.',
      properties: {
        name: { type: 'string', minLength: 1, maxLength: 200 },
        kind: PROPOSE_PARAMETERS.properties!['kind']!,
        modes: PROPOSE_PARAMETERS.properties!['modes']!,
        file_io: PROPOSE_PARAMETERS.properties!['file_io']!,
        input_schema: PROPOSE_PARAMETERS.properties!['input_schema']!,
        output_schema: PROPOSE_PARAMETERS.properties!['output_schema']!,
        examples: PROPOSE_PARAMETERS.properties!['examples']!,
        routes: PROPOSE_PARAMETERS.properties!['routes']!,
        evidence: PROPOSE_PARAMETERS.properties!['evidence']!,
        notes: { type: 'string', maxLength: 4000 },
      },
    },
  },
};

export function createProposeDraftTool(deps: ToolDeps): PlatformTool {
  return buildTool<Record<string, unknown>>(
    {
      name: 'sr_propose_capability_draft',
      label: 'Propose capability draft',
      description:
        'Submit a capability draft (standard layer + provider routes + evidence) for admin ' +
        'review. This never publishes anything: it returns {draft_id, issues[]} and the ' +
        'platform validates the structure and safety of every route.',
      tags: ['platform', 'write-draft'],
      parameters: PROPOSE_PARAMETERS,
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_propose_capability_draft',
          args,
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
        );
        return {
          ok: raw['ok'] !== false,
          draft_id: typeof raw['draft_id'] === 'string' ? raw['draft_id'] : null,
          issues: Array.isArray(raw['issues']) ? raw['issues'] : [],
        };
      },
    },
    deps,
  );
}

export function createUpdateDraftTool(deps: ToolDeps): PlatformTool {
  return buildTool<{ draft_id: string; patch: Record<string, unknown> }>(
    {
      name: 'sr_update_capability_draft',
      label: 'Update capability draft',
      description:
        'Patch an existing capability draft. Only declared fields may change; the platform ' +
        're-runs draft validation and returns the remaining issues.',
      tags: ['platform', 'write-draft'],
      parameters: PATCH_PARAMETERS,
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_update_capability_draft',
          { draft_id: args.draft_id, patch: args.patch },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
        );
        return {
          ok: raw['ok'] !== false,
          draft_id: typeof raw['draft_id'] === 'string' ? raw['draft_id'] : args.draft_id,
          issues: Array.isArray(raw['issues']) ? raw['issues'] : [],
        };
      },
    },
    deps,
  );
}
