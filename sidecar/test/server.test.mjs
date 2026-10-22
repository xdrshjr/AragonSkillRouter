/**
 * HTTP + SSE behaviour of the sidecar (loopback only, no outbound network).
 *
 * The server is started on an ephemeral port with a fake token, so these tests
 * exercise the real listener, the real auth check and a real `text/event-stream`
 * — the parts a unit test of the normaliser cannot reach.
 */

import test, { after, before } from "node:test";
import assert from "node:assert/strict";

import { loadConfig } from "../dist/config.js";
import { createSidecarServer } from "../dist/server.js";
import { parseTagRefs, parseBranchRef, selectEngineTag } from "../dist/releases.js";

const TOKEN = "test-token-0123456789abcdef";
let sidecar;
let base;

before(async () => {
  const config = loadConfig({
    SR_ARAGON_SIDECAR_TOKEN: TOKEN,
    SR_ARAGON_SIDECAR_PORT: "0",
    // A path that cannot exist: /releases/check fails locally and fast, which is
    // exactly the "no silent fallback" path we want to assert.
    SR_ARAGON_REPO_URL: "file:///definitely/not/a/repo",
    SR_ARAGON_SSE_KEEPALIVE_MS: "50",
  });
  sidecar = createSidecarServer(config);
  await new Promise((resolve) => sidecar.server.listen(0, "127.0.0.1", resolve));
  const address = sidecar.server.address();
  base = `http://127.0.0.1:${address.port}`;
});

after(async () => {
  await sidecar.close();
});

const auth = { authorization: `Bearer ${TOKEN}` };

test("/health answers without a token and never leaks the token", async () => {
  const response = await fetch(`${base}/health`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.ok, true);
  assert.equal(body.auth_required, true);
  assert.ok(body.engine.version);
  assert.equal(typeof body.model_configured, "boolean");
  assert.equal(JSON.stringify(body).includes(TOKEN), false);
});

test("/info requires the bearer token", async () => {
  const anon = await fetch(`${base}/info`);
  assert.equal(anon.status, 401);
  assert.equal((await anon.json()).code, "unauthenticated");

  const wrong = await fetch(`${base}/info`, { headers: { authorization: "Bearer nope" } });
  assert.equal(wrong.status, 401);

  const ok = await fetch(`${base}/info`, { headers: auth });
  assert.equal(ok.status, 200);
  const body = await ok.json();
  assert.deepEqual(body.tools.allowed.length, 12);
  assert.equal(body.tools.allowed.includes("platform.publish_capability"), true);
  assert.equal(body.unknown_events, 0);
  assert.equal(body.engine.adapter_version, "1.0.0");
});

test("creating a session without platform callbacks is refused with advice", async () => {
  const response = await fetch(`${base}/sessions`, {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      session_id: "ses_noplat",
      model: { providerId: "openai", modelId: "gpt-4o-mini" },
    }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, "platform_not_configured");
  assert.match(body.message, /platform\{base_url, token\}/);
});

test("creating a session without any model fails fast", async () => {
  const response = await fetch(`${base}/sessions`, {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      session_id: "ses_nomodel",
      platform: { base_url: "http://127.0.0.1:9", token: "t" },
    }),
  });
  assert.equal(response.status, 400);
  assert.equal((await response.json()).code, "model_not_configured");
});

test("a session asking for bash is rejected before an Agent exists", async () => {
  const response = await fetch(`${base}/sessions`, {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      session_id: "ses_bash",
      model: { providerId: "openai", modelId: "gpt-4o-mini" },
      platform: { base_url: "http://127.0.0.1:9", token: "t" },
      tools: [{ name: "bash" }],
    }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.match(body.message, /permission ceiling/);
});

test("an openai-compatible baseUrl is accepted and echoed without the key", async () => {
  const response = await fetch(`${base}/sessions`, {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      session_id: "ses_ok",
      model: { providerId: "openai", modelId: "gpt-4o-mini", baseUrl: "https://zenmux.ai/api/v1" },
      api_key: "sk-should-never-be-echoed",
      api_key_ref: "cred_123",
      platform: { base_url: "http://127.0.0.1:9", token: "tool-token" },
      tools: [{ name: "platform.list_providers" }],
    }),
  });
  assert.equal(response.status, 201);
  const body = await response.json();
  assert.equal(body.session_id, "ses_ok");
  assert.equal(body.resumed, false);
  assert.deepEqual(body.model, {
    providerId: "openai",
    modelId: "gpt-4o-mini",
    baseUrl: "https://zenmux.ai/api/v1",
  });
  assert.deepEqual(body.tools, ["platform.list_providers"]);
  assert.deepEqual(body.tool_wire_names, ["platform_list_providers"]);
  assert.equal(body.api_key_ref, "cred_123");
  assert.equal(JSON.stringify(body).includes("sk-should-never-be-echoed"), false);

  // Resuming the same id is reported as a resume, not a new session.
  const again = await fetch(`${base}/sessions`, {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      session_id: "ses_ok",
      model: { providerId: "openai", modelId: "gpt-4o-mini" },
      platform: { base_url: "http://127.0.0.1:9", token: "tool-token" },
      tools: [{ name: "platform.list_providers" }],
    }),
  });
  assert.equal(again.status, 200);
  assert.equal((await again.json()).resumed, true);
});

test("SSE replays the backlog and resumes from Last-Event-ID", async () => {
  const session = sidecar.sessions.get("ses_ok");
  assert.ok(session, "session must exist for this test");
  const frame = { type: "run.progress", run_id: "run_x", session_id: "ses_ok", phase: "turn_start" };
  session.bus.push(frame);
  session.bus.push(frame);
  session.bus.push(frame);
  const lastSeq = session.bus.lastSeq;

  // Backlog: a subscriber that attaches late still sees everything.
  const controller = new AbortController();
  const response = await fetch(`${base}/sessions/ses_ok/events?since=1`, {
    headers: auth,
    signal: controller.signal,
  });
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /text\/event-stream/);
  assert.equal(response.headers.get("x-replay-gap"), "0");

  const text = await readUntil(response, 2, controller);
  assert.match(text, /^id: 2$/m);
  assert.match(text, /^id: 3$/m);
  assert.match(text, /^event: run\.progress$/m);
  assert.match(text, /: connected session=ses_ok after=1/);
  // seq 1 was explicitly skipped by the resume point.
  assert.doesNotMatch(text, /^id: 1$/m);
  assert.equal(lastSeq, 3);
});

test("SSE asks for auth too", async () => {
  const response = await fetch(`${base}/sessions/ses_ok/events`);
  assert.equal(response.status, 401);
});

test("stop on an idle session is honest about there being nothing to stop", async () => {
  const response = await fetch(`${base}/sessions/ses_ok/stop`, {
    method: "POST",
    headers: auth,
    body: "{}",
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.stopped, false);
  assert.equal(body.reason, "no_run_active");
  assert.deepEqual(body.stopped_steps, []);
  assert.deepEqual(body.may_continue_upstream, []);
});

test("unknown session ids are 404 and unknown routes are 404", async () => {
  const noSession = await fetch(`${base}/sessions/ses_missing/stop`, {
    method: "POST",
    headers: auth,
    body: "{}",
  });
  assert.equal(noSession.status, 404);
  assert.equal((await noSession.json()).code, "session_not_found");

  const noRoute = await fetch(`${base}/nope`, { headers: auth });
  assert.equal(noRoute.status, 404);
});

test("/releases/check reports a git failure as an error, with no fallback commit", async () => {
  const response = await fetch(`${base}/releases/check`, { headers: auth });
  assert.equal(response.status, 502);
  const body = await response.json();
  assert.equal(body.ok, false);
  assert.equal(body.commit, null);
  assert.match(body.source, /git ls-remote/);
  assert.ok(body.error.length > 20);
  assert.match(body.error, /No fallback is applied/);
});

test("tag parsing prefers the peeled commit and orders by semver", () => {
  const output = [
    "1111111111111111111111111111111111111111\trefs/tags/v0.1.0",
    "2222222222222222222222222222222222222222\trefs/tags/v0.2.47",
    "3333333333333333333333333333333333333333\trefs/tags/v0.2.47^{}",
    "4444444444444444444444444444444444444444\trefs/tags/not-a-version",
    "5555555555555555555555555555555555555555\trefs/tags/v0.10.0",
  ].join("\n");
  const tags = parseTagRefs(output);
  assert.deepEqual(
    tags.map((tag) => tag.ref),
    ["v0.10.0", "v0.2.47", "v0.1.0"],
  );
  // The annotated tag's real commit wins over the tag object.
  assert.equal(tags[1].commit, "3333333333333333333333333333333333333333");
});

test("monorepo tags (core-vX.Y.Z) are parsed and separated by package", () => {
  // The real shape of the upstream remote: `core-v0.2.34`, `cli-v0.6.13`.
  const output = [
    "aaa1111111111111111111111111111111111111\trefs/tags/core-v0.2.25",
    "bbb1111111111111111111111111111111111111\trefs/tags/core-v0.2.34",
    "ccc1111111111111111111111111111111111111\trefs/tags/core-v0.2.34^{}",
    "ddd1111111111111111111111111111111111111\trefs/tags/cli-v0.6.22",
    "eee1111111111111111111111111111111111111\trefs/tags/cli-v0.6.13^{}",
    "fff1111111111111111111111111111111111111\trefs/tags/nightly",
  ].join("\n");
  const tags = parseTagRefs(output);
  assert.deepEqual(
    tags.map((tag) => tag.ref),
    ["cli-v0.6.22", "cli-v0.6.13", "core-v0.2.34", "core-v0.2.25"],
  );
  assert.equal(tags[2].package, "core");
  assert.equal(tags[2].version, "0.2.34");
  assert.equal(tags[2].commit, "ccc1111111111111111111111111111111111111");

  // The engine's own stream is followed, never the CLI's.
  const engineTag = selectEngineTag(tags, "@aragon-agent/core");
  assert.equal(engineTag.ref, "core-v0.2.34");
  assert.equal(engineTag.version, "0.2.34");
  const cliTag = selectEngineTag(tags, "@aragon-agent/cli");
  assert.equal(cliTag.ref, "cli-v0.6.22");

  // A plain vX.Y.Z remote still works.
  const plain = parseTagRefs("1234567890abcdef1234567890abcdef12345678\trefs/tags/v1.2.3");
  assert.equal(selectEngineTag(plain, "@aragon-agent/core").ref, "v1.2.3");
  assert.equal(selectEngineTag([], "@aragon-agent/core"), null);
});

test("branch parsing returns the head commit", () => {
  const parsed = parseBranchRef("abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main\n");
  assert.deepEqual(parsed, {
    commit: "abcdef1234567890abcdef1234567890abcdef12",
    ref: "refs/heads/main",
  });
  assert.equal(parseBranchRef(""), null);
});

/** Read the stream until `count` `id:` frames have arrived, then disconnect. */
async function readUntil(response, count, controller) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  try {
    while ((text.match(/^id: \d+$/gm) ?? []).length < count) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    controller.abort();
    await reader.cancel().catch(() => undefined);
  }
  return text;
}
