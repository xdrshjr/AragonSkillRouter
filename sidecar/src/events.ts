/**
 * Normalisation of upstream `AgentEvent`s into the platform event union
 * (docs/CONTRACT.md §5.10) plus the per-session replay buffer behind SSE.
 *
 * TWO RULES GOVERN EVERYTHING HERE.
 *
 * 1. Forward compatibility is a contract, not a courtesy. Upstream *will* add
 *    event types; `schemaVersion`-style promises say a consumer must ignore what
 *    it does not know. So an unrecognised event is counted (`unknown_events`,
 *    surfaced on /info) and dropped — never thrown, never turned into a run
 *    failure. A run that dies because the engine got better is worse than a run
 *    that ignores a field.
 *
 * 2. Terminal events are NOT derived from `agent_end`. The engine emits
 *    `agent_end` on every exit path including errors, so it cannot distinguish
 *    "finished", "stopped" and "failed". The runner emits the terminal event
 *    after `prompt()` settles, where the outcome is actually known. Emitting one
 *    here too would give every client two endings to reconcile.
 */

import type { AgentEvent } from "@aragon-agent/core";

/** The event types this sidecar emits. Mirrors CONTRACT §5.10. */
export type NormalisedType =
  | "run.started"
  | "assistant.delta"
  | "tool.call"
  | "tool.result"
  | "todo.update"
  | "run.progress"
  | "run.stopped"
  | "run.failed"
  | "run.finished";

export interface NormalisedEvent {
  /** Monotonic per session. Doubles as the SSE `id:` so a client can resume. */
  seq: number;
  run_id: string;
  session_id: string;
  /** ISO-8601 UTC. */
  at: string;
  type: NormalisedType;
  [field: string]: unknown;
}

export type EventFrame = Omit<NormalisedEvent, "seq" | "at">;

/**
 * Counters surfaced on /info. `unknown_events` is the number an operator watches
 * after an upgrade: non-zero means the engine grew an event we do not map yet.
 */
export interface NormaliserStats {
  unknown_events: number;
  /** Known-but-intentionally-silent upstream events (e.g. `agent_end`). */
  absorbed_events: number;
  seen: Record<string, number>;
}

function stringifyError(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function clampText(value: unknown, limit: number): { text: string; truncated: boolean; bytes: number } {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  const bytes = Buffer.byteLength(text, "utf8");
  if (text.length <= limit) return { text, truncated: false, bytes };
  return { text: `${text.slice(0, limit)}…[truncated ${bytes - limit} chars]`, truncated: true, bytes };
}

/**
 * One instance per run. It is stateful only for counting, so a retry of the same
 * prompt gets a fresh set of numbers without losing the process-wide totals
 * (which live on the server and are summed from `stats()`).
 */
export class EventNormaliser {
  readonly stats: NormaliserStats = { unknown_events: 0, absorbed_events: 0, seen: {} };
  private turn = 0;

  /** Turn counter is exposed so the runner can report `turns` honestly. */
  get turns(): number {
    return this.turn;
  }

  constructor(private readonly opts: { maxToolOutputChars: number }) {}

  push(raw: AgentEvent): EventFrame[] {
    const type = (raw as { type?: string }).type ?? "unknown";
    this.stats.seen[type] = (this.stats.seen[type] ?? 0) + 1;

    switch (type) {
      case "agent_start":
        // The runner emits run.started itself so the event can carry the model
        // and tool list from the session config; absorbing avoids a duplicate.
        this.stats.absorbed_events += 1;
        return [];

      case "agent_end":
        this.stats.absorbed_events += 1;
        return [];

      case "turn_start":
        this.turn += 1;
        return [{ type: "run.progress", phase: "turn_start", turn: this.turn }];

      case "steering_accepted": {
        const ids = (raw as { ids?: unknown }).ids;
        return [
          {
            type: "run.progress",
            phase: "steering_accepted",
            ids: Array.isArray(ids) ? ids : [],
          },
        ];
      }

      case "turn_end": {
        const ev = raw as { message?: { stopReason?: string }; usage?: Record<string, unknown> };
        return [
          {
            type: "run.progress",
            phase: "turn_end",
            turn: this.turn,
            stop_reason: ev.message?.stopReason ?? null,
            usage: normalizeUsage(ev.usage),
          },
        ];
      }

      case "message_update":
        return this.pushStreamEvent((raw as { streamEvent?: unknown }).streamEvent);

      case "tool_execution_start": {
        const ev = raw as { toolCallId?: string; toolName?: string; args?: unknown };
        return [
          {
            type: "tool.call",
            tool_call_id: ev.toolCallId ?? "",
            tool: ev.toolName ?? "",
            args: ev.args ?? {},
          },
        ];
      }

      case "tool_execution_end": {
        const ev = raw as {
          toolCallId?: string;
          toolName?: string;
          result?: { content?: Array<{ type?: string; text?: string }>; isError?: boolean };
          isError?: boolean;
          duration?: number;
        };
        const content = ev.result?.content ?? [];
        const output = content
          .filter((part) => part?.type === "text")
          .map((part) => part.text ?? "")
          .join("\n");
        const clamped = clampText(output, this.opts.maxToolOutputChars);
        return [
          {
            type: "tool.result",
            tool_call_id: ev.toolCallId ?? "",
            tool: ev.toolName ?? "",
            is_error: Boolean(ev.isError ?? ev.result?.isError),
            duration_ms: typeof ev.duration === "number" ? ev.duration : null,
            output: clamped.text,
            output_bytes: clamped.bytes,
            output_truncated: clamped.truncated,
          },
        ];
      }

      case "code_execution_start":
        // Unreachable by construction: this sidecar never supplies a sandbox.
        // Mapped anyway so the ceiling is visible in code, not just in a comment.
        return [{ type: "run.progress", phase: "code_execution_start" }];

      case "code_execution_end": {
        const ev = raw as { duration?: number; error?: string; output?: string };
        return [
          {
            type: "run.progress",
            phase: "code_execution_end",
            duration_ms: typeof ev.duration === "number" ? ev.duration : null,
            error: ev.error ? clampText(ev.error, 1000).text : null,
            output: ev.output ? clampText(ev.output, this.opts.maxToolOutputChars).text : null,
          },
        ];
      }

      case "compaction_start": {
        const ev = raw as { trigger?: string; messageCount?: number };
        return [
          {
            type: "run.progress",
            phase: "compaction_start",
            trigger: ev.trigger ?? null,
            message_count: ev.messageCount ?? null,
          },
        ];
      }

      case "compaction_end": {
        const ev = raw as {
          applied?: boolean;
          mode?: string;
          reason?: string;
          messagesBefore?: number;
          messagesAfter?: number;
          droppedMessages?: number;
          estimatedTokensBefore?: number;
          estimatedTokensAfter?: number;
          durationMs?: number;
        };
        return [
          {
            type: "run.progress",
            phase: "compaction_end",
            applied: Boolean(ev.applied),
            mode: ev.mode ?? null,
            reason: ev.reason ?? null,
            messages_before: ev.messagesBefore ?? null,
            messages_after: ev.messagesAfter ?? null,
            dropped_messages: ev.droppedMessages ?? null,
            estimated_tokens_before: ev.estimatedTokensBefore ?? null,
            estimated_tokens_after: ev.estimatedTokensAfter ?? null,
            duration_ms: ev.durationMs ?? null,
          },
        ];
      }

      default:
        this.stats.unknown_events += 1;
        return [];
    }
  }

  private pushStreamEvent(streamEvent: unknown): EventFrame[] {
    const ev = (streamEvent ?? {}) as Record<string, unknown>;
    const kind = typeof ev.type === "string" ? ev.type : "unknown";
    switch (kind) {
      case "text_delta":
        return [{ type: "assistant.delta", delta: String(ev.delta ?? "") }];

      case "thinking_start":
        return [{ type: "run.progress", phase: "thinking_start" }];

      case "thinking_delta":
        return [{ type: "run.progress", phase: "thinking_delta", delta: String(ev.delta ?? "") }];

      case "tool_call_start":
        return [
          {
            type: "run.progress",
            phase: "tool_call_start",
            tool_call_id: ev.toolCallId ?? "",
            tool: ev.toolName ?? "",
          },
        ];

      case "tool_call_delta":
        // Argument fragments arrive character by character; forwarding them would
        // be thousands of events for one call. The complete args land on
        // `tool_execution_start`.
        this.stats.absorbed_events += 1;
        return [];

      case "tool_call_end":
        this.stats.absorbed_events += 1;
        return [];

      case "done": {
        const msg = ev.message as { stopReason?: string } | undefined;
        return [
          {
            type: "run.progress",
            phase: "llm_done",
            stop_reason: msg?.stopReason ?? null,
            usage: normalizeUsage(ev.usage as Record<string, unknown> | undefined),
          },
        ];
      }

      case "error":
        // Not terminal: the engine retries, and the loop reports its own failure
        // outcome. Reporting a run failure here would race that.
        return [
          {
            type: "run.progress",
            phase: "llm_error",
            message: clampText(stringifyError(ev.error), 1000).text,
          },
        ];

      case "retry_scheduled":
        return [
          {
            type: "run.progress",
            phase: "retry_scheduled",
            attempt: ev.attempt ?? null,
            max_retries: ev.maxRetries ?? null,
            delay_ms: ev.delayMs ?? null,
            resume_at: ev.resumeAt ?? null,
            error_type: ev.errorType ?? null,
            message: clampText(stringifyError(ev.message), 500).text,
          },
        ];

      case "retry_attempt":
        return [
          {
            type: "run.progress",
            phase: "retry_attempt",
            attempt: ev.attempt ?? null,
            max_retries: ev.maxRetries ?? null,
          },
        ];

      case "stream_restart":
        return [
          {
            type: "run.progress",
            phase: "stream_restart",
            attempt: ev.attempt ?? null,
            discarded_tool_call_ids: Array.isArray(ev.discardedToolCallIds)
              ? ev.discardedToolCallIds
              : [],
          },
        ];

      default:
        this.stats.unknown_events += 1;
        return [];
    }
  }
}

/** Upstream `TokenUsage` → the snake_case shape the platform stores. */
export function normalizeUsage(usage: Record<string, unknown> | undefined): Record<string, number> {
  if (!usage) return {};
  const out: Record<string, number> = {};
  const pairs: Array<[string, string]> = [
    ["input_tokens", "inputTokens"],
    ["output_tokens", "outputTokens"],
    ["cache_read_tokens", "cacheReadTokens"],
    ["cache_write_tokens", "cacheWriteTokens"],
    ["cost", "cost"],
  ];
  for (const [snake, camel] of pairs) {
    const value = usage[camel];
    if (typeof value === "number" && Number.isFinite(value)) out[snake] = value;
  }
  if (typeof usage.inputTokens === "number" && typeof usage.outputTokens === "number") {
    out.total_tokens = usage.inputTokens + usage.outputTokens;
  }
  return out;
}

/**
 * Per-session event log + fan-out.
 *
 * The buffer exists for two reasons that must not be confused:
 *   * `replaySince(seq)` — a late subscriber (the backend attaches *after* the
 *     202 that accepted the message) gets everything it missed;
 *   * the same list is what `GET /sessions/{id}` shows as recent activity.
 *
 * It is a bounded ring: a runaway run cannot exhaust memory, and the oldest
 * sequence number is reported so a client that fell off the back can be told to
 * resync instead of silently losing events.
 */
export class EventBus {
  private readonly events: NormalisedEvent[] = [];
  private seq = 0;
  private readonly listeners = new Set<(event: NormalisedEvent) => void>();

  constructor(private readonly capacity: number) {}

  get lastSeq(): number {
    return this.seq;
  }

  get oldestSeq(): number {
    return this.events.length > 0 ? this.events[0]!.seq : this.seq + 1;
  }

  push(frame: EventFrame): NormalisedEvent {
    this.seq += 1;
    const record = { ...(frame as Record<string, unknown>), seq: this.seq, at: new Date().toISOString() };
    const event = record as unknown as NormalisedEvent;
    this.events.push(event);
    if (this.events.length > this.capacity) this.events.shift();
    for (const listener of [...this.listeners]) {
      try {
        listener(event);
      } catch {
        // A dead subscriber must never break a run.
      }
    }
    return event;
  }

  /**
   * Events with `seq > after`. When `after` is older than the buffer, `gap` is
   * true and the caller is expected to say so rather than pretend.
   */
  replaySince(after: number | null): { events: NormalisedEvent[]; gap: boolean } {
    if (after === null) return { events: [...this.events], gap: false };
    if (after < this.oldestSeq - 1) return { events: [...this.events], gap: true };
    return { events: this.events.filter((event) => event.seq > after), gap: false };
  }

  recent(limit: number): NormalisedEvent[] {
    return this.events.slice(-limit);
  }

  subscribe(listener: (event: NormalisedEvent) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
}
