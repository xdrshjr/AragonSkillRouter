/**
 * Prove the Overview guide's commands actually work.
 *
 * The commands are imported from `frontend/lib/connect-guide.ts` — the same module the
 * React component renders — so this cannot drift into "documented but different".
 *
 * It starts a throwaway instance of the installed app (isolated data dir; the user's
 * database is untouched), creates a real agent token and a published capability, then
 * runs each generated step in PowerShell exactly as a user would paste it:
 *
 *   1. `$env:SR_TOKEN = "…"`            → succeeds, variable set
 *   2. `node <cli> install … --user`    → writes ~/.codex/skills/<name>/SKILL.md
 *   3. `node <cli> verify --user`       → whoami + catalogue, exit 0
 *   4. the `firstCallCommand` snippet   → a real invocation over /api/v1
 *
 * It also asserts the negative: `verify` WITHOUT `--user` must fail (config lives in
 * the user scope), which is the mistake the guide exists to prevent.
 *
 * Usage: node desktop/scripts/verify-connect-guide.mjs
 */

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { connectCommands, firstCallCommand, quotePath } from '../../frontend/lib/connect-guide.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');
const dataDir = path.join(desktopDir, 'build', 'guide-probe');
const exePath = path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe');
const cliPath = path.join(repoRoot, 'packages', 'installer', 'bin', 'skill-router-connect.js');
const PASSWORD = 'guide-Probe!Passw0rd7';
const NEW_PASSWORD = 'guide-Probe!Passw0rd9';
const CODEX_DIR = path.join(os.homedir(), '.codex');
const CONFIG_IN_USER_SCOPE = path.join(CODEX_DIR, '.skill-router.json');

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

/** Run a snippet in PowerShell, exactly as a user would paste it. */
/** Run a snippet the way a user would: write it to a .ps1 and execute the file.
 *
 * `-Command "<script>"` re-parses the argument through the Windows command line, where
 * nested quotes and braces get mangled. A file is what "paste into a terminal and run"
 * actually means, and it has no quoting layer.
 */
function powershellFile(script, cwd) {
  const file = path.join(os.tmpdir(), `sr-guide-${Date.now()}-${Math.random().toString(16).slice(2)}.ps1`);
  // `exit $LASTEXITCODE` is what makes the file's exit status match the last command:
  // PowerShell 5.1 reports 0 from `-File` otherwise, which would hide a real failure.
  writeFileSync(file, `${script}\r\nexit $LASTEXITCODE\r\n`, 'utf8');
  try {
    const stdout = execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', file], {
      cwd,
      encoding: 'utf8',
      timeout: 180_000,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { code: 0, stdout, stderr: '' };
  } catch (error) {
    return {
      code: typeof error.status === 'number' ? error.status : 1,
      stdout: String(error.stdout ?? ''),
      stderr: String(error.stderr ?? ''),
    };
  } finally {
    try {
      rmSync(file, { force: true });
    } catch {
      /* best effort */
    }
  }
}

function powershell(script, cwd) {
  return execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
    cwd,
    encoding: 'utf8',
    timeout: 180_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}

function powershellAllowFailure(script, cwd) {
  return powershellFile(script, cwd);
}

function cookieOf(response, name) {
  for (const entry of response.headers.getSetCookie?.() ?? []) {
    const [pair] = entry.split(';');
    const [key, value] = pair.split('=');
    if (key.trim() === name) return value.trim();
  }
  return null;
}

/**
 * Is an instance actually running? `runtime.json` is a leftover file after a quit, so
 * its presence proves nothing — only a live listener on the recorded shell port does.
 */
async function instanceIsRunning() {
  const runtime = readRuntime();
  if (!runtime?.shellPort) return false;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    const response = await fetch(`http://127.0.0.1:${runtime.shellPort}/__desktop/ping`, { signal: controller.signal });
    clearTimeout(timer);
    return response.ok;
  } catch {
    return false;
  }
}

async function main() {
  if (!existsSync(exePath)) {
    console.error(`installed app not found: ${exePath}`);
    process.exit(2);
  }
  if (!existsSync(cliPath)) {
    console.error(`installer CLI missing: ${cliPath}`);
    process.exit(2);
  }
  // The guide installs into the real user scope; remember what was there before so the
  // check can put the machine back exactly as it found it.
  const preExistingConfig = existsSync(CONFIG_IN_USER_SCOPE);
  const skillsRoot = path.join(CODEX_DIR, 'skills');
  const preExistingSkills = new Set(existsSync(skillsRoot) ? readdirSync(skillsRoot) : []);

  if (await instanceIsRunning()) {
    console.error('A Skill Router instance is already running on its recorded port; quit it and re-run (single-instance app).');
    process.exit(3);
  }
  rmSync(dataDir, { recursive: true, force: true });
  const env = { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD };
  const app = spawn(exePath, [], { env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  app.stdout.on('data', () => undefined);
  app.stderr.on('data', () => undefined);

  let skillDir = null;
  try {
    const runtime = await waitFor(() => (readRuntime()?.shellPort ? readRuntime() : null), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${runtime.shellPort}`;
    await waitFor(async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null), 90_000, 'api');
    record(true, 'throwaway instance running', origin);

    // --- the guide reads these two values from /api/meta ------------------------
    const meta = await (await fetch(`${origin}/api/meta`)).json();
    record(meta.public_base_url === origin, 'the guide uses /api/meta public_base_url', meta.public_base_url);
    record(meta.installer?.available === true, 'the API reports a resolvable installer CLI', meta.installer?.cli_path ?? 'none');
    const reportedCli = meta.installer?.cli_path ?? '';
    record(
      Boolean(reportedCli) && existsSync(reportedCli),
      'the reported CLI path exists on this machine',
      reportedCli,
    );
    record(
      reportedCli.includes('installer'),
      'the printed path is the visible runtime copy, not a PyInstaller internal folder',
      reportedCli,
    );

    // --- seed: admin session, agent token, one published capability -----------
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

    const provider = await (await call('POST', '/api/admin/providers', {
      name: 'Echo', category: 'generic', base_url: 'https://postman-echo.com', tags: [], environment: 'production', status: 'active',
    })).json();
    const credential = await (await call('POST', '/api/admin/credentials', {
      provider_id: provider.id, name: 'echo', auth_type: 'api_key', secret: { api_key: 'not-secret' }, config: {},
    })).json();
    const capability = await (await call('POST', '/api/admin/capabilities', {
      name: 'guide.echo', type: 'generic', description: 'echo', provider_id: provider.id, credential_id: credential.id,
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
    record(tested.status === 'passed', 'capability tested against a real endpoint', tested.status);
    await call('POST', `/api/admin/capabilities/${capability.id}/versions/${versionId}/publish`, { confirm: true });

    const agent = await (await call('POST', '/api/admin/agents', { name: 'guide-probe', description: 'overview guide check' })).json();
    const tokenResponse = await (await call('POST', `/api/admin/agents/${agent.id}/tokens`, { name: 'guide', scopes: ['*'], expires_in_days: 1 })).json();
    const token = tokenResponse.token;
    record(Boolean(token?.startsWith('sr_live_')), 'agent token issued for the probe', `${token.slice(0, 10)}…`);

    // --- the guide's commands, built by the shipped module ---------------------
    const commands = connectCommands({ baseUrl: meta.public_base_url, cliPath: meta.installer.cli_path, target: 'codex-skills' });
    record(commands.length === 3, 'the guide produces three steps', commands.map((c) => c.id).join(', '));
    record(
      commands.every((command) => !/\bnpx\b/.test(command.code)),
      'no step claims an npx/npm-registry install (the package is not published)',
    );
    record(
      commands[1].code.includes('--user') && commands[2].code.includes('--user'),
      'install and verify both use the same scope flag',
    );

    // Step 1: the token assignment. PowerShell expands it in the following steps.
    const step1 = powershellAllowFailure(`${commands[0].code.replace('sr_live_…paste the token here…', token)}\nif ($env:SR_TOKEN -eq '${token}') { 'TOKEN_SET' } else { 'TOKEN_MISSING' }`, repoRoot);
    record(step1.code === 0 && step1.stdout.includes('TOKEN_SET'), 'step 1 sets $env:SR_TOKEN', step1.stdout.trim());

    // Step 2: install, in one PowerShell session that also has the variable.
    const installScript = [
      `$env:SR_TOKEN = "${token}"`,
      commands[1].code,
    ].join('\n');
    const step2 = powershellAllowFailure(installScript, repoRoot);
    record(step2.code === 0, 'step 2 (install) exits 0', step2.code === 0 ? '' : (step2.stderr || step2.stdout).slice(-300));
    const installed = step2.stdout.match(/skills[\\/][\w.-]+[\\/]SKILL\.md/);
    skillDir = installed ? path.join(CODEX_DIR, 'skills', installed[0].split(/[\\/]/)[1]) : null;
    record(Boolean(skillDir) && existsSync(path.join(skillDir, 'SKILL.md')), 'step 2 wrote ~/.codex/skills/<skill>/SKILL.md', skillDir ?? step2.stdout.slice(-200));
    record(existsSync(CONFIG_IN_USER_SCOPE), 'step 2 wrote the connection config', CONFIG_IN_USER_SCOPE);
    if (skillDir) {
      const content = readFileSync(path.join(skillDir, 'SKILL.md'), 'utf8');
      record(!content.includes(token), 'the SKILL does not contain the token', '');
      record(content.includes('guide.echo'), 'the SKILL lists the published capability', '');
    }

    // Step 3: verify, with the same scope flag.
    const verifyScript = [`$env:SR_TOKEN = "${token}"`, commands[2].code].join('\n');
    const step3 = powershellAllowFailure(verifyScript, repoRoot);
    record(step3.code === 0, 'step 3 (verify) exits 0', step3.code === 0 ? '' : (step3.stderr || step3.stdout).slice(-300));
    record(/guide-probe/.test(step3.stdout), 'step 3 reports the agent identity', step3.stdout.split('\n').find((l) => /agent/.test(l))?.trim() ?? '');
    record(
      step3.stdout.includes(capability.slug),
      'step 3 lists the granted capability by slug',
      capability.slug,
    );

    // The negative: without --user, verify looks in the cwd and must fail clearly.
    const withoutScope = commands[2].code.replace(' --user', '');
    const negative = powershellAllowFailure([`$env:SR_TOKEN = "${token}"`, withoutScope].join('\n'), repoRoot);
    record(
      negative.code !== 0 && /No platform URL|does not exist|not_installed/i.test(negative.stdout + negative.stderr),
      'verify without --user fails with an actionable message (why the flag matters)',
      `exit=${negative.code} raw=${JSON.stringify((negative.stdout + negative.stderr).slice(-220))}`,
    );

    // The optional one-liner: a real invocation over /api/v1.
    const snippet = firstCallCommand({ baseUrl: meta.public_base_url, cliPath: meta.installer.cli_path }, capability.slug, { prompt: 'hello from the guide' });
    const invoked = powershellAllowFailure(
      [`$env:SR_TOKEN = "${token}"`, `${snippet} | ConvertTo-Json -Compress -Depth 6`].join('\n'),
      repoRoot,
    );
    let parsed = null;
    try {
      parsed = JSON.parse(invoked.stdout.trim());
    } catch {
      parsed = null;
    }
    record(
      invoked.code === 0 && parsed?.status === 'succeeded' && parsed?.result === 'hello from the guide',
      'the one-liner performs a real invocation',
      parsed
        ? `status=${parsed.status} result=${JSON.stringify(parsed.result)}`
        : `exit=${invoked.code} out=${JSON.stringify(invoked.stdout.slice(-400))} err=${JSON.stringify(invoked.stderr.slice(-400))}`,
    );
  } catch (error) {
    record(false, 'connect guide check', error instanceof Error ? error.message : String(error));
  } finally {
    // Restore the machine: remove only what this probe created.
    try {
      if (!preExistingConfig && existsSync(CONFIG_IN_USER_SCOPE)) rmSync(CONFIG_IN_USER_SCOPE, { force: true });
      if (skillDir && !preExistingSkills.has(path.basename(skillDir))) {
        rmSync(skillDir, { recursive: true, force: true });
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
