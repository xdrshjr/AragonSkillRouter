'use client';

/**
 * Settings + engine console (CONTRACT §5.10).
 *
 * `GET /api/admin/settings` returns the *effective* values plus engine state;
 * `PATCH` returns `{updated[], settings, engine?}` so the UI can tell "saved"
 * apart from "tried to save". Engine actions are synchronous and can take a
 * while (an isolated npm build), so the page tracks elapsed time instead of
 * pretending the click was instant.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type { ListEnvelope } from '@/lib/types';
import type { AgentHealth, AgentToolsPayload, AutoExecutePolicy } from '@/lib/agent';

export interface LoggingSettings {
  store_request_bodies: boolean;
  store_response_bodies: boolean;
}

export interface FileSettings {
  retention_days: number;
}

export interface SecuritySettings {
  session_ttl_minutes: number;
  idle_timeout_minutes: number;
  login_max_attempts: number;
}

export interface AgentModelSettings {
  provider_id?: string;
  model_id?: string;
  base_url?: string | null;
  credential_id?: string;
}

export interface AgentSettings {
  upgrade_policy: 'manual' | 'auto';
  upgrade_channel: 'stable' | 'branch';
  track_branch: string;
  auto_execute: AutoExecutePolicy;
  model: AgentModelSettings;
  model_resolved?: {
    configured: boolean;
    provider_id?: string;
    model_id?: string;
    base_url?: string | null;
    credential_id?: string;
    credential_name?: string;
    error?: { code?: string; message?: string; actions?: string[] };
  };
}

export interface EffectiveSettings {
  logging: LoggingSettings;
  files: FileSettings;
  security: SecuritySettings;
  agent: AgentSettings;
}

export interface EngineStateSummary {
  adapter_version: string;
  sidecar: { url: string; token_configured: boolean };
  tools: string[];
  release: unknown;
  candidate: unknown;
}

export interface SettingsPayload {
  settings: EffectiveSettings;
  engine: EngineStateSummary;
  editable: string[];
  upgrade: { policies: string[]; channels: string[] };
}

export interface SettingsPatch {
  logging?: Partial<LoggingSettings>;
  files?: Partial<FileSettings>;
  security?: Partial<SecuritySettings>;
  agent?: {
    upgrade_policy?: string;
    upgrade_channel?: string;
    track_branch?: string;
    auto_execute?: Partial<AutoExecutePolicy>;
    /** An empty string clears a field so the platform falls back to detection. */
    model?: Partial<AgentModelSettings>;
  };
}

export interface SettingsPatchResult {
  updated: string[];
  settings: EffectiveSettings;
  engine?: EngineStateSummary;
}

export interface EngineCheck {
  name: string;
  status: 'passed' | 'failed' | 'skipped' | string;
  detail: string;
  [key: string]: unknown;
}

export interface EngineRelease {
  id: string;
  channel: string;
  ref: string;
  version: string | null;
  commit_sha: string | null;
  npm_version: string | null;
  status: 'detected' | 'validating' | 'verified' | 'failed' | 'pending_approval' | 'active' | 'rolled_back' | 'superseded';
  adapter_version: string;
  capabilities: Record<string, unknown>;
  checks: EngineCheck[];
  dependency_fingerprint: Record<string, unknown>;
  previous_release_id: string | null;
  activated_at: string | null;
  verified_at: string | null;
  created_at: string;
  notes: string | null;
}

export interface CapabilityMatrixPayload {
  adapter_version: string;
  sidecar_protocol: number;
  supported_core: { min: string; max_exclusive: string };
  cli_min: string;
  event_union: string[];
  endpoints: string[];
  platform_tools: number;
  capabilities_by_status: Record<string, number>;
}

export interface EngineOverview {
  current: EngineRelease | null;
  candidate: EngineRelease | null;
  history: EngineRelease[];
  history_total: number;
  capability_matrix: CapabilityMatrixPayload;
  policy: { upgrade_policy: string; upgrade_channel: string; track_branch: string };
  aligned: { commit: string | null; core_version: string | null; cli_version: string | null };
  npm: { core: string; cli: string };
  sidecar: { url: string; token_configured: boolean };
}

export interface EngineCheckResult {
  release_id?: string;
  status?: string;
  release?: EngineRelease;
  candidate?: EngineRelease | null;
  checks?: EngineCheck[];
  skipped_checks?: EngineCheck[];
  note?: string;
  next_step?: string;
  activated?: boolean;
  rolled_back?: boolean;
  sidecar?: { running_version: string | null; matches_release: boolean };
  warning?: string | null;
  [key: string]: unknown;
}

export const settingsEndpoints = {
  settings: '/api/admin/settings',
  engine: '/api/admin/engine',
  engineReleases: '/api/admin/engine/releases',
  engineCheck: '/api/admin/engine/check',
  engineValidate: (id: string) => `/api/admin/engine/releases/${encodeURIComponent(id)}/validate`,
  engineActivate: (id: string) => `/api/admin/engine/releases/${encodeURIComponent(id)}/activate`,
  engineRollback: '/api/admin/engine/rollback',
} as const;

export const settingsKeys = {
  settings: ['settings'] as const,
  engine: ['engine'] as const,
  releases: ['engine-releases'] as const,
  agentHealth: ['agent-health'] as const,
  agentTools: ['agent-tools'] as const,
};

export function useSettingsQuery() {
  return useQuery<SettingsPayload, Error>({
    queryKey: settingsKeys.settings,
    queryFn: ({ signal }) => api.get<SettingsPayload>(settingsEndpoints.settings, { signal }),
  });
}

export function useUpdateSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation<SettingsPatchResult, Error, SettingsPatch>({
    mutationFn: (body) => api.patch<SettingsPatchResult>(settingsEndpoints.settings, body),
    onSuccess: (result) => {
      queryClient.setQueryData<SettingsPayload | undefined>(settingsKeys.settings, (current) =>
        current ? { ...current, settings: result.settings, engine: result.engine ?? current.engine } : current,
      );
    },
  });
}

export function useEngineQuery() {
  return useQuery<EngineOverview, Error>({
    queryKey: settingsKeys.engine,
    queryFn: ({ signal }) => api.get<EngineOverview>(settingsEndpoints.engine, { signal }),
    staleTime: 10_000,
  });
}

export function useReleasesQuery() {
  return useQuery<ListEnvelope<EngineRelease>, Error>({
    queryKey: settingsKeys.releases,
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<EngineRelease>>(settingsEndpoints.engineReleases, { query: { limit: 50 }, signal }),
  });
}

export function useAgentToolsQuery() {
  return useQuery<AgentToolsPayload, Error>({
    queryKey: settingsKeys.agentTools,
    queryFn: ({ signal }) => api.get<AgentToolsPayload>('/api/admin/agent/tools', { signal }),
  });
}

export function useEngineHealthQuery() {
  return useQuery<AgentHealth, Error>({
    queryKey: settingsKeys.agentHealth,
    queryFn: ({ signal }) => api.get<AgentHealth>('/api/admin/agent/health', { signal }),
    staleTime: 15_000,
  });
}

function useInvalidateEngine() {
  const queryClient = useQueryClient();
  return (result?: EngineCheckResult) => {
    void queryClient.invalidateQueries({ queryKey: settingsKeys.engine });
    void queryClient.invalidateQueries({ queryKey: settingsKeys.releases });
    void queryClient.invalidateQueries({ queryKey: settingsKeys.settings });
    void queryClient.invalidateQueries({ queryKey: settingsKeys.agentHealth });
    if (result) queryClient.setQueryData(['engine-last-result'], result);
  };
}

export function useEngineCheckMutation() {
  const invalidate = useInvalidateEngine();
  return useMutation<EngineCheckResult, Error, void>({
    mutationFn: () => api.post<EngineCheckResult>(settingsEndpoints.engineCheck),
    onSettled: (result) => invalidate(result),
  });
}

export function useEngineValidateMutation() {
  const invalidate = useInvalidateEngine();
  return useMutation<EngineCheckResult, Error, string>({
    mutationFn: (releaseId) => api.post<EngineCheckResult>(settingsEndpoints.engineValidate(releaseId)),
    onSettled: (result, _error, releaseId) => invalidate(result ?? { release_id: releaseId }),
  });
}

export function useEngineActivateMutation() {
  const invalidate = useInvalidateEngine();
  return useMutation<EngineCheckResult, Error, string>({
    mutationFn: (releaseId) => api.post<EngineCheckResult>(settingsEndpoints.engineActivate(releaseId)),
    onSettled: (result, _error, releaseId) => invalidate(result ?? { release_id: releaseId }),
  });
}

export function useEngineRollbackMutation() {
  const invalidate = useInvalidateEngine();
  return useMutation<EngineCheckResult, Error, void>({
    mutationFn: () => api.post<EngineCheckResult>(settingsEndpoints.engineRollback),
    onSettled: (result) => invalidate(result),
  });
}
