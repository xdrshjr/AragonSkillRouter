/**
 * `sr_create_skill_draft` / `sr_validate_skill` — spec §12.3.
 *
 * The platform's own SKILL import pipeline (§7.9) is the replacement for upstream's
 * `skill_install`/`skill_create` builtins, which the gateway never registers. So the
 * agent can draft and validate a SKILL, but installation stays an explicit admin
 * action.
 */

import { PlatformError } from '../platform-client.js';
import { buildTool, type PlatformTool, type ToolDeps } from './index.js';

const NAME_PATTERN = '^[a-z0-9]+(-[a-z0-9]+)*$';

export function createSkillDraftTool(deps: ToolDeps): PlatformTool {
  return buildTool<{
    name: string;
    description: string;
    instructions: string;
    execution_plan?: string[];
    permissions?: Record<string, unknown>;
  }>(
    {
      name: 'sr_create_skill_draft',
      label: 'Create SKILL draft',
      description:
        'Create a SKILL draft (frontmatter-compatible name/description plus instructions, ' +
        'execution plan and permission declaration). Draft only — installation and publish ' +
        'are admin actions.',
      tags: ['platform', 'write-draft'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'description', 'instructions'],
        properties: {
          name: {
            type: 'string',
            minLength: 2,
            maxLength: 64,
            pattern: NAME_PATTERN,
            description: 'kebab-case SKILL name (upstream frontmatter rule, ≤64 chars).',
          },
          description: {
            type: 'string',
            minLength: 10,
            maxLength: 500,
            description: 'What the skill does AND when to use it (upstream treats this as the match key).',
          },
          instructions: { type: 'string', minLength: 20, maxLength: 20_000 },
          execution_plan: {
            type: 'array',
            maxItems: 30,
            items: { type: 'string', minLength: 1, maxLength: 500 },
          },
          permissions: {
            type: 'object',
            additionalProperties: false,
            description: 'Declared permission surface; the platform rejects anything it cannot enforce.',
            properties: {
              allowed_tools: {
                type: 'array',
                maxItems: 24,
                items: { type: 'string', pattern: '^sr_[a-z0-9_]+$' },
                description: 'Platform tool names only — file/shell tools do not exist here.',
              },
              network_hosts: { type: 'array', maxItems: 32, items: { type: 'string', maxLength: 253 } },
              activation: { type: 'string', enum: ['auto', 'always', 'manual'] },
            },
          },
        },
      },
      async invoke(args, ctx, signal) {
        const raw = await deps.platform.invokeTool<Record<string, unknown>>(
          'sr_create_skill_draft',
          {
            name: args.name,
            description: args.description,
            instructions: args.instructions,
            execution_plan: args.execution_plan ?? [],
            permissions: args.permissions ?? {},
          },
          { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
        );
        return {
          ok: raw['ok'] !== false,
          skill_id: typeof raw['skill_id'] === 'string' ? raw['skill_id'] : null,
          issues: Array.isArray(raw['issues']) ? raw['issues'] : [],
        };
      },
    },
    deps,
  );
}

export function createValidateSkillTool(deps: ToolDeps): PlatformTool {
  return buildTool<{ skill_id: string }>(
    {
      name: 'sr_validate_skill',
      label: 'Validate SKILL draft',
      description:
        'Validate a SKILL draft: frontmatter, path/permission declarations and tool references. ' +
        'Returns {ok, issues[]}. Read-only.',
      tags: ['platform', 'read-only'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['skill_id'],
        properties: {
          skill_id: { type: 'string', minLength: 3, maxLength: 200, pattern: '^[A-Za-z0-9._:-]+$' },
        },
      },
      async invoke(args, ctx, signal) {
        try {
          const raw = await deps.platform.invokeTool<Record<string, unknown>>(
            'sr_validate_skill',
            { skill_id: args.skill_id },
            { sessionId: ctx.session_id, turnId: ctx.turn_id, requestId: ctx.request_id, timeoutMs: 20_000, signal },
          );
          return {
            ok: raw['ok'] === true,
            skill_id: args.skill_id,
            issues: Array.isArray(raw['issues']) ? raw['issues'] : [],
          };
        } catch (err) {
          if (err instanceof PlatformError && err.status === 404) {
            return { ok: false, skill_id: args.skill_id, issues: [{ code: 'SKILL_NOT_FOUND' }] };
          }
          throw err;
        }
      },
    },
    deps,
  );
}
