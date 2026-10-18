import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { EXIT } from '../src/http.js';
import { buildMinimalInput, describeStep, runVerify, type VerifyReport } from '../src/verify.js';

const TOKEN = 'srk_9f8e7d6c_secretsecretsecret';
const BASE_URL = 'https://router.example.com';

let calls: string[];
const realFetch = globalThis.fetch;

function jsonResponse(body: unknown, status = 200, requestId = 'rid_verify'): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'x-request-id': requestId },
  });
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response): void {
  const stub = (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    calls.push(`${init?.method ?? 'GET'} ${url}`);
    return Promise.resolve(handler(url, init));
  };
  globalThis.fetch = stub as unknown as typeof fetch;
}

function healthy(url: string): Response {
  if (url.endsWith('/api/v1/health')) {
    return jsonResponse({ status: 'ok', version: '1.0.0', engine: { version: '0.2.34', status: 'available' } });
  }
  if (url.endsWith('/api/v1/whoami')) {
    return jsonResponse({
      token_id: 'tok_1',
      agent: { id: 'agt_1', name: 'local-cli' },
      prefix: 'srk_9f8e7d6c',
      scopes: ['text.*'],
      allowed_modes: ['sync'],
    });
  }
  if (url.includes('/api/v1/capabilities')) {
    return jsonResponse({ items: [{ key: 'text.generate.gpt4o-mini' }] });
  }
  return jsonResponse({ error: { code: 'NOT_FOUND', message: 'not found' } }, 404);
}

function stepNames(report: VerifyReport): string[] {
  return report.steps.map((step) => step.name);
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe('buildMinimalInput', () => {
  it('fills scalar required fields from the schema', () => {
    const minimal = buildMinimalInput({
      type: 'object',
      required: ['prompt', 'duration', 'hd'],
      properties: {
        prompt: { type: 'string' },
        duration: { type: 'integer', minimum: 5 },
        hd: { type: 'boolean' },
      },
    });

    expect(minimal.input).toEqual({ prompt: 'probe', duration: 5, hd: false });
    expect(minimal.filled).toEqual(['prompt', 'duration', 'hd']);
    expect(minimal.skipped).toEqual([]);
  });

  it('prefers enum values and never guesses objects/arrays', () => {
    const minimal = buildMinimalInput({
      type: 'object',
      required: ['format', 'reference_image', 'mask', 'unknown'],
      properties: {
        format: { type: 'string', enum: ['png', 'jpeg'] },
        reference_image: { type: 'object' },
        mask: { type: 'array', items: { type: 'string' } },
      },
    });

    expect(minimal.input).toEqual({ format: 'png' });
    expect(minimal.filled).toEqual(['format']);
    expect(minimal.skipped).toEqual(['reference_image', 'mask', 'unknown']);
  });

  it('returns an empty input when the schema is missing', () => {
    expect(buildMinimalInput(undefined)).toEqual({ input: {}, filled: [], skipped: [] });
  });
});

describe('runVerify', () => {
  it('checks health → whoami → capabilities in that order', async () => {
    mockFetch((url) => healthy(url));

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    expect(report.ok).toBe(true);
    expect(report.exitCode).toBe(EXIT.OK);
    expect(stepNames(report)).toEqual(['GET /api/v1/health', 'GET /api/v1/whoami', 'GET /api/v1/capabilities']);
    expect(calls).toEqual([
      `GET ${BASE_URL}/api/v1/health`,
      `GET ${BASE_URL}/api/v1/whoami`,
      `GET ${BASE_URL}/api/v1/capabilities`,
    ]);
    expect(report.steps[0]?.detail).toContain('version=1.0.0');
    expect(report.steps[1]?.detail).toContain('token_id=tok_1');
    expect(report.steps[2]?.detail).toContain('1 个可用能力');
    expect(report.steps.every((step) => step.requestId === 'rid_verify')).toBe(true);
  });

  it('sends the bearer token on whoami/capabilities but keeps health anonymous', async () => {
    const authHeaders: Array<string | null> = [];
    mockFetch((url, init) => {
      const headers = new Headers(init?.headers);
      authHeaders.push(headers.get('authorization'));
      return healthy(url);
    });

    await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    expect(authHeaders[0]).toBeNull();
    expect(authHeaders[1]).toBe(`Bearer ${TOKEN}`);
    expect(authHeaders[2]).toBe(`Bearer ${TOKEN}`);
  });

  it('reports a revoked token as ✗ on whoami and exits 1', async () => {
    mockFetch((url) => {
      if (url.endsWith('/api/v1/whoami')) {
        return jsonResponse(
          { error: { code: 'TOKEN_REVOKED', message: '令牌已撤销', hint: '重新创建令牌', request_id: 'rid_auth' } },
          401,
          'rid_header',
        );
      }
      return healthy(url);
    });

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    expect(report.ok).toBe(false);
    expect(report.exitCode).toBe(EXIT.FAILURE);
    const failed = report.steps[1];
    expect(failed?.ok).toBe(false);
    expect(failed?.failure).toBe('auth');
    expect(failed?.requestId).toBe('rid_auth');
    expect(failed?.detail).toContain('TOKEN_REVOKED');
    expect(describeStep(failed!)).toContain('✗');
  });

  it('exits 3 when the platform is unreachable', async () => {
    globalThis.fetch = (() => Promise.reject(new TypeError('fetch failed'))) as unknown as typeof fetch;

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    expect(report.ok).toBe(false);
    expect(report.exitCode).toBe(EXIT.CONNECTIVITY);
    expect(report.steps[0]?.failure).toBe('connectivity');
    expect(report.steps[0]?.detail).toContain('NETWORK_ERROR');
    expect(report.steps).toHaveLength(3);
  });

  it('exits 3 on timeout', async () => {
    const timeoutError = new Error('The operation was aborted due to timeout');
    timeoutError.name = 'TimeoutError';
    globalThis.fetch = (() => Promise.reject(timeoutError)) as unknown as typeof fetch;

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    expect(report.exitCode).toBe(EXIT.CONNECTIVITY);
    expect(report.steps[0]?.detail).toContain('NETWORK_TIMEOUT');
  });

  it('runs one real minimal invoke when --probe-capability is given', async () => {
    const bodies: string[] = [];
    mockFetch((url, init) => {
      if (url.includes('/invoke')) {
        bodies.push(String(init?.body));
        return jsonResponse({
          request_id: 'req_1',
          status: 'succeeded',
          provider: { id: 'prv_openai', name: 'OpenAI' },
          output: { text: 'ok' },
        });
      }
      return healthy(url);
    });

    const report = await runVerify({
      baseUrl: BASE_URL,
      token: TOKEN,
      probeCapability: 'text.generate.gpt4o-mini',
    });

    expect(report.ok).toBe(true);
    expect(stepNames(report)).toHaveLength(4);
    expect(stepNames(report)[3]).toBe('POST /api/v1/capabilities/text.generate.gpt4o-mini/invoke');
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toContain('"mode":"sync"');
    expect(report.steps[3]?.detail).toContain('provider=OpenAI');
  });

  it('falls back to platform default mode when sync is unsupported', async () => {
    const bodies: string[] = [];
    mockFetch((url, init) => {
      if (url.includes('/invoke')) {
        bodies.push(String(init?.body));
        if (bodies.length === 1) {
          return jsonResponse(
            {
              error: {
                code: 'UNSUPPORTED_MODE',
                message: '该能力不支持 sync',
                details: { supported: ['async'] },
                request_id: 'rid_mode',
              },
            },
            400,
          );
        }
        return jsonResponse({ request_id: 'req_2', status: 'succeeded', provider: { id: 'prv_kling' } }, 202);
      }
      return healthy(url);
    });

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN, probeCapability: 'video.generate.x' });

    expect(bodies).toHaveLength(2);
    expect(bodies[1]).not.toContain('"mode":"sync"');
    expect(report.steps[3]?.ok).toBe(true);
    expect(report.ok).toBe(true);
  });

  it('derives the probe input from the capability input_schema', async () => {
    const bodies: string[] = [];
    const requested: string[] = [];
    mockFetch((url, init) => {
      if (url.endsWith('/api/v1/capabilities/text.generate.schema')) {
        requested.push(url);
        return jsonResponse({
          key: 'text.generate.schema',
          modes: ['async', 'sync'],
          input_schema: {
            type: 'object',
            required: ['prompt'],
            properties: { prompt: { type: 'string' } },
          },
        });
      }
      if (url.includes('/invoke')) {
        bodies.push(String(init?.body));
        return jsonResponse({ request_id: 'req_s', status: 'succeeded', provider: { id: 'prv_s' } });
      }
      return healthy(url);
    });

    const report = await runVerify({
      baseUrl: BASE_URL,
      token: TOKEN,
      probeCapability: 'text.generate.schema',
    });

    expect(requested).toHaveLength(1);
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toContain('"prompt":"probe"');
    expect(bodies[0]).toContain('"mode":"sync"');
    expect(report.steps[3]?.detail).toContain('input=prompt');
    expect(report.ok).toBe(true);
  });

  it('explains an input-schema rejection instead of pretending the platform is broken', async () => {
    mockFetch((url) => {
      if (url.includes('/invoke')) {
        return jsonResponse(
          {
            error: {
              code: 'INPUT_SCHEMA_INVALID',
              message: 'input.prompt 必填',
              request_id: 'rid_input',
            },
          },
          400,
        );
      }
      return healthy(url);
    });

    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN, probeCapability: 'text.generate.x' });

    const probe = report.steps[3];
    expect(probe?.ok).toBe(false);
    expect(probe?.failure).toBe('probe-input');
    expect(probe?.hint).toContain('invoke.mjs');
    // 只读三项仍然通过 → 连通性正常，退出码是一般失败
    expect(report.ok).toBe(false);
    expect(report.exitCode).toBe(EXIT.FAILURE);
  });

  it('returns a machine-readable shape the CLI --json mode can print', async () => {
    mockFetch((url) => healthy(url));
    const report = await runVerify({ baseUrl: BASE_URL, token: TOKEN });

    const serialized = JSON.stringify({
      ok: report.ok,
      steps: report.steps.map((step) => ({
        name: step.name,
        ok: step.ok,
        detail: step.detail,
        request_id: step.requestId ?? null,
      })),
    });
    expect(serialized).toContain('GET /api/v1/health');
    expect(serialized).not.toContain(TOKEN);
  });
});
