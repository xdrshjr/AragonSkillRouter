/**
 * Tool ceiling + platform callback dispatch (fetch is stubbed; no network).
 *
 * The first two tests are the security-critical ones: they are what stops a
 * future edit from quietly widening what the agent can do.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  PLATFORM_TOOL_NAMES,
  ToolCeilingError,
  ToolDispatcher,
  canonicalToolName,
  wireToolName,
} from "../dist/tools.js";

const options = (overrides = {}) => ({
  baseUrl: "http://127.0.0.1:8000",
  token: "tool_tok",
  sessionId: "ses_1",
  runId: "run_1",
  timeoutMs: 1000,
  ...overrides,
});

test("every allowlisted name round-trips through both spellings", () => {
  for (const name of PLATFORM_TOOL_NAMES) {
    assert.equal(canonicalToolName(name), name);
    assert.equal(canonicalToolName(wireToolName(name)), name);
    // Provider-safe: no dots, length within the 64-char API limit.
    assert.match(wireToolName(name), /^[A-Za-z0-9_-]{1,64}$/);
  }
});

test("shell, file, code and web tools cannot be named at all", () => {
  for (const name of [
    "bash",
    "shell",
    "exec",
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "glob",
    "list_dir",
    "web_fetch",
    "task",
    "code_exec",
    "platform.delete_everything",
    "",
  ]) {
    assert.equal(canonicalToolName(name), null, `${name} must not resolve`);
  }
});

test("build() refuses the whole request when one name is outside the ceiling", () => {
  const dispatcher = new ToolDispatcher(options());
  assert.throws(
    () => dispatcher.build([{ name: "platform.list_providers" }, { name: "bash" }]),
    (error) => {
      assert.ok(error instanceof ToolCeilingError);
      assert.deepEqual(error.rejected, ["bash"]);
      assert.match(error.message, /permission ceiling/);
      assert.match(error.message, /always refused/);
      return true;
    },
  );
});

test("build() returns provider-safe wire names and a canonical mapping", () => {
  const dispatcher = new ToolDispatcher(options());
  const built = dispatcher.build([
    { name: "platform.list_credentials", description: "list them" },
    { name: "platform_publish_capability" },
  ]);
  assert.deepEqual(built.mapping, {
    "platform.list_credentials": "platform_list_credentials",
    "platform.publish_capability": "platform_publish_capability",
  });
  assert.deepEqual(
    built.tools.map((tool) => tool.name),
    ["platform_list_credentials", "platform_publish_capability"],
  );
  assert.equal(built.tools[0].description, "list them");
  // A tool without an explicit schema still gets a permissive object schema.
  assert.equal(built.tools[1].parameters.type, "object");
});

test("a successful callback becomes a textResult carrying the platform payload", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ ok: true, structured: { items: [], total: 0 } }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });

  const dispatcher = new ToolDispatcher(options());
  const [tool] = dispatcher.build([{ name: "platform.list_providers" }]).tools;
  const result = await tool.execute("call_1", { limit: 5 }, {});

  assert.equal(result.isError, undefined);
  assert.deepEqual(JSON.parse(result.content[0].text), { items: [], total: 0 });
  assert.equal(calls.length, 1);
  // Canonical (dotted) name travels in the URL and the body; the bearer token is
  // the run-scoped platform token, not a provider key.
  assert.equal(calls[0].url, "http://127.0.0.1:8000/api/admin/agent/tools/platform.list_providers");
  assert.equal(calls[0].init.headers.authorization, "Bearer tool_tok");
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.tool, "platform.list_providers");
  assert.equal(body.run_id, "run_1");
  assert.equal(body.session_id, "ses_1");
  assert.deepEqual(body.arguments, { limit: 5 });
});

test("a platform error envelope becomes an errorResult the model can act on", async (t) => {
  t.mock.method(globalThis, "fetch", async () =>
    new Response(
      JSON.stringify({
        ok: false,
        code: "test_evidence_missing",
        message: "This version has no passing test.",
        actions: ["Run a real call first."],
      }),
      { status: 409, headers: { "content-type": "application/json" } },
    ),
  );

  const dispatcher = new ToolDispatcher(options());
  const [tool] = dispatcher.build([{ name: "platform.publish_capability" }]).tools;
  const result = await tool.execute("call_2", {}, {});

  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /test_evidence_missing/);
  assert.match(result.content[0].text, /Run a real call first/);
});

test("an unreachable platform is an errorResult naming the URL, not a hang", async (t) => {
  t.mock.method(globalThis, "fetch", async () => {
    throw new Error("connect ECONNREFUSED 127.0.0.1:8000");
  });

  const dispatcher = new ToolDispatcher(options());
  const [tool] = dispatcher.build([{ name: "platform.list_skills" }]).tools;
  const result = await tool.execute("call_3", {}, {});

  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /could not reach the platform/);
  assert.match(result.content[0].text, /ECONNREFUSED/);
});

test("in-flight dispatches are reported so a stop cannot claim they were stopped", async (t) => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  t.mock.method(globalThis, "fetch", async () => {
    await gate;
    return new Response(JSON.stringify({ ok: true, text: "done" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });

  const dispatcher = new ToolDispatcher(options());
  const [tool] = dispatcher.build([{ name: "platform.test_capability" }]).tools;
  const pending = tool.execute("call_4", {}, {});

  const inFlight = dispatcher.report(null);
  assert.equal(inFlight.in_flight.length, 1);
  assert.equal(inFlight.in_flight[0].tool, "platform.test_capability");
  assert.equal(inFlight.in_flight[0].tool_call_id, "call_4");

  release();
  await pending;
  assert.equal(dispatcher.report(null).in_flight.length, 0);
  assert.equal(dispatcher.snapshot()[0].ok, true);
});
