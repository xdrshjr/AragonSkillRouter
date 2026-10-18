'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { apiFetch, apiGet, apiPost, isApiError, uploadFile } from '@/lib/api';
import { formatDateTimeSec } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import { agentStreamPath, openSse, type SseStatus } from '@/lib/sse';
import type {
  AgentApproval,
  AgentAttachment,
  AgentFlow,
  AgentSession,
  AgentSessionDetail,
  AgentTodo,
  AgentTurn,
  JsonObject,
  SendMessageResult,
  SseEnvelope,
  StopReport,
} from '@/lib/types';

/**
 * 浮窗 Agent 的会话状态机 + SSE 订阅（spec §13.3）。
 *
 * 关键约束：
 * - 状态挂在 `AgentProvider`（app/(app)/layout.tsx），**跨页面切换不销毁**
 * - SSE 由本 hook 持有，路由变化只重渲染，不重建订阅（provider 在 (app) layout 内且不随路径重挂）
 * - 断线重连带 last_event_id（见 lib/sse.ts），重放为只读回放
 */

export type TurnPhase = 'idle' | 'sending' | 'queued' | 'running' | 'stopping' | 'error';

export type TimelineKind = 'reading' | 'change' | 'test' | 'missing' | 'notice';

export interface TimelineEntry {
  id: string;
  kind: TimelineKind;
  title: string;
  detail: string | null;
  status: 'running' | 'done' | 'failed';
  durationMs: number | null;
  at: string;
  /** 原始事件（可展开证据） */
  raw?: JsonObject;
}

export interface AgentMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  at: string;
  turnId?: string;
  streaming?: boolean;
  queuePosition?: number | null;
  attachments?: AgentAttachment[];
  failed?: boolean;
  errorHint?: string | null;
}

export interface AgentPanelSize {
  width: number;
  height: number;
}

export interface AgentSessionApi {
  panelOpen: boolean;
  openPanel: () => void;
  closePanel: () => void;
  togglePanel: () => void;
  size: AgentPanelSize;
  setSize: (size: AgentPanelSize) => void;

  sessions: AgentSession[];
  sessionsLoading: boolean;
  sessionId: string | null;
  selectSession: (id: string) => void;
  createSession: () => Promise<void>;

  messages: AgentMessage[];
  timeline: TimelineEntry[];
  todos: AgentTodo[];
  phase: TurnPhase;
  queuePosition: number | null;
  queuedCount: number;
  error: { message: string; hint: string | null; code: string; requestId: string | null } | null;
  sseStatus: SseStatus;
  stopReport: StopReport | null;

  draft: string;
  setDraft: (text: string) => void;
  attachments: AgentAttachment[];
  attachFiles: (files: File[]) => Promise<void>;
  removeAttachment: (fileId: string) => void;
  uploading: { name: string; percent: number } | null;
  cancelUpload: () => void;

  send: (text?: string) => Promise<void>;
  stop: () => Promise<void>;
  retry: (turnId?: string) => Promise<void>;
  clearError: () => void;

  approvals: AgentApproval[];
  decideApproval: (id: string, approve: boolean) => Promise<void>;
  flows: AgentFlow[];
  activeFlow: AgentFlow | null;
  selectFlow: (id: string | null) => void;
  runFlowStep: (flowId: string, step: string) => Promise<void>;
  publishFlow: (flowId: string) => Promise<void>;
}

const AgentContext = React.createContext<AgentSessionApi | null>(null);

const SIZE_KEY = 'sr:agent-panel-size';
const SESSION_KEY = 'sr:agent-session-id';
const DEFAULT_SIZE: AgentPanelSize = { width: 420, height: 640 };
const MIN_SIZE = { width: 360, height: 420 };
const MAX_SIZE = { width: 720, height: 900 };

function readSize(): AgentPanelSize {
  if (typeof window === 'undefined') return DEFAULT_SIZE;
  try {
    const raw = window.localStorage.getItem(SIZE_KEY);
    if (!raw) return DEFAULT_SIZE;
    const parsed = JSON.parse(raw) as Partial<AgentPanelSize>;
    const width = Math.min(MAX_SIZE.width, Math.max(MIN_SIZE.width, Number(parsed.width) || DEFAULT_SIZE.width));
    const height = Math.min(MAX_SIZE.height, Math.max(MIN_SIZE.height, Number(parsed.height) || DEFAULT_SIZE.height));
    return { width, height };
  } catch {
    return DEFAULT_SIZE;
  }
}

function toolKind(toolName: string): TimelineKind {
  if (toolName.includes('fetch') || toolName.includes('parse') || toolName.includes('get') || toolName.includes('list')) {
    return 'reading';
  }
  if (toolName.includes('propose') || toolName.includes('update') || toolName.includes('create') || toolName.includes('install')) {
    return 'change';
  }
  if (toolName.includes('test') || toolName.includes('sample')) return 'test';
  if (toolName.includes('approval') || toolName.includes('finding') || toolName.includes('validate')) {
    return 'missing';
  }
  return 'notice';
}

function stringify(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function payloadString(payload: JsonObject, key: string): string | null {
  const value = payload[key];
  return typeof value === 'string' ? value : null;
}

function payloadNumber(payload: JsonObject, key: string): number | null {
  const value = payload[key];
  return typeof value === 'number' ? value : null;
}

export function AgentProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();

  const [panelOpen, setPanelOpen] = React.useState(false);
  const [size, setSizeState] = React.useState<AgentPanelSize>(DEFAULT_SIZE);
  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const [messages, setMessages] = React.useState<AgentMessage[]>([]);
  const [timeline, setTimeline] = React.useState<TimelineEntry[]>([]);
  const [todos, setTodos] = React.useState<AgentTodo[]>([]);
  const [phase, setPhase] = React.useState<TurnPhase>('idle');
  const [queuePosition, setQueuePosition] = React.useState<number | null>(null);
  const [queuedCount, setQueuedCount] = React.useState(0);
  const [error, setError] = React.useState<AgentSessionApi['error']>(null);
  const [sseStatus, setSseStatus] = React.useState<SseStatus>('closed');
  const [stopReport, setStopReport] = React.useState<StopReport | null>(null);
  const [draft, setDraft] = React.useState('');
  const [attachments, setAttachments] = React.useState<AgentAttachment[]>([]);
  const [uploading, setUploading] = React.useState<{ name: string; percent: number } | null>(null);
  const [activeFlowId, setActiveFlowId] = React.useState<string | null>(null);

  const uploadAbort = React.useRef<AbortController | null>(null);
  const activeTurnId = React.useRef<string | null>(null);

  /* 初始恢复 */
  React.useEffect(() => {
    setSizeState(readSize());
    try {
      const stored = window.localStorage.getItem(SESSION_KEY);
      if (stored) setSessionId(stored);
    } catch {
      /* ignore */
    }
  }, []);

  React.useEffect(() => {
    try {
      if (sessionId) window.localStorage.setItem(SESSION_KEY, sessionId);
      else window.localStorage.removeItem(SESSION_KEY);
    } catch {
      /* ignore */
    }
  }, [sessionId]);

  const sessionsQuery = useQuery<AgentSession[]>({
    queryKey: queryKeys.agent.sessions(),
    queryFn: async () => {
      const result = await apiGet<{ items: AgentSession[] } | AgentSession[]>('/admin/agent/sessions', {
        page: 1,
        page_size: 20,
      });
      return Array.isArray(result) ? result : result.items;
    },
    staleTime: 30_000,
  });

  const approvalsQuery = useQuery<AgentApproval[]>({
    queryKey: queryKeys.agent.approvals(),
    queryFn: async () => {
      const result = await apiGet<{ items: AgentApproval[] } | AgentApproval[]>('/admin/agent/approvals');
      return Array.isArray(result) ? result : result.items;
    },
    refetchInterval: 20_000,
  });

  const flowsQuery = useQuery<AgentFlow[]>({
    queryKey: queryKeys.agent.flows(),
    queryFn: async () => {
      const result = await apiGet<{ items: AgentFlow[] } | AgentFlow[]>('/admin/agent/flows', {
        page: 1,
        page_size: 20,
      });
      return Array.isArray(result) ? result : result.items;
    },
    staleTime: 15_000,
  });

  /* 会话历史加载 */
  const sessionDetail = useQuery<AgentSessionDetail>({
    queryKey: queryKeys.agent.session(sessionId ?? 'none'),
    queryFn: () => apiFetch<AgentSessionDetail>(`/admin/agent/sessions/${sessionId}`),
    enabled: Boolean(sessionId),
    staleTime: 5_000,
  });

  React.useEffect(() => {
    const detail = sessionDetail.data;
    if (!detail) return;
    // 服务端字段缺失（版本差、灰度中）不应把整个 (app) 布局打成 client-side exception：
    // 这里一律按空集处理，退化成「没有历史」而不是白屏。
    const turns = Array.isArray(detail.turns) ? detail.turns : [];
    const toolCalls = Array.isArray(detail.tool_calls) ? detail.tool_calls : [];
    setMessages((prev) => {
      // 没有实时消息时以服务端历史为准
      if (prev.length > 0) return prev;
      return buildMessagesFromTurns(turns);
    });
    if (toolCalls.length > 0) {
      setTimeline((prev) =>
        prev.length > 0
          ? prev
          : toolCalls.map((call) => ({
              id: call.id,
              kind: toolKind(call.tool_name),
              title: call.tool_name,
              detail: call.result_summary,
              status: call.ok === null ? 'running' : call.ok ? 'done' : 'failed',
              durationMs: call.duration_ms,
              at: call.at,
              raw: call.args_summary,
            })),
      );
    }
    if (Array.isArray(detail.todos) && detail.todos.length > 0) setTodos(detail.todos);
  }, [sessionDetail.data]);

  /* 自动挑选/创建会话 */
  React.useEffect(() => {
    if (sessionId) return;
    const first = sessionsQuery.data?.[0];
    if (first) setSessionId(first.id);
  }, [sessionId, sessionsQuery.data]);

  /* ── SSE ──────────────────────────────────────────── */
  const handleEvent = React.useCallback((event: SseEnvelope) => {
    const payload = event.payload ?? {};
    switch (event.type) {
      case 'turn.queued': {
        const position = payloadNumber(payload, 'queue_position');
        setQueuePosition(position);
        setQueuedCount((count) => count + 1);
        setPhase('queued');
        break;
      }
      case 'turn.started': {
        const turnId = event.turn_id ?? null;
        activeTurnId.current = turnId;
        setPhase('running');
        setQueuePosition(null);
        setQueuedCount((count) => Math.max(0, count - 1));
        setMessages((prev) => {
          const next = [...prev];
          const lastAssistant = [...next].reverse().find((m) => m.role === 'assistant' && m.streaming);
          if (lastAssistant) return next;
          next.push({
            id: `a-${turnId ?? Date.now()}`,
            role: 'assistant',
            text: '',
            at: event.at,
            turnId: turnId ?? undefined,
            streaming: true,
          });
          return next;
        });
        break;
      }
      case 'assistant.delta': {
        const text = payloadString(payload, 'text') ?? '';
        setMessages((prev) => {
          const next = [...prev];
          const index = [...next].reverse().findIndex((m) => m.role === 'assistant' && m.streaming);
          if (index < 0) {
            next.push({
              id: `a-${event.turn_id ?? Date.now()}`,
              role: 'assistant',
              text,
              at: event.at,
              turnId: event.turn_id,
              streaming: true,
            });
            return next;
          }
          const realIndex = next.length - 1 - index;
          const target = next[realIndex];
          if (target) next[realIndex] = { ...target, text: `${target.text}${text}` };
          return next;
        });
        break;
      }
      case 'thinking.delta': {
        const text = payloadString(payload, 'text') ?? '';
        setTimeline((prev) => {
          const existingIndex = prev.findIndex((entry) => entry.id === `thinking-${event.turn_id ?? ''}`);
          if (existingIndex >= 0) {
            const next = [...prev];
            const target = next[existingIndex];
            if (target) {
              next[existingIndex] = { ...target, detail: `${target.detail ?? ''}${text}` };
            }
            return next;
          }
          return [
            ...prev,
            {
              id: `thinking-${event.turn_id ?? Date.now()}`,
              kind: 'notice',
              title: '模型推理中',
              detail: text,
              status: 'running',
              durationMs: null,
              at: event.at,
            },
          ];
        });
        break;
      }
      case 'tool.started': {
        const name = payloadString(payload, 'name') ?? 'tool';
        setTimeline((prev) => [
          ...prev,
          {
            id: `${event.turn_id ?? 'turn'}-${event.seq ?? Date.now()}-${name}`,
            kind: toolKind(name),
            title: name,
            detail: stringify(payload.args_summary),
            status: 'running',
            durationMs: null,
            at: event.at,
            raw: payload,
          },
        ]);
        break;
      }
      case 'tool.progress': {
        const percent = payloadNumber(payload, 'percent');
        const message = payloadString(payload, 'message');
        setTimeline((prev) => {
          if (prev.length === 0) return prev;
          const next = [...prev];
          const lastIndex = next.length - 1;
          const target = next[lastIndex];
          if (!target) return prev;
          next[lastIndex] = {
            ...target,
            detail: `${message ?? target.detail ?? ''}${percent !== null ? `（${percent}%）` : ''}`,
          };
          return next;
        });
        break;
      }
      case 'tool.finished': {
        const ok = payload.ok === true;
        const durationMs = payloadNumber(payload, 'duration_ms');
        setTimeline((prev) => {
          const next = [...prev];
          const index = [...next].reverse().findIndex((entry) => entry.status === 'running');
          if (index < 0) return prev;
          const realIndex = next.length - 1 - index;
          const target = next[realIndex];
          if (!target) return prev;
          next[realIndex] = {
            ...target,
            status: ok ? 'done' : 'failed',
            durationMs,
            detail: stringify(payload.result_summary) ?? target.detail,
          };
          return next;
        });
        break;
      }
      case 'todo.updated': {
        const items = payload.items;
        if (Array.isArray(items)) {
          const parsed: AgentTodo[] = [];
          items.forEach((item, index) => {
            if (item && typeof item === 'object') {
              const record = item as Record<string, unknown>;
              const text = typeof record.text === 'string' ? record.text : '';
              const status = record.status;
              if (text) {
                parsed.push({
                  id: typeof record.id === 'string' ? record.id : `todo-${index}`,
                  text,
                  status:
                    status === 'done' || status === 'in_progress' || status === 'pending'
                      ? status
                      : 'pending',
                });
              }
            }
          });
          setTodos(parsed);
        }
        break;
      }
      case 'turn.stopped': {
        const report: StopReport = {
          stopped_steps: Array.isArray(payload.stopped_steps)
            ? payload.stopped_steps.filter((s): s is string => typeof s === 'string')
            : [],
          external_pending: [],
          note: payloadString(payload, 'note'),
        };
        setStopReport(report);
        setPhase('idle');
        setQueuePosition(null);
        setMessages((prev) =>
          prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
        );
        break;
      }
      case 'turn.finished': {
        setPhase('idle');
        setQueuePosition(null);
        setQueuedCount((count) => Math.max(0, count - 1));
        activeTurnId.current = null;
        setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
        setTimeline((prev) => prev.map((t) => (t.status === 'running' ? { ...t, status: 'done' } : t)));
        void queryClient.invalidateQueries({ queryKey: queryKeys.agent.sessions() });
        break;
      }
      case 'turn.failed': {
        const code = payloadString(payload, 'code') ?? 'AGENT_FAILED';
        const hint = payloadString(payload, 'hint');
        setPhase('error');
        setError({ code, message: '本轮执行失败', hint, requestId: null });
        setMessages((prev) => {
          const next = prev.map((m) => (m.streaming ? { ...m, streaming: false, failed: true, errorHint: hint } : m));
          return next;
        });
        setTimeline((prev) => prev.map((t) => (t.status === 'running' ? { ...t, status: 'failed' } : t)));
        break;
      }
      case 'engine.notice': {
        setTimeline((prev) => [
          ...prev,
          {
            id: `notice-${event.seq ?? Date.now()}`,
            kind: 'notice',
            title: '引擎提示',
            detail: stringify(payload) ?? null,
            status: 'done',
            durationMs: null,
            at: event.at,
            raw: payload,
          },
        ]);
        break;
      }
      default:
        break;
    }
  }, [queryClient]);

  React.useEffect(() => {
    if (!sessionId) {
      setSseStatus('closed');
      return;
    }
    const handle = openSse(agentStreamPath(sessionId), {
      onEvent: handleEvent,
      onStatus: setSseStatus,
      onError: (info) => {
        if (info.kind === 'heartbeat') {
          setSseStatus('reconnecting');
        }
      },
    });
    return () => handle.close();
  }, [sessionId, handleEvent]);

  /* ── 操作 ─────────────────────────────────────────── */
  const selectSession = React.useCallback((id: string) => {
    setSessionId(id);
    setMessages([]);
    setTimeline([]);
    setTodos([]);
    setStopReport(null);
    setError(null);
    setPhase('idle');
  }, []);

  const createSession = React.useCallback(async () => {
    const created = await apiPost<AgentSession>('/admin/agent/sessions', {
      title: `新会话 ${formatDateTimeSec(new Date())}`,
      model_profile_id: null,
      permission_profile: {},
    });
    await queryClient.invalidateQueries({ queryKey: queryKeys.agent.sessions() });
    setSessionId(created.id);
    setMessages([]);
    setTimeline([]);
    setTodos([]);
  }, [queryClient]);

  const send = React.useCallback(
    async (text?: string) => {
      const content = (text ?? draft).trim();
      if (!content || !sessionId) return;
      const optimistic: AgentMessage = {
        id: `u-${Date.now()}`,
        role: 'user',
        text: content,
        at: new Date().toISOString(),
        attachments: attachments.length > 0 ? attachments : undefined,
      };
      setMessages((prev) => [...prev, optimistic]);
      setDraft('');
      const sentAttachments = attachments;
      setAttachments([]);
      setError(null);
      setStopReport(null);
      setPhase(phase === 'running' ? 'queued' : 'sending');

      try {
        const result = await apiPost<SendMessageResult>(`/admin/agent/sessions/${sessionId}/messages`, {
          text: content,
          attachments: sentAttachments.map((a) => ({ file_id: a.file_id })),
          links: [],
          queued_ack: true,
        });
        if (result.queue_position && result.queue_position > 0) {
          setQueuePosition(result.queue_position);
          setPhase('queued');
          setQueuedCount((count) => count + 1);
          setMessages((prev) =>
            prev.map((m) => (m.id === optimistic.id ? { ...m, queuePosition: result.queue_position } : m)),
          );
        } else {
          setPhase((current) => (current === 'queued' ? current : 'running'));
        }
      } catch (cause) {
        setPhase('error');
        if (isApiError(cause)) {
          setError({
            code: cause.code,
            message: cause.message,
            hint: cause.hint,
            requestId: cause.requestId,
          });
        } else {
          setError({ code: 'UNKNOWN', message: '发送失败', hint: null, requestId: null });
        }
      }
    },
    [attachments, draft, phase, sessionId],
  );

  const stop = React.useCallback(async () => {
    if (!sessionId) return;
    setPhase('stopping');
    try {
      const report = await apiPost<StopReport>(`/admin/agent/sessions/${sessionId}/stop`, {});
      setStopReport(report);
      setPhase('idle');
      setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
    } catch (cause) {
      setPhase('error');
      if (isApiError(cause)) {
        setError({ code: cause.code, message: cause.message, hint: cause.hint, requestId: cause.requestId });
      }
    }
  }, [sessionId]);

  const retry = React.useCallback(
    async (turnId?: string) => {
      if (!sessionId) return;
      setPhase('sending');
      setError(null);
      try {
        await apiPost(`/admin/agent/sessions/${sessionId}/retry`, {
          turn_id: turnId ?? activeTurnId.current ?? undefined,
        });
        setPhase('running');
      } catch (cause) {
        setPhase('error');
        if (isApiError(cause)) {
          setError({ code: cause.code, message: cause.message, hint: cause.hint, requestId: cause.requestId });
        }
      }
    },
    [sessionId],
  );

  const attachFiles = React.useCallback(async (files: File[]) => {
    for (const file of files) {
      if (file.size > 50 * 1024 * 1024) {
        setError({
          code: 'FILE_TOO_LARGE',
          message: `「${file.name}」超过 50MB 上限`,
          hint: '压缩或拆分后重试',
          requestId: null,
        });
        continue;
      }
      const controller = new AbortController();
      uploadAbort.current = controller;
      // 真实进度：没有 XHR 上传进度时至少不伪造百分比（0 → 100），并让取消真的中断请求
      setUploading({ name: file.name, percent: 0 });
      try {
        const uploaded = await uploadFile(file, { signal: controller.signal });
        setUploading({ name: file.name, percent: 100 });
        setAttachments((prev) => [
          ...prev,
          { file_id: uploaded.file_id, name: file.name, mime: uploaded.mime, size: uploaded.size },
        ]);
      } catch (cause) {
        if (isApiError(cause)) {
          setError({ code: cause.code, message: cause.message, hint: cause.hint, requestId: cause.requestId });
        }
      } finally {
        uploadAbort.current = null;
        setUploading(null);
      }
    }
  }, []);

  const cancelUpload = React.useCallback(() => {
    uploadAbort.current?.abort();
    uploadAbort.current = null;
    setUploading(null);
  }, []);

  const removeAttachment = React.useCallback((fileId: string) => {
    setAttachments((prev) => prev.filter((a) => a.file_id !== fileId));
  }, []);

  const decideApproval = React.useCallback(
    async (id: string, approve: boolean) => {
      await apiPost(`/admin/agent/approvals/${id}/decide`, { approve });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.approvals() });
    },
    [queryClient],
  );

  const runFlowStep = React.useCallback(
    async (flowId: string, step: string) => {
      await apiPost(`/admin/agent/flows/${flowId}/steps/${step}/run`, {});
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.flow(flowId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.flows() });
    },
    [queryClient],
  );

  const publishFlow = React.useCallback(
    async (flowId: string) => {
      await apiPost(`/admin/agent/flows/${flowId}/publish`, {});
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.flow(flowId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.flows() });
    },
    [queryClient],
  );

  const openPanel = React.useCallback(() => setPanelOpen(true), []);
  const closePanel = React.useCallback(() => setPanelOpen(false), []);
  const togglePanel = React.useCallback(() => setPanelOpen((prev) => !prev), []);

  const setSize = React.useCallback((next: AgentPanelSize) => {
    const clamped: AgentPanelSize = {
      width: Math.min(MAX_SIZE.width, Math.max(MIN_SIZE.width, Math.round(next.width))),
      height: Math.min(MAX_SIZE.height, Math.max(MIN_SIZE.height, Math.round(next.height))),
    };
    setSizeState(clamped);
    try {
      window.localStorage.setItem(SIZE_KEY, JSON.stringify(clamped));
    } catch {
      /* ignore */
    }
  }, []);

  const activeFlow = React.useMemo(() => {
    const list = flowsQuery.data ?? [];
    if (activeFlowId) return list.find((flow) => flow.id === activeFlowId) ?? null;
    return list[0] ?? null;
  }, [activeFlowId, flowsQuery.data]);

  const value = React.useMemo<AgentSessionApi>(
    () => ({
      panelOpen,
      openPanel,
      closePanel,
      togglePanel,
      size,
      setSize,
      sessions: sessionsQuery.data ?? [],
      sessionsLoading: sessionsQuery.isLoading,
      sessionId,
      selectSession,
      createSession,
      messages,
      timeline,
      todos,
      phase,
      queuePosition,
      queuedCount,
      error,
      sseStatus,
      stopReport,
      draft,
      setDraft,
      attachments,
      attachFiles,
      removeAttachment,
      uploading,
      cancelUpload,
      send,
      stop,
      retry,
      clearError: () => setError(null),
      approvals: approvalsQuery.data ?? [],
      decideApproval,
      flows: flowsQuery.data ?? [],
      activeFlow,
      selectFlow: setActiveFlowId,
      runFlowStep,
      publishFlow,
    }),
    [
      panelOpen,
      openPanel,
      closePanel,
      togglePanel,
      size,
      setSize,
      sessionsQuery.data,
      sessionsQuery.isLoading,
      sessionId,
      selectSession,
      createSession,
      messages,
      timeline,
      todos,
      phase,
      queuePosition,
      queuedCount,
      error,
      sseStatus,
      stopReport,
      draft,
      attachments,
      attachFiles,
      removeAttachment,
      uploading,
      cancelUpload,
      send,
      stop,
      retry,
      approvalsQuery.data,
      decideApproval,
      flowsQuery.data,
      activeFlow,
      runFlowStep,
      publishFlow,
    ],
  );

  // 该文件是 .ts（按 spec 布局），因此用 createElement 而不是 JSX。
  return React.createElement(AgentContext.Provider, { value }, children);
}

function buildMessagesFromTurns(turns: AgentTurn[]): AgentMessage[] {
  const result: AgentMessage[] = [];
  for (const turn of turns) {
    result.push({
      id: `u-${turn.id}`,
      role: 'user',
      text: turn.user_text,
      at: turn.created_at,
      turnId: turn.id,
      attachments: turn.attachments,
    });
    if (turn.status === 'running' || turn.status === 'queued') {
      result.push({
        id: `a-${turn.id}`,
        role: 'assistant',
        text: '',
        at: turn.created_at,
        turnId: turn.id,
        streaming: turn.status === 'running',
      });
    }
    if (turn.error_code) {
      result.push({
        id: `e-${turn.id}`,
        role: 'system',
        text: `本轮失败：${turn.error_code}`,
        at: turn.finished_at ?? turn.created_at,
        turnId: turn.id,
        failed: true,
        errorHint: turn.error_detail_redacted,
      });
    }
  }
  return result;
}

export function useAgentSession(): AgentSessionApi {
  const context = React.useContext(AgentContext);
  if (!context) {
    throw new Error('useAgentSession 必须在 <AgentProvider> 内使用');
  }
  return context;
}

/** Alt+J 全局开关（spec §13.3：Alt+J 而非 Ctrl+J） */
export function useAgentHotkey(): void {
  const { togglePanel } = useAgentSession();
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.altKey && event.key.toLowerCase() === 'j') {
        event.preventDefault();
        togglePanel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [togglePanel]);
}
