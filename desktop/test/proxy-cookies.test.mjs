/**
 * Regression test for cookie forwarding.
 *
 * `Headers.forEach` comma-joins repeated headers, and `set-cookie` is the one
 * header where that is invalid: a client parsing `sr_session=…, sr_csrf=…` as a
 * single cookie silently loses the second one, so sign-in appears to succeed while
 * the session is never established. The shell proxy must forward each cookie
 * separately (undici exposes them through `getSetCookie()`).
 */

import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';

import { AppServer } from '../dist/app-server.js';

const quietSink = { info() {}, warn() {}, error() {} };

function shellFor(backendOrigin) {
  const shell = new AppServer({
    shellToken: 'token',
    sink: quietSink,
    info: () => ({
      appVersion: '1.0.0',
      backendOrigin,
      sidecarPort: 0,
      dataDir: 'C:/data',
      logsDir: 'C:/logs',
      platform: 'win32',
      arch: 'x64',
      packaged: false,
      enginePackage: 'core',
      engineVersion: '0.2.50',
      adapterVersion: '1.0.0',
    }),
    health: async () => ({ ok: true }),
  });
  shell.setBackendOrigin(backendOrigin);
  shell.setWebOrigin(backendOrigin);
  return shell;
}

test('multiple set-cookie headers survive the proxy as separate cookies', async () => {
  const backend = createServer((_req, res) => {
    res.setHeader('set-cookie', [
      'sr_session=session-value; Path=/; HttpOnly; SameSite=Lax',
      'sr_csrf=csrf-value; Path=/; SameSite=Lax',
    ]);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
  });
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));
  const shell = shellFor(`http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await fetch(`${shell.origin}/api/auth/login`, { method: 'POST' });
    assert.equal(response.status, 200);
    const cookies = response.headers.getSetCookie();
    assert.equal(cookies.length, 2, `expected two separate cookies, got ${JSON.stringify(cookies)}`);
    assert.match(cookies[0], /^sr_session=session-value/);
    assert.match(cookies[1], /^sr_csrf=csrf-value/);
    // A comma-joined value would still contain both names in one entry, which is
    // exactly the failure this test exists to catch.
    assert.ok(!cookies.some((entry) => entry.includes(', sr_')), 'cookies must not be comma-joined');
  } finally {
    await shell.close();
    backend.close();
  }
});

test('a single set-cookie is still forwarded', async () => {
  const backend = createServer((_req, res) => {
    res.writeHead(200, { 'set-cookie': 'sr_session=one; Path=/', 'content-type': 'application/json' });
    res.end('{}');
  });
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));
  const shell = shellFor(`http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await fetch(`${shell.origin}/api/auth/session`);
    const cookies = response.headers.getSetCookie();
    assert.equal(cookies.length, 1);
    assert.match(cookies[0], /sr_session=one/);
  } finally {
    await shell.close();
    backend.close();
  }
});
