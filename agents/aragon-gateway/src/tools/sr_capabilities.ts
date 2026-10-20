/**
 * `sr_capabilities_list` / `sr_capability_get` — spec §12.3 (read-only).
 *
 * Both are served from the platform's capability catalogue (`GET
 * /internal/capabilities`, spec §9.5) because that is the only place where
 * published/draft state and per-route limits live. The gateway filters, it does not
 * decide: a capability that is not in the catalogue is not visible to the agent.
 */

import { PlatformError } from '../platform-client.js';
import { buildTool, type PlatformTool, type ToolDeps } from './index.js';

const SUMMARY_FIELDS = ['key', 'name', 'kind', 'mode', 'modes', 'status'] as const;

export function createCapabilitiesListTool(deps: ToolDeps): PlatformTool {
  return buildTool<{ query?: string; kind?: string; mode?: string; limit?: number }>(
    {
      name: 'sr_capabilities_list',
      label: 'List capabilities',
      description:
        'List the capabilities currently visible on the platform, optionally filtered. ' +
        'Returns key/name/kind/mode/status only — call sr_capability_get for schemas, ' +
        'limits and examples.',
      tags: ['platform', 'read-only'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          query: {
            type: 'string',
            maxLength: 200,
            description: 'Case-insensitive substring match against capability key and name.',
          },
          kind: { type: 'string', maxLength: 64, description: 'Filter by capability kind.' },
          mode: { type: 'string', maxLength: 32, description: 'Filter by execution mode (sync/async/stream).' },
          limit: { type: 'integer', minimum: 1, maximum: 200, default: 50 },
        },
      },
      async invoke(args, ctx, signal) {
        const catalog = await deps.platform.capabilities({ signal, requestId: ctx.request_id });
        const needle = args.query?.trim().toLowerCase() ?? '';
        const rows = catalog.capabilities.filter((cap) => {
          if (args.kind !== undefined && String(cap.kind ?? '') !== args.kind) return false;
          // A capability can support several modes (`modes: string[]`); matching only a
          // single `mode` field would drop every multi-mode capability.
          if (args.mode !== undefined) {
            const modes = cap.modes ?? (cap.mode === undefined ? [] : [cap.mode]);
            if (!modes.includes(args.mode)) return false;
          }
          if (needle === '') return true;
          const haystack = `${cap.key} ${String(cap.name ?? '')}`.toLowerCase();
          return haystack.includes(needle);
        });
        const limit = args.limit ?? 50;
        const projected = rows.slice(0, limit).map((cap) => {
          const row: Record<string, unknown> = {};
          for (const field of SUMMARY_FIELDS) if (cap[field] !== undefined) row[field] = cap[field];
          return row;
        });
        return {
          ok: true,
          capabilities: projected,
          matched: rows.length,
          total: catalog.capabilities.length,
          truncated: rows.length > projected.length,
          catalog_version: catalog.etag ?? null,
        };
      },
    },
    deps,
  );
}

export function createCapabilityGetTool(deps: ToolDeps): PlatformTool {
  return buildTool<{ key: string }>(
    {
      name: 'sr_capability_get',
      label: 'Get capability',
      description:
        'Fetch one capability by key: standard-layer schema, modes, limits, examples and ' +
        'published version. Read-only.',
      tags: ['platform', 'read-only'],
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['key'],
        properties: {
          key: {
            type: 'string',
            minLength: 1,
            maxLength: 200,
            pattern: '^[A-Za-z0-9._:-]+$',
            description: 'Capability key, e.g. "video.generate.kling".',
          },
        },
      },
      async invoke(args, ctx, signal) {
        const path = `/internal/capabilities/${encodeURIComponent(args.key)}`;
        try {
          const detail = await deps.platform.call<Record<string, unknown>>('GET', path, undefined, {
            signal,
            requestId: ctx.request_id,
          });
          return { ok: true, capability: detail };
        } catch (err) {
          if (!(err instanceof PlatformError) || err.status !== 404) throw err;
          // Fall back to the catalogue so an unpublished-but-listed capability still
          // resolves; an unknown key stays a hard "not found".
          const catalog = await deps.platform.capabilities({ signal, requestId: ctx.request_id });
          const hit = catalog.capabilities.find((cap) => cap.key === args.key);
          if (!hit) {
            return { ok: false, code: 'CAPABILITY_NOT_FOUND', key: args.key };
          }
          return { ok: true, capability: hit, detail_unavailable: true };
        }
      },
    },
    deps,
  );
}
