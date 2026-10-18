/**
 * TanStack Query 客户端 + 统一 key 工厂。
 * 服务端状态一律走 Query，不把接口数据塞进组件本地 state。
 */

import { QueryClient } from '@tanstack/react-query';
import type { ListParams } from './api';
import type { PageSize, SortDir } from './types';

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error: unknown) => {
          const retryable =
            typeof error === 'object' &&
            error !== null &&
            'retryable' in error &&
            (error as { retryable?: boolean }).retryable === true;
          const code =
            typeof error === 'object' && error !== null && 'code' in error
              ? String((error as { code?: string }).code)
              : '';
          if (code === 'UNAUTHENTICATED' || code === 'SESSION_EXPIRED' || code === 'NOT_FOUND') {
            return false;
          }
          return retryable && failureCount < 2;
        },
        retryDelay: (n) => Math.min(8_000, 800 * 2 ** n),
      },
      mutations: {
        retry: false,
      },
    },
  });
}

export interface ListKeyParams extends ListParams {
  sort?: string;
  dir?: SortDir;
  page_size?: PageSize;
}

/** key 工厂：所有 list key 都包含查询参数，便于精确失效 */
export const queryKeys = {
  auth: {
    all: ['auth'] as const,
    bootstrap: () => ['auth', 'bootstrap'] as const,
    me: () => ['auth', 'me'] as const,
  },
  overview: () => ['overview'] as const,
  providers: {
    all: ['providers'] as const,
    list: (params: ListKeyParams) => ['providers', 'list', params] as const,
    detail: (id: string) => ['providers', 'detail', id] as const,
    options: () => ['providers', 'options'] as const,
    credentials: (providerId: string, params?: ListKeyParams) =>
      ['providers', providerId, 'credentials', params ?? {}] as const,
  },
  credentials: {
    all: ['credentials'] as const,
    detail: (id: string) => ['credentials', 'detail', id] as const,
    verifications: (id: string) => ['credentials', id, 'verifications'] as const,
    impact: (id: string) => ['credentials', id, 'impact'] as const,
  },
  capabilities: {
    all: ['capabilities'] as const,
    list: (params: ListKeyParams) => ['capabilities', 'list', params] as const,
    detail: (id: string) => ['capabilities', 'detail', id] as const,
    routes: (id: string) => ['capabilities', id, 'routes'] as const,
    versions: (id: string) => ['capabilities', id, 'versions'] as const,
    version: (id: string, v: string) => ['capabilities', id, 'versions', v] as const,
    diff: (id: string, from: string, to: string) =>
      ['capabilities', id, 'diff', from, to] as const,
    impact: (id: string) => ['capabilities', id, 'impact'] as const,
  },
  skills: {
    all: ['skills'] as const,
    list: (params: ListKeyParams) => ['skills', 'list', params] as const,
    detail: (id: string) => ['skills', 'detail', id] as const,
    versions: (id: string) => ['skills', id, 'versions'] as const,
    dependencies: (id: string) => ['skills', id, 'dependencies'] as const,
    preview: (id: string) => ['skills', id, 'preview'] as const,
    importPreview: (token: string) => ['skills', 'import', token] as const,
  },
  access: {
    agents: (params: ListKeyParams) => ['access', 'agents', params] as const,
    agent: (id: string) => ['access', 'agents', id] as const,
    tokens: (params?: ListKeyParams) => ['access', 'tokens', params ?? {}] as const,
  },
  calls: {
    list: (params: ListKeyParams) => ['calls', 'list', params] as const,
    detail: (requestId: string) => ['calls', 'detail', requestId] as const,
  },
  tasks: {
    list: (params: ListKeyParams) => ['tasks', 'list', params] as const,
    detail: (id: string) => ['tasks', 'detail', id] as const,
  },
  agent: {
    sessions: () => ['agent', 'sessions'] as const,
    session: (id: string) => ['agent', 'session', id] as const,
    flows: (params?: ListKeyParams) => ['agent', 'flows', params ?? {}] as const,
    flow: (id: string) => ['agent', 'flow', id] as const,
    approvals: () => ['agent', 'approvals'] as const,
    config: () => ['agent', 'config'] as const,
  },
  engine: {
    status: () => ['engine', 'status'] as const,
    candidates: () => ['engine', 'candidates'] as const,
    runs: () => ['engine', 'runs'] as const,
    run: (id: string) => ['engine', 'runs', id] as const,
    capabilities: () => ['engine', 'capabilities'] as const,
  },
  settings: {
    detail: () => ['settings'] as const,
    audit: (params: ListKeyParams) => ['settings', 'audit', params] as const,
  },
} as const;
