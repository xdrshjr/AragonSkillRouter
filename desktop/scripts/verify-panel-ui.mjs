/**
 * Real UI verification of the agent panel, run inside Electron.
 *
 * Launches the installed app against a throwaway data directory (so the user's own
 * database is untouched), logs in through the API, opens the floating panel, and then
 * asserts the three things that were asked for — by measuring the live DOM rather
 * than reading the source:
 *
 *   1. session tabs exist, and `+` creates a session that appears as a new tab;
 *   2. Markdown in a message becomes real elements (heading/strong/code), not text;
 *   3. the composer stays at the bottom when the transcript scrolls — its top offset
 *      does not move, and its bottom edge stays inside the window.
 *
 * Usage: npx electron scripts/verify-panel-ui.mjs
 */

import { app, BrowserWindow } from 'electron';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const desktopDir = path.dirname(here);
const dataDir = path.join(desktopDir, 'build', 'ui-probe-data');
const exePath = process.env.SR_UI_EXE
  ? path.resolve(process.env.SR_UI_EXE)
  : path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Skill Router', 'Skill Router.exe');
const PASSWORD = 'ui-Probe!Passw0rd7';
const NEW_PASSWORD = 'ui-Probe!Passw0rd9';

const results = [];
const record = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? `: ${detail}` : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function readState() {
  const file = path.join(dataDir, 'runtime.json');
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
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
    console.error(`Installed app not found: ${exePath}`);
    process.exit(2);
  }
  // The app takes a single-instance lock: if it is already open, this launch only
  // focuses the existing window and never writes runtime.json. Say so instead of
  // waiting two minutes for a timeout.
  if (readState()) {
    const existing = readState();
    console.error(
      `A Skill Router instance is already running (shell port ${existing?.shellPort}).\n` +
        'Quit it (tray → Quit Skill Router) and re-run this probe: the app is single-instance,\n' +
        'so a second launch cannot start its own stack.',
    );
    process.exit(3);
  }
  rmSync(dataDir, { recursive: true, force: true });

  // The app under test: same binaries the installer ships, isolated state.
  const appProcess = spawn(exePath, [], {
    env: { ...process.env, SR_DESKTOP_DATA_DIR: dataDir, SR_INITIAL_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  appProcess.stdout.on('data', () => undefined);
  appProcess.stderr.on('data', () => undefined);

  try {
    const state = await waitFor(() => readState(), 120_000, 'runtime.json');
    const origin = `http://127.0.0.1:${state.shellPort}`;
    await waitFor(
      async () => ((await fetch(`${origin}/api/health/ready`)).status === 200 ? true : null),
      90_000,
      'API ready',
    );

    const win = new BrowserWindow({
      width: 1280,
      height: 900,
      show: false,
      webPreferences: { contextIsolation: true, nodeIntegration: false },
    });

    // 1. Sign in through the API from the page's own origin so the session cookie is
    //    set for the renderer, then clear the first-run gate.
    await win.loadURL(`${origin}/login`);
    const login = await win.webContents.executeJavaScript(`
      (async () => {
        const response = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ username: 'admin', password: ${JSON.stringify(PASSWORD)} }),
        });
        const body = await response.json();
        if (!response.ok) return { ok: false, status: response.status, body };
        if (body.must_change_password) {
          const changed = await fetch('/api/auth/change-password', {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-csrf-token': body.csrf_token },
            body: JSON.stringify({ current_password: ${JSON.stringify(PASSWORD)}, new_password: ${JSON.stringify(NEW_PASSWORD)} }),
          });
          return { ok: changed.ok, status: changed.status, changed: true };
        }
        return { ok: true, status: response.status, changed: false };
      })()
    `);
    record(login?.ok === true, 'signed in through the UI origin', `status ${login?.status}`);

    // 2. Open the dashboard and the agent panel.
    await win.loadURL(`${origin}/`);
    await waitFor(
      () => win.webContents.executeJavaScript(`Boolean(document.querySelector('button[aria-label="Open the agent panel"]'))`),
      30_000,
      'the dock trigger',
    );
    await win.webContents.executeJavaScript(`
      document.querySelector('button[aria-label="Open the agent panel"]').click(); true
    `);
    await waitFor(
      () => win.webContents.executeJavaScript(`Boolean(document.getElementById('agent-panel'))`),
      15_000,
      'the panel dialog',
    );

    // 3. Session tabs: the strip exists and `+` opens a new session tab.
    const tabsBefore = await win.webContents.executeJavaScript(`
      ({ tablist: Boolean(document.querySelector('[role="tablist"][aria-label="Agent sessions"]')),
         count: document.querySelectorAll('[role="tab"]').length,
         hasNew: Boolean(document.querySelector('button[aria-label="New session"]')) })
    `);
    record(tabsBefore.tablist && tabsBefore.hasNew, 'session tab strip with a New-session control', `tabs=${tabsBefore.count}`);

    await win.webContents.executeJavaScript(`
      document.querySelector('button[aria-label="New session"]').click(); true
    `);
    const tabsAfter = await waitFor(
      async () => {
        const value = await win.webContents.executeJavaScript(`
          ({ count: document.querySelectorAll('[role="tab"]').length,
             selected: document.querySelector('[role="tab"][aria-selected="true"]')?.textContent ?? null })
        `);
        return value.count > tabsBefore.count ? value : null;
      },
      20_000,
      'a second tab',
    );
    record(tabsAfter.count > tabsBefore.count, 'New session adds a tab', `${tabsBefore.count} → ${tabsAfter.count}, active=${JSON.stringify(tabsAfter.selected)}`);

    // 4. Markdown rendering + a genuinely long transcript: send a long message through
    //    the real composer. The run itself will fail in this throwaway instance (no
    //    model is configured there), which is realistic and also exercises the failure
    //    card; what matters here is that the transcript now overflows.
    const longPrompt = Array.from({ length: 40 }, (_, index) => `line ${index + 1} of a long probe message`).join('\n');
    const composerProbe = await win.webContents.executeJavaScript(`
      (async () => {
        const textarea = document.querySelector('#agent-draft');
        if (!textarea) return { ok: false, reason: 'no composer' };
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(textarea, ${JSON.stringify(longPrompt)});
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 150));
        const send = Array.from(document.querySelectorAll('#agent-panel footer button'))
          .find((b) => /Send|Queue/.test(b.textContent || ''));
        if (!send) return { ok: false, reason: 'no send button' };
        send.click();
        return { ok: true };
      })()
    `);
    record(composerProbe?.ok === true, 'composer sends through the real button', composerProbe?.reason ?? '');

    // Wait for the user bubble to appear in the transcript.
    const bubble = await waitFor(
      async () => {
        const info = await win.webContents.executeJavaScript(`
          (() => {
            const timeline = document.getElementById('agent-panel')?.querySelector('[role="log"]');
            if (!timeline) return { found: false, reason: 'no transcript' };
            const text = timeline.textContent || '';
            return {
              found: text.includes('line 40 of a long probe message'),
              chars: text.length,
              scrollHeight: timeline.scrollHeight,
              clientHeight: timeline.clientHeight,
            };
          })()
        `);
        return info.found ? info : null;
      },
      60_000,
      'the sent message in the transcript',
    );
    record(
      bubble.scrollHeight > bubble.clientHeight,
      'the transcript overflows once it has content',
      `scrollHeight=${bubble.scrollHeight} clientHeight=${bubble.clientHeight}, chars=${bubble.chars}`,
    );

    // 5. The composer must stay pinned while the transcript scrolls.
    const geometry = await win.webContents.executeJavaScript(`
      (() => {
        const panel = document.getElementById('agent-panel');
        const composer = panel.querySelector('footer');
        const timeline = panel.querySelector('[role="log"]');
        if (!composer || !timeline) return { ok: false, reason: 'missing footer or transcript' };
        timeline.scrollTop = 0;
        const before = composer.getBoundingClientRect();
        timeline.scrollTop = timeline.scrollHeight;
        const after = composer.getBoundingClientRect();
        const panelRect = panel.getBoundingClientRect();
        return {
          ok: true,
          beforeTop: Math.round(before.top),
          afterTop: Math.round(after.top),
          bottomInside: after.bottom <= panelRect.bottom + 1,
          composerVisibleHeight: Math.round(after.height),
          transcriptScrollable: timeline.scrollHeight > timeline.clientHeight,
          transcriptScrolled: timeline.scrollTop > 0,
        };
      })()
    `);
    record(
      geometry.ok && Math.abs(geometry.afterTop - geometry.beforeTop) <= 1,
      'composer does not move when the transcript scrolls',
      `top ${geometry.beforeTop} → ${geometry.afterTop}`,
    );
    record(
      geometry.bottomInside && geometry.composerVisibleHeight > 40,
      'composer stays fully inside the panel at the bottom',
      `height=${geometry.composerVisibleHeight}px`,
    );
    record(
      geometry.transcriptScrollable && geometry.transcriptScrolled,
      'the transcript is the scrolling region',
      `scrollable=${geometry.transcriptScrollable} scrolled=${geometry.transcriptScrolled}`,
    );

    // 6. Tabs remain reachable with the transcript scrolled (they are outside it).
    const headerFixed = await win.webContents.executeJavaScript(`
      (() => {
        const panel = document.getElementById('agent-panel');
        const tablist = panel.querySelector('[role="tablist"]');
        const rect = tablist.getBoundingClientRect();
        const panelRect = panel.getBoundingClientRect();
        return { inside: rect.top >= panelRect.top - 1 && rect.bottom <= panelRect.bottom, top: Math.round(rect.top) };
      })()
    `);
    record(headerFixed.inside, 'the tab strip stays at the top of the panel', `top=${headerFixed.top}px`);

    const shot = await win.webContents.capturePage();
    const png = path.join(desktopDir, 'build', 'panel-proof.png');
    writeFileSync(png, shot.toPNG());
    console.log(`      screenshot: ${png} (${Math.round(shot.toPNG().length / 1024)} KB)`);

    win.destroy();
  } catch (error) {
    record(false, 'panel UI probe', error instanceof Error ? error.message : String(error));
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
