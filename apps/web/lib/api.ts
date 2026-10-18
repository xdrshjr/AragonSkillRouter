/**
 * 统一 fetch 封装（spec §9.1）。
 *
 * 约定：
 * - 同源 `/api/...`（next.config.mjs 反代到 FastAPI）
 * - cookie 会话：credentials: 'include'
 * - 写请求带 `X-CSRF-Token`（来自 `sr_csrf` cookie）
 * - 每个请求带 `X-Request-Id`，响应回显；错误体里的 request_id 用于报障
 * - 所有非 2xx 统一抛 ApiError（code/message/hint/request_id/retryable/details）
 * - 401 → 跳登录；503 MAINTENANCE → 广播维护事件（AppShell 展示维护横幅）
 */

import type { ApiErrorShape, JsonObject } from './types';

export const MAINTENANCE_EVENT = 'sr:maintenance';
export const OFFLINE_EVENT = 'sr:connectivity';

export class ApiError extends Error {
  readonly code: string;
  readonly hint: string | null;
  readonly requestId: string | null;
  readonly status: number;
  readonly retryable: boolean;
  readonly details: JsonObject;
  readonly body: ApiErrorShape | null;

  constructor(init: {
    code: string;
    message: string;
    hint?: string | null;
    requestId?: string | null;
    status?: number;
    retryable?: boolean;
    details?: JsonObject;
    body?: ApiErrorShape | null;
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.code = init.code;
    this.hint = init.hint ?? null;
    this.requestId = init.requestId ?? null;
    this.status = init.status ?? 0;
    this.retryable = init.retryable ?? false;
    this.details = init.details ?? {};
    this.body = init.body ?? null;
  }

  /** 网络中断 / 无法连接（可自动重试） */
  get isNetwork(): boolean {
    return this.code === 'NETWORK_OFFLINE';
  }

  get isUnauthenticated(): boolean {
    return (
      this.code === 'UNAUTHENTICATED' ||
      this.code === 'SESSION_EXPIRED' ||
      this.code === 'TOKEN_INVALID'
    );
  }

  get isForbidden(): boolean {
    return (
      this.code === 'PASSWORD_CHANGE_REQUIRED' ||
      this.code === 'CSRF_INVALID' ||
      this.code === 'TOKEN_SCOPE_DENIED' ||
      this.status === 403
    );
  }

  get isMaintenance(): boolean {
    return this.code === 'MAINTENANCE';
  }

  get isNotFound(): boolean {
    return this.code === 'NOT_FOUND' || this.status === 404;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** 供 UI 展示：统一的错误文案与 hint */
export function describeError(error: unknown): {
  code: string;
  message: string;
  hint: string | null;
  requestId: string | null;
  retryable: boolean;
} {
  if (error instanceof ApiError) {
    return {
      code: error.code,
      message: error.message,
      hint: error.hint,
      requestId: error.requestId,
      retryable: error.retryable || error.isNetwork,
    };
  }
  if (error instanceof Error) {
    return {
      code: 'UNKNOWN',
      message: error.message,
      hint: null,
      requestId: null,
      retryable: false,
    };
  }
  return { code: 'UNKNOWN', message: '未知错误', hint: null, requestId: null, retryable: false };
}

/* ── cookie / request id ─────────────────────────────── */

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const raw = document.cookie;
  if (!raw) return null;
  for (const part of raw.split(';')) {
    const trimmed = part.trim();
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    if (trimmed.slice(0, eq) === name) {
      return decodeURIComponent(trimmed.slice(eq + 1));
    }
  }
  return null;
}

export function newRequestId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID().replace(/-/g, '').slice(0, 16);
  }
  return `web-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/* ── 查询串 ──────────────────────────────────────────── */

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export function buildQuery(params?: QueryParams): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/* ── 请求 ────────────────────────────────────────────── */

/** 请求体：JSON 可序列化值、数组或 FormData（api.ts 负责序列化） */
export type RequestBody =
  | Record<string, unknown>
  | readonly unknown[]
  | string
  | number
  | boolean
  | FormData
  | null;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: RequestBody;
  query?: QueryParams;
  headers?: Record<string, string>;
  /** 客户端自定义追踪 id（服务端会回显） */
  requestId?: string;
  /** 写操作幂等键（spec §9.1） */
  idempotencyKey?: string;
  signal?: AbortSignal;
  /** 401 时是否自动跳登录（登录接口自身应传 false） */
  redirectOnAuthError?: boolean;
}

export interface ApiResponse<T> {
  data: T;
  requestId: string | null;
  status: number;
}

const NO_REDIRECT_PATHS = ['/admin/auth/login', '/admin/auth/bootstrap', '/admin/auth/me'];

function shouldAutoRedirect(path: string): boolean {
  if (typeof window === 'undefined') return false;
  if (window.location.pathname.startsWith('/login')) return false;
  return !NO_REDIRECT_PATHS.some((p) => path.startsWith(p));
}

function redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  if (window.location.pathname.startsWith('/login')) return;
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

function emit(name: string, detail: unknown): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

function errorFromBody(
  body: unknown,
  status: number,
  fallbackRequestId: string,
): ApiError {
  const shape = (body as { error?: ApiErrorShape } | null)?.error ?? null;
  if (shape && typeof shape.code === 'string') {
    return new ApiError({
      code: shape.code,
      message: shape.message || '请求失败',
      hint: shape.hint ?? null,
      requestId: shape.request_id ?? fallbackRequestId,
      status,
      retryable: shape.retryable ?? false,
      details: shape.details ?? {},
      body: shape,
    });
  }
  const map: Record<number, { code: string; message: string; hint: string }> = {
    400: { code: 'VALIDATION_FAILED', message: '请求字段校验失败', hint: '检查输入后重试' },
    401: { code: 'UNAUTHENTICATED', message: '未登录或会话已过期', hint: '重新登录' },
    403: { code: 'FORBIDDEN', message: '没有该操作的权限', hint: '联系管理员确认角色' },
    404: { code: 'NOT_FOUND', message: '对象不存在', hint: '检查标识是否正确' },
    409: { code: 'CONFLICT', message: '操作与当前状态冲突', hint: '刷新后重试' },
    413: { code: 'FILE_TOO_LARGE', message: '请求体过大', hint: '压缩后重试' },
    429: { code: 'RATE_LIMITED', message: '请求过于频繁', hint: '稍后重试' },
    500: { code: 'INTERNAL', message: '平台内部错误', hint: '把 request_id 报告给管理员' },
    502: { code: 'UPSTREAM_ERROR', message: '上游返回错误', hint: '查看服务日志' },
    503: { code: 'MAINTENANCE', message: '平台暂时不可用', hint: '稍后重试' },
    504: { code: 'UPSTREAM_TIMEOUT', message: '网关超时', hint: '稍后重试' },
  };
  const known = map[status];
  return new ApiError({
    code: known?.code ?? 'HTTP_ERROR',
    message: known?.message ?? `请求失败（HTTP ${status}）`,
    hint: known?.hint ?? null,
    requestId: fallbackRequestId,
    status,
    retryable: status >= 500 || status === 429,
    body: null,
  });
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? '';
  if (response.status === 204) return null;
  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  const text = await response.text().catch(() => '');
  return text ? { raw: text } : null;
}

/**
 * 发请求并返回 {data, requestId, status}。
 * 成功（2xx）时不会抛错；非 2xx 一律抛 ApiError。
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const method = options.method ?? 'GET';
  const requestId = options.requestId ?? newRequestId();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-Request-Id': requestId,
    ...(options.headers ?? {}),
  };

  const isForm = options.body instanceof FormData;
  let body: BodyInit | undefined;
  if (options.body !== undefined && options.body !== null) {
    if (isForm) {
      body = options.body as FormData;
    } else if (typeof options.body === 'string') {
      headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
      body = options.body;
    } else {
      headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
      body = JSON.stringify(options.body);
    }
  }

  const isWrite = method !== 'GET';
  if (isWrite) {
    const csrf = readCookie('sr_csrf');
    if (csrf) headers['X-CSRF-Token'] = csrf;
    if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
  }

  const url = `/api${path.startsWith('/') ? path : `/${path}`}${buildQuery(options.query)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body,
      credentials: 'include',
      redirect: 'follow',
      cache: 'no-store',
      signal: options.signal,
    });
  } catch (cause) {
    emit(OFFLINE_EVENT, { online: false });
    throw new ApiError({
      code: 'NETWORK_OFFLINE',
      message: '连接已断开，正在重试…',
      hint: '检查网络或平台进程是否在运行',
      requestId,
      status: 0,
      retryable: true,
      details: cause instanceof Error ? { reason: cause.message } : {},
    });
  }

  emit(OFFLINE_EVENT, { online: true });

  const echoedRequestId = response.headers.get('X-Request-Id') ?? requestId;
  const payload = await parseBody(response);

  if (!response.ok) {
    const error = errorFromBody(payload, response.status, echoedRequestId);
    if (error.isMaintenance) {
      emit(MAINTENANCE_EVENT, { active: true, requestId: error.requestId });
    }
    if (
      response.status === 401 &&
      options.redirectOnAuthError !== false &&
      shouldAutoRedirect(path)
    ) {
      redirectToLogin();
    }
    throw error;
  }

  return { data: payload as T, requestId: echoedRequestId, status: response.status };
}

/** 只要 data 的便捷版本 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { data } = await apiRequest<T>(path, options);
  return data;
}

export const apiGet = <T,>(path: string, query?: QueryParams, signal?: AbortSignal): Promise<T> =>
  apiFetch<T>(path, { method: 'GET', query, signal });

export const apiPost = <T,>(
  path: string,
  body?: RequestBody,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> => apiFetch<T>(path, { ...options, method: 'POST', body });

export const apiPatch = <T,>(
  path: string,
  body?: RequestBody,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> => apiFetch<T>(path, { ...options, method: 'PATCH', body });

export const apiDelete = <T,>(
  path: string,
  options: Omit<RequestOptions, 'method'> = {},
): Promise<T> => apiFetch<T>(path, { ...options, method: 'DELETE' });

/** 分页列表查询参数（spec §9.1） */
export interface ListParams extends QueryParams {
  page?: number;
  page_size?: number;
  sort?: string;
  q?: string;
  status?: string;
}

/** 上传附件（管理端 `POST /api/admin/files`，会话 + CSRF）。
 *
 * 控制台只有会话 Cookie，没有平台令牌，因此不能走 `/api/v1/files`
 * （那条要求 `Authorization: Bearer srk_...`，会对登录用户必然 401）。
 */
export async function uploadFile(
  file: File,
  options: { requestId?: string; signal?: AbortSignal } = {},
): Promise<{
  file_id: string;
  mime: string;
  size: number;
  sha256?: string;
  expires_at?: string | null;
  download_url?: string;
}> {
  const form = new FormData();
  form.append('file', file, file.name);
  return apiFetch('/admin/files', {
    method: 'POST',
    body: form,
    requestId: options.requestId,
    signal: options.signal,
  });
}
