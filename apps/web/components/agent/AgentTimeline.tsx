'use client';

import { CheckCircle2, FileSearch, FlaskConical, HelpCircle, Info, Loader2, XCircle } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/components/ui/cn';
import { formatDuration, formatShortTime } from '@/lib/format';
import { useAgentSession, type TimelineEntry, type TimelineKind } from './useAgentSession';

/**
 * Agent 时间线（spec §13.3）：把工具与思考事件归到四类——
 *   正在读取的资料 / 配置变更 / 测试结果 / 待补信息
 * 每条可展开证据（原始事件），工具显示状态与耗时。
 */

const KIND_META: Record<
  TimelineKind,
  { label: string; icon: React.ReactNode; tone: 'info' | 'accent' | 'ok' | 'warn' | 'neutral' }
> = {
  reading: { label: '正在读取的资料', icon: <FileSearch size={14} strokeWidth={1.75} aria-hidden />, tone: 'info' },
  change: { label: '配置变更', icon: <CheckCircle2 size={14} strokeWidth={1.75} aria-hidden />, tone: 'accent' },
  test: { label: '测试结果', icon: <FlaskConical size={14} strokeWidth={1.75} aria-hidden />, tone: 'ok' },
  missing: { label: '待补信息', icon: <HelpCircle size={14} strokeWidth={1.75} aria-hidden />, tone: 'warn' },
  notice: { label: '引擎提示', icon: <Info size={14} strokeWidth={1.75} aria-hidden />, tone: 'neutral' },
};

const ORDER: TimelineKind[] = ['reading', 'change', 'test', 'missing', 'notice'];

function StatusIcon({ status }: { status: TimelineEntry['status'] }) {
  if (status === 'running') {
    return <Loader2 size={14} strokeWidth={1.75} className="animate-spin text-info" aria-label="运行中" />;
  }
  if (status === 'failed') return <XCircle size={14} strokeWidth={1.75} className="text-danger" aria-label="失败" />;
  return <CheckCircle2 size={14} strokeWidth={1.75} className="text-ok" aria-label="完成" />;
}

function EntryRow({ entry }: { entry: TimelineEntry }) {
  const [open, setOpen] = React.useState(false);
  const hasEvidence = Boolean(entry.raw) || Boolean(entry.detail);
  return (
    <li className="rounded-sm border border-border bg-surface">
      <div className="flex items-start gap-2 px-2 py-2">
        <StatusIcon status={entry.status} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-13 text-content">{entry.title}</p>
          <p className="mt-0.5 flex flex-wrap items-center gap-2 text-12 text-content-muted">
            <span>{formatShortTime(entry.at)}</span>
            {entry.durationMs !== null ? <span>· {formatDuration(entry.durationMs)}</span> : null}
            {entry.status === 'running' ? <span>· 运行中</span> : null}
            {entry.status === 'failed' ? <span className="text-danger">· 失败</span> : null}
          </p>
          {entry.detail && !open ? (
            <p className="mt-1 line-clamp-2 break-words text-12 text-content-muted">{entry.detail}</p>
          ) : null}
          {open && entry.detail ? (
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
              {entry.detail}
            </pre>
          ) : null}
        </div>
        {hasEvidence ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="shrink-0 rounded-sm border border-border px-1.5 py-0.5 text-12 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          >
            {open ? '收起证据' : '查看证据'}
          </button>
        ) : null}
      </div>
    </li>
  );
}

export function AgentTimeline({ className }: { className?: string }) {
  const { timeline, todos } = useAgentSession();
  const [filter, setFilter] = React.useState<TimelineKind | 'all'>('all');

  const grouped = React.useMemo(() => {
    const map = new Map<TimelineKind, TimelineEntry[]>();
    for (const entry of timeline) {
      const list = map.get(entry.kind) ?? [];
      list.push(entry);
      map.set(entry.kind, list);
    }
    return map;
  }, [timeline]);

  const visibleKinds = filter === 'all' ? ORDER : [filter];

  if (timeline.length === 0 && todos.length === 0) {
    return (
      <p className={cn('px-1 py-2 text-13 text-content-muted', className)}>
        还没有进度条目。发送需求后，这里会逐条显示读取的资料、配置变更与测试结果。
      </p>
    );
  }

  return (
    <section className={cn('flex flex-col gap-2', className)} aria-label="Agent 进度时间线">
      <div role="group" aria-label="按类别筛选" className="flex flex-wrap gap-1">
        <button
          type="button"
          aria-pressed={filter === 'all'}
          onClick={() => setFilter('all')}
          className={cn(
            'rounded-sm border px-1.5 py-0.5 text-12 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
            filter === 'all'
              ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
              : 'border-border text-content-muted hover:bg-hover',
          )}
        >
          全部
        </button>
        {ORDER.map((kind) => {
          const count = grouped.get(kind)?.length ?? 0;
          if (count === 0) return null;
          return (
            <button
              key={kind}
              type="button"
              aria-pressed={filter === kind}
              onClick={() => setFilter(kind)}
              className={cn(
                'rounded-sm border px-1.5 py-0.5 text-12 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                filter === kind
                  ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
                  : 'border-border text-content-muted hover:bg-hover',
              )}
            >
              {KIND_META[kind].label} {count}
            </button>
          );
        })}
      </div>

      {todos.length > 0 ? (
        <div className="rounded-sm border border-border bg-surface px-2 py-2">
          <h4 className="text-12 font-medium text-content-muted">计划（TODO）</h4>
          <ul className="mt-1 space-y-1">
            {todos.map((todo) => (
              <li key={todo.id} className="flex items-start gap-2 text-13">
                <span aria-hidden className="mt-1.5">
                  {todo.status === 'done' ? '✓' : todo.status === 'in_progress' ? '◐' : '○'}
                </span>
                <span className={cn('text-content', todo.status === 'done' && 'text-content-muted line-through')}>
                  {todo.text}
                </span>
                <span className="sr-only">
                  {todo.status === 'done' ? '已完成' : todo.status === 'in_progress' ? '进行中' : '待处理'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {visibleKinds.map((kind) => {
        const entries = grouped.get(kind) ?? [];
        if (entries.length === 0) return null;
        const meta = KIND_META[kind];
        return (
          <div key={kind} className="flex flex-col gap-1">
            <h4 className="flex items-center gap-1.5 text-12 font-medium text-content-muted">
              <span className={kind === 'missing' ? 'text-warn' : 'text-content-muted'}>{meta.icon}</span>
              {meta.label}
              <Badge tone={meta.tone}>{entries.length}</Badge>
            </h4>
            <ul className="flex flex-col gap-1">
              {entries.map((entry) => (
                <EntryRow key={entry.id} entry={entry} />
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}
