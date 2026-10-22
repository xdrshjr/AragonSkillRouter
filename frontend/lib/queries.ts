'use client';

/**
 * React Query hooks for every endpoint the frontend actually uses.
 * Keys are centralised so a mutation can invalidate exactly what went stale.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from '@tanstack/react-query';

import { api, endpoints, type QueryParams } from '@/lib/api';
import type {
  CapabilityConfig,
  CapabilityDetail,
  CapabilityFilters,
  CapabilityInput,
  CapabilityTestRow,
  CapabilityVersion,
  Credential,
  CredentialDeleteResult,
  CredentialImpact,
  CredentialInput,
  CredentialUpdateInput,
  CredentialVerifyResult,
  DiffResponse,
  HealthCheckResult,
  InvocationMode,
  InvokeResult,
  ListEnvelope,
  LoginResponse,
  OverviewPayload,
  Problem,
  Provider,
  ProviderDetail,
  ProviderFilters,
  ProviderInput,
  Route,
  RouteInput,
  RoutePreview,
  SessionPayload,
  TestVersionResponse,
  ValidateResponse,
} from '@/lib/types';

export interface CredentialFilters {
  provider_id?: string;
  status?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export const queryKeys = {
  session: ['session'] as const,
  bootstrapStatus: ['bootstrap-status'] as const,
  overview: ['overview'] as const,
  overviewProblems: (severity?: string) => ['overview', 'problems', severity ?? 'all'] as const,
  providers: (filters: ProviderFilters) => ['providers', filters] as const,
  provider: (id: string) => ['provider', id] as const,
  credentials: (filters: CredentialFilters) => ['credentials', filters] as const,
  credential: (id: string) => ['credential', id] as const,
  credentialImpact: (id: string) => ['credential', id, 'impact'] as const,
  capabilities: (filters: CapabilityFilters) => ['capabilities', filters] as const,
  capability: (id: string) => ['capability', id] as const,
  versions: (id: string) => ['capability', id, 'versions'] as const,
  version: (id: string, versionId: string) => ['capability', id, 'version', versionId] as const,
  tests: (id: string) => ['capability', id, 'tests'] as const,
  routes: (id: string) => ['capability', id, 'routes'] as const,
  diff: (id: string, from: string, to: string) => ['capability', id, 'diff', from, to] as const,
};

function cleanParams(params: Record<string, unknown>): QueryParams {
  const out: QueryParams = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') out[key] = value;
  }
  return out;
}

/* ------------------------------------------------------------------ session --- */

export function useSessionQuery(
  options?: Partial<UseQueryOptions<SessionPayload, Error, SessionPayload, readonly unknown[]>>,
) {
  return useQuery<SessionPayload, Error>({
    queryKey: queryKeys.session,
    queryFn: ({ signal }) => api.get<SessionPayload>(endpoints.auth.session, { skipAuthRedirect: true, signal }),
    retry: false,
    staleTime: 30_000,
    ...options,
  });
}

export function useLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation<LoginResponse, Error, { username: string; password: string }>({
    mutationFn: (body) => api.post<LoginResponse>(endpoints.auth.login, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  return useMutation<{ ok: boolean }, Error, void>({
    mutationFn: () => api.post<{ ok: boolean }>(endpoints.auth.logout),
    onSuccess: () => queryClient.clear(),
  });
}

export function useChangePasswordMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    { ok: boolean; csrf_token: string; must_change_password: boolean },
    Error,
    { current_password: string; new_password: string }
  >({
    mutationFn: (body) => api.post(endpoints.auth.changePassword, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session });
    },
  });
}

/* ----------------------------------------------------------------- overview --- */

export function useOverviewQuery() {
  return useQuery<OverviewPayload, Error>({
    queryKey: queryKeys.overview,
    queryFn: ({ signal }) => api.get<OverviewPayload>(endpoints.overview, { signal }),
  });
}

export function useOverviewProblemsQuery(severity?: string, limit = 50) {
  return useQuery<ListEnvelope<Problem>, Error>({
    queryKey: queryKeys.overviewProblems(severity),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<Problem>>(endpoints.overviewProblems, {
        query: cleanParams({ severity, limit }),
        signal,
      }),
  });
}

/* ---------------------------------------------------------------- providers --- */

export function useProvidersQuery(filters: ProviderFilters) {
  return useQuery<ListEnvelope<Provider>, Error>({
    queryKey: queryKeys.providers(filters),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<Provider>>(endpoints.providers, {
        query: cleanParams({ ...filters }),
        signal,
      }),
  });
}

export function useProviderQuery(id: string | undefined) {
  return useQuery<ProviderDetail, Error>({
    queryKey: queryKeys.provider(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<ProviderDetail>(endpoints.provider(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useCreateProviderMutation() {
  const queryClient = useQueryClient();
  return useMutation<ProviderDetail, Error, ProviderInput>({
    mutationFn: (body) => api.post<ProviderDetail>(endpoints.providers, body),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ['providers'] });
      queryClient.setQueryData(queryKeys.provider(created.id), created);
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useUpdateProviderMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<ProviderDetail, Error, Partial<ProviderInput>>({
    mutationFn: (body) => api.patch<ProviderDetail>(endpoints.provider(id), body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.provider(id), updated);
      void queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
}

export function useDeleteProviderMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<{ deleted: boolean; id: string; slug: string; counts: Record<string, number> }, Error, void>({
    mutationFn: () => api.del(endpoints.provider(id)),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.provider(id) });
      void queryClient.invalidateQueries({ queryKey: ['providers'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

/* -------------------------------------------------------------- credentials --- */

export function useCredentialsQuery(filters: CredentialFilters, enabled = true) {
  return useQuery<ListEnvelope<Credential>, Error>({
    queryKey: queryKeys.credentials(filters),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<Credential>>(endpoints.credentials, {
        query: cleanParams({ ...filters }),
        signal,
      }),
    enabled,
  });
}

export function useCredentialQuery(id: string | undefined) {
  return useQuery<Credential & { impact: CredentialImpact }, Error>({
    queryKey: queryKeys.credential(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<Credential & { impact: CredentialImpact }>(endpoints.credential(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useCreateCredentialMutation() {
  const queryClient = useQueryClient();
  return useMutation<Credential, Error, CredentialInput & { provider_id: string }>({
    mutationFn: (body) => api.post<Credential>(endpoints.credentials, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
      void queryClient.invalidateQueries({ queryKey: ['provider'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useUpdateCredentialMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Credential, Error, CredentialUpdateInput>({
    mutationFn: (body) => api.patch<Credential>(endpoints.credential(id), body),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
      void queryClient.setQueryData(queryKeys.credential(id), updated);
      void queryClient.invalidateQueries({ queryKey: ['provider'] });
    },
  });
}

export function useDeleteCredentialMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<CredentialDeleteResult, Error, { force?: boolean }>({
    mutationFn: ({ force }) =>
      api.del<CredentialDeleteResult>(endpoints.credential(id), { query: { force: force ? 'true' : undefined } }),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.credential(id) });
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
      void queryClient.invalidateQueries({ queryKey: ['provider'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useVerifyCredentialMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<CredentialVerifyResult, Error, { probe_url?: string; method?: 'GET' | 'HEAD' | 'POST' }>({
    mutationFn: (body) => api.post<CredentialVerifyResult>(endpoints.credentialVerify(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.credential(id) });
      void queryClient.invalidateQueries({ queryKey: ['provider'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

/* ------------------------------------------------------------- capabilities --- */

export function useCapabilitiesQuery(filters: CapabilityFilters) {
  return useQuery<ListEnvelope<CapabilityDetail>, Error>({
    queryKey: queryKeys.capabilities(filters),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<CapabilityDetail>>(endpoints.capabilities, {
        query: cleanParams({ ...filters }),
        signal,
      }),
  });
}

export function useCapabilityQuery(id: string | undefined) {
  return useQuery<CapabilityDetail, Error>({
    queryKey: queryKeys.capability(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<CapabilityDetail>(endpoints.capability(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useCreateCapabilityMutation() {
  const queryClient = useQueryClient();
  return useMutation<CapabilityDetail, Error, CapabilityInput>({
    mutationFn: (body) => api.post<CapabilityDetail>(endpoints.capabilities, body),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
      queryClient.setQueryData(queryKeys.capability(created.id), created);
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useUpdateCapabilityMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<
    CapabilityDetail,
    Error,
    Partial<CapabilityInput> & { credential_id?: string | null; provider_id?: string | null; needs_live_agent?: boolean }
  >({
    mutationFn: (body) => api.patch<CapabilityDetail>(endpoints.capability(id), body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.capability(id), updated);
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
    },
  });
}

export function useVersionsQuery(id: string | undefined) {
  return useQuery<ListEnvelope<CapabilityVersion>, Error>({
    queryKey: queryKeys.versions(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<ListEnvelope<CapabilityVersion>>(endpoints.capabilityVersions(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useVersionQuery(id: string | undefined, versionId: string | undefined) {
  return useQuery<CapabilityVersion, Error>({
    queryKey: queryKeys.version(id ?? 'missing', versionId ?? 'missing'),
    queryFn: ({ signal }) =>
      api.get<CapabilityVersion>(endpoints.capabilityVersion(id as string, versionId as string), { signal }),
    enabled: Boolean(id && versionId),
  });
}

export function useUpdateVersionMutation(id: string, versionId: string) {
  const queryClient = useQueryClient();
  return useMutation<CapabilityVersion, Error, { config?: CapabilityConfig; notes?: string | null }>({
    mutationFn: (body) => api.patch<CapabilityVersion>(endpoints.capabilityVersion(id, versionId), body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.version(id, versionId), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
    },
  });
}

export function useCreateVersionMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<CapabilityVersion, Error, { from_version_id?: string; notes?: string }>({
    mutationFn: (body) => api.post<CapabilityVersion>(endpoints.capabilityVersions(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
    },
  });
}

export function useValidateVersionMutation(id: string, versionId: string) {
  const queryClient = useQueryClient();
  return useMutation<ValidateResponse, Error, void>({
    mutationFn: () => api.post<ValidateResponse>(endpoints.capabilityValidate(id, versionId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.version(id, versionId) });
    },
  });
}

export function useTestVersionMutation(id: string, versionId: string) {
  const queryClient = useQueryClient();
  return useMutation<TestVersionResponse, Error, { input: Record<string, unknown>; mode: InvocationMode }>({
    mutationFn: (body) => api.post<TestVersionResponse>(endpoints.capabilityTest(id, versionId), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.tests(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.version(id, versionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function usePublishVersionMutation(id: string, versionId: string) {
  const queryClient = useQueryClient();
  return useMutation<CapabilityDetail, Error, void>({
    mutationFn: () => api.post<CapabilityDetail>(endpoints.capabilityPublish(id, versionId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useDeprecateVersionMutation(id: string, versionId: string) {
  const queryClient = useQueryClient();
  return useMutation<CapabilityDetail, Error, void>({
    mutationFn: () => api.post<CapabilityDetail>(endpoints.capabilityDeprecate(id, versionId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
    },
  });
}

export function useRollbackMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<CapabilityDetail, Error, { version_id: string; reason?: string }>({
    mutationFn: (body) => api.post<CapabilityDetail>(endpoints.capabilityRollback(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.versions(id) });
      void queryClient.invalidateQueries({ queryKey: ['capabilities'] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

export function useTestsQuery(id: string | undefined) {
  return useQuery<ListEnvelope<CapabilityTestRow>, Error>({
    queryKey: queryKeys.tests(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<ListEnvelope<CapabilityTestRow>>(endpoints.capabilityTests(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useDiffQuery(id: string | undefined, from: string | undefined, to: string | undefined) {
  return useQuery<DiffResponse, Error>({
    queryKey: queryKeys.diff(id ?? 'missing', from ?? '-', to ?? '-'),
    queryFn: ({ signal }) =>
      api.get<DiffResponse>(endpoints.capabilityVersionDiff(id as string, from as string, to as string), { signal }),
    enabled: Boolean(id && from && to && from !== to),
  });
}

export function useInvokeMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<
    InvokeResult,
    Error,
    { input: Record<string, unknown>; mode?: InvocationMode; dry_run?: boolean; version_id?: string }
  >({
    mutationFn: (body) => api.post<InvokeResult>(endpoints.capabilityInvoke(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

/* ------------------------------------------------------------------- routes --- */

export function useRoutesQuery(id: string | undefined) {
  return useQuery<ListEnvelope<Route>, Error>({
    queryKey: queryKeys.routes(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<ListEnvelope<Route>>(endpoints.capabilityRoutes(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useCreateRouteMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Route, Error, RouteInput>({
    mutationFn: (body) => api.post<Route>(endpoints.capabilityRoutes(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routes(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
    },
  });
}

export function useUpdateRouteMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Route, Error, { routeId: string; body: Partial<RouteInput> }>({
    mutationFn: ({ routeId, body }) => api.patch<Route>(endpoints.route(routeId), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routes(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
    },
  });
}

export function useDeleteRouteMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<{ deleted: boolean }, Error, string>({
    mutationFn: (routeId) => api.del<{ deleted: boolean }>(endpoints.route(routeId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routes(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
    },
  });
}

export function useHealthCheckMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<HealthCheckResult, Error, string>({
    mutationFn: (routeId) => api.post<HealthCheckResult>(endpoints.routeHealthCheck(routeId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routes(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.capability(id) });
    },
  });
}

export function useRoutePreviewMutation(id: string) {
  return useMutation<RoutePreview, Error, { input: Record<string, unknown>; mode: InvocationMode; provider_id?: string }>({
    mutationFn: (body) => api.post<RoutePreview>(endpoints.capabilityRoutePreview(id), body),
  });
}
