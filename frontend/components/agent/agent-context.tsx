'use client';

/**
 * State for the floating agent panel.
 *
 * The provider is mounted once by `app/(dashboard)/layout.tsx`, so everything
 * below survives client-side route changes: the drawer state, the draft text, the
 * uploaded attachments, the scroll offset, the active run and the session list.
 * A reload restores the same fields from `localStorage` (not `sessionStorage`,
 * because a new tab should continue the same conversation).
 *
 * Streaming lives in `lib/sse.ts`; this module only turns raw CONTRACT §5.10
 * events into renderable timeline entries. Nothing here claims a stop, a success
 * or a cancellation the backend did not report.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { describeError } from '@/lib/api';
import { useToast } from '@/components/ui/toast';
import { openSseStream, type SseConnectionState } from '@/lib/sse';
import {
  agentEndpoints,
  createSession,
  deleteSession,
  fetchAgentHealth,
  fetchSession,
  fetchSessions,
  queueRunMessage,
  sendMessage,
  stopRun,
  type AgentAttachmentMeta,
  type AgentEventRecord,
  type AgentHealth,
  type AgentMessage,
  type AgentRunStatus,
  type AgentSessionDetail,
  type AgentSessionSummary,
  type StopReport,
} from '@/lib/agent';

/* -------------------------------------------------------------------- types -- */

export interface DraftAttachment {
  id: string;
  filename: string;
  contentType: string;
  size: number;
}

export type EntryKind =
  | 'user'
  | 'assistant'
  | 'tool'
  | 'todo'
  | 'plan'
  | 'change'
  | 'test'
  | 'waiting'
  | 'stopped'
  | 'failed'
  | 'info';

export interface PanelEntry {
  id: string;
  kind: EntryKind;
  at: string;
  text?: string;
  streaming?: boolean;
  attachments?: AgentAttachmentMeta[];
  tool?: {
    callId: string;
    name: string;
    args?: string;
    status: 'running' | 'done' | 'error';
    result?: string;
    durationMs?: number | null;
  };
  items?: string[];
  detail?: string;
  report?: StopReport;
}

export interface RunState {
  id: string | null;
  status: AgentRunStatus | null;
  startedAt: string | null;
  /** Human label for the current step ("Testing endpoint" …). */
  step: string;
}

export interface QueuedChip {
  text: string;
  at: string;
  /** `false` until the backend's `message.queued` event arrives. */
  acknowledged: boolean;
}

interface PersistedState {
  open: boolean;
  sessionId: string | null;
  draft: string;
  attachments: DraftAttachment[];
  activeRunId: string | null;
  scrollTop: number;
  sessions: AgentSessionSummary[];
}

export interface AgentContextValue {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  sessionId: string | null;
  session: AgentSessionDetail | null;
  sessionLoading: boolean;
  sessionError: Error | null;
  draft: string;
  setDraft: (text: string) => void;
  attachments: DraftAttachment[];
  addAttachment: (file: DraftAttachment) => void;
  removeAttachment: (id: string) => void;
  entries: PanelEntry[];
  run: RunState;
  connection: SseConnectionState;
  queued: QueuedChip | null;
  awaitingInput: boolean;
  sending: boolean;
  stopping: boolean;
  stopReport: StopReport | null;
  lastFailedPrompt: string | null;
  scrollTop: number;
  setScrollTop: (value: number) => void;
  sessions: AgentSessionSummary[];
  sessionsLoading: boolean;
  sessionsError: Error | null;
  refreshSessions: () => void;
  health: AgentHealth | null;
  healthError: Error | null;
  healthLoading: boolean;
  refreshHealth: () => void;
  send: () => Promise<void>;
  stop: () => Promise<void>;
  retry: () => Promise<void>;
  startNewSession: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  /** Archives a session (soft delete: `archived_at`), then moves the user off it. */
  archiveSession: (sessionId: string) => Promise<void>;
  /** True while a session is being archived, keyed by session id. */
  archivingSessionId: string | null;
  error: string | null;
  clearError: () => void;
}

const STORAGE_KEY = 'sr.agent.v1';

const EMPTY_PERSISTED: PersistedState = {
  open: false,
  sessionId: null,
  draft: '',
  attachments: [],
  activeRunId: null,
  scrollTop: 0,
  sessions: [],
};

function readPersisted(): PersistedState {
  if (typeof window === 'undefined') return EMPTY_PERSISTED;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY_PERSISTED;
    const parsed = JSON.parse(raw) as Partial<PersistedState> | null;
    if (!parsed || typeof parsed !== 'object') return EMPTY_PERSISTED;
    return {
      open: Boolean(parsed.open),
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : null,
      draft: typeof parsed.draft === 'string' ? parsed.draft : '',
      attachments: Array.isArray(parsed.attachments)
        ? parsed.attachments.filter(
            (item): item is DraftAttachment =>
              Boolean(item) && typeof item === 'object' && typeof (item as DraftAttachment).id === 'string',
          )
        : [],
      activeRunId: typeof parsed.activeRunId === 'string' ? parsed.activeRunId : null,
      scrollTop: typeof parsed.scrollTop === 'number' ? parsed.scrollTop : 0,
      sessions: Array.isArray(parsed.sessions) ? (parsed.sessions as AgentSessionSummary[]) : [],
    };
  } catch {
    return EMPTY_PERSISTED;
  }
}

const TERMINAL: AgentRunStatus[] = ['succeeded', 'failed', 'stopped'];

const STEP_BY_TOOL: Record<string, string> = {
  'platform.list_providers': 'Reading providers',
  'platform.list_credentials': 'Reading credentials',
  'platform.list_capabilities': 'Reading capabilities',
  'platform.get_capability': 'Reading capability',
  'platform.create_capability_draft': 'Generating config',
  'platform.update_capability_draft': 'Generating config',
  'platform.validate_capability': 'Validating config',
  'platform.test_capability': 'Testing endpoint',
  'platform.publish_capability': 'Publishing capability',
  'platform.list_skills': 'Reading SKILLs',
  'platform.save_skill_draft': 'Saving SKILL draft',
  'platform.get_test_evidence': 'Reading test evidence',
};

function stepForTool(tool: string | undefined): string {
  if (!tool) return 'Working';
  return STEP_BY_TOOL[tool] ?? `Calling ${tool.replace('platform.', '').replace(/_/g, ' ')}`;
}

const PHASE_LABELS: Record<string, string> = {
  turn_start: 'Thinking',
  thinking_start: 'Thinking',
  tool_call_start: 'Preparing a tool call',
  llm_done: 'Reading the model answer',
  llm_error: 'Model error — retrying',
  retry_scheduled: 'Retrying the model call',
  retry_attempt: 'Retrying the model call',
  stream_restart: 'Restarting the stream',
  steering_accepted: 'Applying your message',
  compaction_start: 'Compacting the transcript',
  compaction_end: 'Compacted the transcript',
};

function entriesFromMessages(messages: AgentMessage[]): PanelEntry[] {
  return messages
    .filter((message) => message.role === 'user' || message.role === 'assistant')
    .map((message) => ({
      id: `msg-${message.id}`,
      kind: message.role === 'user' ? ('user' as const) : ('assistant' as const),
      at: message.created_at,
      text: message.content,
      attachments: message.meta?.attachments,
    }));
}

let entryCounter = 0;
function nextId(prefix: string): string {
  entryCounter += 1;
  return `${prefix}-${Date.now()}-${entryCounter}`;
}

function finishStreaming(entries: PanelEntry[]): PanelEntry[] {
  let touched = false;
  const next = entries.map((entry) => {
    if (entry.kind === 'assistant' && entry.streaming) {
      touched = true;
      return { ...entry, streaming: false };
    }
    return entry;
  });
  return touched ? next : entries;
}

function safeParse(data: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(data) as unknown;
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function safeJson(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/* ------------------------------------------------------------------ context -- */

const AgentContext = createContext<AgentContextValue | null>(null);

export function AgentProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const queryClient = useQueryClient();

  const [open, setOpenState] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const [entries, setEntries] = useState<PanelEntry[]>([]);
  const [run, setRun] = useState<RunState>({ id: null, status: null, startedAt: null, step: 'Idle' });
  const [connection, setConnection] = useState<SseConnectionState>('idle');
  const [queued, setQueued] = useState<QueuedChip | null>(null);
  const [awaitingInput, setAwaitingInput] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopReport, setStopReport] = useState<StopReport | null>(null);
  const [lastFailedPrompt, setLastFailedPrompt] = useState<string | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [cachedSessions, setCachedSessions] = useState<AgentSessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<Error | null>(null);
  const [session, setSession] = useState<AgentSessionDetail | null>(null);
  const [archivingSessionId, setArchivingSessionId] = useState<string | null>(null);

  const sawPlan = useRef(false);
  const sawChange = useRef(false);
  const currentPrompt = useRef('');
  const streamHandle = useRef<{ close: () => void } | null>(null);
  const hydrated = useRef(false);

  /* ------------------------------------------------------------- persistence -- */

  const loadSession = useCallback(async (id: string) => {
    setSessionLoading(true);
    setSessionError(null);
    try {
      const detail = await fetchSession(id);
      setSession(detail);
      setSessionId(id);
      setEntries(entriesFromMessages(detail.messages));
      sawPlan.current = false;
      sawChange.current = false;
      setAwaitingInput(false);
      setStopReport(null);
      const live = detail.runs.find((item) => item.status === 'queued' || item.status === 'running');
      const newest = detail.runs[0];
      const nextRun = live ?? newest ?? null;
      setRun({
        id: nextRun?.id ?? null,
        status: nextRun?.status ?? null,
        startedAt: nextRun?.started_at ?? nextRun?.created_at ?? null,
        step: live ? 'Resuming the live run' : 'Idle',
      });
      setLastFailedPrompt(newest?.status === 'failed' ? newest.prompt : null);
    } catch (cause) {
      setSessionError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      setSessionLoading(false);
    }
  }, []);

  // Restore the persisted panel state after mount (never during render, so the
  // server-rendered markup and the first client paint always agree).
  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;
    const persisted = readPersisted();
    setOpenState(persisted.open);
    setDraft(persisted.draft);
    setAttachments(persisted.attachments);
    setScrollTop(persisted.scrollTop);
    setCachedSessions(persisted.sessions);
    setRun({ id: persisted.activeRunId, status: null, startedAt: null, step: 'Resuming' });
    if (persisted.sessionId) void loadSession(persisted.sessionId);
  }, [loadSession]);

  useEffect(() => {
    if (!hydrated.current) return;
    if (typeof window === 'undefined') return;
    const payload: PersistedState = {
      open,
      sessionId,
      draft,
      attachments,
      activeRunId: run.id,
      scrollTop,
      sessions: cachedSessions.slice(0, 25),
    };
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch {
      // Storage may be full or disabled; the panel still works for this page view.
    }
  }, [open, sessionId, draft, attachments, run.id, scrollTop, cachedSessions]);

  const setOpen = useCallback((next: boolean) => setOpenState(next), []);
  const toggle = useCallback(() => setOpenState((value) => !value), []);

  /* ------------------------------------------------------------------ health -- */

  const healthQuery = useQuery<AgentHealth, Error>({
    queryKey: ['agent-health'],
    queryFn: ({ signal }) => fetchAgentHealth(signal),
    staleTime: 30_000,
    retry: 1,
  });

  const sessionsQuery = useQuery<AgentSessionSummary[], Error>({
    queryKey: ['agent-sessions'],
    queryFn: async ({ signal }) => (await fetchSessions(undefined, signal)).items,
    enabled: open,
    staleTime: 10_000,
    retry: 1,
  });

  useEffect(() => {
    if (sessionsQuery.data) setCachedSessions(sessionsQuery.data);
  }, [sessionsQuery.data]);

  /* -------------------------------------------------------------- event apply -- */

  const handleEvent = useCallback((event: AgentEventRecord) => {
    const at = typeof event.at === 'string' ? event.at : new Date().toISOString();
    const type = event.type;
    if (type === 'keepalive') return;

    if (type === 'assistant.delta') {
      const delta = typeof event.delta === 'string' ? event.delta : '';
      if (!delta) return;
      setEntries((current) => {
        const last = current[current.length - 1];
        if (last && last.kind === 'assistant' && last.streaming) {
          return [...current.slice(0, -1), { ...last, text: `${last.text ?? ''}${delta}` }];
        }
        return [...current, { id: nextId('assistant'), kind: 'assistant', at, text: delta, streaming: true }];
      });
      return;
    }

    switch (type) {
      case 'run.started': {
        setRun((current) => ({
          ...current,
          status: 'running',
          startedAt: current.startedAt ?? at,
          step: 'Starting the run',
        }));
        setQueued(null);
        const model = typeof event.model === 'string' ? event.model : null;
        setEntries((current) => [
          ...current,
          { id: nextId('info'), kind: 'info', at, text: model ? `Run started with ${model}.` : 'Run started.' },
        ]);
        break;
      }
      case 'run.progress': {
        const phase = typeof event.phase === 'string' ? event.phase : '';
        const tool = typeof event.tool === 'string' ? event.tool : undefined;
        const label = tool ? stepForTool(tool) : (PHASE_LABELS[phase] ?? 'Working');
        setRun((current) => ({ ...current, status: 'running', step: label }));
        if (phase === 'llm_error' || phase === 'retry_scheduled' || phase === 'stream_restart') {
          const message = typeof event.message === 'string' ? event.message : label;
          const attempt = typeof event.attempt === 'number' ? ` (attempt ${event.attempt})` : '';
          setEntries((current) => [
            ...current,
            { id: nextId('info'), kind: 'info', at, text: `${message}${attempt}` },
          ]);
        }
        break;
      }
      case 'tool.call': {
        const callId = String(event.tool_call_id ?? nextId('call'));
        const tool = String(event.tool ?? 'tool');
        setRun((current) => ({ ...current, status: 'running', step: stepForTool(tool) }));
        setEntries((current) => [
          ...current,
          {
            id: nextId('tool'),
            kind: 'tool',
            at,
            tool: { callId, name: tool, args: safeJson(event.args), status: 'running' },
          },
        ]);
        break;
      }
      case 'tool.result': {
        const callId = String(event.tool_call_id ?? '');
        const isError = Boolean(event.is_error);
        setEntries((current) => {
          const reversedIndex = [...current]
            .reverse()
            .findIndex((item) => item.kind === 'tool' && item.tool?.callId === callId);
          if (reversedIndex === -1) {
            return [
              ...current,
              {
                id: nextId('tool'),
                kind: 'tool',
                at,
                tool: {
                  callId: callId || nextId('call'),
                  name: String(event.tool ?? 'tool'),
                  status: isError ? 'error' : 'done',
                  result: safeJson(event.output),
                  durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
                },
              },
            ];
          }
          const target = current.length - 1 - reversedIndex;
          const existing = current[target];
          const next = [...current];
          next[target] = {
            ...existing,
            tool: {
              callId: existing.tool?.callId ?? callId,
              name: existing.tool?.name ?? String(event.tool ?? 'tool'),
              args: existing.tool?.args,
              status: isError ? 'error' : 'done',
              result: safeJson(event.output),
              durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
            },
          };
          return next;
        });
        break;
      }
      case 'todo.update': {
        const items = Array.isArray(event.items)
          ? event.items.map((item) => {
              if (typeof item === 'string') return item;
              if (item && typeof item === 'object') {
                const record = item as Record<string, unknown>;
                return String(record.text ?? record.title ?? '');
              }
              return '';
            })
          : [];
        setEntries((current) => [...current, { id: nextId('todo'), kind: 'todo', at, items }]);
        break;
      }
      case 'plan.proposed': {
        sawPlan.current = true;
        const title = typeof event.title === 'string' ? event.title : 'Proposed plan';
        const steps = Array.isArray(event.steps)
          ? event.steps.map((step) => (typeof step === 'string' ? step : safeJson(step) ?? ''))
          : [];
        setEntries((current) => [...current, { id: nextId('plan'), kind: 'plan', at, text: title, items: steps }]);
        break;
      }
      case 'change.applied': {
        sawChange.current = true;
        const summary =
          typeof event.summary === 'string'
            ? event.summary
            : typeof event.description === 'string'
              ? event.description
              : 'A configuration change was applied.';
        setEntries((current) => [
          ...current,
          { id: nextId('change'), kind: 'change', at, text: summary, detail: safeJson(event.details) },
        ]);
        break;
      }
      case 'test.result': {
        const status = typeof event.status === 'string' ? event.status : 'unknown';
        setEntries((current) => [
          ...current,
          { id: nextId('test'), kind: 'test', at, text: `Test ${status}`, detail: safeJson(event.details ?? event.result ?? event) },
        ]);
        break;
      }
      case 'message.queued': {
        setQueued((current) => (current ? { ...current, acknowledged: true } : current));
        setEntries((current) => [
          ...current,
          { id: nextId('info'), kind: 'info', at, text: 'Queued message picked up by the run.' },
        ]);
        break;
      }
      case 'run.stopped': {
        const report: StopReport = {
          run_id: String(event.run_id ?? ''),
          status: 'stopped',
          stopped: true,
          reason: String(event.reason ?? 'stop_requested'),
          settled: typeof event.settled === 'boolean' ? event.settled : undefined,
          stopped_steps: Array.isArray(event.stopped_steps) ? (event.stopped_steps as StopReport['stopped_steps']) : [],
          may_continue_upstream: Array.isArray(event.may_continue_upstream)
            ? (event.may_continue_upstream as StopReport['may_continue_upstream'])
            : [],
          note: 'Listed upstream calls may still complete and produce invocation records.',
        };
        setStopReport(report);
        setRun((current) => ({ ...current, status: 'stopped', step: 'Stopped' }));
        setEntries((current) => [
          ...finishStreaming(current),
          { id: nextId('stopped'), kind: 'stopped', at, report },
        ]);
        break;
      }
      case 'run.failed': {
        const message = typeof event.message === 'string' ? event.message : 'The run failed.';
        setRun((current) => ({ ...current, status: 'failed', step: 'Failed' }));
        setLastFailedPrompt(currentPrompt.current || null);
        setEntries((current) => [
          ...finishStreaming(current),
          { id: nextId('failed'), kind: 'failed', at, text: message, detail: safeJson(event.details) },
        ]);
        break;
      }
      case 'run.finished': {
        const text = typeof event.text === 'string' ? event.text : '';
        setRun((current) => ({ ...current, status: 'succeeded', step: 'Waiting for input' }));
        setEntries((current) => {
          let next = finishStreaming(current);
          const hasAssistant = next.some((entry) => entry.kind === 'assistant');
          if (text && !hasAssistant) {
            next = [...next, { id: nextId('assistant'), kind: 'assistant', at, text }];
          }
          if (sawPlan.current && !sawChange.current) {
            next = [
              ...next,
              {
                id: nextId('waiting'),
                kind: 'waiting',
                at,
                text: 'Waiting for you: the run proposed a plan and ended its turn. Reply to confirm or adjust it.',
              },
            ];
          }
          return next;
        });
        setAwaitingInput(sawPlan.current && !sawChange.current);
        break;
      }
      default:
        break;
    }
  }, []);

  /* -------------------------------------------------------------- SSE wiring -- */

  const activeRunId = run.id;

  const loadSessionQuietly = useCallback(async () => {
    if (!sessionId) return;
    try {
      const detail = await fetchSession(sessionId);
      setSession(detail);
    } catch {
      // A failed background refresh must not wipe the streamed transcript.
    }
  }, [sessionId]);

  useEffect(() => {
    streamHandle.current?.close();
    streamHandle.current = null;
    if (!activeRunId) {
      setConnection('idle');
      return;
    }
    const handle = openSseStream({
      url: agentEndpoints.stream(activeRunId),
      onEvent: (event) => {
        const parsed = safeParse(event.data);
        if (!parsed) return;
        const record = parsed as AgentEventRecord;
        if (typeof record.seq !== 'number') record.seq = Number(event.id ?? 0);
        if (!record.type) record.type = event.type;
        handleEvent(record);
      },
      onState: (state) => setConnection(state),
      stopWhen: (event) => {
        const parsed = safeParse(event.data);
        const type = String(parsed?.type ?? event.type);
        return type === 'run.finished' || type === 'run.failed' || type === 'run.stopped';
      },
    });
    streamHandle.current = handle;
    return () => {
      handle.close();
      streamHandle.current = null;
    };
  }, [activeRunId, handleEvent]);

  useEffect(() => {
    return () => {
      streamHandle.current?.close();
      streamHandle.current = null;
    };
  }, []);

  const runStatus = run.status;
  useEffect(() => {
    if (!runStatus || !TERMINAL.includes(runStatus)) return;
    streamHandle.current?.close();
    streamHandle.current = null;
    setConnection('closed');
    void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
    void loadSessionQuietly();
  }, [runStatus, loadSessionQuietly, queryClient]);

  /* ------------------------------------------------------------------ actions -- */

  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionId) return sessionId;
    const created = await createSession(undefined);
    setSession(created);
    setSessionId(created.id);
    setEntries(entriesFromMessages(created.messages ?? []));
    void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
    return created.id;
  }, [sessionId, queryClient]);

  const sendText = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text && attachments.length === 0) return;
      setError(null);
      setSending(true);
      try {
        const id = await ensureSession();
        const meta: AgentAttachmentMeta[] = attachments.map((item) => ({
          id: item.id,
          filename: item.filename,
          content_type: item.contentType,
          size: item.size,
        }));
        const now = new Date().toISOString();

        const live = run.id && run.status && !TERMINAL.includes(run.status) ? run.id : null;
        if (live) {
          // A run is already live: queue instead of starting a second one. The chip
          // says "waiting to be picked up" until this POST returns, because only the
          // backend's answer proves the message was stored.
          setQueued({ text, at: now, acknowledged: false });
          try {
            await queueRunMessage(live, text);
          } catch (cause) {
            setQueued(null);
            throw cause;
          }
          setQueued((current) => (current ? { ...current, acknowledged: true } : current));
          setEntries((current) => [
            ...current,
            { id: nextId('user'), kind: 'user', at: now, text, attachments: meta },
          ]);
          toast.info({
            title: 'Queued — waiting to be picked up',
            description: 'The live run will process this after its current turn.',
          });
        } else {
          const result = await sendMessage(id, text, meta, { queueIfBusy: true });
          currentPrompt.current = text;
          sawPlan.current = false;
          sawChange.current = false;
          setAwaitingInput(false);
          setStopReport(null);
          setEntries((current) => [
            ...current,
            { id: nextId('user'), kind: 'user', at: now, text, attachments: meta },
          ]);
          setDraft('');
          setAttachments([]);
          setRun({
            id: result.run.id,
            status: result.run.status,
            startedAt: result.run.started_at ?? now,
            step: result.queued ? 'Waiting to be picked up' : 'Starting the run',
          });
          setQueued(result.queued ? { text, at: now, acknowledged: false } : null);
          void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
        }
      } catch (cause) {
        setError(describeError(cause));
        toast.apiError(cause, 'The message was not accepted');
      } finally {
        setSending(false);
      }
    },
    [attachments, ensureSession, queryClient, run.id, run.status, toast],
  );

  const send = useCallback(async () => {
    await sendText(draft);
  }, [draft, sendText]);

  const retry = useCallback(async () => {
    if (!lastFailedPrompt) return;
    await sendText(lastFailedPrompt);
  }, [lastFailedPrompt, sendText]);

  const stop = useCallback(async () => {
    if (!run.id) return;
    setStopping(true);
    try {
      const report = await stopRun(run.id);
      setStopReport(report);
      setEntries((current) => [
        ...finishStreaming(current),
        { id: nextId('stopped'), kind: 'stopped', at: new Date().toISOString(), report },
      ]);
      setRun((current) => ({
        ...current,
        status: report.status as AgentRunStatus,
        step: report.stopped ? 'Stopped' : 'Not stopped',
      }));
      if (report.stopped) {
        toast.warning({ title: 'Run stopped', description: report.note ?? 'The engine confirmed the stop.' });
      } else {
        toast.warning({
          title: report.status === 'failed' ? 'Stop failed — the run is marked failed' : 'Nothing was stopped',
          description: report.note ?? report.reason,
        });
      }
    } catch (cause) {
      setError(describeError(cause));
      toast.apiError(cause, 'Stop was not accepted');
    } finally {
      setStopping(false);
    }
  }, [run.id, toast]);

  const startNewSession = useCallback(async () => {
    setSessionLoading(true);
    setSessionError(null);
    try {
      const created = await createSession(undefined);
      setSession(created);
      setSessionId(created.id);
      setEntries([]);
      setRun({ id: null, status: null, startedAt: null, step: 'Idle' });
      setQueued(null);
      setStopReport(null);
      setAwaitingInput(false);
      setLastFailedPrompt(null);
      setDraft('');
      setAttachments([]);
      sawPlan.current = false;
      sawChange.current = false;
      void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
    } catch (cause) {
      setSessionError(cause instanceof Error ? cause : new Error(String(cause)));
      toast.apiError(cause, 'Could not start a new session');
    } finally {
      setSessionLoading(false);
    }
  }, [queryClient, toast]);

  const selectSession = useCallback(
    async (id: string) => {
      await loadSession(id);
    },
    [loadSession],
  );

  /**
   * Archive a session and get the panel out of it.
   *
   * `DELETE /api/admin/agent/sessions/{id}` sets `archived_at`, so the transcript is
   * not destroyed — it just stops appearing in the tab strip. If the archived
   * session was the one on screen, the panel switches to the next most recent
   * session (or returns to the empty "new session" state when none is left), because
   * leaving the UI pointed at an archived id would show a transcript that can no
   * longer be refreshed.
   */
  const archiveSession = useCallback(
    async (id: string) => {
      setArchivingSessionId(id);
      try {
        await deleteSession(id);
        // Drop it from the cached list immediately: the refetch is async and the tab
        // would otherwise linger for a beat after the click.
        setCachedSessions((current) => current.filter((item) => item.id !== id));
        queryClient.setQueryData<AgentSessionSummary[]>(['agent-sessions'], (current) =>
          (current ?? []).filter((item) => item.id !== id),
        );
        if (sessionId === id) {
          const next = cachedSessions.find((item) => item.id !== id);
          if (next) {
            await loadSession(next.id);
          } else {
            setSessionId(null);
            setSession(null);
            setEntries([]);
            setRun({ id: null, status: null, startedAt: null, step: 'Idle' });
            setQueued(null);
            setStopReport(null);
            setAwaitingInput(false);
            setLastFailedPrompt(null);
          }
        }
        void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
        toast.success({ title: 'Session archived', description: 'Its transcript is kept; it no longer appears in the tabs.' });
      } catch (cause) {
        toast.apiError(cause, 'Could not archive the session');
      } finally {
        setArchivingSessionId(null);
      }
    },
    [cachedSessions, loadSession, queryClient, sessionId, toast],
  );

  const addAttachment = useCallback((file: DraftAttachment) => {
    setAttachments((current) => (current.some((item) => item.id === file.id) ? current : [...current, file]));
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => current.filter((item) => item.id !== id));
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const value = useMemo<AgentContextValue>(
    () => ({
      open,
      setOpen,
      toggle,
      sessionId,
      session,
      sessionLoading,
      sessionError,
      draft,
      setDraft,
      attachments,
      addAttachment,
      removeAttachment,
      entries,
      run,
      connection,
      queued,
      awaitingInput,
      sending,
      stopping,
      stopReport,
      lastFailedPrompt,
      scrollTop,
      setScrollTop,
      sessions: cachedSessions,
      sessionsLoading: sessionsQuery.isLoading,
      sessionsError: sessionsQuery.error ?? null,
      refreshSessions: () => void sessionsQuery.refetch(),
      health: healthQuery.data ?? null,
      healthError: healthQuery.error ?? null,
      healthLoading: healthQuery.isLoading,
      refreshHealth: () => void healthQuery.refetch(),
      send,
      stop,
      retry,
      startNewSession,
      selectSession,
      archiveSession,
      archivingSessionId,
      error,
      clearError,
    }),
    [
      open,
      setOpen,
      toggle,
      sessionId,
      session,
      sessionLoading,
      sessionError,
      draft,
      attachments,
      addAttachment,
      removeAttachment,
      entries,
      run,
      connection,
      queued,
      awaitingInput,
      sending,
      stopping,
      stopReport,
      lastFailedPrompt,
      scrollTop,
      cachedSessions,
      sessionsQuery,
      healthQuery,
      send,
      stop,
      retry,
      startNewSession,
      selectSession,
      archiveSession,
      archivingSessionId,
      error,
      clearError,
    ],
  );

  return <AgentContext.Provider value={value}>{children}</AgentContext.Provider>;
}

export function useAgent(): AgentContextValue {
  const context = useContext(AgentContext);
  if (!context) throw new Error('useAgent must be used inside <AgentProvider>.');
  return context;
}
