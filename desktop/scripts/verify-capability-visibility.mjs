/**
 * Prove the two fixes against the installed build, end to end.
 *
 * Scenario: the platform has one capability that the user "added" but which is a draft with
 * an incomplete config (exactly the reported situation). Then:
 *
 *   1. the agent's `/api/v1/capabilities` is empty **and explains itself** (`empty_hint`);
 *   2. the Overview's status names the draft and the missing fields;
 *   3. after publishing a real capability, the same token sees it;
 *   4. and `?refresh=1` works, so a stale ETag cannot hide new grants.
 *
 *   node desktop/scripts/verify-capability-visibility.mjs
 */

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const dataDir = path.join(desktopDir, 'build', 'visibility-probe');
const exePath = path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe');
const PASSWORD = 'visibility-Probe!Passw0rd7';
const NEW_PASSWORD = 'visibility-Probe!Passw0rd9';

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
  rmSync(dataDir, { recursive: true, force: true });
  const app = spawn(exePath, [], {
    env: { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  app.stdout.on('data', () => undefined);
  app.stderr.on('data', () => undefined);

  try {
    const runtime = await waitFor(() => (readRuntime()?.shellPort ? readRuntime() : null), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${runtime.shellPort}`;
    await waitFor(async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null), 90_000, 'api');

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

    // --- the reported situation: a draft capability with an empty config ---------
    const draft = await (await call('POST', '/api/admin/capabilities', {
      name: 'empty draft', type: 'generic', description: 'added but never configured',
    })).json();
    record(draft.status === 'draft', 'a new capability starts as a draft', draft.status);

    const agent = await (await call('POST', '/api/admin/agents', { name: 'Visibility agent' })).json();
    const tokenResponse = await (await call('POST', `/api/admin/agents/${agent.id}/tokens`, {
      name: 'v', scopes: ['*'], expires_in_days: 1,
    })).json();
    const token = tokenResponse.token;
    const authHeaders = { authorization: `Bearer ${token}` };

    // --- 1. the agent's empty list explains itself -------------------------------
    const emptyList = await fetch(`${origin}/api/v1/capabilities`, { headers: authHeaders });
    const emptyBody = await emptyList.json();
    record(emptyList.status === 200 && emptyBody.count === 0, 'the agent sees zero capabilities', `count=${emptyBody.count}`);
    record(
      typeof emptyBody.empty_hint === 'string' && /published/.test(emptyBody.empty_hint) && /scope/.test(emptyBody.empty_hint),
      'the empty list explains the two possible causes',
      String(emptyBody.empty_hint).slice(0, 90) + '…',
    );
    record(
      (emptyBody.notes ?? []).some((note) => /refresh=1/.test(note)),
      'it tells the caller how to bypass a stale ETag',
      '',
    );
    // An empty list must not leak anything about what exists but is not granted.
    record(
      !/empty draft/.test(JSON.stringify(emptyBody)),
      'the response does not leak the name of the ungranted draft',
      '',
    );

    // --- 2. the platform diagnoses the draft for its admin ----------------------
    const status = await (await call('GET', '/api/admin/connect/status')).json();
    const diagnosed = (status.unpublished ?? []).find((item) => item.slug === draft.slug);
    record(Boolean(diagnosed), 'the Overview is told about the unpublished capability', draft.slug);
    record(
      diagnosed?.blockers?.some((blocker) => /still draft/.test(blocker)),
      'the diagnosis names the draft state',
      JSON.stringify(diagnosed?.blockers),
    );
    record(
      diagnosed?.validation_errors?.some((error) => error.field === 'request.url'),
      'the diagnosis names the missing request URL',
      JSON.stringify((diagnosed?.validation_errors ?? []).map((error) => error.field)),
    );
    record(
      /Fill the fields/.test(diagnosed?.next_step ?? ''),
      'it says what to do next',
      diagnosed?.next_step ?? '',
    );

    // --- 3. publish a real capability; the same token now sees it ---------------
    const provider = await (await call('POST', '/api/admin/providers', {
      name: 'Echo', category: 'generic', base_url: 'https://postman-echo.com', tags: [], environment: 'production', status: 'active',
    })).json();
    const credential = await (await call('POST', '/api/admin/credentials', {
      provider_id: provider.id, name: 'echo', auth_type: 'api_key', secret: { api_key: 'not-secret' }, config: {},
    })).json();
    const capability = await (await call('POST', '/api/admin/capabilities', {
      name: 'visibility.echo', type: 'generic', description: 'echo', provider_id: provider.id, credential_id: credential.id,
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
    record(tested.status === 'passed', 'the new capability passed its test', tested.status ?? '');
    await call('POST', `/api/admin/capabilities/${capability.id}/versions/${versionId}/publish`, { confirm: true });

    const refreshed = await fetch(`${origin}/api/v1/capabilities?refresh=1`, { headers: authHeaders });
    const refreshedBody = await refreshed.json();
    record(
      refreshed.status === 200 && refreshedBody.count === 1 && refreshedBody.capabilities[0].slug === capability.slug,
      'the same token now sees the published capability',
      `count=${refreshedBody.count}`,
    );
    record(!("empty_hint" in refreshedBody), 'the hint disappears once something is available', '');

    // A capability call through the token works, so "visible" means "callable".
    const invoked = await fetch(`${origin}/api/v1/invocations`, {
      method: 'POST',
      headers: { ...authHeaders, 'content-type': 'application/json' },
      body: JSON.stringify({ capability: capability.slug, input: { prompt: 'visible and callable' } }),
    });
    const invocationBody = await invoked.json();
    record(
      invoked.status === 200 && invocationBody.status === 'succeeded' && invocationBody.result === 'visible and callable',
      'and the capability it now sees is callable',
      `status=${invocationBody.status}`,
    );
  } catch (error) {
    record(false, 'capability visibility flow', error instanceof Error ? error.message : String(error));
  } finally {
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
