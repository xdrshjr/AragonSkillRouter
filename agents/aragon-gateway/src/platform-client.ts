/**
 * Client for the platform's internal API (spec §9.5, direction gateway → Python).
 *
 * The gateway implements `/healthz`, `/internal/turns*` and
 * `/internal/admin/reload`; it *calls* these three:
 *
 *   POST /internal/credentials/materialize  {credential_ref, purpose, ttl_s<=60}
 *   GET  /internal/capabilities            capability catalogue for `sr_*` tools
 *   POST /internal/audit                   per-tool-call audit entry
 *
 * Plus the tool-proxy paths the `sr_*` tools use (see tools/README note in
 * ../README.md — §9.5 does not spell them out, so the gateway treats the platform
 * as the system of record and forwards every tool body verbatim).
 *
 * Two invariants:
 * 1. Every request carries `X-Internal-Token`. There is no anonymous path.
 * 2. Secrets returned by `materialize()` are never logged, never cached to disk and
 *    never echoed in an error message. `redactSecrets()` is the only way they are
 *    allowed to appear anywhere near a log line.
 */

import type { AuditEntry } from './types.js';

export interface PlatformClientOptions {
  internalUrl: string;
  sharedSecret: string;
  timeoutMs: number;
  /** Implementation seam for tests. Defaults to global `fetch`. */
  fetchImpl?: typeof fetch;
}

/** Response of `POST /internal/credentials/materialize` (spec §9.5). */
export interface MaterializedCredential {
  provider_id: string;
  base_url?: string;
  kind: string;
  auth_type: string;
  header_name?: string;
  /** Short-lived plaintext. Process memory only — never log, never persist. */
  secret: string;
  /** When this value must be discarded (epoch ms), computed locally. */
  expires_at: number;
}

export interface CapabilitySummary {
  key: string;
  name?: string;
  kind?: string;
  mode?: string;
  status?: string;
  [key: string]: unknown;
}

export interface CapabilityCatalog {
  capabilities: CapabilitySummary[];
  etag?: string;
  fetched_at: string;
}

export class PlatformError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  constructor(message: string, status: number, code: string, requestId: string) {
    super(message);
    this.name = 'PlatformError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

/** Max TTL the platform accepts for a materialized credential (spec §9.5). */
export const MAX_CREDENTIAL_TTL_S = 60;

const SECRET_KEY_PATTERN = /(secret|token|api[-_]?key|password|passwd|authorization|cookie|bearer)/i;

/**
 * Recursively redact secret-looking fields.
 *
 * Strings are NOT clipped here: tool results legitimately carry long documents, and
 * the redactor's job is secrets, not length. Length is bounded separately by
 * `redactedSummary()` (logs/audit) and `MAX_RESULT_CHARS` (model context).
 */
export function redactSecrets(value: unknown, depth = 0): unknown {
  if (depth > 8) return '[depth-limited]';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((v) => redactSecrets(v, depth + 1));
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = SECRET_KEY_PATTERN.test(k) ? '[redacted]' : redactSecrets(v, depth + 1);
    }
    return out;
  }
  return value;
}

/** Render an unknown value as a short, secret-free string for logs/audit. */
export function redactedSummary(value: unknown, maxChars = 400): string {
  let text: string;
  try {
    text = JSON.stringify(redactSecrets(value));
  } catch {
    text = String(value);
  }
  if (text === undefined) text = String(value);
  return text.length > maxChars ? `${text.slice(0, maxChars)}…` : text;
}

export class PlatformClient {
  private readonly internalUrl: string;
  private readonly sharedSecret: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly credentialCache = new Map<string, MaterializedCredential>();

  constructor(options: PlatformClientOptions) {
    this.internalUrl = options.internalUrl.replace(/\/+$/, '');
    this.sharedSecret = options.sharedSecret;
    this.timeoutMs = options.timeoutMs;
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  }

  /**
   * Low-level call. Throws `PlatformError` for transport and HTTP failures; the
   * message includes the platform's own error code and request id when present so
   * a failure is traceable in the UI (spec §7.14).
   */
  async call<T>(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
    opts: { timeoutMs?: number; requestId?: string; signal?: AbortSignal } = {},
  ): Promise<T> {
    const url = `${this.internalUrl}${path.startsWith('/') ? path : `/${path}`}`;
    const headers: Record<string, string> = {
      'X-Internal-Token': this.sharedSecret,
      accept: 'application/json',
    };
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (opts.requestId) headers['x-request-id'] = opts.requestId;

    const signals: AbortSignal[] = [AbortSignal.timeout(opts.timeoutMs ?? this.timeoutMs)];
    if (opts.signal) signals.push(opts.signal);

    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method,
        headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.any(signals),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      throw new PlatformError(`platform request ${method} ${path} failed: ${message}`, 0, 'TRANSPORT_ERROR', opts.requestId ?? '');
    }

    const requestId = response.headers.get('x-request-id') ?? opts.requestId ?? '';
    const text = await response.text();

    if (!response.ok) {
      let code = `HTTP_${response.status}`;
      let detail = text.slice(0, 300);
      try {
        const parsed = JSON.parse(text) as { code?: unknown; message?: unknown; detail?: unknown };
        if (typeof parsed.code === 'string') code = parsed.code;
        if (typeof parsed.message === 'string') detail = parsed.message;
        else if (typeof parsed.detail === 'string') detail = parsed.detail;
      } catch {
        /* non-JSON error body: keep the clipped text */
      }
      throw new PlatformError(
        `platform ${method} ${path} → ${response.status} ${code}: ${detail}`,
        response.status,
        code,
        requestId,
      );
    }

    if (text.trim() === '') return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new PlatformError(
        `platform ${method} ${path} returned non-JSON body`,
        response.status,
        'BAD_JSON',
        requestId,
      );
    }
  }

  /**
   * Materialize a short-lived provider credential.
   *
   * `ttl_s` is clamped to `MAX_CREDENTIAL_TTL_S` (spec §9.5 hard limit: 60s) and
   * cached in memory until 5s before expiry so a burst of tool calls inside one
   * turn does not hammer the platform's 60/min rate limit.
   */
  async materialize(
    credentialRef: string,
    purpose: string,
    ttlS: number = 45,
    opts: { requestId?: string; forceRefresh?: boolean; signal?: AbortSignal } = {},
  ): Promise<MaterializedCredential> {
    if (credentialRef.trim() === '') {
      throw new PlatformError('credential_ref is empty', 0, 'INVALID_ARGUMENT', opts.requestId ?? '');
    }
    const ttl = Math.max(1, Math.min(Math.floor(ttlS), MAX_CREDENTIAL_TTL_S));
    const cached = this.credentialCache.get(credentialRef);
    if (!opts.forceRefresh && cached && cached.expires_at - 5_000 > Date.now()) {
      return cached;
    }

    const body = {
      credential_ref: credentialRef,
      purpose: purpose.slice(0, 200),
      ttl_s: ttl,
    };
    const raw = await this.call<Record<string, unknown>>('POST', '/internal/credentials/materialize', body, {
      requestId: opts.requestId,
      ...(opts.signal ? { signal: opts.signal } : {}),
      timeoutMs: Math.max(this.timeoutMs, 10_000),
    });

    const secret = raw['secret'];
    if (typeof secret !== 'string' || secret === '') {
      this.credentialCache.delete(credentialRef);
      throw new PlatformError(
        'materialize() response has no secret; refusing to proceed without a credential',
        0,
        'MALFORMED_CREDENTIAL',
        opts.requestId ?? '',
      );
    }

    const credential: MaterializedCredential = {
      provider_id: typeof raw['provider_id'] === 'string' ? raw['provider_id'] : '',
      ...(typeof raw['base_url'] === 'string' ? { base_url: raw['base_url'] } : {}),
      kind: typeof raw['kind'] === 'string' ? raw['kind'] : 'api_key',
      auth_type: typeof raw['auth_type'] === 'string' ? raw['auth_type'] : 'header',
      ...(typeof raw['header_name'] === 'string' ? { header_name: raw['header_name'] } : {}),
      secret,
      expires_at: Date.now() + ttl * 1000,
    };
    this.credentialCache.set(credentialRef, credential);
    return credential;
  }

  /** Drop every cached credential. Called on abort/shutdown so memory is clean. */
  clearCredentials(): void {
    this.credentialCache.clear();
  }

  /** Drop one cached credential (after a turn, or when a provider rejects it). */
  forgetCredential(credentialRef: string): void {
    this.credentialCache.delete(credentialRef);
  }

  /** `GET /internal/capabilities` — the platform capability catalogue. */
  async capabilities(opts: { signal?: AbortSignal; requestId?: string } = {}): Promise<CapabilityCatalog> {
    const raw = await this.call<unknown>('GET', '/internal/capabilities', undefined, {
      requestId: opts.requestId,
      ...(opts.signal ? { signal: opts.signal } : {}),
    });
    return normalizeCatalog(raw);
  }

  /**
   * `POST /internal/audit`. Audit is best-effort by design: a tool call that
   * succeeded must not be reported as failed because the audit write raced a
   * restart. The failure is surfaced to the caller instead of swallowed.
   */
  async audit(entry: AuditEntry): Promise<void> {
    await this.call<unknown>('POST', '/internal/audit', entry, { timeoutMs: 10_000 });
  }

  /**
   * Forward a platform-tool invocation to the platform, which owns validation,
   * budgets, fetch-guard and persistence. The gateway adds no semantics beyond
   * timeouts and redaction.
   */
  async invokeTool<T = unknown>(
    name: string,
    args: Record<string, unknown>,
    ctx: { sessionId: string; turnId: string; requestId: string; timeoutMs: number; signal?: AbortSignal },
  ): Promise<T> {
    const path = `/internal/tools/${encodeURIComponent(name)}`;
    return this.call<T>(
      'POST',
      path,
      {
        tool: name,
        arguments: args,
        session_id: ctx.sessionId,
        turn_id: ctx.turnId,
      },
      {
        timeoutMs: ctx.timeoutMs,
        requestId: ctx.requestId,
        ...(ctx.signal ? { signal: ctx.signal } : {}),
      },
    );
  }
}

function normalizeCatalog(raw: unknown): CapabilityCatalog {
  const fetched_at = new Date().toISOString();
  if (Array.isArray(raw)) {
    return { capabilities: raw.filter(isCapability), fetched_at };
  }
  if (raw !== null && typeof raw === 'object') {
    const obj = raw as Record<string, unknown>;
    const list = obj['capabilities'];
    const etag = typeof obj['etag'] === 'string' ? obj['etag'] : undefined;
    return {
      capabilities: Array.isArray(list) ? list.filter(isCapability) : [],
      ...(etag === undefined ? {} : { etag }),
      fetched_at,
    };
  }
  return { capabilities: [], fetched_at };
}

function isCapability(value: unknown): value is CapabilitySummary {
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof (value as Record<string, unknown>)['key'] === 'string'
  );
}
