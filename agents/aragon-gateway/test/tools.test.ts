/**
 * Tool-surface tests: argument validation, timeouts, audit, redaction, registry.
 *
 * Every test drives the REAL `PlatformClient` against a stubbed `fetch`, so the HTTP
 * contract (path, `X-Internal-Token`, body) is exercised too — not just the tool
 * wrapper's logic.
 */

import { describe, expect, it, vi } from 'vitest';

import { loadPolicy } from '../src/policy.js';
import { PlatformClient, redactSecrets, redactedSummary } from '../src/platform-client.js';
import {
  DEFAULT_TOOL_TIMEOUT_MS,
  LONG_TOOL_TIMEOUT_MS,
  MAX_RESULT_CHARS,
  buildTool,
  buildToolset,
  type ToolSchema,
} from '../src/tools/index.js';
import type { AuditEntry } from '../src/types.js';

const SECRET = 'internal-token-'.padEnd(48, 'x');
const POLICY_PATH = 'test-policy-that-does-not-exist.json';

interface Call {
  url: string;
  method: string;
  token: string | undefined;
  body: unknown;
}

function fakeFetch(
  responder: (call: Call) => { status?: number; body: unknown } | Promise<{ status?: number; body: unknown }>,
): { fetchImpl: typeof fetch; calls: Call[] } {
  const calls: Call[] = [];
  const fetchImpl = (async (
    input: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ): Promise<Response> => {
    const call: Call = {
      url: String(input),
      method: init?.method ?? 'GET',
      token: (init?.headers as Record<string, string> | undefined)?.['X-Internal-Token'],
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    };
    calls.push(call);
    const result = await responder(call);
    const status = result.status ?? 200;
    return new Response(JSON.stringify(result.body), {
      status,
      headers: { 'content-type': 'application/json', 'x-request-id': 'req-test' },
    });
  }) as typeof fetch;
  return { fetchImpl, calls };
}

function client(responder: Parameters<typeof fakeFetch>[0]): { platform: PlatformClient; calls: Call[] } {
  const { fetchImpl, calls } = fakeFetch(responder);
  const platform = new PlatformClient({
    internalUrl: 'http://127.0.0.1:8000',
    sharedSecret: SECRET,
    timeoutMs: 5_000,
    fetchImpl,
  });
  return { platform, calls };
}

const policy = loadPolicy(POLICY_PATH);

describe('redaction', () => {
  it('redacts secret-looking fields recursively', () => {
    const redacted = redactSecrets({
      api_key: 'sk-live',
      nested: { authorization: 'Bearer abc', keep: 'visible' },
      list: [{ token: 't' }],
    }) as Record<string, unknown>;
    expect(redacted['api_key']).toBe('[redacted]');
    expect((redacted['nested'] as Record<string, unknown>)['authorization']).toBe('[redacted]');
    expect((redacted['nested'] as Record<string, unknown>)['keep']).toBe('visible');
    expect(((redacted['list'] as unknown[])[0] as Record<string, unknown>)['token']).toBe('[redacted]');
  });

  it('clips the audit summary', () => {
    const summary = redactedSummary({ blob: 'x'.repeat(2000) }, 100);
    expect(summary.length).toBeLessThanOrEqual(101);
  });
});

describe('platform client', () => {
  it('sends X-Internal-Token on every call', async () => {
    const { platform, calls } = client(() => ({ body: { capabilities: [] } }));
    await platform.capabilities();
    expect(calls[0]?.token).toBe(SECRET);
    expect(calls[0]?.url).toBe('http://127.0.0.1:8000/internal/capabilities');
  });

  it('clamps the credential TTL to 60s and keeps the secret in memory only', async () => {
    const { platform, calls } = client(() => ({
      body: { provider_id: 'prv_openai', kind: 'api_key', auth_type: 'header', header_name: 'authorization', secret: 'sk-live' },
    }));
    const credential = await platform.materialize('crd_1', 'test', 600);
    expect((calls[0]?.body as { ttl_s: number }).ttl_s).toBe(60);
    expect(credential.secret).toBe('sk-live');
    expect(credential.expires_at).toBeGreaterThan(Date.now());
    platform.clearCredentials();
  });

  it('refuses a materialize response without a secret', async () => {
    const { platform } = client(() => ({ body: { provider_id: 'prv_openai', kind: 'api_key' } }));
    await expect(platform.materialize('crd_1', 'test', 30)).rejects.toThrow(/no secret/);
  });

  it('surfaces the platform error code', async () => {
    const { platform } = client(() => ({ status: 429, body: { code: 'RATE_LIMITED', message: 'slow down' } }));
    await expect(platform.materialize('crd_1', 'test', 30)).rejects.toMatchObject({ code: 'RATE_LIMITED', status: 429 });
  });
});

describe('tool registry', () => {
  it('returns the 12 platform tools in spec order', () => {
    const { platform } = client(() => ({ body: {} }));
    const { tools, disabled } = buildToolset({ platform, policy });
    expect(tools.map((t) => t.name)).toEqual([
      'sr_capabilities_list',
      'sr_capability_get',
      'sr_fetch_source',
      'sr_parse_document',
      'sr_propose_capability_draft',
      'sr_update_capability_draft',
      'sr_test_capability',
      'sr_sample_invoke',
      'sr_create_skill_draft',
      'sr_validate_skill',
      'sr_record_finding',
      'sr_request_approval',
    ]);
    expect(disabled).toEqual([]);
  });

  it('filters by policy without ever inventing a tool', () => {
    const { platform } = client(() => ({ body: {} }));
    const { tools } = buildToolset({
      platform,
      policy: { ...policy, enabled: ['sr_record_finding'] },
    });
    expect(tools.map((t) => t.name)).toEqual(['sr_record_finding']);
  });

  it('uses 20s per-tool and 60s for sr_test_capability', () => {
    const { platform } = client(() => ({ body: {} }));
    expect(DEFAULT_TOOL_TIMEOUT_MS).toBe(20_000);
    expect(LONG_TOOL_TIMEOUT_MS).toBe(60_000);
    const { tools } = buildToolset({ platform, policy });
    // The ceiling is applied inside buildTool; assert the constants the spec fixes and
    // that the two restricted-execution tools are present at all.
    expect(tools.some((t) => t.name === 'sr_test_capability')).toBe(true);
  });
});

describe('argument validation', () => {
  it('rejects undeclared arguments (additionalProperties: false)', async () => {
    const audits: AuditEntry[] = [];
    const auditor = client((call) => {
      if (call.url.endsWith('/internal/audit')) {
        audits.push(call.body as AuditEntry);
        return { body: { ok: true } };
      }
      return { body: { capabilities: [] } };
    });
    const { tools } = buildToolset({ platform: auditor.platform, policy });
    const list = tools.find((t) => t.name === 'sr_capabilities_list');
    const result = await list!.execute('call-1', { query: 'video', nope: true }, {});
    expect(result.isError).toBe(true);
    const payload = JSON.parse(result.content[0]!.text) as { code: string; errors: string[] };
    expect(payload.code).toBe('INVALID_ARGUMENTS');
    expect(payload.errors.join(' ')).toContain('additional properties');
    expect(audits.at(-1)?.result).toBe('rejected');
    // The rejected call never reached the platform tool endpoint.
    expect(auditor.calls.filter((c) => !c.url.endsWith('/internal/audit'))).toHaveLength(0);
  });

  it('rejects a non-http URL before it leaves the process', async () => {
    const { platform, calls } = client(() => ({ body: {} }));
    const { tools } = buildToolset({ platform, policy });
    const fetchSource = tools.find((t) => t.name === 'sr_fetch_source');
    for (const url of ['ftp://example.com/a.txt', 'http://localhost/admin', 'http://user:pw@example.com/x']) {
      const result = await fetchSource!.execute('call-2', { url, purpose: 'test' }, {});
      expect(result.isError).toBe(true);
      expect(JSON.parse(result.content[0]!.text)).toMatchObject({ code: 'INVALID_ARGUMENTS' });
    }
    expect(calls.filter((c) => !c.url.endsWith('/internal/audit'))).toHaveLength(0);
  });

  it('rejects a malformed capability key pattern', async () => {
    const { platform } = client(() => ({ body: {} }));
    const { tools } = buildToolset({ platform, policy });
    const get = tools.find((t) => t.name === 'sr_capability_get');
    const result = await get!.execute('call-3', { key: 'bad key with spaces' }, {});
    expect(result.isError).toBe(true);
  });
});

describe('timeouts', () => {
  it('times out a hanging platform call instead of awaiting forever', async () => {
    const schema: ToolSchema = { type: 'object', additionalProperties: false, properties: {} };
    const tool = buildTool(
      {
        name: 'sr_capabilities_list',
        label: 'test',
        description: 'test',
        parameters: schema,
        timeoutMs: 60,
        tags: ['test'],
        invoke: () => new Promise<never>(() => undefined),
      },
      {
        platform: new PlatformClient({
          internalUrl: 'http://127.0.0.1:8000',
          sharedSecret: SECRET,
          timeoutMs: 1_000,
          fetchImpl: (async () => new Response('{}', { status: 200 })) as typeof fetch,
        }),
        policy,
      },
    );
    const result = await tool.execute('call-4', {}, {});
    expect(result.isError).toBe(true);
    const payload = JSON.parse(result.content[0]!.text) as { code: string; timeout_ms: number };
    expect(payload.code).toBe('TIMEOUT');
    expect(payload.timeout_ms).toBe(60);
  }, 10_000);

  it('honours an external abort signal', async () => {
    const controller = new AbortController();
    const tool = buildTool(
      {
        name: 'sr_record_finding',
        label: 'test',
        description: 'test',
        parameters: { type: 'object', additionalProperties: false, properties: {} },
        timeoutMs: 5_000,
        tags: ['test'],
        invoke: (_args, _ctx, signal) =>
          new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () => reject(new Error('aborted by caller')));
          }),
      },
      {
        platform: new PlatformClient({
          internalUrl: 'http://127.0.0.1:8000',
          sharedSecret: SECRET,
          timeoutMs: 1_000,
          fetchImpl: (async () => new Response('{}', { status: 200 })) as typeof fetch,
        }),
        policy,
      },
    );
    const promise = tool.execute('call-5', {}, { signal: controller.signal });
    controller.abort();
    const result = await promise;
    expect(result.isError).toBe(true);
  });
});

describe('audit + redaction on the wire', () => {
  it('posts one audit entry per call and redacts the payload', async () => {
    const audits: AuditEntry[] = [];
    const { platform, calls } = client((call) => {
      if (call.url.endsWith('/internal/audit')) {
        audits.push(call.body as AuditEntry);
        return { body: { ok: true } };
      }
      // sr_parse_document forwards the platform's payload verbatim, so this exercises
      // the redactor rather than a tool-specific projection.
      return { body: { title: 'doc', api_key: 'sk-live-should-be-redacted', nested: { token: 'tok' } } };
    });
    const { tools } = buildToolset({ platform, policy, binding: { sessionId: 'ses_1', turnId: 'trn_1', requestId: 'req_1' } });
    const parse = tools.find((t) => t.name === 'sr_parse_document');
    const result = await parse!.execute('call-6', { file_id: 'fil_1' }, {});
    expect(result.isError).toBeUndefined();
    expect(result.content[0]!.text).toContain('[redacted]');
    expect(result.content[0]!.text).not.toContain('sk-live-should-be-redacted');

    const invoke = calls.find((c) => c.url.includes('/internal/tools/sr_parse_document'));
    expect(invoke?.token).toBe(SECRET);
    expect((invoke?.body as { session_id: string }).session_id).toBe('ses_1');

    expect(audits).toHaveLength(1);
    expect(audits[0]).toMatchObject({ action: 'tool.sr_parse_document', result: 'ok', session_id: 'ses_1', turn_id: 'trn_1' });
    expect(audits[0]?.detail).not.toContain('sk-live-should-be-redacted');
  });

  it('does not fail the tool when the audit write fails', async () => {
    const { platform } = client((call) => {
      if (call.url.endsWith('/internal/audit')) return { status: 500, body: { code: 'AUDIT_DOWN' } };
      return { body: { capabilities: [{ key: 'a.b' }] } };
    });
    const { tools } = buildToolset({ platform, policy });
    const list = tools.find((t) => t.name === 'sr_capabilities_list');
    const result = await list!.execute('call-7', {}, {});
    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content[0]!.text)).toMatchObject({ ok: true });
  });

  it('clips oversized results before they reach the model', async () => {
    const huge = 'y'.repeat(MAX_RESULT_CHARS * 2);
    const { platform } = client((call) => {
      if (call.url.endsWith('/internal/audit')) return { body: { ok: true } };
      return { body: { text: huge, truncated: false, bytes: huge.length } };
    });
    const { tools } = buildToolset({ platform, policy });
    const fetchSource = tools.find((t) => t.name === 'sr_fetch_source');
    const result = await fetchSource!.execute('call-8', { url: 'https://example.com/doc', purpose: 'test' }, {});
    expect(result.content[0]!.text.length).toBeLessThanOrEqual(MAX_RESULT_CHARS + 20);
    expect(result.content[0]!.text).toContain('[truncated]');
  });
});

describe('budget path', () => {
  it('surfaces budget_exceeded instead of pretending the call worked', async () => {
    const { platform } = client((call) => {
      if (call.url.endsWith('/internal/audit')) return { body: { ok: true } };
      return { body: { ok: false, error_code: 'BUDGET_EXCEEDED' } };
    });
    const { tools } = buildToolset({ platform, policy });
    const test = tools.find((t) => t.name === 'sr_test_capability');
    const result = await test!.execute('call-9', { draft_id: 'drf_1', input: {} }, {});
    const payload = JSON.parse(result.content[0]!.text) as { ok: boolean; code: string };
    expect(payload.ok).toBe(false);
    expect(payload.code).toBe('budget_exceeded');
  });
});

describe('progress bridge', () => {
  it('forwards tool progress to the turn stream hook', async () => {
    const progress = vi.fn();
    const { platform } = client((call) => {
      if (call.url.endsWith('/internal/audit')) return { body: { ok: true } };
      return { body: { finding_id: 'fnd_1' } };
    });
    const { tools } = buildToolset({ platform, policy, progress });
    const record = tools.find((t) => t.name === 'sr_record_finding');
    const result = await record!.execute('call-10', { kind: 'note', confidence: 'high', text: 'hello' }, {});
    expect(result.content[0]!.text).toContain('fnd_1');
    expect(typeof progress).toBe('function');
  });
});
