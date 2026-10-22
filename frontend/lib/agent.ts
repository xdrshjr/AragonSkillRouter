'use client';

/**
 * Client for the agent console surface (`/api/admin/agent/*`, CONTRACT §5.10).
 *
 * Two things live here because both the floating panel and the SKILL import flow
 * need them and neither may edit `lib/api.ts`:
 *
 *  - typed calls for sessions / runs / stop / health;
 *  - `uploadFile`, a multipart POST to `/api/admin/files`. `lib/api.ts` only knows
 *    how to send JSON, and a `FormData` body must not set `content-type` (the
 *    browser owns the boundary). CSRF is echoed exactly like the JSON wrapper.
 */

import { ApiError, api, readCsrfToken } from '@/lib/api';

/* -------------------------------------------------------------------- types -- */

export type AgentRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'stopped';
export type AgentMessageRole = 'user' | 'assistant' | 'system' | 'tool';

export interface AgentAttachmentMeta {
  id: string;
  filename: string | null;
  content_type?: string | null;
  size?: number | null;
}

export interface AgentRunError {
  code?: string;
  message?: string;
  actions?: string[];
  retryable?: boolean;
}

export interface AgentRun {
  id: string;
  session_id: string;
  status: AgentRunStatus;
  prompt: string;
  stop_reason: string | null;
  error: AgentRunError | null;
  engine_release: string | null;
  usage: Record<string, unknown>;
  queued_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentSessionSummary {
  id: string;
  title: string;
  status: string;
  engine_release: string | null;
  created_at: string;
  updated_at: string;
  run_count?: number;
}

export interface AgentMessage {
  id: string;
  session_id: string;
  seq: number;
  role: AgentMessageRole;
  content: string;
  meta: {
    attachments?: AgentAttachmentMeta[];
    run_id?: string;
    usage?: Record<string, unknown>;
    [key: string]: unknown;
  };
  created_at: string;
}

export interface AgentSessionDetail extends AgentSessionSummary {
  messages: AgentMessage[];
  runs: AgentRun[];
}

export interface AgentModelState {
  configured: boolean;
  provider_id?: string;
  model_id?: string;
  base_url?: string | null;
  credential_id?: string;
  credential_name?: string;
  error?: { code?: string; message?: string; actions?: string[] };
}

export interface AutoExecutePolicy {
  enabled: boolean;
  allowed_providers: string[];
  allowed_capabilities: string[];
  max_calls: number;
  max_minutes: number;
  max_cost_usd: number;
}

export interface AgentSidecarHealth {
  ok?: boolean;
  engine?: {
    package?: string;
    version?: string;
    commit?: string;
    adapter_version?: string;
  };
  model_configured?: boolean;
  error?: { code?: string; message?: string } | null;
  [key: string]: unknown;
}

export interface AgentHealth {
  ok: boolean;
  sidecar: AgentSidecarHealth;
  platform: {
    adapter_version: string;
    model: AgentModelState;
    auto_execute: AutoExecutePolicy;
    tools: string[];
    sidecar: { url: string; token_configured: boolean };
  };
  capability_matrix: {
    capabilities: { total: number; by_status: Record<string, number> };
    tools: number;
    engine_release: unknown;
  };
}

export interface StopReportStep {
  kind?: string;
  tool?: string;
  tool_call_id?: string;
  turn?: number;
  [key: string]: unknown;
}

export interface StopReport {
  run_id: string;
  status: string;
  /** `false` means the engine kept running — never rendered as "cancelled". */
  stopped: boolean;
  reason: string;
  settled?: boolean;
  stopped_steps: StopReportStep[];
  may_continue_upstream: StopReportStep[];
  note?: string;
  error?: AgentRunError;
}

export interface SendMessageResult {
  queued: boolean;
  run: AgentRun;
  message?: AgentMessage;
  events: string;
  note?: string;
}

export interface AgentEventRecord {
  seq: number;
  type: string;
  at?: string;
  run_id?: string;
  session_id?: string;
  [key: string]: unknown;
}

export interface UploadedFile {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  purpose: string;
  url: string;
  created_at: string;
  deduplicated?: boolean;
}

export interface AgentToolsPayload {
  allowed: string[];
  mutating: string[];
  definitions: unknown;
  policy: AutoExecutePolicy;
}

/* --------------------------------------------------------------- endpoints -- */

export const agentEndpoints = {
  sessions: '/api/admin/agent/sessions',
  session: (id: string) => `/api/admin/agent/sessions/${encodeURIComponent(id)}`,
  sessionMessages: (id: string) => `/api/admin/agent/sessions/${encodeURIComponent(id)}/messages`,
  run: (id: string) => `/api/admin/agent/runs/${encodeURIComponent(id)}`,
  runEvents: (id: string) => `/api/admin/agent/runs/${encodeURIComponent(id)}/events`,
  runStop: (id: string) => `/api/admin/agent/runs/${encodeURIComponent(id)}/stop`,
  runQueue: (id: string) => `/api/admin/agent/runs/${encodeURIComponent(id)}/queue`,
  stream: (runId: string) => `/api/stream/agent/${encodeURIComponent(runId)}`,
  health: '/api/admin/agent/health',
  tools: '/api/admin/agent/tools',
  files: '/api/admin/files',
} as const;

/* ------------------------------------------------------------------ client -- */

interface ListEnvelope<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export async function fetchAgentHealth(signal?: AbortSignal): Promise<AgentHealth> {
  return api.get<AgentHealth>(agentEndpoints.health, { signal });
}

export async function fetchAgentTools(signal?: AbortSignal): Promise<AgentToolsPayload> {
  return api.get<AgentToolsPayload>(agentEndpoints.tools, { signal });
}

export async function fetchSessions(search?: string, signal?: AbortSignal): Promise<ListEnvelope<AgentSessionSummary>> {
  return api.get<ListEnvelope<AgentSessionSummary>>(agentEndpoints.sessions, {
    query: { limit: 50, search },
    signal,
  });
}

export async function fetchSession(sessionId: string, signal?: AbortSignal): Promise<AgentSessionDetail> {
  return api.get<AgentSessionDetail>(agentEndpoints.session(sessionId), { signal });
}

export async function createSession(title?: string): Promise<AgentSessionDetail> {
  return api.post<AgentSessionDetail>(agentEndpoints.sessions, { title: title ?? null });
}

export async function deleteSession(sessionId: string): Promise<{ id: string; archived: boolean }> {
  return api.del<{ id: string; archived: boolean }>(agentEndpoints.session(sessionId));
}

export async function sendMessage(
  sessionId: string,
  text: string,
  attachments: AgentAttachmentMeta[],
  options?: { queueIfBusy?: boolean },
): Promise<SendMessageResult> {
  return api.post<SendMessageResult>(agentEndpoints.sessionMessages(sessionId), {
    text,
    attachments: attachments.map((item) => ({ id: item.id, filename: item.filename })),
    queue_if_busy: options?.queueIfBusy ?? true,
  });
}

export async function stopRun(runId: string): Promise<StopReport> {
  return api.post<StopReport>(agentEndpoints.runStop(runId));
}

export async function queueRunMessage(runId: string, text: string): Promise<AgentRun> {
  return api.post<AgentRun>(agentEndpoints.runQueue(runId), { text });
}

export async function fetchRunEvents(
  runId: string,
  after = 0,
  signal?: AbortSignal,
): Promise<{ run_id: string; status: string; items: AgentEventRecord[]; next_after: number }> {
  return api.get<{ run_id: string; status: string; items: AgentEventRecord[]; next_after: number }>(
    agentEndpoints.runEvents(runId),
    { query: { after, limit: 500 }, signal },
  );
}

/* ------------------------------------------------------------------ uploads -- */

/**
 * Multipart upload to `POST /api/admin/files`.
 *
 * Used by the agent composer (documents, screenshots, cURL examples) and by the
 * SKILL import flow (`source_type: "file"`). The response is the public file
 * row: id, filename, content type, size and a mediated download URL.
 */
export async function uploadFile(
  file: File,
  purpose: 'upload' | 'artifact' = 'upload',
  signal?: AbortSignal,
): Promise<UploadedFile> {
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('purpose', purpose);

  const headers: Record<string, string> = { accept: 'application/json' };
  const csrf = readCsrfToken();
  if (csrf) headers['x-csrf-token'] = csrf;

  let response: Response;
  try {
    response = await fetch(agentEndpoints.files, {
      method: 'POST',
      body: form,
      headers,
      credentials: 'include',
      cache: 'no-store',
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'network_error',
      message: 'The upload did not reach the server. Check the connection and retry.',
      details: cause instanceof Error ? cause.message : String(cause),
      actions: ['Retry the upload.', 'Confirm the backend is running.'],
    });
  }

  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const envelope =
      payload && typeof payload === 'object' && 'error' in payload
        ? (payload as { error?: Record<string, unknown> }).error
        : undefined;
    throw new ApiError({
      code: typeof envelope?.code === 'string' ? envelope.code : `http_${response.status}`,
      message:
        typeof envelope?.message === 'string'
          ? envelope.message
          : `The upload failed with HTTP ${response.status}.`,
      status: response.status,
      details: envelope?.details ?? payload,
      requestId:
        typeof envelope?.request_id === 'string' ? envelope.request_id : response.headers.get('x-request-id'),
      actions: Array.isArray(envelope?.actions)
        ? envelope.actions.filter((item): item is string => typeof item === 'string')
        : [],
    });
  }

  return payload as UploadedFile;
}
