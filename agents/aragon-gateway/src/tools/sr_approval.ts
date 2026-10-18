/**
 * `sr_request_approval` — spec §12.3 (needs a human).
 *
 * The agent's escape hatch when an action falls outside the pre-authorized scope
 * (extra providers, extra budget, publishing without evidence). The platform owns the
 * approval row, the 10-minute expiry and the audit trail (§7.12); the gateway only
 * files the request and reports the decision it got back.
 */

import { buildTool, type PlatformTool, type ToolDeps } from './index.js';

export function createRequestApprovalTool(deps: ToolDeps): PlatformTool {
  return buildTool<{
    action: string;
    target: string;
    impact: string;
    requested_scope: Record<string, unknown>;
  }>(
    {
      name: 'sr_request_approval',
      label: 'Request human approval',
      description:
        'File an approval request for something outside the current authorized scope ' +
        '(new provider, higher budget, publishing, destructive change). Returns ' +
        '{approval_id, decision}; decision "pending" means the admin must answer — do not ' +
        'retry the blocked action.',
      tags: ['platform', 'human-in-the-loop'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['action', 'target', 'impact', 'requested_scope'],
        properties: {
          action: {
            type: 'string',
            minLength: 3,
            maxLength: 120,
            pattern: '^[a-z0-9]+([._:-][a-z0-9]+)*$',
            description: 'Machine-readable action key, e.g. "provider.add" or "budget.increase".',
          },
          target: {
            type: 'string',
            minLength: 1,
            maxLength: 300,
            description: 'Object the action applies to (capability key, provider id, skill id).',
          },
          impact: {
            type: 'string',
            minLength: 10,
            maxLength: 2000,
            description: 'What changes if approved, including cost and blast radius.',
          },
          requested_scope: {
            type: 'object',
            additionalProperties: false,
            properties: {
              providers: { type: 'array', maxItems: 16, items: { type: 'string', maxLength: 100 } },
              max_test_calls: { type: 'integer', minimum: 1, maximum: 1000 },
              max_cost_usd: { type: 'number', minimum: 0, maximum: 10_000 },
              window_hours: { type: 'integer', minimum: 1, maximum: 720 },
              one_shot: { type: 'boolean', default: true },
            },
          },
        },
      },
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_request_approval',
          {
            action: args.action,
            target: args.target,
            impact: args.impact,
            requested_scope: args.requested_scope,
          },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
        );
        const decision = typeof raw['decision'] === 'string' ? raw['decision'] : 'pending';
        return {
          ok: true,
          approval_id: typeof raw['approval_id'] === 'string' ? raw['approval_id'] : null,
          decision,
          expires_at: raw['expires_at'] ?? null,
          message:
            decision === 'pending'
              ? 'Waiting for an administrator decision (expires in 10 minutes). Do not retry the blocked action.'
              : `Decision: ${decision}.`,
        };
      },
    },
    deps,
  );
}
