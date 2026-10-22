#!/usr/bin/env node
/**
 * Launch the *packaged* desktop app and verify it from the outside.
 *
 * This is the only check that proves the installer payload works: it starts
 * `Skill Router.exe` (the electron-builder output, not the dev entry), waits for
 * the shell proxy to come up on the origin recorded in runtime.json, and then
 * drives the UI and API through that origin exactly like the window does.
 *
 * Checks:
 *   1. the app starts and writes its runtime state (ports, token)
 *   2. the UI loads through the shell origin
 *   3. sign-in works through the proxy (cookies survive the hop)
 *   4. an authenticated admin read works through the proxy
 *   5. the agent engine is reachable and reported by the API
 *   6. the app shuts down cleanly and leaves no orphaned child processes
 *
 * Usage:
 *   node desktop/scripts/verify-packaged.mjs [--exe <path>] [--keep]
 *
 * `--keep` leaves the app running for manual inspection.
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
const keep = argv.includes('--keep');
const exePath = path.resolve(
  argValue('--exe', path.join(repoRoot, 'dist-desktop', 'win-unpacked', 'Skill Router.exe')),
);
const dataDir = path.resolve(argValue('--data', path.join(desktopDir, 'build', 'packaged-data')));

const results = [];
const record = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}: ${detail}`);
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'no attempt made';
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await sleep(400);
  }
  throw new Error(`${label} timed out (${lastError})`);
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
  const raw = response.headers.getSetCookie?.() ?? [];
  for (const entry of raw) {
    const [pair] = entry.split(';');
    const [key, value] = pair.split('=');
    if (key.trim() === name) return value.trim();
  }
  return null;
}

function listChildProcesses() {
  try {
    const output = execFileSync('tasklist', ['/fo', 'csv', '/nh'], { encoding: 'utf8' });
    return output
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => line.split('","').map((part) => part.replace(/^"|"$/g, '')))
      .filter((cols) => cols[0] && /skillrouter-backend|Skill Router\.exe/i.test(cols[0]))
      .map((cols) => `${cols[0]}(${cols[1]})`);
  } catch {
    return [];
  }
}

async function main() {
  if (!existsSync(exePath)) {
    console.error(`Packaged app not found: ${exePath}\nRun \`npm run pack\` (or npm run dist) first.`);
    process.exit(2);
  }
  rmSync(dataDir, { recursive: true, force: true });

  console.log(`app:  ${exePath}`);
  console.log(`data: ${dataDir}\n`);

  const child = spawn(exePath, [], {
    env: {
      ...process.env,
      // Isolated state so this run cannot touch a real user's database.
      SR_DESKTOP_DATA_DIR: dataDir,
      SR_INITIAL_ADMIN_PASSWORD: 'packaged-Passw0rd!7',
      // The packaged app is a production deployment; allow the loopback proxy the
      // test itself runs behind (fake-IP is irrelevant for 127.0.0.1).
      SR_OUTBOUND_ALLOW_FAKE_IP: 'true',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: false,
  });
  const appLog = [];
  child.stdout.on('data', (chunk) => appLog.push(chunk.toString('utf8')));
  child.stderr.on('data', (chunk) => appLog.push(chunk.toString('utf8')));
  let exited = false;
  child.on('exit', (code) => {
    exited = true;
    if (code !== 0) console.log(`      app exited with ${code}`);
  });

  try {
    // 1. runtime state → shell origin
    const state = await waitFor(() => {
      const current = readState();
      return current && current.shellPort ? current : null;
    }, 120_000, 'runtime.json with a shell port');
    record(Boolean(state.shellPort && state.backendPort && state.webPort), 'app started and recorded its ports', `shell=${state.shellPort} api=${state.backendPort} web=${state.webPort} engine=${state.sidecarPort}`);

    const origin = `http://127.0.0.1:${state.shellPort}`;

    // 2. UI through the shell
    const ui = await waitFor(async () => {
      const response = await fetch(`${origin}/login`, { redirect: 'manual' });
      return response.status === 200 ? response : null;
    }, 90_000, 'UI through the shell origin');
    const html = await ui.text();
    record(/DOCTYPE|<html/i.test(html) && html.length > 500, 'UI served through the shell origin', `${html.length} bytes`);

    // 3. Sign-in through the proxy: cookies must survive the hop.
    const login = await fetch(`${origin}/api/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin },
      body: JSON.stringify({ username: 'admin', password: 'packaged-Passw0rd!7' }),
    });
    const loginBody = await login.json().catch(() => ({}));
    const session = readCookie(login, 'sr_session');
    const csrf = loginBody.csrf_token ?? readCookie(login, 'sr_csrf');
    record(
      login.status === 200 && Boolean(session) && Boolean(csrf),
      'sign-in through the proxy sets a session cookie',
      `HTTP ${login.status}, csrf=${csrf ? 'present' : 'missing'}`,
    );

    const cookieHeader = `sr_session=${session}; sr_csrf=${csrf}`;

    // The first-run gate must be in force in the packaged app too.
    const gated = await fetch(`${origin}/api/admin/overview`, { headers: { origin, cookie: cookieHeader } });
    const gatedBody = await gated.json().catch(() => ({}));
    record(
      gated.status === 403 && gatedBody?.error?.code === 'password_change_required',
      'first-run password gate is enforced in the packaged app',
      gatedBody?.error?.code ?? String(gated.status),
    );

    // 4. Change the password, then a real authenticated read through the proxy.
    const changed = await fetch(`${origin}/api/auth/change-password`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin, cookie: cookieHeader, 'x-csrf-token': csrf },
      body: JSON.stringify({ current_password: 'packaged-Passw0rd!7', new_password: 'packaged-Test!Passw0rd9' }),
    });
    const changedBody = await changed.json().catch(() => ({}));
    record(changed.status === 200, 'password change accepted through the proxy', `HTTP ${changed.status}`);
    const csrf2 = changedBody.csrf_token ?? csrf;

    const overview = await fetch(`${origin}/api/admin/overview`, {
      headers: { origin, cookie: cookieHeader },
    });
    const overviewBody = await overview.json().catch(() => ({}));
    record(
      overview.status === 200 && Boolean(overviewBody.counts),
      'authenticated admin read through the proxy',
      `capabilities=${overviewBody?.counts?.capabilities?.total ?? '?'}`,
    );

    // A mutation must be refused without the CSRF header even in the packaged app.
    const noCsrf = await fetch(`${origin}/api/admin/providers`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin, cookie: cookieHeader },
      body: JSON.stringify({ name: 'packaged probe', category: 'generic' }),
    });
    record(noCsrf.status === 403, 'CSRF still enforced inside the packaged app', `HTTP ${noCsrf.status}`);

    // 5. Agent engine registered with the API.
    const engine = await fetch(`${origin}/api/admin/agent/health`, {
      headers: { origin, cookie: cookieHeader },
    });
    const engineBody = await engine.json().catch(() => ({}));
    const engineVersion = engineBody?.sidecar?.engine?.version ?? null;
    record(
      engine.status === 200 && Boolean(engineVersion),
      'API reports the bundled agent engine',
      `core=${engineVersion} adapter=${engineBody?.sidecar?.engine?.adapter_version ?? '?'}`,
    );

    // 6. Desktop-only shell metadata is reachable (the IPC path the UI uses).
    const info = await fetch(`${origin}/__desktop/ping`);
    record(info.status === 200, 'shell proxy answers on its own origin', `HTTP ${info.status}`);

    if (keep) {
      console.log('\n--keep given: the app is still running; quit it from the tray menu.');
      return;
    }

    // 7. Quit and check nothing is orphaned.
    child.kill();
    await waitFor(() => exited, 30_000, 'app exit');
    await sleep(2500);
    const leftovers = listChildProcesses();
    record(leftovers.length === 0, 'no orphaned child processes after quit', leftovers.length ? leftovers.join(', ') : 'none');
  } catch (error) {
    record(false, 'packaged verification', error instanceof Error ? error.message : String(error));
    console.log(`\n--- app output ---\n${appLog.join('').slice(-1500)}`);
    try {
      child.kill();
    } catch {
      /* already gone */
    }
  } finally {
    if (!keep) {
      try {
        execFileSync('taskkill', ['/f', '/im', 'skillrouter-backend.exe'], { stdio: 'ignore' });
      } catch {
        /* nothing to kill */
      }
    }
    const failed = results.filter((entry) => !entry.ok);
    console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
    if (!keep) {
      console.log(`data kept for inspection: ${dataDir}`);
    }
    process.exit(failed.length === 0 ? 0 : 1);
  }
}

await main();
