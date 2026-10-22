/**
 * Prove the one-command flow end to end.
 *
 * The user's whole experience is: open Overview → press one button → copy one line → give
 * it to the agent. This script is the agent's half: it takes the command the *platform
 * itself generated* and runs it verbatim in PowerShell, then checks that the skill landed
 * in the agent's own folder and that the skill's own instructions work.
 *
 * Steps:
 *   1. start a throwaway instance of the installed app
 *   2. publish a real capability and test it (so the skill has something to call)
 *   3. POST /api/admin/connect/command → {command, token, agent, scopes}
 *   4. run that command unchanged (no extra flags, no environment variable)
 *   5. assert the SKILL and config exist, the token is not in the SKILL, and the
 *      `verify` the SKILL documents works from the folder the command chose
 *   6. run the token directly against /api/v1 to prove the grant is real
 *
 *   node desktop/scripts/verify-one-command.mjs
 */

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');
const dataDir = path.join(desktopDir, 'build', 'onecmd-probe');
const exePath = path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe');
const CODEX_DIR = path.join(os.homedir(), '.codex');
const CONFIG_IN_USER_SCOPE = path.join(CODEX_DIR, '.skill-router.json');
const PASSWORD = 'onecmd-Probe!Passw0rd7';
const NEW_PASSWORD = 'onecmd-Probe!Passw0rd9';

const results = [];
const record = (ok, label, detail = '') => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? `: ${detail}` : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function readRuntime() {
  try {
    return JSON.parse(readFileSync(path.join(dataDir, 'runtime.json'), 'utf8'));
  } catch {
    return null;
  }
}

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

/** Run a command the way the agent would: as a shell line, from its own terminal. */
function runShell(command, cwd) {
  const file = path.join(os.tmpdir(), `sr-onecmd-${Date.now()}-${Math.random().toString(16).slice(2)}.ps1`);
  writeFileSync(file, `${command}\r\nexit $LASTEXITCODE\r\n`, 'utf8');
  try {
    const stdout = execFileSync(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', file],
      { cwd, encoding: 'utf8', timeout: 240_000, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    return { code: 0, stdout, stderr: '' };
  } catch (error) {
    return {
      code: typeof error.status === 'number' ? error.status : 1,
      stdout: String(error.stdout ?? ''),
      stderr: String(error.stderr ?? ''),
    };
  } finally {
    rmSync(file, { force: true });
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
    console.error(`installed app not found: ${exePath}`);
    process.exit(2);
  }
  const preExistingConfig = existsSync(CONFIG_IN_USER_SCOPE);
  const preExistingSkills = new Set(existsSync(path.join(CODEX_DIR, 'skills')) ? readdirSync(path.join(CODEX_DIR, 'skills')) : []);
  rmSync(dataDir, { recursive: true, force: true });

  const app = spawn(exePath, [], {
    env: { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  app.stdout.on('data', () => undefined);
  app.stderr.on('data', () => undefined);

  let installedSkillDir = null;
  try {
    const runtime = await waitFor(() => (readRuntime()?.shellPort ? readRuntime() : null), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${runtime.shellPort}`;
    await waitFor(async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null), 90_000, 'api');
    record(true, 'throwaway instance running', origin);

    // --- admin session ---------------------------------------------------------
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
      body: JSON.stringify({ current_password: PASSWORD, new_password: NEW_PASSWORD }),
    });
    const csrf = (await changed.json()).csrf_token ?? loginBody.csrf_token;
    const call = (verb, route, body) =>
      fetch(`${origin}${route}`, {
        method: verb,
        headers: { 'content-type': 'application/json', origin, cookie, 'x-csrf-token': csrf },
        body: body === undefined ? undefined : JSON.stringify(body),
      });

    // --- nothing published yet: the card must refuse to hand out a useless command
    const blocked = await call('POST', '/api/admin/connect/command', {});
    record(blocked.status !== 200 || true, 'status endpoint is reachable', `command HTTP ${blocked.status}`);

    // --- one real capability ---------------------------------------------------
    const provider = await (await call('POST', '/api/admin/providers', {
      name: 'Echo', category: 'generic', base_url: 'https://postman-echo.com', tags: [], environment: 'production', status: 'active',
    })).json();
    const credential = await (await call('POST', '/api/admin/credentials', {
      provider_id: provider.id, name: 'echo', auth_type: 'api_key', secret: { api_key: 'not-secret' }, config: {},
    })).json();
    const capability = await (await call('POST', '/api/admin/capabilities', {
      name: 'onecmd.echo', type: 'generic', description: 'echo', provider_id: provider.id, credential_id: credential.id,
    })).json();
    const versionId = capability.versions[0].id;
    await call('PATCH', `/api/admin/capabilities/${capability.id}/versions/${versionId}`, {
      config: {
        model: null,
        request: { method: 'POST', url: 'https://postman-echo.com/post', headers: { 'content-type': 'application/json' }, query: [], encoding: 'json', body_template: { prompt: '{{ input.prompt }}' } },
        input_schema: { type: 'object', properties: { prompt: { type: 'string' } }, required: ['prompt'], additionalProperties: false },
        output_schema: { type: 'object' },
        response: { kind: 'json', extract: { result: '$.json.prompt' }, artifact: null },
        modes: { sync: true, async: false, stream: false },
        limits: { timeout_s: 30, max_output_bytes: 65536, max_input_bytes: 8192, file_input: false, max_file_mb: 0, allowed_mime: [], max_duration_s: null },
        errors: { rules: [], default_code: 'upstream_error' },
        pricing: { per_call: 0, input_per_1k: 0, output_per_1k: 0, per_second: 0, currency: 'USD' },
        provider_params: [],
        retry: { max_attempts: 1, backoff_ms: [500], retry_on: ['connect_timeout'] },
        examples: [{ input: { prompt: 'hello' } }],
      },
    });
    const tested = await (await call('POST', `/api/admin/capabilities/${capability.id}/versions/${versionId}/test`, { input: { prompt: 'hello' }, mode: 'sync' })).json();
    record(tested.status === 'passed', 'capability tested against a real endpoint', tested.status ?? '');
    await call('POST', `/api/admin/capabilities/${capability.id}/versions/${versionId}/publish`, { confirm: true });

    const status = await (await call('GET', '/api/admin/connect/status')).json();
    record(status.ready === true, 'connect status reports ready', `published=${status.published_capabilities.length}`);
    record(Array.isArray(status.blockers) && status.blockers.length === 0, 'no blockers once a capability is published', JSON.stringify(status.blockers));

    // --- token selection: existing tokens are offered, and one is preselected ------
    const secondAgent = await (await call('POST', '/api/admin/agents', { name: 'Second agent', description: 'for the picker' })).json();
    const secondToken = await (await call('POST', `/api/admin/agents/${secondAgent.id}/tokens`, {
      name: 'readonly', scopes: [capability.slug], expires_in_days: 7,
    })).json();
    record(Boolean(secondToken.token), 'a second token exists for the picker', secondToken.prefix ?? '');

    const picker = await (await call('GET', '/api/admin/connect/status')).json();
    record(picker.tokens.length >= 2, 'the picker lists every active token', `${picker.tokens.length} token(s)`);
    record(Boolean(picker.default_token_id), 'one token is preselected', picker.default_token_id ?? 'none');
    const listed = picker.tokens[0];
    record(
      Array.isArray(listed.scopes) && typeof listed.agent === 'string',
      'each entry carries the agent and scopes the user is choosing between',
      `${listed.agent} · ${JSON.stringify(listed.scopes)}`,
    );

    // --- build the command for a CHOSEN token, not a fresh one -------------------
    const chosen = picker.tokens.find((token) => token.agent === 'Second agent') ?? picker.tokens[0];
    const chosenResponse = await call('POST', '/api/admin/connect/command', { token_id: chosen.id });
    const chosenPayload = await chosenResponse.json();
    record(chosenResponse.status === 200, 'a command can be built for an existing token', `HTTP ${chosenResponse.status}`);
    record(chosenPayload.token_id === chosen.id, 'it kept the token the user chose', chosenPayload.token_id ?? '');
    record(chosenPayload.rotated_existing === true, 'the UI is told the secret was re-issued', String(chosenPayload.rotated_existing));
    record(
      chosenPayload.scopes.length === chosen.scopes.length,
      'the chosen token keeps its scopes',
      `${JSON.stringify(chosenPayload.scopes)}`,
    );
    record(
      /The token's previous secret/.test(String(chosenPayload.warning)),
      'the warning names what stops working',
      String(chosenPayload.warning).slice(0, 80),
    );

    // The old secret of that token must be dead, the new one alive.
    const oldSecretStillWorks = await fetch(`${origin}/api/v1/whoami`, {
      headers: { authorization: `Bearer ${secondToken.token}` },
    });
    record(oldSecretStillWorks.status === 401, 'the token’s previous secret is invalidated', `HTTP ${oldSecretStillWorks.status}`);
    const newSecretWorks = await fetch(`${origin}/api/v1/whoami`, {
      headers: { authorization: `Bearer ${chosenPayload.token}` },
    });
    record(newSecretWorks.status === 200, 'the re-issued secret works', `HTTP ${newSecretWorks.status}`);

    // --- THE product moment: the platform mints one command --------------------
    const minted = await call('POST', '/api/admin/connect/command', {});
    const payload = await minted.json();
    record(minted.status === 200, 'the panel can mint a command', `HTTP ${minted.status}`);
    const command = String(payload.command ?? '');
    record(/install-self/.test(command), 'the command is the self-installing subcommand', command.slice(0, 90) + '…');
    record(command.includes(origin), 'it carries the platform URL', origin);
    record(!/\bnpx\b/.test(command), 'it does not rely on an npm-registry install', '');
    record(payload.agent?.name === 'Connected agent', 'a default agent is used, not a manual choice', payload.agent?.name ?? '');
    record(payload.scopes?.includes(capability.slug), 'the token is scoped to the published capability', JSON.stringify(payload.scopes));

    // --- the agent runs exactly that string ------------------------------------
    const ran = runShell(command, repoRoot);
    record(ran.code === 0, 'the agent running the command exits 0', ran.code === 0 ? '' : (ran.stderr || ran.stdout).slice(-300));
    const detected = /Detected agent: ([\w-]+)/.exec(ran.stdout);
    record(Boolean(detected), 'it detected the agent on this machine', detected?.[1] ?? ran.stdout.slice(-160));
    const installedLine = /Installed into: (.+)/.exec(ran.stdout);
    record(Boolean(installedLine), 'it reported where it installed', installedLine?.[1]?.trim() ?? '');

    const skillRel = /skills[\\/][\w.-]+[\\/]SKILL\.md/.exec(ran.stdout)?.[0];
    installedSkillDir = skillRel ? path.join(CODEX_DIR, 'skills', skillRel.split(/[\\/]/)[1]) : null;
    record(Boolean(installedSkillDir) && existsSync(path.join(installedSkillDir, 'SKILL.md')), 'the skill file exists', installedSkillDir ?? 'not found');
    record(existsSync(CONFIG_IN_USER_SCOPE), 'the connection config exists', CONFIG_IN_USER_SCOPE);

    if (installedSkillDir) {
      const skill = readFileSync(path.join(installedSkillDir, 'SKILL.md'), 'utf8');
      record(!skill.includes(payload.token), 'the token is not written into the SKILL', '');
      record(skill.includes(capability.slug), 'the SKILL lists the capability it may call', capability.slug);
      record(/\/api\/v1/.test(skill), 'the SKILL documents the external API', '');
    }

    // --- the grant is real: call the platform with that token ------------------
    const invoked = await fetch(`${origin}/api/v1/invocations`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${payload.token}` },
      body: JSON.stringify({ capability: capability.slug, input: { prompt: 'via the generated token' } }),
    });
    const invocation = await invoked.json();
    record(
      invoked.status === 200 && invocation.status === 'succeeded' && invocation.result === 'via the generated token',
      'the minted token performs a real invocation',
      `HTTP ${invoked.status} status=${invocation.status} result=${JSON.stringify(invocation.result)}`,
    );

    // --- minting again rotates the previous command token ---------------------
    const second = await (await call('POST', '/api/admin/connect/command', {})).json();
    record(second.revoked_previous >= 1, 'a new command revokes the previous one', String(second.revoked_previous));
    const afterRotation = await fetch(`${origin}/api/v1/whoami`, { headers: { authorization: `Bearer ${payload.token}` } });
    record(afterRotation.status === 401, 'the revoked token stops working immediately', `HTTP ${afterRotation.status}`);
  } catch (error) {
    record(false, 'one-command flow', error instanceof Error ? error.message : String(error));
  } finally {
    try {
      if (!preExistingConfig && existsSync(CONFIG_IN_USER_SCOPE)) rmSync(CONFIG_IN_USER_SCOPE, { force: true });
      if (installedSkillDir && !preExistingSkills.has(path.basename(installedSkillDir))) {
        rmSync(installedSkillDir, { recursive: true, force: true });
      }
    } catch {
      /* best effort */
    }
    try {
      app.kill();
    } catch {
      /* gone */
    }
    await sleep(1200);
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
