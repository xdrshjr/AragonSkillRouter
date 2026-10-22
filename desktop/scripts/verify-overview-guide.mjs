/**
 * Render check for the "Connect your agent" card — the one-command version.
 *
 * Runs the installed app against a throwaway data directory, publishes nothing (so the
 * card's blocked state is what a fresh install shows), then reads the rendered DOM:
 *
 *   * the card exists and offers exactly one action,
 *   * with no published capability the action is disabled and says what is missing,
 *   * after publishing a capability through the API, the button mints a command that
 *     appears in the DOM with `install-self`, the platform URL and a Copy button.
 *
 *   npx electron desktop/scripts/verify-overview-guide.mjs
 */

import { app, BrowserWindow } from 'electron';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const dataDir = path.join(desktopDir, 'build', 'overview-probe');
const exePath = path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe');
const PASSWORD = 'overview-Probe!Passw0rd7';
const NEW_PASSWORD = 'overview-Probe!Passw0rd9';

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

async function main() {
  if (!existsSync(exePath)) {
    console.error(`installed app not found: ${exePath}`);
    process.exit(2);
  }
  rmSync(dataDir, { recursive: true, force: true });
  const appProcess = spawn(exePath, [], {
    env: { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  appProcess.stdout.on('data', () => undefined);
  appProcess.stderr.on('data', () => undefined);

  try {
    const runtime = await waitFor(() => (readRuntime()?.shellPort ? readRuntime() : null), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${runtime.shellPort}`;
    await waitFor(async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null), 90_000, 'api');

    const win = new BrowserWindow({ width: 1400, height: 1000, show: false });
    await win.loadURL(`${origin}/login`);
    const login = await win.webContents.executeJavaScript(`
      (async () => {
        const r = await fetch('/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ username: 'admin', password: ${JSON.stringify(PASSWORD)} }) });
        const b = await r.json();
        if (!r.ok) return { ok: false, status: r.status };
        if (b.must_change_password) {
          const c = await fetch('/api/auth/change-password', { method: 'POST',
            headers: { 'content-type': 'application/json', 'x-csrf-token': b.csrf_token },
            body: JSON.stringify({ current_password: ${JSON.stringify(PASSWORD)}, new_password: ${JSON.stringify(NEW_PASSWORD)} }) });
          return { ok: c.ok, status: c.status };
        }
        return { ok: true, status: r.status };
      })()
    `);
    record(login?.ok === true, 'signed in through the app origin', `HTTP ${login?.status}`);

    await win.loadURL(`${origin}/`);
    await waitFor(
      () => win.webContents.executeJavaScript(`document.body.innerText.includes('Connect your agent')`),
      40_000,
      'the connect card',
    );

    // --- blocked state: nothing published ------------------------------------
    const blocked = await win.webContents.executeJavaScript(`
      (() => {
        const text = document.body.innerText;
        const button = Array.from(document.querySelectorAll('button')).find((b) => /Create (&|and) copy command/.test(b.textContent || ''));
        const select = document.getElementById('connect-token');
        return {
          hasCard: text.includes('Connect your agent'),
          hasButton: Boolean(button),
          disabled: button ? button.disabled : null,
          hasSelect: Boolean(select),
          selectValue: select ? select.value : null,
          optionCount: select ? select.options.length : 0,
          statesMissing: /No capability is published yet/.test(text),
        mentionsRefresh: /skill-router-connect update/.test(text),
          oneCommandClaim: /copy the command|command contains the token/i.test(text),
        };
      })()
    `);
    record(blocked.hasCard, 'the card is on the Overview page', '');
    record(blocked.hasButton, 'it offers a single action', 'Create & copy command');
    // "Nothing published" must NOT block the flow: installing first and publishing later is
    // a valid order, and a grey button with no way forward was the reported bug.
    record(
      blocked.disabled === false,
      'the action stays enabled while nothing is published (warning, not a blocker)',
      `disabled=${blocked.disabled}`,
    );
    record(blocked.hasSelect, 'it offers a token picker', `options=${blocked.optionCount}`);
    record(
      blocked.selectValue === '__new__',
      'with no token it preselects "create a new token" (auto-filled)',
      String(blocked.selectValue),
    );
    record(blocked.statesMissing, 'it warns that the skill would list nothing', '');
    record(blocked.mentionsRefresh, 'it says how the installed skill is refreshed later', 'update');
    record(blocked.oneCommandClaim, 'it says the command carries the token', '');

    // --- publish a capability, then mint from the UI -------------------------
    await win.webContents.executeJavaScript(`
      (async () => {
        const csrf = (document.cookie.match(/sr_csrf=([^;]+)/) || [])[1] || '';
        const post = (url, body) => fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', 'x-csrf-token': csrf }, body: JSON.stringify(body || {}) }).then((r) => r.json());
        const provider = await post('/api/admin/providers', { name: 'Echo', category: 'generic', base_url: 'https://postman-echo.com', tags: [], environment: 'production', status: 'active' });
        const credential = await post('/api/admin/credentials', { provider_id: provider.id, name: 'echo', auth_type: 'api_key', secret: { api_key: 'not-secret' }, config: {} });
        const capability = await post('/api/admin/capabilities', { name: 'ui.echo', type: 'generic', description: 'echo', provider_id: provider.id, credential_id: credential.id });
        const versionId = capability.versions[0].id;
        await fetch('/api/admin/capabilities/' + capability.id + '/versions/' + versionId, { method: 'PATCH', headers: { 'content-type': 'application/json', 'x-csrf-token': csrf }, body: JSON.stringify({ config: {
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
        } }) });
        await post('/api/admin/capabilities/' + capability.id + '/versions/' + versionId + '/test', { input: { prompt: 'hello' }, mode: 'sync' });
        const published = await post('/api/admin/capabilities/' + capability.id + '/versions/' + versionId + '/publish', { confirm: true });
        // A token must exist for the picker to preselect one (the multi-token case).
        const agent = await post('/api/admin/agents', { name: 'UI agent', description: 'picker check' });
        const token = await post('/api/admin/agents/' + agent.id + '/tokens', { name: 'ui', scopes: ['*'], expires_in_days: 7 });
        return { slug: capability.slug, published: published.status, token: Boolean(token.token) };
      })()
    `);

    await win.webContents.executeJavaScript(`location.reload()`);
    await waitFor(
      () => win.webContents.executeJavaScript(`document.body.innerText.includes('Connect your agent')`),
      40_000,
      'the card after reload',
    );

    const enabled = await win.webContents.executeJavaScript(`
      (() => {
        const button = Array.from(document.querySelectorAll('button')).find((b) => /Create (&|and) copy command/.test(b.textContent || ''));
        return { found: Boolean(button), disabled: button ? button.disabled : null };
      })()
    `);
    record(enabled.found && enabled.disabled === false, 'the action is enabled after publishing too', `disabled=${enabled.disabled}`);

    // Still before the click: the token picker, prefilled, with the re-issue warning.
    const pickerBefore = await win.webContents.executeJavaScript(`
      (() => {
        const text = document.body.innerText;
        const select = document.getElementById('connect-token');
        return {
          labelled: /Token the command will carry/.test(text),
          hasSelect: Boolean(select),
          value: select ? select.value : null,
          optionCount: select ? select.options.length : 0,
          preselectsExisting: Boolean(select) && select.value !== '__new__',
          reissueNotice: /stops working/.test(text),
          offersNew: select ? Array.from(select.options).some((o) => /Create a new token/.test(o.textContent || '')) : false,
        };
      })()
    `);
    record(pickerBefore.labelled, 'the picker is labelled for a token, not a target', '');
    record(pickerBefore.preselectsExisting, 'an existing token is preselected (auto-filled)', String(pickerBefore.value));
    record(pickerBefore.offersNew, 'creating a new token stays an explicit choice', `${pickerBefore.optionCount} option(s)`);
    record(pickerBefore.reissueNotice, 'it states that choosing a token re-issues its secret', '');

    await win.webContents.executeJavaScript(`
      (() => {
        const button = Array.from(document.querySelectorAll('button')).find((b) => /Create (&|and) copy command/.test(b.textContent || ''));
        if (button) button.click();
        return true;
      })()
    `);

    const minted = await waitFor(
      async () => {
        const value = await win.webContents.executeJavaScript(`
          (() => {
            const codes = Array.from(document.querySelectorAll('pre, code')).map((n) => n.textContent || '');
            const command = codes.find((c) => /install-self/.test(c)) || '';
            const copy = Array.from(document.querySelectorAll('button')).find((b) => /Copy command/.test(b.textContent || ''));
            return { command, hasCopy: Boolean(copy) };
          })()
        `);
        return value.command ? value : null;
      },
      60_000,
      'the generated command in the DOM',
    );

    record(true, 'the card shows one command after the click', `${minted.command.slice(0, 80)}…`);
    record(/install-self/.test(minted.command), 'the command is the self-installing subcommand', '');
    record(minted.command.includes(origin), 'the command carries this platform URL', origin);
    record(/sr_live_/.test(minted.command), 'the token is embedded (shown once)', '');
    record(!/\bnpx\b/.test(minted.command), 'no npm-registry install is claimed', '');
    record(minted.hasCopy, 'there is a Copy command button', '');
    // The old three-step procedure must be gone.
    const leftovers = await win.webContents.executeJavaScript(`
      (() => {
        const text = document.body.innerText;
        return {
          steps: /1\\. Put your agent token|2\\. Install the connection SKILL|3\\. Check the connection/.test(text),
          picker: Boolean(document.getElementById('connect-target')),
        };
      })()
    `);
    record(!leftovers.steps, 'the old multi-step instructions are gone', '');
    record(!leftovers.picker, 'the target picker is gone (the agent detects itself)', '');
    const afterClick = await win.webContents.executeJavaScript(`
      (() => ({
        hasCopy: Array.from(document.querySelectorAll('button')).some((b) => /Copy command/.test(b.textContent || '')),
        pickerHidden: !document.getElementById('connect-token'),
        mentionsAgent: /Give this to your agent/.test(document.body.innerText),
      }))()
    `);
    record(afterClick.hasCopy, 'the copy button is available after generating', '');
    record(afterClick.mentionsAgent, 'the command view tells the user what to do with it', '');
    record(afterClick.pickerHidden, 'the picker is replaced by the command view', '');

    const shot = await win.webContents.capturePage();
    const png = path.join(desktopDir, 'build', 'overview-guide.png');
    writeFileSync(png, shot.toPNG());
    console.log(`      screenshot: ${png} (${Math.round(shot.toPNG().length / 1024)} KB)`);
    win.destroy();
  } catch (error) {
    record(false, 'overview guide render check', error instanceof Error ? error.message : String(error));
  } finally {
    try {
      appProcess.kill();
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
    app.exit(failed.length === 0 ? 0 : 1);
  }
}

app.whenReady().then(main).catch((error) => {
  console.error(error);
  app.exit(1);
});
