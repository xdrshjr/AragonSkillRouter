/**
 * Outcome classification — the "never report a false success" rule.
 *
 * Upstream's `Agent.runLoopWithLifecycle` catches a loop error, prints it to
 * stderr and resolves `prompt()` normally. A naive runner therefore reports a
 * provider outage as a successful run with no output, and the operator sees a
 * green tick next to work that never happened. These tests pin the compensating
 * logic so a refactor cannot quietly reintroduce that.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { classifyOutcome, trackOutcomeEvidence } from "../dist/session.js";

const state = (overrides = {}) => ({
  completed_turns: 0,
  last_error: null,
  stop_requested_at: null,
  outcome: "running",
  ...overrides,
});

const messageUpdate = (streamEvent) => ({ type: "message_update", streamEvent });

test("a resolved prompt with one completed turn is a finish", () => {
  const run = state({ completed_turns: 1 });
  assert.equal(classifyOutcome(run, null), "finished");
});

test("a resolved prompt with no completed turn is a failure, not a finish", () => {
  // This is the exact shape of a provider outage: the loop threw internally,
  // Agent swallowed it, and prompt() resolved.
  const run = state();
  assert.equal(classifyOutcome(run, null), "failed");
});

test("a thrown error is a failure and keeps its message", () => {
  const run = state({ completed_turns: 2 });
  assert.equal(classifyOutcome(run, new Error("boom")), "failed");
});

test("an operator stop wins over everything", () => {
  const run = state({ completed_turns: 0, stop_requested_at: Date.now() });
  assert.equal(classifyOutcome(run, new Error("aborted")), "stopped");
});

test("a retry_scheduled event is recorded as the failure evidence", () => {
  const run = state();
  trackOutcomeEvidence(
    run,
    messageUpdate({
      type: "retry_scheduled",
      attempt: 3,
      maxRetries: 3,
      errorType: "network_error",
      message: "openai network error: fetch failed",
    }),
  );
  assert.deepEqual(run.last_error, {
    message: "openai network error: fetch failed",
    type: "network_error",
  });
  assert.equal(run.completed_turns, 0);
  assert.equal(classifyOutcome(run, null), "failed");
});

test("a completed turn clears the error evidence", () => {
  const run = state();
  trackOutcomeEvidence(run, messageUpdate({ type: "retry_scheduled", errorType: "rate_limited", message: "429" }));
  assert.ok(run.last_error);
  trackOutcomeEvidence(run, { type: "turn_end", message: { role: "assistant", content: [] }, usage: {} });
  assert.equal(run.last_error, null);
  assert.equal(run.completed_turns, 1);
  assert.equal(classifyOutcome(run, null), "finished");
});

test("an llm error event is captured even when no retry was scheduled", () => {
  const run = state();
  trackOutcomeEvidence(run, messageUpdate({ type: "error", error: new Error("context length exceeded") }));
  assert.equal(run.last_error.message, "context length exceeded");
  assert.equal(classifyOutcome(run, null), "failed");
});

test("a done stream event counts as a completed turn", () => {
  const run = state();
  trackOutcomeEvidence(run, messageUpdate({ type: "done", message: { role: "assistant" }, usage: {} }));
  assert.equal(run.completed_turns, 1);
  assert.equal(classifyOutcome(run, null), "finished");
});

test("irrelevant events do not fabricate an outcome", () => {
  const run = state();
  trackOutcomeEvidence(run, { type: "agent_start" });
  trackOutcomeEvidence(run, messageUpdate({ type: "text_delta", delta: "hi" }));
  trackOutcomeEvidence(run, { type: "tool_execution_start", toolCallId: "t", toolName: "platform_list_providers", args: {} });
  assert.equal(run.completed_turns, 0);
  assert.equal(run.last_error, null);
  assert.equal(classifyOutcome(run, null), "failed");
});
