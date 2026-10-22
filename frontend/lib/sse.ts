'use client';

/**
 * Server-sent events over `fetch` + `ReadableStream`.
 *
 * Why not `EventSource`: `GET /api/stream/agent/{run_id}` authenticates with the
 * session cookie, and `EventSource` cannot send an `Authorization` header — nor
 * can it be aborted cleanly or resume with a header on every reconnect. This
 * client therefore owns the framing, the `Last-Event-ID` resume and the backoff.
 *
 * Rules encoded here (CONTRACT §5.10, backend `app/api/stream.py`):
 *  - every frame carries `id: <seq>`; the last id is replayed as `Last-Event-ID`
 *    so a reconnect resumes exactly where it stopped;
 *  - `keepalive` arrives every 15 s and `: comment` lines (idle timeout, connected
 *    notices) must never break the parser — they are consumed silently;
 *  - `retry:` sets the base backoff; failures back off exponentially with jitter
 *    and flip the visible state to `reconnecting`;
 *  - a 4xx other than 408/429 is not retried: the request will never succeed, so
 *    the caller is told once instead of hammering the server.
 */

export type SseConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error';

export interface SseEvent {
  /** `id:` field — the run's monotonic seq. */
  id: string | null;
  /** `event:` field, defaulting to `message`. */
  type: string;
  /** Joined `data:` lines. */
  data: string;
}

export interface SseStateDetail {
  attempt: number;
  delayMs?: number;
  error?: Error;
}

export interface SseStreamOptions {
  url: string;
  headers?: Record<string, string>;
  /** Seq to resume from (sent as `Last-Event-ID`). */
  lastEventId?: string | null;
  onEvent: (event: SseEvent) => void;
  onState?: (state: SseConnectionState, detail?: SseStateDetail) => void;
  /**
   * Return true once the event is terminal for the caller (run.stopped,
   * run.finished, run.failed…). The stream then closes without reconnecting.
   */
  stopWhen?: (event: SseEvent) => boolean;
  /** Reconnect attempts before giving up. Default 6. */
  maxAttempts?: number;
  signal?: AbortSignal;
}

export interface SseStream {
  close: () => void;
  lastEventId: () => string | null;
}

const DEFAULT_ATTEMPTS = 6;
const BASE_BACKOFF_MS = 700;
const MAX_BACKOFF_MS = 10_000;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve();
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

interface ParsedFrame {
  id: string | null;
  type: string;
  data: string;
  retryMs: number | null;
}

/**
 * Parse one frame (already split on the blank line). Returns `null` for a frame
 * that only carried comments — `: keepalive`, `: idle timeout` — which is the
 * case that must not disturb the caller's state.
 */
function parseFrame(raw: string): ParsedFrame | null {
  let type = 'message';
  let id: string | null = null;
  let retryMs: number | null = null;
  const dataLines: string[] = [];

  for (const line of raw.split('\n')) {
    if (!line) continue;
    if (line.startsWith(':')) continue; // comment / keepalive
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') type = value || 'message';
    else if (field === 'data') dataLines.push(value);
    else if (field === 'id') id = value;
    else if (field === 'retry') {
      const parsed = Number.parseInt(value, 10);
      if (Number.isFinite(parsed) && parsed >= 0) retryMs = parsed;
    }
  }

  if (dataLines.length === 0) return null;
  return { id, type, data: dataLines.join('\n'), retryMs };
}

/** Only these statuses can succeed on a retry. */
function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

/**
 * Open an SSE stream. Returns a handle immediately; the connection work happens
 * asynchronously and every state change is reported through `onState`.
 */
export function openSseStream(options: SseStreamOptions): SseStream {
  const controller = new AbortController();
  const maxAttempts = options.maxAttempts ?? DEFAULT_ATTEMPTS;
  let closed = false;
  let lastEventId: string | null = options.lastEventId ?? null;
  let baseBackoff = BASE_BACKOFF_MS;
  let attempt = 0;

  const externalAbort = () => {
    closed = true;
    controller.abort();
  };
  if (options.signal) {
    if (options.signal.aborted) externalAbort();
    else options.signal.addEventListener('abort', externalAbort, { once: true });
  }

  const setState = (state: SseConnectionState, detail?: SseStateDetail) => {
    if (!options.onState) return;
    options.onState(state, detail ?? { attempt });
  };

  const dispatch = (frame: ParsedFrame) => {
    if (frame.id) lastEventId = frame.id;
    if (frame.retryMs !== null) baseBackoff = Math.max(200, frame.retryMs);
    const event: SseEvent = { id: frame.id, type: frame.type, data: frame.data };
    try {
      options.onEvent(event);
    } catch (error) {
      // A rendering error in a consumer must not kill the transport silently.
      setState('error', { attempt, error: error instanceof Error ? error : new Error(String(error)) });
    }
    if (options.stopWhen?.(event)) {
      closed = true;
      controller.abort();
    }
  };

  const readBody = async (body: ReadableStream<Uint8Array>): Promise<number> => {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let received = 0;
    try {
      while (!closed) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        let boundary = buffer.indexOf('\n\n');
        while (boundary !== -1) {
          const rawFrame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const frame = parseFrame(rawFrame);
          if (frame) {
            // A keepalive proves the connection works, not that the run moved on;
            // counting it as progress would keep a finished run reconnecting.
            if (frame.type !== 'keepalive') received += 1;
            dispatch(frame);
            if (closed) return received;
          }
          boundary = buffer.indexOf('\n\n');
        }
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        // The stream may already be closed; nothing to release.
      }
    }
    return received;
  };

  const run = async () => {
    while (!closed) {
      const state = attempt === 0 ? 'connecting' : 'reconnecting';
      setState(state);
      let received = 0;
      try {
        const headers: Record<string, string> = {
          accept: 'text/event-stream',
          ...(options.headers ?? {}),
        };
        if (lastEventId) headers['last-event-id'] = lastEventId;
        const response = await fetch(options.url, {
          method: 'GET',
          headers,
          credentials: 'include',
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) {
          const error = new Error(`The event stream answered HTTP ${response.status}.`);
          if (!isRetryableStatus(response.status)) {
            setState('error', { attempt, error });
            return;
          }
          throw error;
        }
        if (!response.body) {
          setState('error', { attempt, error: new Error('This browser cannot stream an event response.') });
          return;
        }
        attempt = 0;
        setState('open');
        received = await readBody(response.body);
        if (closed) return;
        // A clean end with nothing new means the run is over (the backend closes
        // the stream after the terminal state) — reconnecting would loop.
        if (received === 0) {
          setState('closed');
          return;
        }
        attempt += 1;
      } catch (cause) {
        if (closed) return;
        const error = cause instanceof Error ? cause : new Error(String(cause));
        if (error.name === 'AbortError') return;
        attempt += 1;
        if (attempt > maxAttempts) {
          setState('error', { attempt, error });
          return;
        }
      }
      if (closed) return;
      const delay = Math.min(MAX_BACKOFF_MS, baseBackoff * 2 ** Math.max(0, attempt - 1));
      const jitter = Math.round(delay * 0.2 * Math.random());
      setState('reconnecting', { attempt, delayMs: delay + jitter });
      await sleep(delay + jitter, controller.signal);
    }
    setState('closed');
  };

  void run();

  return {
    close: () => {
      if (closed) return;
      closed = true;
      controller.abort();
      setState('closed');
    },
    lastEventId: () => lastEventId,
  };
}
