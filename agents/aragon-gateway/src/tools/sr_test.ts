/**
 * `sr_test_capability` / `sr_sample_invoke` — spec §12.3 (restricted execution).
 *
 * These are the only tools that cause a real upstream request, so they get the
 * longer 60s ceiling and a mandatory platform budget pre-check: the platform decides
 * whether the flow still has test-call / cost budget (§12.3 "预算与自动执行范围").
 * A budget rejection is returned as `budget_exceeded` — never silently retried and
 * never turned into a fake success.
 */

import { buildTool, LONG_TOOL_TIMEOUT_MS, type PlatformTool, type ToolDeps } from './index.js';

export function createTestCapabilityTool(deps: ToolDeps): PlatformTool {
  return buildTool<{
    draft_id: string;
    input: Record<string, unknown>;
    route_id?: string;
    save_payload?: boolean;
  }>(
    {
      name: 'sr_test_capability',
      label: 'Test capability draft',
      description:
        'Run one real upstream request against a capability draft route (credential injected ' +
        'by the platform proxy) and return a redacted preview: ' +
        '{ok,http_status,latency_ms,response_preview,error_code,used_route}. Counts against ' +
        'the flow test-call budget.',
      tags: ['platform', 'restricted-execution'],
      timeoutMs: LONG_TOOL_TIMEOUT_MS,
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['draft_id', 'input'],
        properties: {
          draft_id: { type: 'string', minLength: 3, maxLength: 200 },
          input: {
            type: 'object',
            additionalProperties: true,
            description: 'Capability input for this test call; validated against the draft input_schema.',
          },
          route_id: {
            type: 'string',
            maxLength: 200,
            description: 'Pin the test to one route; omit to let the platform pick by priority.',
          },
          save_payload: {
            type: 'boolean',
            default: false,
            description: 'Persist the redacted request/response as test evidence (default false).',
          },
        },
      },
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_test_capability',
          {
            draft_id: args.draft_id,
            input: args.input,
            ...(args.route_id === undefined ? {} : { route_id: args.route_id }),
            save_payload: args.save_payload === true,
          },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 55_000, signal },
        );
        if (raw['error_code'] === 'BUDGET_EXCEEDED' || raw['code'] === 'budget_exceeded') {
          return {
            ok: false,
            code: 'budget_exceeded',
            message:
              'The flow test-call/cost budget is exhausted. Ask the administrator via ' +
              'sr_request_approval instead of retrying.',
            detail: raw,
          };
        }
        return {
          ok: raw['ok'] === true,
          http_status: typeof raw['http_status'] === 'number' ? raw['http_status'] : null,
          latency_ms: typeof raw['latency_ms'] === 'number' ? raw['latency_ms'] : null,
          response_preview: raw['response_preview'] ?? null,
          error_code: raw['error_code'] ?? null,
          used_route: raw['used_route'] ?? null,
          test_run_id: raw['test_run_id'] ?? null,
        };
      },
    },
    deps,
  );
}

export function createSampleInvokeTool(deps: ToolDeps): PlatformTool {
  return buildTool<{ capability_key: string; version?: string; input: Record<string, unknown> }>(
    {
      name: 'sr_sample_invoke',
      label: 'Sample invoke published capability',
      description:
        'Run one minimal invoke against a published capability version and return a redacted ' +
        'result. Restricted execution: counts against the flow budget.',
      tags: ['platform', 'restricted-execution'],
      timeoutMs: LONG_TOOL_TIMEOUT_MS,
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['capability_key', 'input'],
        properties: {
          capability_key: {
            type: 'string',
            minLength: 3,
            maxLength: 200,
            pattern: '^[a-z0-9]+([._-][a-z0-9]+)*$',
          },
          version: {
            type: 'string',
            maxLength: 32,
            description: 'Capability version (semver); omit for the published version.',
          },
          input: { type: 'object', additionalProperties: true },
        },
      },
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_sample_invoke',
          {
            capability_key: args.capability_key,
            ...(args.version === undefined ? {} : { version: args.version }),
            input: args.input,
          },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 55_000, signal },
        );
        if (raw['code'] === 'budget_exceeded' || raw['error_code'] === 'BUDGET_EXCEEDED') {
          return {
            ok: false,
            code: 'budget_exceeded',
            message: 'Flow budget exhausted; request approval instead of retrying.',
            detail: raw,
          };
        }
        return {
          ok: raw['ok'] === true,
          request_id: raw['request_id'] ?? null,
          status: raw['status'] ?? null,
          output: raw['output'] ?? null,
          usage: raw['usage'] ?? null,
          error_code: raw['error_code'] ?? null,
        };
      },
    },
    deps,
  );
}
