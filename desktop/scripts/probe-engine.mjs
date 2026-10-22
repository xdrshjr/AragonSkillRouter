#!/usr/bin/env node
/**
 * Isolated end-to-end test of the AragonAgent engine against a real provider.
 *
 * Starts (1) a stub platform that serves the tool-callback route the sidecar calls
 * and (2) the real sidecar, then drives one agent turn that must call a tool and
 * answer. Nothing touches the running app's database or ports.
 *
 * Purpose: decide which adapter/baseUrl/model actually works for a provider before
 * writing that configuration into the platform, instead of guessing.
 *
 *   node desktop/scripts/probe-engine.mjs --provider anthropic \
 *        --base-url https://open.bigmodel.cn/api/anthropic --model glm-4.6
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const providerId = arg('--provider', 'anthropic');
const modelId = arg('--model', 'glm-4.6');
const baseUrl = arg('--base-url', 'https://open.bigmodel.cn/api/anthropic');
const apiKey = process.env.PROBE_API_KEY ?? arg('--key', '');

if (!apiKey) {
  console.error('Set PROBE_API_KEY (the provider key) in the environment.');
  process.exit(2);
}

const sidecarDir = path.join(desktopDir, 'resources', 'runtime', 'sidecar');
const sidecarEntry = path.join(sidecarDir, 'main.js');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const token = 'probe-token';
  const sidecarPort = await freePort();

  // --- stub platform: implements the one route the sidecar calls for tools ---
  const toolCalls = [];
  const stub = createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      let body = {};
      try {
        body = JSON.parse(raw || '{}');
      } catch {
        /* ignore */
      }
      if (req.url?.startsWith('/api/admin/agent/tools/')) {
        const name = decodeURIComponent(req.url.split('/').pop() ?? '');
        toolCalls.push({ name, args: body.arguments });
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            ok: true,
            text: 'platform.list_capabilities result: two capabilities — text.summarize, video.generate',
          }),
        );
        return;
      }
      res.writeHead(404, { 'content-type': 'application/json' });
      res.end('{"ok":false,"code":"not_found"}');
    });
  });
  await new Promise((r) => stub.listen(0, '127.0.0.1', r));
  const stubPort = stub.address().port;

  // --- real sidecar ---
  const child = spawn(process.execPath, [sidecarEntry], {
    cwd: sidecarDir,
    env: {
      ...process.env,
      SR_ARAGON_SIDECAR_HOST: '127.0.0.1',
      SR_ARAGON_SIDECAR_PORT: String(sidecarPort),
      SR_ARAGON_SIDECAR_TOKEN: token,
      ELECTRON_RUN_AS_NODE: '1',
      SR_ARAGON_LLM_TIMEOUT_MS: '120000',
      SR_ARAGON_IDLE_TIMEOUT_MS: '120000',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const logs = [];
  child.stdout.on('data', (c) => logs.push(c.toString('utf8')));
  child.stderr.on('data', (c) => logs.push(c.toString('utf8')));

  const base = `http://127.0.0.1:${sidecarPort}`;
  const auth = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };

  try {
    // wait for health
    for (let i = 0; i < 120; i += 1) {
      try {
        const r = await fetch(`${base}/health`);
        if (r.ok) break;
      } catch {
        /* booting */
      }
      await sleep(250);
    }

    const sessionId = `probe${Date.now().toString(36)}`;
    const create = await fetch(`${base}/sessions`, {
      method: 'POST',
      headers: auth,
      body: JSON.stringify({
        session_id: sessionId,
        model: { providerId, modelId, baseUrl },
        api_key: apiKey,
        api_key_ref: `probe:${providerId}/${modelId}`,
        system_prompt:
          'You are a probe. When asked, call the platform_list_capabilities tool once, then answer in one short sentence.',
        tools: [
          {
            name: 'platform_list_capabilities',
            description: 'List the router capabilities.',
            parameters: { type: 'object', properties: {}, additionalProperties: false },
          },
        ],
        platform: { base_url: `http://127.0.0.1:${stubPort}`, token: 'stub' },
        max_tokens: 2048,
      }),
    });
    const created = await create.json();
    if (!create.ok) {
      console.log(JSON.stringify({ ok: false, stage: 'create_session', status: create.status, body: created }, null, 2));
      process.exit(1);
    }

    // subscribe to events BEFORE sending the message so nothing is missed
    const controller = new AbortController();
    const eventsPromise = (async () => {
      const seen = [];
      try {
        const response = await fetch(`${base}/sessions/${sessionId}/events`, {
          headers: { authorization: `Bearer ${token}`, accept: 'text/event-stream' },
          signal: controller.signal,
        });
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let index;
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, index);
            buffer = buffer.slice(index + 2);
            const dataLine = frame.split('\n').find((l) => l.startsWith('data:'));
            if (!dataLine) continue;
            try {
              const event = JSON.parse(dataLine.slice(5).trim());
              seen.push(event);
              if (['run.finished', 'run.failed', 'run.stopped'].includes(event.type)) {
                return seen;
              }
            } catch {
              /* keepalive */
            }
          }
        }
      } catch {
        /* aborted */
      }
      return seen;
    })();

    await sleep(400);
    const send = await fetch(`${base}/sessions/${sessionId}/messages`, {
      method: 'POST',
      headers: auth,
      body: JSON.stringify({
        text: 'Call platform_list_capabilities, then tell me in one sentence what it returned.',
      }),
    });
    if (!send.ok) {
      console.log(JSON.stringify({ ok: false, stage: 'send', status: send.status, body: await send.text() }, null, 2));
      process.exit(1);
    }

    const events = await Promise.race([eventsPromise, sleep(180_000).then(() => null)]);
    controller.abort();
    if (!events) {
      console.log(JSON.stringify({ ok: false, stage: 'events', detail: 'timed out', logs: logs.join('').slice(-1200) }, null, 2));
      process.exit(1);
    }

    const types = events.map((e) => e.type);
    const deltas = events.filter((e) => e.type === 'assistant.delta').map((e) => e.delta ?? e.text ?? '');
    const terminal = events[events.length - 1] ?? {};
    const toolCallsSeen = events.filter((e) => e.type === 'tool.call');
    const toolResults = events.filter((e) => e.type === 'tool.result');

    const report = {
      providerId,
      modelId,
      baseUrl,
      terminal: { type: terminal.type, status: terminal.status ?? null, error: terminal.error ?? null },
      counts: {
        events: events.length,
        toolCalls: toolCallsSeen.length,
        toolResults: toolResults.length,
        deltas: deltas.length,
      },
      toolCalls: toolCallsSeen.map((e) => e.tool),
      toolResultOk: toolResults.map((e) => e.is_error === false),
      answer: deltas.join('').slice(0, 400) || terminal.text?.slice(0, 400) || '',
      stubReceived: toolCalls,
      ok:
        terminal.type === 'run.finished' &&
        toolCallsSeen.length >= 1 &&
        toolResults.some((e) => e.is_error === false) &&
        (deltas.join('') || terminal.text || '').length > 0,
      eventTypes: [...new Set(types)],
    };
    console.log(JSON.stringify(report, null, 2));
    if (!report.ok) console.log('\nsidecar log:\n' + logs.join('').slice(-2000));
    process.exit(report.ok ? 0 : 1);
  } finally {
    try {
      child.kill();
    } catch {
      /* gone */
    }
    stub.close();
  }
}

await main();
