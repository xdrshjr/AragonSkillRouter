/**
 * `sr_record_finding` — spec §12.3 (write note).
 *
 * The agent's way of recording a gap honestly instead of guessing: missing
 * information, an unverifiable claim, an unsafe route it refused to draft. The
 * platform stores it as an `agent_artifacts`-adjacent note and shows it in the flow
 * view, so "what the agent could not establish" survives the turn.
 */

import { buildTool, type PlatformTool, type ToolDeps } from './index.js';

export function createRecordFindingTool(deps: ToolDeps): PlatformTool {
  return buildTool<{
    kind: string;
    confidence: string;
    text: string;
    targets?: string[];
  }>(
    {
      name: 'sr_record_finding',
      label: 'Record a finding',
      description:
        'Record a finding, gap or caveat for the human reviewer: missing information, ' +
        'unverified behaviour, an unsafe route that was deliberately not drafted. Returns ' +
        '{finding_id}.',
      tags: ['platform', 'note'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['kind', 'confidence', 'text'],
        properties: {
          kind: {
            type: 'string',
            enum: ['missing_info', 'gap', 'risk', 'note', 'blocked', 'assumption'],
          },
          confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
          text: { type: 'string', minLength: 3, maxLength: 4000 },
          targets: {
            type: 'array',
            maxItems: 16,
            items: { type: 'string', minLength: 1, maxLength: 300 },
            description: 'Capability keys, draft ids, file ids or URLs this finding is about.',
          },
        },
      },
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_record_finding',
          {
            kind: args.kind,
            confidence: args.confidence,
            text: args.text,
            targets: args.targets ?? [],
          },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
        );
        return {
          ok: raw['ok'] !== false,
          finding_id: typeof raw['finding_id'] === 'string' ? raw['finding_id'] : null,
          kind: args.kind,
        };
      },
    },
    deps,
  );
}
