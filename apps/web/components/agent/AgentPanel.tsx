'use client';

import { MessageSquarePlus, Minimize2, Search, SquarePen, X } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Select } from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { formatShortTime } from '@/lib/format';
import { AgentComposer } from './AgentComposer';
import { AgentTimeline } from './AgentTimeline';
import { FlowPanel } from './FlowPanel';
import { useAgentSession } from './useAgentSession';

/**
 * 浮窗 Agent 面板（spec §13.3）：
 * - ≥1280px 固定 420×640；<1024px 全宽抽屉
 * - 可拖拽调整尺寸（localStorage 记忆）
 * - Esc 关闭；焦点陷阱 + aria-modal
 */
const NARROW_BREAKPOINT = 1024;

export function AgentPanel() {
  const {
    panelOpen,
    closePanel,
    size,
    setSize,
    sessions,
    sessionsLoading,
    sessionId,
    selectSession,
    createSession,
    messages,
    phase,
    error,
    clearError,
    stopReport,
    retry,
    sseStatus,
    approvals,
    flows,
    activeFlow,
    selectFlow,
    runFlowStep,
    publishFlow,
    decideApproval,
  } = useAgentSession();

  const panelRef = React.useRef<HTMLDivElement | null>(null);
  const [narrow, setNarrow] = React.useState(false);
  const [sessionSearch, setSessionSearch] = React.useState('');
  const dragState = React.useRef<{ startX: number; startY: number; width: number; height: number } | null>(null);

  React.useEffect(() => {
    const onResize = (): void => setNarrow(window.innerWidth < NARROW_BREAKPOINT);
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // Esc 关闭 + 焦点陷阱（Tab 在面板内循环）
  React.useEffect(() => {
    if (!panelOpen) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        closePanel();
        return;
      }
      if (event.key !== 'Tab') return;
      const root = panelRef.current;
      if (!root) return;
      const focusables = root.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [panelOpen, closePanel]);

  React.useEffect(() => {
    if (panelOpen && panelRef.current) {
      const focusable = panelRef.current.querySelector<HTMLElement>('textarea, button, [tabindex="0"]');
      focusable?.focus();
    }
  }, [panelOpen]);

  const onPointerDown = (event: React.PointerEvent<HTMLButtonElement>): void => {
    event.preventDefault();
    dragState.current = {
      startX: event.clientX,
      startY: event.clientY,
      width: size.width,
      height: size.height,
    };
    const onMove = (moveEvent: PointerEvent): void => {
      const state = dragState.current;
      if (!state) return;
      setSize({
        width: state.width + (state.startX - moveEvent.clientX),
        height: state.height + (state.startY - moveEvent.clientY),
      });
    };
    const onUp = (): void => {
      dragState.current = null;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  const filteredSessions = React.useMemo(() => {
    const term = sessionSearch.trim().toLowerCase();
    const recent = sessions.slice(0, 20);
    if (!term) return recent;
    return recent.filter(
      (session) => session.title.toLowerCase().includes(term) || session.id.toLowerCase().includes(term),
    );
  }, [sessionSearch, sessions]);

  if (!panelOpen) return null;

  return (
    <>
      {/* ≤1024px 的遮罩 */}
      {narrow ? (
        <div
          aria-hidden
          onClick={closePanel}
          className="fixed inset-0 z-panel bg-[rgba(28,26,23,0.36)]"
        />
      ) : null}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Agent 面板"
        style={
          narrow
            ? undefined
            : { width: size.width, height: size.height, right: 24, bottom: 24 }
        }
        className={cn(
          'fixed z-panel flex flex-col border border-border bg-surface sr-enter',
          narrow ? 'inset-0' : 'rounded-md',
        )}
      >
        {/* 头部 */}
        <header className="flex items-center gap-2 border-b border-border px-3 py-2">
          <span className="text-14 font-semibold text-content">Agent</span>
          <Badge
            tone={sseStatus === 'open' ? 'ok' : sseStatus === 'reconnecting' ? 'warn' : 'neutral'}
            dot
          >
            {sseStatus === 'open' ? '已连接' : sseStatus === 'reconnecting' ? '重连中' : '未连接'}
          </Badge>
          {phase === 'running' || phase === 'queued' ? (
            <Badge tone="info" dot>
              {phase === 'queued' ? '排队中' : '执行中'}
            </Badge>
          ) : null}
          <div className="ml-auto flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              aria-label="新建会话"
              onClick={() => void createSession()}
              icon={<SquarePen size={20} strokeWidth={1.75} />}
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label="关闭 Agent 面板（Esc）"
              onClick={closePanel}
              icon={narrow ? <X size={20} strokeWidth={1.75} /> : <Minimize2 size={20} strokeWidth={1.75} />}
            />
          </div>
        </header>

        {/* 会话选择器（最近 20 个，可搜索） */}
        <div className="flex items-center gap-2 border-b border-border px-3 py-2">
          <div className="w-8 shrink-0 text-content-muted">
            <Search size={16} strokeWidth={1.75} aria-hidden />
          </div>
          <input
            value={sessionSearch}
            onChange={(event) => setSessionSearch(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索会话"
            className="h-8 min-w-0 flex-1 rounded border border-border bg-surface px-2 text-13 text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
          <div className="w-[168px] shrink-0">
            <Select
              size="sm"
              aria-label="切换会话"
              value={sessionId ?? undefined}
              placeholder={sessionsLoading ? '加载中…' : '选择会话'}
              onValueChange={selectSession}
              options={filteredSessions.map((session) => ({
                value: session.id,
                label: session.title,
                hint: formatShortTime(session.updated_at),
              }))}
            />
          </div>
        </div>

        {/* 停止报告 */}
        {stopReport ? (
          <div className="border-b border-border bg-[var(--sr-warn-weak)] px-3 py-2 text-12 text-content">
            <p>
              已停止：{stopReport.stopped_steps.length > 0 ? stopReport.stopped_steps.join('、') : '（无已停止步骤）'}
            </p>
            {stopReport.external_pending.length > 0 ? (
              <p className="mt-1">
                可能仍在远端运行：
                {stopReport.external_pending.map((item) => `${item.kind}:${item.ref}`).join('、')}
              </p>
            ) : null}
            <Link href="/tasks" className="mt-1 inline-block text-accent underline-none hover:underline">
              去任务里对账 →
            </Link>
          </div>
        ) : null}

        {/* 错误 */}
        {error ? (
          <div role="alert" className="border-b border-border bg-[var(--sr-danger-weak)] px-3 py-2 text-12 text-content">
            <div className="flex items-start justify-between gap-2">
              <p className="break-words">
                {error.message}
                {error.hint ? <span className="block text-content-muted">{error.hint}</span> : null}
              </p>
              <button
                type="button"
                onClick={clearError}
                aria-label="关闭错误提示"
                className="rounded-sm p-0.5 text-content-muted hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
              >
                <X size={14} strokeWidth={1.75} aria-hidden />
              </button>
            </div>
            {error.requestId ? <RequestIdCopy requestId={error.requestId} className="mt-1" /> : null}
            <div className="mt-1 flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => void retry()}>
                重试此步
              </Button>
            </div>
          </div>
        ) : null}

        {/* 主体：对话 / 进度 / 流程 */}
        <Tabs defaultValue="chat" className="flex min-h-0 flex-1 flex-col">
          <TabsList className="mx-3 mt-2">
            <TabsTrigger value="chat">对话</TabsTrigger>
            <TabsTrigger value="timeline">进度</TabsTrigger>
            <TabsTrigger value="flow">流程</TabsTrigger>
          </TabsList>

          <TabsContent value="chat" className="min-h-0 flex-1 overflow-y-auto px-3 sr-scroll">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-8 text-center">
                <MessageSquarePlus size={24} strokeWidth={1.75} className="text-content-muted" aria-hidden />
                <p className="text-13 text-content-muted">
                  描述你要接入的能力，或粘贴接口文档链接。Agent 会先解析再给你可复核的草稿。
                </p>
              </div>
            ) : (
              <ul className="flex flex-col gap-2 py-2">
                {messages.map((message) => (
                  <li
                    key={message.id}
                    className={cn(
                      'rounded border px-3 py-2 text-13',
                      message.role === 'user'
                        ? 'border-border bg-[var(--sr-surface-sunken)] text-content'
                        : message.role === 'system'
                          ? 'border-danger bg-[var(--sr-danger-weak)] text-content'
                          : 'border-border bg-surface text-content',
                    )}
                  >
                    <div className="mb-1 flex items-center gap-2 text-12 text-content-muted">
                      <span>{message.role === 'user' ? '我' : message.role === 'assistant' ? 'Agent' : '系统'}</span>
                      <span>· {formatShortTime(message.at)}</span>
                      {message.queuePosition ? (
                        <Badge tone="warn">排队中（第 {message.queuePosition} 条）</Badge>
                      ) : null}
                      {message.streaming ? <Badge tone="info" dot>生成中</Badge> : null}
                    </div>
                    <p className="whitespace-pre-wrap break-words">{message.text || (message.streaming ? '…' : '')}</p>
                    {message.errorHint ? (
                      <p className="mt-1 text-12 text-danger">{message.errorHint}</p>
                    ) : null}
                    {message.attachments && message.attachments.length > 0 ? (
                      <ul className="mt-1 flex flex-wrap gap-1">
                        {message.attachments.map((attachment) => (
                          <li key={attachment.file_id}>
                            <Badge tone="outline">{attachment.name ?? attachment.file_id}</Badge>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </TabsContent>

          <TabsContent value="timeline" className="min-h-0 flex-1 overflow-y-auto px-3 sr-scroll">
            <AgentTimeline />
          </TabsContent>

          <TabsContent value="flow" className="min-h-0 flex-1 overflow-y-auto px-3 sr-scroll">
            {flows.length > 1 ? (
              <div className="mb-2">
                <Select
                  size="sm"
                  aria-label="选择接入流程"
                  value={activeFlow?.id ?? undefined}
                  onValueChange={(value) => selectFlow(value)}
                  options={flows.map((flow) => ({ value: flow.id, label: flow.title }))}
                />
              </div>
            ) : null}
            <FlowPanel
              flow={activeFlow}
              approvals={approvals}
              onRunStep={runFlowStep}
              onPublish={publishFlow}
              onDecide={decideApproval}
              compact
            />
          </TabsContent>
        </Tabs>

        <AgentComposer />

        {/* 拖拽调整尺寸（仅宽屏） */}
        {!narrow ? (
          <button
            type="button"
            aria-label="调整面板尺寸（拖动）"
            onPointerDown={onPointerDown}
            className="absolute left-0 top-0 h-4 w-4 cursor-nwse-resize rounded-tl-md border-l border-t border-transparent hover:border-accent focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        ) : null}
      </div>
    </>
  );
}
