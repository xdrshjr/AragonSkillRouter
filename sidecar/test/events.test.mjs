/**
 * Event normalisation + replay buffer (no network, no engine calls).
 *
 * These tests pin the mapping table in sidecar/README.md. If upstream renames an
 * event, a test fails here rather than a run silently losing its progress.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { EventNormaliser, EventBus } from "../dist/events.js";

const normaliser = () => new EventNormaliser({ maxToolOutputChars: 40 });

test("agent_start and agent_end are absorbed, not forwarded", () => {
  const n = normaliser();
  assert.deepEqual(n.push({ type: "agent_start" }), []);
  assert.deepEqual(n.push({ type: "agent_end", messages: [] }), []);
  assert.equal(n.stats.absorbed_events, 2);
  assert.equal(n.stats.unknown_events, 0);
});

test("turn lifecycle becomes run.progress and counts turns", () => {
  const n = normaliser();
  assert.deepEqual(n.push({ type: "turn_start" }), [
    { type: "run.progress", phase: "turn_start", turn: 1 },
  ]);
  const end = n.push({
    type: "turn_end",
    message: { role: "assistant", content: [], stopReason: "tool_use" },
    usage: { inputTokens: 10, outputTokens: 4, cost: 0.5 },
  });
  assert.equal(end.length, 1);
  assert.equal(end[0].type, "run.progress");
  assert.equal(end[0].phase, "turn_end");
  assert.equal(end[0].stop_reason, "tool_use");
  assert.deepEqual(end[0].usage, { input_tokens: 10, output_tokens: 4, cost: 0.5, total_tokens: 14 });
  assert.equal(n.turns, 1);
});

test("text deltas become assistant.delta", () => {
  const n = normaliser();
  assert.deepEqual(
    n.push({ type: "message_update", streamEvent: { type: "text_delta", delta: "hello" } }),
    [{ type: "assistant.delta", delta: "hello" }],
  );
});

test("tool execution start/end become tool.call and tool.result", () => {
  const n = normaliser();
  assert.deepEqual(
    n.push({
      type: "tool_execution_start",
      toolCallId: "t1",
      toolName: "platform_list_providers",
      args: { limit: 5 },
    }),
    [
      {
        type: "tool.call",
        tool_call_id: "t1",
        tool: "platform_list_providers",
        args: { limit: 5 },
      },
    ],
  );

  const end = n.push({
    type: "tool_execution_end",
    toolCallId: "t1",
    toolName: "platform_list_providers",
    result: { content: [{ type: "text", text: "x".repeat(100) }] },
    isError: false,
    duration: 12,
  });
  assert.equal(end[0].type, "tool.result");
  assert.equal(end[0].is_error, false);
  assert.equal(end[0].duration_ms, 12);
  assert.equal(end[0].output_bytes, 100);
  assert.equal(end[0].output_truncated, true);
  assert.match(end[0].output, /truncated/);
});

test("tool_call_delta is absorbed to avoid thousands of events per call", () => {
  const n = normaliser();
  assert.deepEqual(
    n.push({
      type: "message_update",
      streamEvent: { type: "tool_call_delta", toolCallId: "t1", argsDelta: "{" },
    }),
    [],
  );
  assert.equal(n.stats.absorbed_events, 1);
});

test("llm error is progress, not a terminal failure", () => {
  const n = normaliser();
  const out = n.push({
    type: "message_update",
    streamEvent: { type: "error", error: new Error("connection reset") },
  });
  assert.equal(out.length, 1);
  assert.equal(out[0].type, "run.progress");
  assert.equal(out[0].phase, "llm_error");
  assert.match(out[0].message, /connection reset/);
});

test("retry events are mapped with their scheduling data", () => {
  const n = normaliser();
  const scheduled = n.push({
    type: "message_update",
    streamEvent: {
      type: "retry_scheduled",
      attempt: 1,
      maxRetries: 3,
      delayMs: 500,
      resumeAt: 1_700_000_000_000,
      errorType: "rate_limited",
      message: "429",
    },
  });
  assert.equal(scheduled[0].phase, "retry_scheduled");
  assert.equal(scheduled[0].attempt, 1);
  assert.equal(scheduled[0].delay_ms, 500);

  const attempt = n.push({
    type: "message_update",
    streamEvent: { type: "retry_attempt", attempt: 2, maxRetries: 3 },
  });
  assert.equal(attempt[0].phase, "retry_attempt");

  const restart = n.push({
    type: "message_update",
    streamEvent: { type: "stream_restart", attempt: 2, discardedToolCallIds: ["t9"] },
  });
  assert.equal(restart[0].phase, "stream_restart");
  assert.deepEqual(restart[0].discarded_tool_call_ids, ["t9"]);
});

test("compaction events are surfaced without leaking the summary body", () => {
  const n = normaliser();
  const start = n.push({ type: "compaction_start", trigger: "auto", messageCount: 40 });
  assert.equal(start[0].phase, "compaction_start");
  assert.equal(start[0].message_count, 40);

  const end = n.push({
    type: "compaction_end",
    applied: true,
    mode: "summarized",
    messagesBefore: 40,
    messagesAfter: 6,
    droppedMessages: 34,
    estimatedTokensBefore: 118_000,
    estimatedTokensAfter: 23_000,
    summary: "SECRET SUMMARY BODY",
    durationMs: 900,
  });
  assert.equal(end[0].phase, "compaction_end");
  assert.equal(end[0].estimated_tokens_before, 118_000);
  assert.equal(JSON.stringify(end[0]).includes("SECRET SUMMARY BODY"), false);
});

test("unknown upstream events are counted and dropped, never fatal", () => {
  const n = normaliser();
  assert.deepEqual(n.push({ type: "brand_new_event_from_the_future", x: 1 }), []);
  assert.deepEqual(
    n.push({ type: "message_update", streamEvent: { type: "future_stream_event" } }),
    [],
  );
  assert.equal(n.stats.unknown_events, 2);
});

test("EventBus assigns monotonic seq and replays only what was missed", () => {
  const bus = new EventBus(100);
  const base = { type: "run.progress", run_id: "run_1", session_id: "ses_1", phase: "turn_start" };

  assert.equal(bus.push(base).seq, 1);
  assert.equal(bus.push(base).seq, 2);
  assert.equal(bus.push(base).seq, 3);

  // A client that has seen nothing gets everything.
  assert.equal(bus.replaySince(null).events.length, 3);
  // Last-Event-ID: 2 → only seq 3.
  const resumed = bus.replaySince(2);
  assert.equal(resumed.gap, false);
  assert.deepEqual(
    resumed.events.map((event) => event.seq),
    [3],
  );
  // A client already up to date gets nothing and no gap.
  assert.deepEqual(bus.replaySince(3).events, []);
  assert.equal(bus.replaySince(3).gap, false);
});

test("EventBus reports a gap instead of silently truncating the replay", () => {
  const bus = new EventBus(3);
  const base = { type: "run.progress", run_id: "run_1", session_id: "ses_1", phase: "turn_start" };
  for (let i = 0; i < 10; i += 1) bus.push(base);

  assert.equal(bus.lastSeq, 10);
  assert.equal(bus.oldestSeq, 8);
  // Asking from seq 1 in a 3-slot ring: events 2..7 are gone.
  const replay = bus.replaySince(1);
  assert.equal(replay.gap, true);
  assert.deepEqual(
    replay.events.map((event) => event.seq),
    [8, 9, 10],
  );
});

test("EventBus notifies live subscribers and survives a throwing one", () => {
  const bus = new EventBus(10);
  const seen = [];
  bus.subscribe(() => {
    throw new Error("dead subscriber");
  });
  const unsubscribe = bus.subscribe((event) => seen.push(event.seq));
  bus.push({ type: "run.progress", run_id: "r", session_id: "s", phase: "turn_start" });
  unsubscribe();
  bus.push({ type: "run.progress", run_id: "r", session_id: "s", phase: "turn_start" });
  assert.deepEqual(seen, [1]);
});
