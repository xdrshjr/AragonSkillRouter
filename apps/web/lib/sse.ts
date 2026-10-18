/**
 * SSE 封装（spec §9.4）。
 *
 * - 事件体：`data:` 一行 JSON（`{id,type,at,turn_id?,seq?,payload}`）
 * - 断线重连带 `Last-Event-ID` 语义：浏览器 EventSource 无法设置请求头，
 *   因此重连时把上一条事件 id 作为 `last_event_id` 查询参数带上（服务端应同时支持两种方式）。
 * - 心跳：服务端每 15s 发 `: keep-alive` 注释；客户端在 heartbeatMs 内未收到任何字节则主动重连。
 * - 指数退避 + 抖动，最大 maxDelayMs；可见性恢复（页面重新可见）时立即重连。
 */

import type { SseEnvelope } from './types';

export type SseStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface SseErrorInfo {
  kind: 'network' | 'heartbeat' | 'http';
  retryInMs: number | null;
  attempt: number;
}

export interface SseHandle {
  close(): void;
  status(): SseStatus;
  lastEventId(): string | null;
}

export interface OpenSseOptions {
  onEvent: (event: SseEnvelope) => void;
  onOpen?: () => void;
  onStatus?: (status: SseStatus) => void;
  onError?: (info: SseErrorInfo) => void;
  /** 心跳超时（毫秒），默认 45s（服务端 15s 一次注释） */
  heartbeatMs?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** 不带则无限重连 */
  maxAttempts?: number;
  lastEventId?: string | null;
  /** 额外的查询参数（如 since） */
  query?: Record<string, string | number | undefined>;
}

function joinQuery(base: string, params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== '');
  if (entries.length === 0) return base;
  const search = new URLSearchParams();
  for (const [k, v] of entries) search.set(k, String(v));
  return `${base}${base.includes('?') ? '&' : '?'}${search.toString()}`;
}

function parseEnvelope(raw: string): SseEnvelope | null {
  const text = raw.trim();
  if (!text || text.startsWith(':')) return null;
  try {
    const parsed = JSON.parse(text) as Partial<SseEnvelope> & { payload?: unknown };
    if (!parsed || typeof parsed.type !== 'string') return null;
    const payload =
      parsed.payload && typeof parsed.payload === 'object' && !Array.isArray(parsed.payload)
        ? (parsed.payload as SseEnvelope['payload'])
        : {};
    return {
      id: parsed.id,
      type: parsed.type,
      at: typeof parsed.at === 'string' ? parsed.at : new Date().toISOString(),
      turn_id: parsed.turn_id,
      session_id: parsed.session_id,
      seq: parsed.seq,
      payload,
    };
  } catch {
    return null;
  }
}

/**
 * 打开一条 SSE 流。`path` 以 `/api` 开头（同源），例如
 * `/api/admin/agent/sessions/<id>/stream`。
 */
export function openSse(path: string, options: OpenSseOptions): SseHandle {
  const {
    onEvent,
    onOpen,
    onStatus,
    onError,
    heartbeatMs = 45_000,
    baseDelayMs = 1_000,
    maxDelayMs = 30_000,
    maxAttempts,
  } = options;

  let source: EventSource | null = null;
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let lastId: string | null = options.lastEventId ?? null;
  let attempt = 0;
  let current: SseStatus = 'connecting';
  let closed = false;

  const setStatus = (next: SseStatus): void => {
    if (current === next) return;
    current = next;
    onStatus?.(next);
  };

  const clearTimers = (): void => {
    if (heartbeatTimer) {
      clearTimeout(heartbeatTimer);
      heartbeatTimer = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const armHeartbeat = (): void => {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    heartbeatTimer = setTimeout(() => {
      // 心跳超时：主动断开并重连（不依赖浏览器默认行为）
      teardown();
      const delay = backoff(attempt++);
      onError?.({ kind: 'heartbeat', retryInMs: delay, attempt });
      scheduleReconnect(delay);
    }, heartbeatMs);
  };

  const backoff = (n: number): number => {
    const raw = Math.min(maxDelayMs, baseDelayMs * 2 ** Math.min(n, 6));
    const jitter = raw * 0.2 * Math.random();
    return Math.round(raw + jitter);
  };

  const teardown = (): void => {
    if (source) {
      source.onopen = null;
      source.onerror = null;
      source.onmessage = null;
      source.close();
      source = null;
    }
  };

  const scheduleReconnect = (delay: number): void => {
    if (closed) return;
    if (maxAttempts !== undefined && attempt > maxAttempts) {
      setStatus('closed');
      return;
    }
    setStatus('reconnecting');
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, delay);
  };

  function connect(): void {
    if (closed) return;
    clearTimers();
    setStatus(attempt === 0 ? 'connecting' : 'reconnecting');

    const url = joinQuery(path, { ...options.query, last_event_id: lastId ?? undefined });

    try {
      source = new EventSource(url, { withCredentials: true });
    } catch {
      scheduleReconnect(backoff(attempt++));
      return;
    }

    source.onopen = () => {
      attempt = 0;
      setStatus('open');
      armHeartbeat();
      onOpen?.();
    };

    source.onmessage = (event: MessageEvent<string>) => {
      armHeartbeat();
      if (event.lastEventId) lastId = event.lastEventId;
      const envelope = parseEnvelope(event.data);
      if (!envelope) return;
      if (envelope.id !== undefined && envelope.id !== null) lastId = String(envelope.id);
      onEvent(envelope);
    };

    source.onerror = () => {
      // 服务端明确关闭 / 网络错误：EventSource 会自行重试，但无法携带 last_event_id，
      // 因此统一由我们自己重连。
      teardown();
      clearTimers();
      const delay = backoff(attempt++);
      onError?.({ kind: 'network', retryInMs: delay, attempt });
      scheduleReconnect(delay);
    };
  }

  const onVisible = (): void => {
    if (closed) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
      if (current === 'reconnecting') {
        attempt = 0;
        connect();
      }
    }
  };

  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisible);
  }

  connect();

  return {
    close(): void {
      closed = true;
      clearTimers();
      teardown();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisible);
      }
      setStatus('closed');
    },
    status: () => current,
    lastEventId: () => lastId,
  };
}

/** 管理端 Agent 会话流 */
export function agentStreamPath(sessionId: string): string {
  return `/api/admin/agent/sessions/${encodeURIComponent(sessionId)}/stream`;
}

/** 管理端全局通知流 */
export const ADMIN_EVENTS_PATH = '/api/admin/events';
