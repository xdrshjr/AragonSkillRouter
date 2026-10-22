#!/usr/bin/env node
/**
 * Verify the new model-discovery endpoint against a REAL provider.
 *
 * The Settings form offers a "Fetch models" button backed by
 * `POST /api/admin/providers/{id}/models`. This drives that endpoint on the installed
 * app with the account's real key, so the button's answer is proven rather than
 * assumed. The key is passed through the environment and never printed.
 *
 *   PROBE_API_KEY=… node desktop/scripts/verify-models-endpoint.mjs
 */

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');

const argv = process.argv.slice(2);
const argValue = (name, fallback) => {
  const index = argv.indexOf(name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
};
const exePath = path.resolve(
  argValue('--exe', path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe')),
);
const dataDir = path.resolve(argValue('--data', path.join(desktopDir, 'build', 'models-data')));
const baseUrl = argValue('--base-url', 'https://open.bigmodel.cn/api/anthropic');
const providerName = argValue('--name', 'GLM');
const apiKey = process.env.PROBE_API_KEY ?? '';
const PASSWORD = 'models-Test!Passw0rd7';

if (!apiKey) {
  console.error('Set PROBE_API_KEY in the environment (the provider key).');
  process.exit(2);
}

const results = [];
const record = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}: ${detail}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

function cookieOf(response, name) {
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
    await waitFor(
      async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null),
      90_000,
      'API ready',
    );

    const login = await fetch(`${origin}/api/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin },
      body: JSON.stringify({ username: 'admin', password: PASSWORD }),
    });
    const loginBody = await login.json();
    const cookie = `sr_session=${cookieOf(login, 'sr_session')}; sr_csrf=${cookieOf(login, 'sr_csrf')}`;
    const changed = await fetch(`${origin}/api/auth/change-password`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': loginBody.csrf_token },
      body: JSON.stringify({ current_password: PASSWORD, new_password: 'models-Fixed!Passw0rd9' }),
    });
    const csrf = (await changed.json()).csrf_token ?? loginBody.csrf_token;
    const post = (route, body) =>
      fetch(`${origin}${route}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': csrf },
        body: JSON.stringify(body ?? {}),
      });

    const provider = await post('/api/admin/providers', {
      name: providerName,
      category: 'llm',
      base_url: baseUrl,
      tags: ['llm'],
      environment: 'production',
      status: 'active',
    });
    const providerBody = await provider.json();
    record(provider.status === 201, 'provider created', `HTTP ${provider.status} base_url=${baseUrl}`);

    const credential = await post('/api/admin/credentials', {
      provider_id: providerBody.id,
      name: `${providerName} key`,
      auth_type: 'api_key',
      secret: { api_key: apiKey },
      config: {},
    });
    record(credential.status === 201, 'credential stored (secret encrypted)', `HTTP ${credential.status}`);

    const models = await post(`/api/admin/providers/${providerBody.id}/models`);
    const body = await models.json().catch(() => ({}));

    if (models.status !== 200) {
      record(false, 'model discovery against the real provider', `HTTP ${models.status}: ${JSON.stringify(body).slice(0, 300)}`);
    } else {
      const items = body.items ?? [];
      record(
        items.length > 0,
        'model discovery against the real provider',
        `${body.count} model(s) from ${body.source_url}`,
      );
      record(
        items.some((id) => String(id).includes('glm')),
        'the list contains real GLM model ids',
        items.slice(0, 6).join(', ') + (items.length > 6 ? ` … +${items.length - 6}` : ''),
      );
      record(
        items.includes('glm-5.3-flash'),
        'the configured model id is one the provider actually serves',
        items.includes('glm-5.3-flash') ? 'glm-5.3-flash present' : `missing; sample: ${items.slice(0, 4).join(', ')}`,
      );
    }
  } catch (error) {
    record(false, 'model discovery check', error instanceof Error ? error.message : String(error));
  } finally {
    try {
      child.kill();
    } catch {
      /* gone */
    }
    await sleep(1500);
    try {
      execFileSync('taskkill', ['/f', '/im', 'skillrouter-backend.exe'], { stdio: 'ignore' });
    } catch {
      /* not running */
    }
    const failed = results.filter((r) => !r.ok);
    console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
    process.exit(failed.length === 0 ? 0 : 1);
  }
}

await main();
