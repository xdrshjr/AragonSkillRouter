/**
 * Does the shell proxy survive an `Expect: 100-continue` client?
 *
 * PowerShell's `Invoke-RestMethod` sends that header on every POST. The proxy used to
 * forward it to undici, which never completes the interim handshake, and the caller got
 * a 502 — so an external agent written in PowerShell could not call the platform
 * through the desktop app. The proxy now strips `expect`, and this asserts the backend
 * never sees it and the request succeeds.
 *
 * The client is a raw `node:http` request on purpose: Node's `fetch` refuses to send the
 * header at all, so it cannot reproduce the failure it is meant to catch.
 */

import { createServer, request as httpRequest } from 'node:http';
import assert from 'node:assert/strict';
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

/** POST with `Expect: 100-continue`, the way PowerShell does. */
function postWithExpect(origin, path, payload) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, origin);
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const req = httpRequest(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname + url.search,
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'content-length': body.length,
          expect: '100-continue',
        },
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') }));
      },
    );
    req.on('error', reject);
    // PowerShell waits for the interim 100 before sending the body; emulating that is
    // what exercises the forwarding path.
    req.on('continue', () => req.end(body));
    // If the server does not ask us to continue, send it anyway so the test cannot hang.
    setTimeout(() => req.end(body), 200);
    req.flushHeaders();
  });
}

test('Expect: 100-continue is not forwarded and the call succeeds', async () => {
  let sawExpect = null;
  let body = '';
  const backend = createServer((req, res) => {
    sawExpect = req.headers.expect ?? null;
    req.on('data', (chunk) => {
      body += chunk;
    });
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));

  const shell = shellFor(`http://127.0.0.1:${backend.address().port}`);
  await shell.listen(0);
  try {
    const response = await postWithExpect(shell.origin, '/api/v1/invocations', { capability: 'x', input: { prompt: 'hi' } });
    assert.equal(response.status, 200, 'the proxy must not fail the request');
    assert.equal(sawExpect, null, 'the backend must not receive the Expect header');
    assert.match(body, /"capability":"x"/, 'the body still reaches the backend');
  } finally {
    await shell.close();
    backend.close();
  }
});
