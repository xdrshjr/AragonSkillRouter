#!/usr/bin/env node
/**
 * Reproduce the reported failure and prove the fix, against the installed app.
 *
 * The report: adding an LLM credential for `open.bigmodel.cn` failed with
 * "resolves to an address inside a private or reserved range (198.18.0.201)".
 *
 * This script starts the installed app with an isolated data dir, then:
 *   1. reads /api/meta and prints the effective outbound policy
 *   2. creates the provider + credential exactly as the UI does
 *   3. runs POST /api/admin/credentials/{id}/verify, which performs a REAL
 *      outbound request to the provider — the operation that was blocked
 *   4. asserts a second, unrelated provider host is also reachable (the failure
 *      was machine-wide, not host-specific)
 *   5. asserts loopback and cloud-metadata targets are still refused
 *
 * Exit code 0 means the reported scenario now works and the guard is still tight.
 *
 *   node desktop/scripts/verify-outbound.mjs [--exe <path>] [--key <api key>]
 */

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');

const argv = process.argv.slice(2);
const argValue = (name, fallback) => {
  const index = argv.indexOf(name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
};

const exePath = path.resolve(
  argValue('--exe', path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe')),
);
const dataDir = path.resolve(argValue('--data', path.join(desktopDir, 'build', 'outbound-data')));
const apiKey = argValue('--key', process.env.SR_TEST_ZHIPU_KEY ?? 'verify-not-a-real-key');
const PASSWORD = 'outbound-Test!Passw0rd7';

const results = [];
const record = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}: ${detail}`);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last = 'no attempt';
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      last = error instanceof Error ? error.message : String(error);
    }
    await sleep(400);
  }
  throw new Error(`${label} timed out (${last})`);
}

function readState() {
  const file = path.join(dataDir, 'runtime.json');
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

function readCookie(response, name) {
  for (const entry of response.headers.getSetCookie?.() ?? []) {
    const [pair] = entry.split(';');
    const [key, value] = pair.split('=');
    if (key.trim() === name) return value.trim();
  }
  return null;
}

async function main() {
  if (!existsSync(exePath)) {
    console.error(`Installed app not found: ${exePath}`);
    process.exit(2);
  }
  rmSync(dataDir, { recursive: true, force: true });
  console.log(`app:  ${exePath}`);
  console.log(`data: ${dataDir}\n`);

  const child = spawn(exePath, [], {
    env: { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  child.stdout.on('data', () => undefined);
  child.stderr.on('data', () => undefined);

  try {
    const state = await waitFor(() => readState(), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${state.shellPort}`;
    await waitFor(async () => {
      const response = await fetch(`${origin}/api/health/ready`);
      return response.status === 200 ? response : null;
    }, 90_000, 'API ready');

    // Sign in and clear the first-run gate so admin routes answer.
    const login = await fetch(`${origin}/api/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin },
      body: JSON.stringify({ username: 'admin', password: PASSWORD }),
    });
    const loginBody = await login.json();
    const cookie = `sr_session=${readCookie(login, 'sr_session')}; sr_csrf=${readCookie(login, 'sr_csrf')}`;
    const changed = await fetch(`${origin}/api/auth/change-password`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': loginBody.csrf_token },
      body: JSON.stringify({ current_password: PASSWORD, new_password: 'outbound-Fixed!Passw0rd9' }),
    });
    const csrf = (await changed.json()).csrf_token ?? loginBody.csrf_token;
    const authed = (method, body) =>
      fetch(`${origin}${method.path}`, {
        method: method.verb,
        headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': csrf },
        body: body ? JSON.stringify(body) : undefined,
      });

    // 1. Effective policy must be visible, not implicit.
    const meta = await (await fetch(`${origin}/api/meta`)).json();
    const policy = meta.network_policy ?? {};
    record(
      policy.fake_ip_detected === true && policy.fake_ip_exemption_active === true,
      'outbound policy detects the fake-IP proxy and exempts only that range',
      `mode=${policy.fake_ip_mode} detected=${policy.fake_ip_detected} reason=${String(policy.fake_ip_reason).slice(0, 90)}`,
    );

    // 2. The exact configuration the user attempted.
    const provider = await authed(
      { path: '/api/admin/providers', verb: 'POST' },
      {
        name: 'Zhipu AI',
        category: 'llm',
        base_url: 'https://open.bigmodel.cn/api/paas/v4',
        docs_url: 'https://open.bigmodel.cn/dev/api',
        tags: ['llm', 'zhipu'],
        environment: 'production',
        notes: 'Reproduces the reported credential failure.',
        status: 'active',
      },
    );
    const providerBody = await provider.json();
    record(provider.status === 201, 'provider for open.bigmodel.cn created', `HTTP ${provider.status}`);

    const credential = await authed(
      { path: '/api/admin/credentials', verb: 'POST' },
      {
        provider_id: providerBody.id,
        name: 'Zhipu API key',
        auth_type: 'bearer',
        secret: { token: apiKey },
        config: { test_url: 'https://open.bigmodel.cn/api/paas/v4/models' },
      },
    );
    const credentialBody = await credential.json();
    record(
      credential.status === 201,
      'credential saved (secret encrypted, not echoed)',
      `HTTP ${credential.status} hint=${credentialBody.hint ?? '—'} leaked=${JSON.stringify(credentialBody).includes(apiKey)}`,
    );

    // 3. The operation that was blocked: a REAL outbound request.
    const verify = await authed({ path: `/api/admin/credentials/${credentialBody.id}/verify`, verb: 'POST' }, { method: 'GET' });
    const verifyBody = await verify.json();
    const blocked = /private_target_blocked|resolves to an address inside/.test(JSON.stringify(verifyBody));
    record(
      verify.status === 200 && !blocked,
      'credential verification reaches the provider (the reported failure)',
      `HTTP ${verify.status} result=${verifyBody.status ?? verifyBody.error?.code} detail=${String(verifyBody.message ?? verifyBody.error?.message ?? '').slice(0, 110)}`,
    );

    // 4. The failure was machine-wide: a second, unrelated host must work too.
    const second = await authed(
      { path: '/api/admin/providers', verb: 'POST' },
      { name: 'DeepSeek', category: 'llm', base_url: 'https://api.deepseek.com/v1', tags: ['llm'], environment: 'production', status: 'active' },
    );
    const secondBody = await second.json();
    const secondCred = await authed(
      { path: '/api/admin/credentials', verb: 'POST' },
      { provider_id: secondBody.id, name: 'DeepSeek key', auth_type: 'bearer', secret: { token: 'not-a-real-key' }, config: { test_url: 'https://api.deepseek.com/v1/models' } },
    );
    const secondCredBody = await secondCred.json();
    const secondVerify = await authed({ path: `/api/admin/credentials/${secondCredBody.id}/verify`, verb: 'POST' }, { method: 'GET' });
    const secondVerifyBody = await secondVerify.json();
    const secondBlocked = /private_target_blocked/.test(JSON.stringify(secondVerifyBody));
    record(
      secondVerify.status === 200 && !secondBlocked,
      'a second provider host is also reachable (failure was machine-wide)',
      `result=${secondVerifyBody.status ?? secondVerifyBody.error?.code} (401 expected: the key is fake)`,
    );

    // 5. The guard must still refuse the targets it exists for.
    const loopback = await authed(
      { path: '/api/admin/capabilities', verb: 'POST' },
      { name: 'policy probe lo', type: 'generic', description: 'probe' },
    );
    const loopbackBody = await loopback.json();
    const versionId = loopbackBody.versions?.[0]?.id;
    const probeConfig = (url) => ({
      model: 'probe',
      request: { method: 'POST', url, headers: { 'content-type': 'application/json' }, encoding: 'json', query: [], body_template: { ping: true } },
      input_schema: { type: 'object', properties: {}, additionalProperties: false },
      response: { kind: 'json', extract: { result: '$.ok' } },
      modes: { sync: true, async: false, stream: false },
      limits: { timeout_s: 5, max_output_bytes: 4096, max_input_bytes: 4096, max_duration_s: 5 },
      errors: { rules: [], default_code: 'upstream_error' },
      pricing: { per_call: 0, input_per_1k: 0, output_per_1k: 0, per_second: 0, currency: 'USD' },
      retry: { max_attempts: 0, backoff_ms: [], retry_on: [] },
      examples: [],
    });
    await authed({ path: `/api/admin/capabilities/${loopbackBody.id}/versions/${versionId}`, verb: 'PATCH' }, { config: probeConfig('http://127.0.0.1:9/blocked') });
    const probe = await authed({ path: `/api/admin/capabilities/${loopbackBody.id}/invoke`, verb: 'POST' }, { input: {}, mode: 'sync' });
    const probeBody = await probe.json();
    const probeText = JSON.stringify(probeBody);
    record(
      /private_target_blocked|blocked|allowlist/i.test(probeText),
      'loopback is still refused for capability calls',
      `${probeBody.error?.code ?? probe.status}`,
    );

    const metadata = await authed(
      { path: '/api/admin/providers', verb: 'POST' },
      { name: 'Metadata probe', category: 'generic', base_url: 'http://169.254.169.254', tags: [], environment: 'production', status: 'active' },
    );
    const metadataBody = await metadata.json();
    const metadataCred = await authed(
      { path: '/api/admin/credentials', verb: 'POST' },
      { provider_id: metadataBody.id, name: 'metadata probe', auth_type: 'api_key', secret: { api_key: 'x' }, config: { test_url: 'http://169.254.169.254/latest/meta-data/' } },
    );
    const metadataCredBody = await metadataCred.json();
    const metadataVerify = await authed({ path: `/api/admin/credentials/${metadataCredBody.id}/verify`, verb: 'POST' }, { method: 'GET' });
    const metadataVerifyBody = await metadataVerify.json();
    // A policy refusal must be reported as `blocked` (the request was never sent),
    // not as `unreachable` — that would send the user hunting a network fault.
    const metadataMessage = String(metadataVerifyBody.message ?? '');
    record(
      metadataVerifyBody.status === 'blocked' && /metadata/i.test(metadataMessage),
      'cloud metadata endpoint is still refused, reported as blocked (not unreachable)',
      `status=${metadataVerifyBody.status}: ${metadataMessage.slice(0, 90)}`,
    );
  } catch (error) {
    record(false, 'outbound verification', error instanceof Error ? error.message : String(error));
  } finally {
    try {
      child.kill();
    } catch {
      /* already gone */
    }
    await sleep(1500);
    try {
      execFileSync('taskkill', ['/f', '/im', 'skillrouter-backend.exe'], { stdio: 'ignore' });
    } catch {
      /* nothing to kill */
    }
    const failed = results.filter((entry) => !entry.ok);
    console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
    console.log(`data kept for inspection: ${dataDir}`);
    process.exit(failed.length === 0 ? 0 : 1);
  }
}

await main();
