'use client';

/**
 * Records (CONTRACT §5.6): invocations and async tasks.
 *
 * Read-only except cancel/reconcile, which return a *truthful* answer. The UI
 * therefore renders `supported`, `canceled`, `cancel_requested` and
 * `upstream_may_continue` separately instead of collapsing them into "cancelled".
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type { Invocation, InvocationStatus, ListEnvelope } from '@/lib/types';

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancel_requested'
  | 'cancelled'
  | 'uncertain';

export interface InvocationDetail extends Invocation {
  route_id: string | null;
  route_reason: string | null;
  idempotency_key: string | null;
  request_digest: string | null;
  request_preview: unknown;
  response_preview: unknown;
}

export interface TaskArtifact {
  id: string;
  filename: string | null;
  content_type: string | null;
  size: number | null;
  purpose?: string | null;
  sha256?: string | null;
  invocation_id?: string | null;
  created_at?: string | null;
  url: string;
}

export interface Task {
  id: string;
  request_id: string | null;
  kind: string;
  status: TaskStatus;
  progress: number;
  progress_note: string | null;
  upstream_task_id: string | null;
  attempts: number;
  max_attempts: number;
  capability: { id: string; slug: string | null; name: string | null } | null;
  provider: { id: string | null; name: string | null } | null;
  agent: { id: string | null; name: string | null } | null;
  route_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  deadline: string | null;
  poll_after: string | null;
  result_summary: string;
  artifacts: TaskArtifact[];
  error: { code?: string; message?: string } | null;
  budget_usd: number | null;
  cost: { actual_usd: number | null; currency: string } | null;
  /** Only on the detail response. */
  result?: unknown;
  result_document?: unknown;
  events?: TaskEvent[];
}

export interface TaskEvent {
  at: string | null;
  type: string;
  status: string;
  title: string;
  detail: string | null;
  source: string;
  invocation_id?: string;
}

export interface CancelResult {
  task: Task;
  task_id: string;
  status: string;
  supported: boolean;
  canceled: boolean;
  cancel_requested: boolean;
  upstream_may_continue: boolean;
  message: string;
}

export interface InvocationFilters {
  search?: string;
  status?: string;
  capability?: string;
  agent?: string;
  surface?: string;
  limit?: number;
  offset?: number;
}

export interface TaskFilters {
  search?: string;
  status?: string;
  capability?: string;
  agent?: string;
  limit?: number;
  offset?: number;
}

export const recordEndpoints = {
  invocations: '/api/admin/invocations',
  invocation: (id: string) => `/api/admin/invocations/${encodeURIComponent(id)}`,
  tasks: '/api/admin/tasks',
  task: (id: string) => `/api/admin/tasks/${encodeURIComponent(id)}`,
  taskCancel: (id: string) => `/api/admin/tasks/${encodeURIComponent(id)}/cancel`,
  taskReconcile: (id: string) => `/api/admin/tasks/${encodeURIComponent(id)}/reconcile`,
} as const;

export const INVOCATION_STATUSES: InvocationStatus[] = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancelled',
  'uncertain',
  'rejected',
];

export const TASK_STATUSES: TaskStatus[] = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancel_requested',
  'cancelled',
  'uncertain',
];

function clean(params: Record<string, unknown>): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'string' || typeof value === 'number') out[key] = value;
  }
  return out;
}

export function useInvocationsQuery(filters: InvocationFilters) {
  return useQuery<ListEnvelope<Invocation>, Error>({
    queryKey: ['invocations', filters],
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<Invocation>>(recordEndpoints.invocations, { query: clean({ ...filters }), signal }),
  });
}

export function useTasksQuery(filters: TaskFilters) {
  return useQuery<ListEnvelope<Task>, Error>({
    queryKey: ['tasks', filters],
    queryFn: ({ signal }) => api.get<ListEnvelope<Task>>(recordEndpoints.tasks, { query: clean({ ...filters }), signal }),
  });
}

export function useInvocationQuery(id: string | undefined) {
  return useQuery<InvocationDetail, Error>({
    queryKey: ['invocation', id],
    queryFn: ({ signal }) => api.get<InvocationDetail>(recordEndpoints.invocation(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useTaskQuery(id: string | undefined) {
  return useQuery<Task, Error>({
    queryKey: ['task', id],
    queryFn: ({ signal }) => api.get<Task>(recordEndpoints.task(id as string), { signal }),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'queued' || status === 'running' || status === 'cancel_requested' ? 5000 : false;
    },
  });
}

export function useCancelTaskMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<CancelResult, Error, { reason?: string }>({
    mutationFn: (body) => api.post<CancelResult>(recordEndpoints.taskCancel(id), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['task', id] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });
}

export function useReconcileTaskMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Task, Error, void>({
    mutationFn: () => api.post<Task>(recordEndpoints.taskReconcile(id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['task', id] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });
}

/**
 * `/records/[id]` renders either record kind. Invocations answer for an id **or**
 * a request id, so a 404 from the invocation route is the only signal that the id
 * belongs to a task — the resolver reports that honestly instead of guessing.
 */
export type RecordResolution =
  | { kind: 'invocation'; invocation: InvocationDetail }
  | { kind: 'task'; task: Task };

export function useRecordResolver(id: string | undefined) {
  return useQuery<RecordResolution, Error>({
    queryKey: ['record', id],
    enabled: Boolean(id),
    refetchOnWindowFocus: false,
    queryFn: async () => {
      try {
        const invocation = await api.get<InvocationDetail>(recordEndpoints.invocation(id as string));
        return { kind: 'invocation', invocation };
      } catch (cause) {
        const status = cause && typeof cause === 'object' && 'status' in cause ? Number((cause as { status?: number }).status) : 0;
        if (status !== 404) throw cause;
        const task = await api.get<Task>(recordEndpoints.task(id as string));
        return { kind: 'task', task };
      }
    },
  });
}
