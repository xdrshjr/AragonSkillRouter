'use client';

/**
 * Access surface: agents, agent tokens and budget policies (CONTRACT §5.8), plus
 * the live limit counters from `/api/admin/usage/limits`.
 *
 * The only response that ever contains a token is `POST /agents/{id}/tokens`; the
 * UI shows it once in a copy-once dialog and never stores it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type { ListEnvelope } from '@/lib/types';

export interface Agent {
  id: string;
  name: string;
  slug: string;
  description: string;
  status: 'active' | 'disabled';
  created_at: string;
  updated_at: string;
  revoked_at: string | null;
  counts: { tokens: number; active_tokens: number };
}

export interface AgentToken {
  id: string;
  agent_id: string;
  agent_name: string | null;
  name: string;
  token_prefix: string;
  scopes: string[];
  status: 'active' | 'revoked' | 'expired';
  expires_at: string | null;
  days_until_expiry: number | null;
  expiring_soon: boolean;
  rate_limit_per_min: number | null;
  max_concurrency: number | null;
  budget_usd: number | null;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface CreatedToken extends AgentToken {
  /** Only present on the create response. */
  token?: string;
  shown_once?: boolean;
  warning?: string;
  already_revoked?: boolean;
  revoked?: boolean;
  effective?: string;
  token_record?: AgentToken;
}

export interface TokenCreateInput {
  name: string;
  scopes: string[];
  expires_in_days?: number | null;
  rate_limit_per_min?: number | null;
  max_concurrency?: number | null;
  budget_usd?: number | null;
}

export type BudgetScope = 'global' | 'provider' | 'capability' | 'agent';
export type BudgetWindow = 'minute' | 'day' | 'month';

export interface BudgetPolicy {
  id: string;
  scope_type: BudgetScope;
  scope_id: string | null;
  window: BudgetWindow;
  max_calls: number | null;
  max_cost_usd: number | null;
  rate_per_min: number | null;
  max_concurrency: number | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface BudgetInput {
  scope_type: BudgetScope;
  scope_id?: string | null;
  window: BudgetWindow;
  max_calls?: number | null;
  max_cost_usd?: number | null;
  rate_per_min?: number | null;
  max_concurrency?: number | null;
  enabled?: boolean;
}

export interface AgentLimitCounters {
  agent: { id: string; name: string; status: string };
  agent_id?: string;
  calls_last_minute?: number;
  concurrency?: number;
  limits: {
    rate_limit_per_min: number | null;
    max_concurrency: number | null;
    budget_usd: number | null;
  };
}

export interface UsageLimits {
  global: { running_invocations?: number; calls_last_minute?: number; concurrency?: number };
  agents: AgentLimitCounters[];
  policies: BudgetPolicy[];
  generated_at: string;
}

export const accessEndpoints = {
  agents: '/api/admin/agents',
  agent: (id: string) => `/api/admin/agents/${encodeURIComponent(id)}`,
  agentTokens: (id: string) => `/api/admin/agents/${encodeURIComponent(id)}/tokens`,
  tokens: '/api/admin/tokens',
  revokeToken: (id: string) => `/api/admin/tokens/${encodeURIComponent(id)}/revoke`,
  budgets: '/api/admin/budgets',
  budget: (id: string) => `/api/admin/budgets/${encodeURIComponent(id)}`,
  usageLimits: '/api/admin/usage/limits',
} as const;

export const accessKeys = {
  agents: ['agents'] as const,
  tokens: (agentId: string) => ['agent-tokens', agentId] as const,
  allTokens: ['tokens'] as const,
  budgets: ['budgets'] as const,
  limits: ['usage-limits'] as const,
};

export function useAgentsQuery() {
  return useQuery<ListEnvelope<Agent>, Error>({
    queryKey: accessKeys.agents,
    queryFn: ({ signal }) => api.get<ListEnvelope<Agent>>(accessEndpoints.agents, { query: { limit: 100 }, signal }),
  });
}

export function useCreateAgentMutation() {
  const queryClient = useQueryClient();
  return useMutation<Agent, Error, { name: string; description: string }>({
    mutationFn: (body) => api.post<Agent>(accessEndpoints.agents, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.agents });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
    },
  });
}

export function useUpdateAgentMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Agent, Error, { name?: string; description?: string; status?: 'active' | 'disabled' }>({
    mutationFn: (body) => api.patch<Agent>(accessEndpoints.agent(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.agents });
      void queryClient.invalidateQueries({ queryKey: accessKeys.tokens(id) });
    },
  });
}

export function useDeleteAgentMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<{ ok: boolean; id: string; revoked_tokens: number; status: string }, Error, void>({
    mutationFn: () => api.del(accessEndpoints.agent(id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.agents });
      void queryClient.invalidateQueries({ queryKey: accessKeys.allTokens });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
    },
  });
}

export function useAgentTokensQuery(agentId: string | undefined) {
  return useQuery<ListEnvelope<AgentToken>, Error>({
    queryKey: accessKeys.tokens(agentId ?? 'missing'),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<AgentToken>>(accessEndpoints.agentTokens(agentId as string), {
        query: { limit: 100 },
        signal,
      }),
    enabled: Boolean(agentId),
  });
}

export function useCreateTokenMutation(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation<CreatedToken, Error, TokenCreateInput>({
    mutationFn: (body) => api.post<CreatedToken>(accessEndpoints.agentTokens(agentId), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.tokens(agentId) });
      void queryClient.invalidateQueries({ queryKey: accessKeys.agents });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
    },
  });
}

export function useRevokeTokenMutation(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation<CreatedToken, Error, string>({
    mutationFn: (tokenId) => api.post<CreatedToken>(accessEndpoints.revokeToken(tokenId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.tokens(agentId) });
      void queryClient.invalidateQueries({ queryKey: accessKeys.agents });
      void queryClient.invalidateQueries({ queryKey: accessKeys.allTokens });
    },
  });
}

export function useBudgetsQuery() {
  return useQuery<ListEnvelope<BudgetPolicy>, Error>({
    queryKey: accessKeys.budgets,
    queryFn: ({ signal }) => api.get<ListEnvelope<BudgetPolicy>>(accessEndpoints.budgets, { query: { limit: 100 }, signal }),
  });
}

export function useCreateBudgetMutation() {
  const queryClient = useQueryClient();
  return useMutation<BudgetPolicy, Error, BudgetInput>({
    mutationFn: (body) => api.post<BudgetPolicy>(accessEndpoints.budgets, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.budgets });
      void queryClient.invalidateQueries({ queryKey: accessKeys.limits });
    },
  });
}

export function useUpdateBudgetMutation() {
  const queryClient = useQueryClient();
  return useMutation<BudgetPolicy, Error, { id: string; body: Partial<BudgetInput> }>({
    mutationFn: ({ id, body }) => api.patch<BudgetPolicy>(accessEndpoints.budget(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.budgets });
      void queryClient.invalidateQueries({ queryKey: accessKeys.limits });
    },
  });
}

export function useDeleteBudgetMutation() {
  const queryClient = useQueryClient();
  return useMutation<{ ok: boolean; id: string }, Error, string>({
    mutationFn: (id) => api.del<{ ok: boolean; id: string }>(accessEndpoints.budget(id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: accessKeys.budgets });
      void queryClient.invalidateQueries({ queryKey: accessKeys.limits });
    },
  });
}

export function useUsageLimitsQuery() {
  return useQuery<UsageLimits, Error>({
    queryKey: accessKeys.limits,
    queryFn: ({ signal }) => api.get<UsageLimits>(accessEndpoints.usageLimits, { signal }),
    staleTime: 15_000,
  });
}
