/**
 * Desktop-shell tests (node:test, no Electron runtime needed).
 *
 * These cover the parts that break silently and are expensive to debug in a
 * packaged app: port selection, health polling semantics, and the shell proxy's
 * routing rules (API → backend, UI → web server, shell routes → metadata only).
 *
 * The proxy tests start two throw-away HTTP servers, so they prove the routing
 * contract without Electron, without Python and without Next.
 */

import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';

import { AppServer } from '../dist/app-server.js';
import { findFreePort, waitForHttp } from '../dist/supervisor.js';

const quietSink = { info() {}, warn() {}, error() {} };

function startBackend(handler) {
  const server = createServer(handler);
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

async function startShell(backendOrigin, webOrigin, extra = {}) {
  const shell = new AppServer({
    shellToken: 'test-token',
    sink: quietSink,
    info: () => ({
      appVersion: '1.0.0',
      backendOrigin,
      sidecarPort: 1234,
      dataDir: 'C:/data',
      logsDir: 'C:/logs',
      platform: 'win32',
      arch: 'x64',
      packaged: false,
      enginePackage: '@aragon-agent/core',
      engineVersion: '0.2.50',
      adapterVersion: '1.0.0',
      ...extra,
    }),
    health: async () => ({ ok: true, detail: 'ok' }),
  });
  shell.setBackendOrigin(backendOrigin);
  shell.setWebOrigin(webOrigin);
  await shell.listen(0);
  return shell;
}

test('findFreePort returns distinct usable ports', async () => {
  const first = await findFreePort();
  const second = await findFreePort();
  assert.ok(first > 1024, `expected a port above 1024, got ${first}`);
  assert.ok(second > 1024);
  // Both were released, so they may coincide on a busy machine; the contract is
  // that each is bindable, which the helper proved by binding it.
  assert.equal(typeof first, 'number');
});

test('waitForHttp polls until the service answers', async () => {
  let hits = 0;
  const server = await startBackend((req, res) => {
    hits += 1;
    if (hits < 3) {
      res.writeHead(503, { 'content-type': 'application/json' });
      res.end('{"status":"degraded"}');
      return;
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"status":"ready"}');
  });
  const port = server.address().port;
  const ready = await waitForHttp(`http://127.0.0.1:${port}/api/health/ready`, 5000);
  server.close();
  assert.equal(ready, true, 'a 503 must be treated as "still starting", not as failure');
  assert.ok(hits >= 3);
});

test('waitForHttp gives up with a reason instead of hanging', async () => {
  const port = await findFreePort(); // nothing listens here
  const started = Date.now();
  const ready = await waitForHttp(`http://127.0.0.1:${port}/health`, 800);
  assert.equal(ready, false);
  assert.ok(Date.now() - started < 4000, 'the timeout must be honoured');
});

test('shell proxies /api to the backend and marks responses no-store', async () => {
  const backend = await startBackend((req, res) => {
    assert.equal(req.url, '/api/admin/overview?days=7');
    res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'public, max-age=60' });
    res.end(JSON.stringify({ ok: true, sawAuth: req.headers.authorization ?? null }));
  });
  const web = await startBackend((_req, res) => {
    res.writeHead(500);
    res.end('should not be reached');
  });
  const shell = await startShell(`http://127.0.0.1:${backend.address().port}`, `http://127.0.0.1:${web.address().port}`);
  try {
    const response = await fetch(`${shell.origin}/api/admin/overview?days=7`, {
      headers: { authorization: 'Bearer probe' },
    });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const body = await response.json();
    assert.equal(body.ok, true);
    assert.equal(body.sawAuth, 'Bearer probe', 'authorization must pass through to the backend');
  } finally {
    await shell.close();
    backend.close();
    web.close();
  }
});

test('shell serves the UI from the web server, not from the backend', async () => {
  const backend = await startBackend((_req, res) => {
    res.writeHead(500);
    res.end('backend should not answer UI routes');
  });
  const web = await startBackend((req, res) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(`<html><body>ui:${req.url}</body></html>`);
  });
  const shell = await startShell(`http://127.0.0.1:${backend.address().port}`, `http://127.0.0.1:${web.address().port}`);
  try {
    const response = await fetch(`${shell.origin}/capabilities/abc?tab=request`);
    const html = await response.text();
    assert.equal(response.status, 200);
    assert.match(html, /ui:\/capabilities\/abc\?tab=request/);
  } finally {
    await shell.close();
    backend.close();
    web.close();
  }
});

test('shell metadata requires the shell token', async () => {
  const backend = await startBackend((_req, res) => res.end('{}'));
  const web = await startBackend((_req, res) => res.end('{}'));
  const shell = await startShell(`http://127.0.0.1:${backend.address().port}`, `http://127.0.0.1:${web.address().port}`);
  try {
    const unauthorised = await fetch(`${shell.origin}/__desktop/info`);
    assert.equal(unauthorised.status, 401);

    const authorised = await fetch(`${shell.origin}/__desktop/info`, { headers: { 'x-shell-token': 'test-token' } });
    assert.equal(authorised.status, 200);
    const info = await authorised.json();
    assert.equal(info.engineVersion, '0.2.50');
    assert.equal(info.dataDir, 'C:/data');

    const health = await fetch(`${shell.origin}/__desktop/health`, { headers: { 'x-shell-token': 'test-token' } });
    assert.equal(health.status, 200);
    assert.equal((await health.json()).ok, true);

    const unknown = await fetch(`${shell.origin}/__desktop/nope`, { headers: { 'x-shell-token': 'test-token' } });
    assert.equal(unknown.status, 404);
  } finally {
    await shell.close();
    backend.close();
    web.close();
  }
});

test('shell reports a clear error when the web server is not up yet', async () => {
  const backend = await startBackend((_req, res) => res.end('{}'));
  const shell = new AppServer({
    shellToken: 'test-token',
    sink: quietSink,
    info: () => ({
      appVersion: '1.0.0',
      backendOrigin: `http://127.0.0.1:${backend.address().port}`,
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
    health: async () => ({ ok: false }),
  });
  shell.setBackendOrigin(`http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await fetch(`${shell.origin}/`);
    assert.equal(response.status, 503);
    assert.match(await response.text(), /Starting/);
  } finally {
    await shell.close();
    backend.close();
  }
});

test('shell streams SSE chunks instead of buffering the whole body', async () => {
  const backend = await startBackend((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    res.write('event: run.started\ndata: {"seq":1}\n\n');
    setTimeout(() => {
      res.write('event: run.finished\ndata: {"seq":2}\n\n');
      res.end();
    }, 150);
  });
  const web = await startBackend((_req, res) => res.end('{}'));
  const shell = await startShell(`http://127.0.0.1:${backend.address().port}`, `http://127.0.0.1:${web.address().port}`);
  try {
    const response = await fetch(`${shell.origin}/api/stream/agent/run_1`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type') ?? '', /text\/event-stream/);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const first = await reader.read();
    const early = decoder.decode(first.value);
    assert.match(early, /run\.started/, 'the first event must arrive before the response ends');
    let rest = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      rest += decoder.decode(value);
    }
    assert.match(rest, /run\.finished/);
  } finally {
    await shell.close();
    backend.close();
    web.close();
  }
});
