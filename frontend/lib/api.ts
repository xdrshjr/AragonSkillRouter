/**
 * Typed fetch wrapper for the Skill Router admin API.
 *
 * Rules encoded here (CONTRACT §2.1, §9):
 *  - every request is same-origin and goes to a relative `/api/...` path, so the
 *    session cookie and the CSRF cookie behave normally through the Next rewrite;
 *  - mutations echo the `sr_csrf` cookie in `x-csrf-token`;
 *  - a 401 sends the browser to `/login?next=<current path>`;
 *  - every failure is normalised into `ApiError` with
 *    `{code, message, details, requestId, actions}` so the UI can always show the
 *    request id and the actionable next steps.
 */

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface ApiErrorShape {
  code: string;
  message: string;
  details: unknown;
  requestId: string | null;
  actions: string[];
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: unknown;
  readonly requestId: string | null;
  readonly actions: string[];

  constructor(init: {
    code: string;
    message: string;
    status?: number;
    details?: unknown;
    requestId?: string | null;
    actions?: string[];
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.code = init.code;
    this.status = init.status ?? 0;
    this.details = init.details ?? null;
    this.requestId = init.requestId ?? null;
    this.actions = init.actions ?? [];
  }

  toShape(): ApiErrorShape {
    return {
      code: this.code,
      message: this.message,
      details: this.details,
      requestId: this.requestId,
      actions: this.actions,
    };
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** Message plus the request id, so a user can quote one line to support. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.requestId ? `${error.message} (request ${error.requestId})` : error.message;
  }
  if (error instanceof Error) return error.message;
  return 'Unexpected error.';
}

export function errorActions(error: unknown): string[] {
  return error instanceof ApiError ? error.actions : [];
}

export function errorRequestId(error: unknown): string | null {
  return error instanceof ApiError ? error.requestId : null;
}

const CSRF_COOKIE = 'sr_csrf';
const CSRF_HEADER = 'x-csrf-token';
const LOGIN_PATH = '/login';

let csrfFallback: string | null = null;

/**
 * The CSRF token from `GET /api/auth/session` is authoritative; the cookie is
 * the primary source. This only covers the (rare) case where the cookie is not
 * readable yet but the session payload already carried the token.
 */
export function setCsrfFallback(token: string | null): void {
  csrfFallback = token;
}

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const entry = part.trim();
    if (entry.startsWith(prefix)) return decodeURIComponent(entry.slice(prefix.length));
  }
  return null;
}

export function readCsrfToken(): string | null {
  return readCookie(CSRF_COOKIE) ?? csrfFallback;
}

/** Serialise query params, dropping empty values. */
export function queryString(params?: QueryParams): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : '';
}

function redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  if (window.location.pathname === LOGIN_PATH) return;
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`${LOGIN_PATH}?next=${encodeURIComponent(next)}`);
}

interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Used by the session probe: it decides itself how to react to a 401. */
  skipAuthRedirect?: boolean;
}

const JSON_TYPE = 'application/json';

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes(JSON_TYPE) || contentType.includes('+json')) {
    try {
      return JSON.parse(text);
    } catch {
      throw new ApiError({
        code: 'invalid_json_response',
        message: 'The server answered with malformed JSON. The request may have failed half-way.',
        status: response.status,
        requestId: response.headers.get('x-request-id'),
      });
    }
  }
  return text;
}

function normaliseError(response: Response, payload: unknown): ApiError {
  const headerRequestId = response.headers.get('x-request-id');
  if (payload && typeof payload === 'object' && 'error' in payload) {
    const envelope = (payload as { error?: unknown }).error;
    if (envelope && typeof envelope === 'object') {
      const detail = envelope as Record<string, unknown>;
      return new ApiError({
        code: typeof detail.code === 'string' ? detail.code : `http_${response.status}`,
        message:
          typeof detail.message === 'string' && detail.message
            ? detail.message
            : `Request failed with HTTP ${response.status}.`,
        status: response.status,
        details: detail.details ?? null,
        requestId: typeof detail.request_id === 'string' ? detail.request_id : headerRequestId,
        actions: Array.isArray(detail.actions) ? detail.actions.filter((a): a is string => typeof a === 'string') : [],
      });
    }
  }
  const fallback =
    typeof payload === 'string' && payload.trim() ? payload.trim().slice(0, 400) : `Request failed with HTTP ${response.status}.`;
  return new ApiError({
    code: `http_${response.status}`,
    message: fallback,
    status: response.status,
    details: payload ?? null,
    requestId: headerRequestId,
  });
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const url = `${path}${queryString(options.query)}`;
  const headers: Record<string, string> = { accept: JSON_TYPE, ...(options.headers ?? {}) };
  const isMutation = method !== 'GET';
  let body: string | undefined;

  if (options.body !== undefined) {
    headers['content-type'] = JSON_TYPE;
    body = JSON.stringify(options.body);
  }
  if (isMutation) {
    const token = readCsrfToken();
    if (token) headers[CSRF_HEADER] = token;
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body,
      credentials: 'include',
      cache: 'no-store',
      signal: options.signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'network_error',
      message: 'Could not reach the Skill Router API. Check that the backend is running and try again.',
      details: cause instanceof Error ? cause.message : String(cause),
      actions: ['Confirm the backend is up on its port.', 'Reload the page and retry the action.'],
    });
  }

  const payload = await parseBody(response);

  if (!response.ok) {
    const error = normaliseError(response, payload);
    if (response.status === 401 && !options.skipAuthRedirect) {
      redirectToLogin();
    }
    throw error;
  }

  return payload as T;
}

export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, 'body'>): Promise<T> => request<T>('GET', path, options),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'body'>): Promise<T> =>
    request<T>('POST', path, { ...options, body: body ?? {} }),
  patch: <T>(path: string, body: unknown, options?: Omit<RequestOptions, 'body'>): Promise<T> =>
    request<T>('PATCH', path, { ...options, body }),
  del: <T>(path: string, options?: Omit<RequestOptions, 'body'>): Promise<T> => request<T>('DELETE', path, options),
};

/* --------------------------------------------------------------- endpoints --- */

export const endpoints = {
  auth: {
    login: '/api/auth/login',
    logout: '/api/auth/logout',
    session: '/api/auth/session',
    changePassword: '/api/auth/change-password',
  },
  bootstrapStatus: '/api/bootstrap/status',
  overview: '/api/admin/overview',
  overviewProblems: '/api/admin/overview/problems',
  providers: '/api/admin/providers',
  provider: (id: string) => `/api/admin/providers/${encodeURIComponent(id)}`,
  credentials: '/api/admin/credentials',
  credential: (id: string) => `/api/admin/credentials/${encodeURIComponent(id)}`,
  credentialVerify: (id: string) => `/api/admin/credentials/${encodeURIComponent(id)}/verify`,
  credentialTestCall: (id: string) => `/api/admin/credentials/${encodeURIComponent(id)}/test-call`,
  credentialImpact: (id: string) => `/api/admin/credentials/${encodeURIComponent(id)}/impact`,
  capabilities: '/api/admin/capabilities',
  capability: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}`,
  capabilityVersions: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/versions`,
  capabilityVersion: (id: string, versionId: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}`,
  capabilityVersionDiff: (id: string, from: string, to: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
  capabilityValidate: (id: string, versionId: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/validate`,
  capabilityTest: (id: string, versionId: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/test`,
  capabilityPublish: (id: string, versionId: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/publish`,
  capabilityDeprecate: (id: string, versionId: string) =>
    `/api/admin/capabilities/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/deprecate`,
  capabilityRollback: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/rollback`,
  capabilityTests: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/tests`,
  capabilityInvoke: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/invoke`,
  capabilityRoutes: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/routes`,
  capabilityRoutePreview: (id: string) => `/api/admin/capabilities/${encodeURIComponent(id)}/route-preview`,
  route: (routeId: string) => `/api/admin/routes/${encodeURIComponent(routeId)}`,
  routeHealthCheck: (routeId: string) => `/api/admin/routes/${encodeURIComponent(routeId)}/health-check`,
} as const;
