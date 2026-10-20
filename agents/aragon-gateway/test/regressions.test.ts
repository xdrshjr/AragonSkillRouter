/**
 * Regressions found while making the Agent actually answer through a real provider.
 *
 * Each case encodes a live failure, not a hypothetical:
 *
 * 1. `GET /internal/capabilities` answers `{items: [...]}`; the gateway only read
 *    `capabilities`, so `sr_capabilities_list` reported an empty catalogue forever.
 * 2. The catalogue exposes `modes: string[]`, so filtering on a single `mode` dropped
 *    every multi-mode capability.
 * 3. A turn the engine child rejected was logged and then finished silently, leaving
 *    the platform's turn `running` with zero events (the Agent panel spun forever).
 */

import { describe, expect, it } from 'vitest';

import { rejectedTurnPayload } from '../src/engine-host.js';
import { loadPolicy } from '../src/policy.js';
import { PlatformClient } from '../src/platform-client.js';
import { buildToolset } from '../src/tools/index.js';

const SECRET = 'internal-token-'.padEnd(48, 'x');

function client(responder: (url: string) => unknown): PlatformClient {
  const fetchImpl = (async (input: Parameters<typeof fetch>[0]): Promise<Response> => {
    const body = responder(String(input));
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json', 'x-request-id': 'req-test' },
    });
  }) as typeof fetch;
  return new PlatformClient({
    internalUrl: 'http://127.0.0.1:8000',
    sharedSecret: SECRET,
    timeoutMs: 5_000,
    fetchImpl,
  });
}

const PLATFORM_CATALOG = {
  items: [
    {
      key: 'text.generate.echo',
      name: 'Echo',
      kind: 'text',
      modes: ['sync', 'stream'],
      version: '1.0.0',
      tags: [],
      input_schema: { type: 'object' },
      examples: [],
    },
    {
      key: 'image.generate.kling',
      name: 'Kling',
      kind: 'image',
      modes: ['async'],
      version: '1.0.0',
      tags: [],
      input_schema: { type: 'object' },
      examples: [],
    },
  ],
};

describe('platform catalogue normalization', () => {
  it('reads the platform `items` envelope', async () => {
    const platform = client(() => PLATFORM_CATALOG);
    const catalog = await platform.capabilities();
    expect(catalog.capabilities.map((c) => c.key)).toEqual([
      'text.generate.echo',
      'image.generate.kling',
    ]);
  });

  it('projects modes/mode so tool filters keep multi-mode capabilities', async () => {
    const platform = client(() => PLATFORM_CATALOG);
    const catalog = await platform.capabilities();
    const echo = catalog.capabilities[0];
    expect(echo?.modes).toEqual(['sync', 'stream']);
    expect(echo?.mode).toBe('sync');
    expect(echo?.status).toBe('published');
  });

  it('sr_capabilities_list returns rows from the real envelope and filters by mode', async () => {
    const platform = client((url) =>
      url.endsWith('/internal/audit') ? { ok: true } : PLATFORM_CATALOG,
    );
    const { tools } = buildToolset({ platform, policy: loadPolicy('test-policy-that-does-not-exist.json') });
    const list = tools.find((t) => t.name === 'sr_capabilities_list');
    const everything = JSON.parse((await list!.execute('c1', {}, {})).content[0]!.text) as {
      capabilities: Array<{ key: string; total?: number }>;
      total: number;
    };
    expect(everything.total).toBe(2);
    expect(everything.capabilities).toHaveLength(2);

    const streaming = JSON.parse((await list!.execute('c2', { mode: 'stream' }, {})).content[0]!.text) as {
      capabilities: Array<{ key: string }>;
    };
    expect(streaming.capabilities.map((c) => c.key)).toEqual(['text.generate.echo']);

    const async = JSON.parse((await list!.execute('c3', { mode: 'async' }, {})).content[0]!.text) as {
      capabilities: Array<{ key: string }>;
    };
    expect(async.capabilities.map((c) => c.key)).toEqual(['image.generate.kling']);
  });
});

describe('rejected child turn', () => {
  it('emits turn.failed so the platform turn cannot stay running', () => {
    const payload = rejectedTurnPayload({ detail: 'no model configured for this turn', code: 'TURN_CONFLICT' });
    expect(payload['code']).toBe('TURN_CONFLICT');
    expect(String(payload['message'])).toContain('no model configured');
    expect(String(payload['hint'])).toContain('Agent 模型配置');
  });

  it('defaults the code when the child did not supply one', () => {
    expect(rejectedTurnPayload({}).code).toBe('ENGINE_TURN_REJECTED');
  });
});
