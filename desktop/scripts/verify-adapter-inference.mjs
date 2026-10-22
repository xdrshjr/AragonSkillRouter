#!/usr/bin/env node
/**
 * Verify the *shipped* desktop binary infers the engine adapter from the provider's
 * protocol, using auto-detection only (no `agent.model` setting).
 *
 * Background: a provider named "GLM" with base_url `…/api/anthropic` used to be
 * mapped to the OpenAI adapter by name, so the agent POSTed OpenAI JSON to an
 * Anthropic endpoint and reported a 404 that looked like a bad key. The fix makes
 * the base_url decide. This script proves the fix is in the installed payload.
 *
 * Method: fresh isolated data dir → create the provider + credential through the
 * public API → read /api/admin/agent/health → assert provider_id === "anthropic".
 *
 *   node desktop/scripts/verify-adapter-inference.mjs [--exe <path>]
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
const dataDir = path.resolve(argValue('--data', path.join(desktopDir, 'build', 'infer-data')));
const PASSWORD = 'infer-Test!Passw0rd7';

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
      body: JSON.stringify({ current_password: PASSWORD, new_password: 'infer-Fixed!Passw0rd9' }),
    });
    const csrf = (await changed.json()).csrf_token ?? loginBody.csrf_token;
    const post = (route, body) =>
      fetch(`${origin}${route}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': csrf },
        body: JSON.stringify(body),
      });

    // A provider whose *name* reveals nothing but whose base_url is Anthropic.
    const provider = await post('/api/admin/providers', {
      name: 'GLM',
      category: 'llm',
      base_url: 'https://open.bigmodel.cn/api/anthropic',
      tags: ['llm'],
      environment: 'production',
      status: 'active',
    });
    const providerBody = await provider.json();
    record(provider.status === 201, 'provider created with an Anthropic-compatible base_url', `HTTP ${provider.status}`);

    const credential = await post('/api/admin/credentials', {
      provider_id: providerBody.id,
      name: 'GLM key',
      auth_type: 'api_key',
      secret: { api_key: 'infer-not-a-real-key' },
      config: { model: 'glm-5.3-flash' },
    });
    record(credential.status === 201, 'credential created naming a model (no agent.model setting)', `HTTP ${credential.status}`);

    // Deliberately do NOT write agent.model: this exercises auto-detection.
    const health = await fetch(`${origin}/api/admin/agent/health`, { headers: { origin, cookie } });
    const body = await health.json();
    const model = body?.platform?.model ?? {};
    record(
      model.configured === true && model.provider_id === 'anthropic' && model.model_id === 'glm-5.3-flash',
      'shipped binary infers the anthropic adapter from the base_url',
      `configured=${model.configured} provider_id=${model.provider_id} model=${model.model_id}`,
    );
    record(
      model.base_url === 'https://open.bigmodel.cn/api/anthropic',
      'resolved base_url is the provider endpoint',
      String(model.base_url),
    );
  } catch (error) {
    record(false, 'adapter inference check', error instanceof Error ? error.message : String(error));
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
