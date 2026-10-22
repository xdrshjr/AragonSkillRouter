/**
 * Regression test for the gzip/content-length mismatch.
 *
 * Node's `fetch` transparently decompresses a gzip response. The shell proxy then
 * writes the *plain* bytes downstream; if it also forwarded `content-encoding:
 * gzip` (or the compressed `content-length`), the client would wait for a gunzip
 * that never completes and hang with no error — which is precisely what happened
 * against the real Next.js server, whose responses are gzip-compressed.
 */

import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import test from 'node:test';

import { AppServer } from '../dist/app-server.js';

const quietSink = { info() {}, warn() {}, error() {} };
const HTML = `<!doctype html><html><body>${'x'.repeat(4096)}</body></html>`;

function shellFor(webOrigin, backendOrigin) {
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
  shell.setWebOrigin(webOrigin);
  return shell;
}

test('gzipped upstream bodies arrive decoded and framed correctly', async () => {
  const compressed = gzipSync(Buffer.from(HTML, 'utf8'));
  const web = createServer((_req, res) => {
    res.writeHead(200, {
      'content-type': 'text/html; charset=utf-8',
      'content-encoding': 'gzip',
      'content-length': String(compressed.length),
    });
    res.end(compressed);
  });
  await new Promise((resolve) => web.listen(0, '127.0.0.1', resolve));
  const backend = createServer((_req, res) => res.end('{}'));
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));

  const shell = shellFor(`http://127.0.0.1:${web.address().port}`, `http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await fetch(`${shell.origin}/login`);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-encoding'), null, 'the proxy must not re-declare gzip');
    assert.equal(response.headers.get('content-length'), null, 'the decoded length is unknown up front');
    const body = await response.text();
    assert.equal(body, HTML, 'the client must receive the decoded document, not a gzip stream');
  } finally {
    await shell.close();
    web.close();
    backend.close();
  }
});

test('gzipped API responses also complete (no-store preserved)', async () => {
  const payload = JSON.stringify({ counts: { capabilities: { total: 3 } } });
  const compressed = gzipSync(Buffer.from(payload, 'utf8'));
  const backend = createServer((_req, res) => {
    res.writeHead(200, {
      'content-type': 'application/json',
      'content-encoding': 'gzip',
      'content-length': String(compressed.length),
      'cache-control': 'public, max-age=60',
    });
    res.end(compressed);
  });
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));
  const web = createServer((_req, res) => res.end('{}'));
  await new Promise((resolve) => web.listen(0, '127.0.0.1', resolve));

  const shell = shellFor(`http://127.0.0.1:${web.address().port}`, `http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await fetch(`${shell.origin}/api/admin/overview`);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await response.json(), JSON.parse(payload));
  } finally {
    await shell.close();
    web.close();
    backend.close();
  }
});
