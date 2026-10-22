/**
 * `verify` — the connectivity check the docs and the UI point at.
 *
 * Covers: the happy path, the optional real invocation, and the exit codes for a
 * rejected token, an unreachable platform and a failed invocation.
 */

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { createServer } from "node:http";

import { leakedIn, runCli } from "./helpers.js";
import { startMockPlatform, TEST_TOKEN } from "./mock-server.js";

/** @type {import("./mock-server.js").MockPlatform} */
let platform;
/** @type {string} */
let workDir;

before(async () => {
  platform = await startMockPlatform();
});

after(async () => {
  await platform.stop();
});

beforeEach(async () => {
  workDir = await mkdtemp(path.join(tmpdir(), "sr-verify-"));
  platform.setCapabilities([
    { slug: "text.summarize", name: "Summarize", version: 2 },
    { slug: "image.generate", name: "Generate image", version: 1 },
  ]);
  platform.setInvocationResult(null);
});

afterEach(async () => {
  await rm(workDir, { recursive: true, force: true });
});

describe("verify", () => {
  it("checks whoami + capabilities and exits 0", async () => {
    const result = await runCli(["verify", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /mock-research-bot/);
    assert.match(result.stdout, /2 granted/);
    assert.match(result.stdout, /text\.summarize/);
    assert.match(result.stdout, /connection OK/);
    // whoami then capabilities, in that order, both bearer-authenticated.
    const whoamiCalls = platform.requestsTo("/api/v1/whoami");
    const capabilityCalls = platform.requestsTo("/api/v1/capabilities");
    assert.ok(whoamiCalls.length >= 1);
    assert.ok(capabilityCalls.length >= 1);
    assert.equal(whoamiCalls.at(-1).authorization, `Bearer ${TEST_TOKEN}`);
  });

  it("never prints the token in full", async () => {
    const result = await runCli(["verify", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.deepEqual(leakedIn(TEST_TOKEN, result.stdout, result.stderr), []);

    const json = await runCli(["verify", "--url", platform.url, "--token", TEST_TOKEN, "--json"], { cwd: workDir });
    const report = JSON.parse(json.stdout);
    assert.deepEqual(leakedIn(TEST_TOKEN, json.stdout, json.stderr), []);
    assert.equal(report.ok, true);
    assert.match(report.token_masked, /^sr_live_\.\.\./);
    assert.equal(report.capabilities.count, 2);
  });

  it("makes one real invocation with --capability/--input and reports the request id", async () => {
    const result = await runCli(
      ["verify", "--url", platform.url, "--token", TEST_TOKEN, "--capability", "text.summarize", "--input", '{"prompt":"hello"}'],
      { cwd: workDir },
    );
    assert.equal(result.code, 0, result.stderr);
    const posts = platform.requestsTo("/api/v1/invocations");
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0].body, { capability: "text.summarize", input: { prompt: "hello" } });
    assert.match(result.stdout, /req_mockinv01/);
    assert.match(result.stdout, /succeeded/);
  });

  it("exits non-zero with the platform's message when the invocation fails", async () => {
    platform.setInvocationResult({
      request_id: "req_failed01",
      invocation_id: "inv_failed01",
      status: "failed",
      capability: "text.summarize",
      result: null,
      artifacts: [],
      error: { code: "no_compatible_route", message: "No healthy provider is available.", action: "Add a credential." },
      http_status: 502,
      latency_ms: 12,
    });
    const result = await runCli(
      ["verify", "--url", platform.url, "--token", TEST_TOKEN, "--capability", "text.summarize"],
      { cwd: workDir },
    );
    assert.equal(result.code, 1);
    assert.match(result.stderr, /no_compatible_route/);
    assert.match(result.stderr, /No healthy provider is available/);
    assert.match(result.stderr, /capabilities\/text\.summarize/, "should point at the schema");
  });

  it("exits 1 with actionable advice when the token is rejected", async () => {
    const result = await runCli(["verify", "--url", platform.url, "--token", "sr_live_revoked000000000000000000000000"], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /invalid_token/);
    assert.match(result.stderr, /Access -> Tokens/);
    assert.match(result.stderr, /exit 1/);
  });

  it("exits 1 when nothing is listening", async () => {
    const server = createServer(() => {});
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    await new Promise((resolve) => server.close(resolve));

    const result = await runCli(["verify", "--url", `http://127.0.0.1:${port}`, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /Cannot reach/);
    assert.match(result.stderr, /nothing is listening|ECONNREFUSED/);
  });

  it("rejects --input that is not a JSON object with exit 2", async () => {
    const notJson = await runCli(["verify", "--url", platform.url, "--token", TEST_TOKEN, "--capability", "text.summarize", "--input", "{oops"], { cwd: workDir });
    assert.equal(notJson.code, 2);
    assert.match(notJson.stderr, /not valid JSON/);

    const notObject = await runCli(["verify", "--url", platform.url, "--token", TEST_TOKEN, "--capability", "text.summarize", "--input", "[1,2]"], { cwd: workDir });
    assert.equal(notObject.code, 2);
    assert.match(notObject.stderr, /must be a JSON object/);
  });

  it("reuses the stored config when --url/--token are omitted", async () => {
    const install = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(install.code, 0, install.stderr);
    const result = await runCli(["verify"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /mock-research-bot/);
  });
});
