/**
 * SSE writer for the internal turn event stream (spec §9.4, §9.5).
 *
 * One `TurnStream` per turn id. Responsibilities:
 * - monotonic `seq` per turn (the platform's SSE envelope requires it, §9.4);
 * - `: keep-alive` comment every 15s (configurable) so proxies and the Python
 *   client do not idle-timeout a long tool call;
 * - backpressure: an engine event listener is *synchronous*, so writes are queued
 *   and flushed against `drain`. If the peer stops reading and the queue passes a
 *   ceiling, the stream is closed instead of growing without bound — the client
 *   reconnects with `Last-Event-ID` and replays from the buffer;
 * - replay: `Last-Event-ID` support straight from the in-memory event log. Replay
 *   is read-only by construction: nothing here can re-execute a turn.
 */

import type { PlatformEvent, PlatformEventType } from './types.js';

/** Minimal sink so the stream is testable without Fastify. */
export interface SseSink {
  /** `false` means "buffered, wait for drain" (Node `writable.write` contract). */
  write(chunk: string): boolean;
  once(event: 'drain' | 'close' | 'error', listener: (...args: unknown[]) => void): void;
  end(): void;
}

export interface TurnStreamOptions {
  turnId: string;
  keepAliveMs: number;
  /** Max queued SSE chunks before the peer is considered dead. */
  maxQueuedChunks?: number;
  /** Max events retained for replay. */
  maxBufferedEvents?: number;
  now?: () => Date;
}

const DEFAULT_MAX_QUEUED_CHUNKS = 2_000;
const DEFAULT_MAX_BUFFERED_EVENTS = 20_000;

export class TurnStream {
  readonly turnId: string;
  private readonly keepAliveMs: number;
  private readonly maxQueuedChunks: number;
  private readonly maxBufferedEvents: number;
  private readonly now: () => Date;

  private sink: SseSink | null = null;
  private seq = 0;
  private readonly buffer: PlatformEvent[] = [];
  private pending: string[] = [];
  private flushing = false;
  private keepAliveTimer: NodeJS.Timeout | null = null;
  private closed = false;
  private overflowed = false;
  private droppedForReplay = 0;

  constructor(options: TurnStreamOptions) {
    this.turnId = options.turnId;
    this.keepAliveMs = options.keepAliveMs;
    this.maxQueuedChunks = options.maxQueuedChunks ?? DEFAULT_MAX_QUEUED_CHUNKS;
    this.maxBufferedEvents = options.maxBufferedEvents ?? DEFAULT_MAX_BUFFERED_EVENTS;
    this.now = options.now ?? (() => new Date());
  }

  get lastSeq(): number {
    return this.seq;
  }

  get isClosed(): boolean {
    return this.closed;
  }

  /** Attach a sink and flush anything already emitted (late subscriber). */
  attach(sink: SseSink, lastEventId = 0): void {
    this.sink = sink;
    sink.once('close', () => this.detach(sink));
    sink.once('error', () => this.detach(sink));

    const replay = this.eventsSince(lastEventId);
    if (lastEventId > 0 && replay.length === 0 && this.buffer.length > 0) {
      // Nothing new since the client's cursor — legal, just say so.
      this.queue(`: no new events since ${lastEventId}\n\n`);
    }
    for (const event of replay) this.queue(serialize(event));
    this.startKeepAlive();
    void this.flush();
  }

  /** Detach only if the given sink is still the active one. */
  detach(sink?: SseSink): void {
    if (sink && this.sink !== sink) return;
    this.stopKeepAlive();
    this.sink = null;
    this.pending = [];
  }

  /** Events with `seq > lastEventId`, oldest first. */
  eventsSince(lastEventId: number): PlatformEvent[] {
    if (this.buffer.length === 0) return [];
    const first = this.buffer[0];
    if (first && first.seq > lastEventId + 1) {
      this.droppedForReplay = lastEventId === 0 ? 0 : first.seq - 1 - lastEventId;
    }
    return this.buffer.filter((e) => e.seq > lastEventId);
  }

  /** Number of events the client's cursor skipped because they were evicted. */
  get replayGap(): number {
    return this.droppedForReplay;
  }

  /** Emit one platform event. Synchronous by design (upstream listeners are sync). */
  emit(type: PlatformEventType, payload: Record<string, unknown> = {}): PlatformEvent {
    this.seq += 1;
    const event: PlatformEvent = {
      seq: this.seq,
      turn_id: this.turnId,
      type,
      at: this.now().toISOString(),
      payload,
    };
    this.buffer.push(event);
    if (this.buffer.length > this.maxBufferedEvents) {
      this.buffer.shift();
    }
    if (this.sink) this.queue(serialize(event));
    return event;
  }

  /**
   * Adopt an event that was produced elsewhere while KEEPING its `seq`.
   *
   * Host mode runs the engine in a child process; the child already assigned the
   * per-turn sequence numbers, and the platform's SSE envelope requires them to be
   * monotonic, so the parent must not renumber.
   */
  ingest(event: PlatformEvent): void {
    if (this.closed) return;
    this.seq = Math.max(this.seq, event.seq);
    this.buffer.push(event);
    if (this.buffer.length > this.maxBufferedEvents) this.buffer.shift();
    if (this.sink) this.queue(serialize(event));
  }

  /** Send an SSE comment (used for keep-alive and diagnostics). */
  comment(text: string): void {
    if (!this.sink) return;
    this.queue(`: ${text}\n\n`);
  }

  /** Close the stream politely; the event log stays readable for a reconnect. */
  close(): void {
    this.closed = true;
    this.stopKeepAlive();
    if (this.sink) {
      try {
        this.sink.end();
      } catch {
        /* peer already gone */
      }
      this.sink = null;
    }
  }

  private startKeepAlive(): void {
    this.stopKeepAlive();
    if (this.keepAliveMs <= 0) return;
    this.keepAliveTimer = setInterval(() => {
      if (!this.sink) return;
      this.queue(': keep-alive\n\n');
    }, this.keepAliveMs);
    // Never keep the process alive just for a heartbeat.
    this.keepAliveTimer.unref?.();
  }

  private stopKeepAlive(): void {
    if (this.keepAliveTimer) {
      clearInterval(this.keepAliveTimer);
      this.keepAliveTimer = null;
    }
  }

  private queue(chunk: string): void {
    if (this.closed) return;
    this.pending.push(chunk);
    if (this.pending.length > this.maxQueuedChunks) {
      // The peer is not draining. Closing is safer than unbounded memory growth and
      // is recoverable: the platform reconnects with Last-Event-ID and replays.
      this.overflowed = true;
      this.closed = true;
      this.stopKeepAlive();
      const sink = this.sink;
      this.sink = null;
      this.pending = [];
      try {
        sink?.end();
      } catch {
        /* ignore */
      }
      return;
    }
    void this.flush();
  }

  private async flush(): Promise<void> {
    if (this.flushing) return;
    this.flushing = true;
    try {
      while (this.pending.length > 0) {
        const sink = this.sink;
        if (!sink) return;
        const chunk = this.pending.shift();
        if (chunk === undefined) return;
        let ok: boolean;
        try {
          ok = sink.write(chunk);
        } catch {
          this.detach(sink);
          return;
        }
        if (!ok) {
          await new Promise<void>((resolve) => {
            const done = (): void => resolve();
            sink.once('drain', done);
            sink.once('close', done);
          });
        }
      }
    } finally {
      this.flushing = false;
      if (this.overflowed) {
        this.pending = [];
      }
    }
  }
}

function serialize(event: PlatformEvent): string {
  return `id: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** Registry of live turn streams, keyed by turn id. */
export class StreamRegistry {
  private readonly streams = new Map<string, TurnStream>();
  private readonly keepAliveMs: number;

  constructor(keepAliveMs: number) {
    this.keepAliveMs = keepAliveMs;
  }

  ensure(turnId: string): TurnStream {
    const existing = this.streams.get(turnId);
    if (existing) return existing;
    const stream = new TurnStream({ turnId, keepAliveMs: this.keepAliveMs });
    this.streams.set(turnId, stream);
    return stream;
  }

  get(turnId: string): TurnStream | undefined {
    return this.streams.get(turnId);
  }

  delete(turnId: string): void {
    const stream = this.streams.get(turnId);
    stream?.close();
    this.streams.delete(turnId);
  }

  get size(): number {
    return this.streams.size;
  }

  closeAll(): void {
    for (const stream of this.streams.values()) stream.close();
    this.streams.clear();
  }
}
