#!/usr/bin/env node
/**
 * End-to-end smoke test for the assembled desktop runtime.
 *
 * It starts the *real* three processes the desktop app supervises (frozen API,
 * Next standalone UI, AragonAgent sidecar) with an isolated data directory, then
 * checks the guarantees the shell depends on:
 *
 *   1. the API becomes ready and self-initialises its database and keyring
 *   2. the UI server answers with real HTML
 *   3. sign-in works, so cookies and the CSRF token are honoured
 *   4. an admin API read succeeds with the session cookie
 *   5. the sidecar reports its engine version
 *   6. the API's trusted-origin rule accepts the shell origin and rejects others
 *
 * Run it after `npm run build`:
 *   node desktop/scripts/smoke.mjs            (keeps the data dir for inspection)
 *   node desktop/scripts/smoke.mjs --clean    (removes it afterwards)
 *
 * It deliberately does not need Electron: the same binaries ship inside the
 * installer, so this validates the payload rather than the window chrome.
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const runtimeDir = path.join(desktopDir, 'resources', 'runtime');
const clean = process.argv.includes('--clean');
const dataDir = path.join(desktopDir, 'build', 'smoke-data');

const ADMIN_PASSWORD = 'smoke-test-Passw0rd!7';
const results = [];
const children = [];

function record(ok, label, detail) {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}: ${detail}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function start(name, command, args, env, cwd) {
  const child = spawn(command, args, {
    cwd,
    env: { ...process.env, ...env },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const log = [];
  child.stdout.on('data', (chunk) => log.push(chunk.toString('utf8')));
  child.stderr.on('data', (chunk) => log.push(chunk.toString('utf8')));
  child.on('exit', (code) => {
    if (code !== 0 && code !== null) {
      console.log(`      ${name} exited with ${code}: ${log.join('').trim().slice(-500)}`);
    }
  });
  children.push({ name, child });
  return { child, log };
}

async function waitFor(url, timeoutMs, accept = (status) => status < 500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: 'manual' });
      if (accept(response.status)) return response;
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`${url} did not answer within ${timeoutMs} ms`);
}

function readCookie(response, name) {
  const raw = response.headers.getSetCookie?.() ?? [];
  for (const entry of raw) {
    const [pair] = entry.split(';');
    const [key, value] = pair.split('=');
    if (key.trim() === name) return value.trim();
  }
  return null;
}

async function main() {
  for (const required of ['backend/skillrouter-backend.exe', 'web/server.js', 'sidecar/main.js']) {
    if (!existsSync(path.join(runtimeDir, required))) {
      console.error(`Runtime incomplete: missing ${required}. Run \`npm run build\` in desktop/ first.`);
      process.exit(2);
    }
  }
  if (clean) rmSync(dataDir, { recursive: true, force: true });
  mkdirSync(dataDir, { recursive: true });

  const backendPort = await freePort();
  const webPort = await freePort();
  const sidecarPort = await freePort();
  const shellOrigin = `http://127.0.0.1:${await freePort()}`;
  const backendOrigin = `http://127.0.0.1:${backendPort}`;
  const sidecarToken = 'smoke-sidecar-token';

  const backendEnv = {
    SR_DESKTOP_MODE: '1',
    SR_ENVIRONMENT: 'production',
    SR_DATA_DIR: dataDir,
    SR_DATABASE_PATH: path.join(dataDir, 'skillrouter.db'),
    SR_MASTER_KEY_FILE: path.join(dataDir, 'keys', 'master.keyring.json'),
    SR_INITIAL_ADMIN_PASSWORD: ADMIN_PASSWORD,
    SR_BACKEND_BASE_URL: backendOrigin,
    SR_PUBLIC_BASE_URL: shellOrigin,
    SR_TRUSTED_ORIGINS: shellOrigin,
    SR_COOKIE_SECURE: 'false',
    SR_LOG_LEVEL: 'info',
    SR_ARAGON_SIDECAR_URL: `http://127.0.0.1:${sidecarPort}`,
    SR_ARAGON_SIDECAR_TOKEN: sidecarToken,
  };

  console.log(`runtime: ${runtimeDir}`);
  console.log(`data:    ${dataDir}\n`);

  start(
    'sidecar',
    process.execPath,
    [path.join(runtimeDir, 'sidecar', 'main.js')],
    {
      SR_ARAGON_SIDECAR_HOST: '127.0.0.1',
      SR_ARAGON_SIDECAR_PORT: String(sidecarPort),
      SR_ARAGON_SIDECAR_TOKEN: sidecarToken,
      ELECTRON_RUN_AS_NODE: undefined,
    },
    path.join(runtimeDir, 'sidecar'),
  );
  start(
    'backend',
    path.join(runtimeDir, 'backend', 'skillrouter-backend.exe'),
    ['--host', '127.0.0.1', '--port', String(backendPort), '--log-level', 'info'],
    backendEnv,
    path.join(runtimeDir, 'backend'),
  );
  start(
    'web',
    process.execPath,
    [path.join(runtimeDir, 'web', 'server.js')],
    { PORT: String(webPort), HOSTNAME: '127.0.0.1', NODE_ENV: 'production', NEXT_TELEMETRY_DISABLED: '1' },
    path.join(runtimeDir, 'web'),
  );

  // 1. API ready (self-initialising on a cold data dir).
  const ready = await waitFor(`${backendOrigin}/api/health/ready`, 90_000, (status) => status === 200);
  const readyBody = await ready.json();
  record(readyBody.status === 'ready', 'API ready', `${readyBody.status}, db=${readyBody.checks?.database?.status}`);

  // 2. First-run bootstrap created the admin and asked for a password change.
  record(
    readyBody.checks?.bootstrap?.must_change_password === true,
    'first-run admin requires a password change',
    `admin=${readyBody.checks?.bootstrap?.admin}`,
  );

  // 3. UI server answers HTML.
  const ui = await waitFor(`http://127.0.0.1:${webPort}/login`, 60_000);
  const html = await ui.text();
  record(html.includes('<html') || html.includes('<!DOCTYPE'), 'UI server serves /login', `${html.length} bytes, HTTP ${ui.status}`);

  // 4. Sign in through the API with the shell Origin, and keep the cookies.
  const login = await fetch(`${backendOrigin}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: shellOrigin },
    body: JSON.stringify({ username: 'admin', password: ADMIN_PASSWORD }),
  });
  const loginBody = await login.json().catch(() => ({}));
  const session = readCookie(login, 'sr_session');
  const csrf = loginBody.csrf_token ?? readCookie(login, 'sr_csrf');
  record(login.status === 200 && Boolean(session) && Boolean(csrf), 'sign-in returns session + CSRF', `HTTP ${login.status}`);

  // 5. A mutation is rejected without the CSRF header and accepted with it.
  const cookieHeader = `sr_session=${session}; sr_csrf=${csrf}`;
  const noCsrf = await fetch(`${backendOrigin}/api/admin/providers`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: shellOrigin, cookie: cookieHeader },
    body: JSON.stringify({ name: 'smoke provider', category: 'generic' }),
  });
  record(noCsrf.status === 403, 'mutation without CSRF is refused', `HTTP ${noCsrf.status}`);

  // The admin must change the password before business routes answer; the gate
  // itself is the expected answer here, which proves the gate is wired.
  const gated = await fetch(`${backendOrigin}/api/admin/providers`, {
    headers: { origin: shellOrigin, cookie: cookieHeader },
  });
  const gatedBody = await gated.json().catch(() => ({}));
  record(
    gated.status === 403 && gatedBody?.error?.code === 'password_change_required',
    'business routes blocked until the password changes',
    `${gatedBody?.error?.code ?? gated.status}`,
  );

  // 6. Change the password, then read a real admin page.
  const changed = await fetch(`${backendOrigin}/api/auth/change-password`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: shellOrigin, cookie: cookieHeader, 'x-csrf-token': csrf },
    body: JSON.stringify({ current_password: ADMIN_PASSWORD, new_password: 'smoke-Test!Passw0rd9' }),
  });
  const changedBody = await changed.json().catch(() => ({}));
  record(changed.status === 200, 'password change accepted', `HTTP ${changed.status}`);
  const csrf2 = changedBody.csrf_token ?? csrf;

  const overview = await fetch(`${backendOrigin}/api/admin/overview`, {
    headers: { origin: shellOrigin, cookie: cookieHeader },
  });
  const overviewBody = await overview.json().catch(() => ({}));
  record(
    overview.status === 200 && Boolean(overviewBody.counts),
    'authenticated admin read works',
    `HTTP ${overview.status}, capabilities=${overviewBody?.counts?.capabilities?.total ?? '?'}`,
  );

  // 7. A foreign Origin is refused (the shell is the only trusted origin).
  const foreign = await fetch(`${backendOrigin}/api/admin/providers`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: 'https://evil.example', cookie: cookieHeader, 'x-csrf-token': csrf2 },
    body: JSON.stringify({ name: 'x', category: 'generic' }),
  });
  record(foreign.status === 403, 'foreign Origin refused', `HTTP ${foreign.status}`);

  // 8. The agent engine reports its version.
  const health = await fetch(`http://127.0.0.1:${sidecarPort}/health`);
  const healthBody = await health.json().catch(() => ({}));
  const engineVersion = healthBody?.engine?.version ?? healthBody?.engineVersion ?? null;
  record(health.status === 200 && Boolean(engineVersion), 'agent engine healthy', `core=${engineVersion}`);

  // 9. The engine is registered with the API (adapter sees the same version).
  const engineState = await fetch(`${backendOrigin}/api/admin/agent/health`, {
    headers: { origin: shellOrigin, cookie: cookieHeader },
  });
  const engineBody = await engineState.json().catch(() => ({}));
  const reported = engineBody?.sidecar?.engine?.version ?? null;
  record(
    engineState.status === 200 && reported === engineVersion,
    'API and sidecar agree on the engine version',
    `api=${reported} sidecar=${engineVersion}`,
  );

  // 10. The bundled build marker matches what shipped.
  const marker = JSON.parse(readFileSync(path.join(runtimeDir, 'runtime.json'), 'utf8'));
  record(
    marker.engine?.version === engineVersion,
    'runtime manifest matches the running engine',
    `${marker.engine?.package}@${marker.engine?.version}`,
  );
}

function stopAll() {
  for (const { name, child } of children) {
    if (child.exitCode === null) {
      try {
        child.kill();
      } catch {
        console.log(`      could not stop ${name}`);
      }
    }
  }
}

try {
  await main();
} catch (error) {
  record(false, 'smoke run', error instanceof Error ? error.message : String(error));
} finally {
  stopAll();
  // Give the children a moment to release the SQLite handles before cleanup.
  await new Promise((resolve) => setTimeout(resolve, 1500));
  const failed = results.filter((entry) => !entry.ok);
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  if (clean && failed.length === 0) {
    try {
      rmSync(dataDir, { recursive: true, force: true });
      console.log('smoke data removed');
    } catch {
      console.log(`could not remove ${dataDir} (files still open); delete it manually`);
    }
  } else {
    console.log(`data kept for inspection: ${dataDir}`);
  }
  process.exit(failed.length === 0 ? 0 : 1);
}
