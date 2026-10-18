'use client';

import { MessageSquarePlus, Plus, RefreshCw } from 'lucide-react';
import * as React from 'react';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { AgentTimeline } from '@/components/agent/AgentTimeline';
import { AgentComposer } from '@/components/agent/AgentComposer';
import { FlowPanel } from '@/components/agent/FlowPanel';
import { useAgentSession } from '@/components/agent/useAgentSession';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Skeleton } from '@/components/ui/skeleton';
import { formatRelative, formatShortTime } from '@/lib/format';

/**
 * Agent 工作台（spec §13.2 `/agent`）：
 * 左：会话列表；中：对话与时间线；右：接入流程七步 + 待审批卡片。
 * 与浮窗共用同一会话状态（AgentProvider 在 (app)/layout），因此两处不重复订阅 SSE。
 */
export default function AgentPage() {
  const {
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

  const scrollRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Agent 工作台"
        description="让 Agent 读文档、生成草稿、跑测试并留证据；涉及越权或超预算的操作会先请求你审批。"
        status={
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={sseStatus === 'open' ? 'ok' : sseStatus === 'reconnecting' ? 'warn' : 'neutral'} dot>
              事件流：{sseStatus === 'open' ? '已连接' : sseStatus === 'reconnecting' ? '重连中' : '未连接'}
            </Badge>
            <Badge tone={phase === 'running' ? 'info' : 'neutral'} dot>
              {phase === 'running' ? '执行中' : phase === 'queued' ? '排队中' : '空闲'}
            </Badge>
            <Badge tone="neutral">待审批 {approvals.filter((item) => item.status === 'pending').length}</Badge>
          </div>
        }
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={() => void createSession()} icon={<Plus size={16} strokeWidth={1.75} />}>
              新建会话
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void retry()}
              disabled={!sessionId || phase === 'running'}
              icon={<RefreshCw size={16} strokeWidth={1.75} />}
            >
              重试上一轮
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[220px_minmax(0,1fr)_340px]">
        {/* 会话列表 */}
        <aside aria-label="会话列表" className="sr-card p-2">
          <h2 className="px-1 py-1 text-12 font-medium text-content-muted">最近会话</h2>
          {sessionsLoading ? (
            <div className="flex flex-col gap-1">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} height={32} />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <p className="px-1 py-2 text-12 text-content-muted">还没有会话。点「新建会话」开始。</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {sessions.slice(0, 20).map((session) => {
                const active = session.id === sessionId;
                return (
                  <li key={session.id}>
                    <button
                      type="button"
                      onClick={() => selectSession(session.id)}
                      aria-current={active ? 'true' : undefined}
                      className={cn(
                        'flex w-full flex-col items-start gap-0.5 rounded-sm px-2 py-1.5 text-left',
                        'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                        active ? 'bg-[var(--sr-accent-weak)] text-accent' : 'text-content hover:bg-hover',
                      )}
                    >
                      <span className="line-clamp-1 text-13">{session.title}</span>
                      <span className="text-12 text-content-muted">
                        {formatRelative(session.updated_at)} · {session.turn_count} 轮
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* 对话 */}
        <section aria-label="对话" className="sr-card flex min-h-[520px] flex-col">
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 sr-scroll">
            {error ? (
              <div role="alert" className="mb-2 rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-13 text-content">{error.message}</p>
                  <button
                    type="button"
                    onClick={clearError}
                    className="text-12 text-content-muted hover:text-content"
                    aria-label="关闭错误"
                  >
                    关闭
                  </button>
                </div>
                {error.hint ? <p className="mt-1 text-12 text-content-muted">{error.hint}</p> : null}
                {error.requestId ? <RequestIdCopy requestId={error.requestId} className="mt-1" /> : null}
                <Button variant="secondary" size="sm" className="mt-2" onClick={() => void retry()}>
                  重试此步
                </Button>
              </div>
            ) : null}

            {stopReport ? (
              <div className="mb-2 rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2 text-12 text-content">
                <p>已停止：{stopReport.stopped_steps.join('、') || '（无已停止步骤）'}</p>
                {stopReport.external_pending.length > 0 ? (
                  <p className="mt-1">
                    可能仍在远端运行：
                    {stopReport.external_pending.map((item) => `${item.kind}:${item.ref}`).join('、')}
                  </p>
                ) : null}
                <a href="/tasks" className="mt-1 inline-block text-accent hover:underline">
                  去任务里对账 →
                </a>
              </div>
            ) : null}

            {messages.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-16 text-center">
                <MessageSquarePlus size={24} strokeWidth={1.75} className="text-content-muted" aria-hidden />
                <p className="max-w-[420px] text-13 text-content-muted">
                  例如：「把这个 OpenAPI 文档接成 text.generate 能力，先给我草稿和测试结果」。
                  Agent 会先解析再产出可复核的变更，不会直接替你做危险操作。
                </p>
              </div>
            ) : (
              <ul className="flex flex-col gap-2">
                {messages.map((message) => (
                  <li
                    key={message.id}
                    className={cn(
                      'rounded border px-3 py-2 text-13',
                      message.role === 'user'
                        ? 'border-border bg-[var(--sr-surface-sunken)]'
                        : message.role === 'system'
                          ? 'border-danger bg-[var(--sr-danger-weak)]'
                          : 'border-border bg-surface',
                    )}
                  >
                    <div className="mb-1 flex flex-wrap items-center gap-2 text-12 text-content-muted">
                      <span>{message.role === 'user' ? '我' : message.role === 'assistant' ? 'Agent' : '系统'}</span>
                      <span>· {formatShortTime(message.at)}</span>
                      {message.queuePosition ? <Badge tone="warn">排队中（第 {message.queuePosition} 条）</Badge> : null}
                      {message.streaming ? <Badge tone="info" dot>生成中</Badge> : null}
                      {message.failed ? <Badge tone="danger">失败</Badge> : null}
                    </div>
                    <p className="whitespace-pre-wrap break-words text-content">
                      {message.text || (message.streaming ? '…' : '')}
                    </p>
                    {message.errorHint ? <p className="mt-1 text-12 text-danger">{message.errorHint}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <AgentComposer />
        </section>

        {/* 右：流程 + 审批 + 时间线 */}
        <aside aria-label="接入流程" className="flex flex-col gap-4">
          <section className="sr-card p-3">
            {flows.length > 1 ? (
              <label className="mb-2 flex flex-col gap-1">
                <span className="text-12 text-content-muted">选择流程</span>
                <select
                  value={activeFlow?.id ?? ''}
                  onChange={(event) => selectFlow(event.target.value || null)}
                  aria-label="选择接入流程"
                  className="h-8 w-full rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                >
                  {flows.map((flow) => (
                    <option key={flow.id} value={flow.id}>
                      {flow.title}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <FlowPanel
              flow={activeFlow}
              approvals={approvals}
              onRunStep={runFlowStep}
              onPublish={publishFlow}
              onDecide={decideApproval}
            />
          </section>

          <section className="sr-card p-3">
            <h2 className="mb-2 text-13 font-medium text-content">进度时间线</h2>
            <AgentTimeline />
          </section>
        </aside>
      </div>
    </div>
  );
}
