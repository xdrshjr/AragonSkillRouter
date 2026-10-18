'use client';

import {
  Ban,
  CloudOff,
  Inbox,
  Plus,
  PlugZap,
  RotateCw,
  ServerCrash,
} from 'lucide-react';
import * as React from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';

/**
 * 五种空/异常态（spec §8.4）：首次使用 / 空数据 / 无权限 / 网络中断 / 服务不可用。
 * 每种都给出**可执行的下一步**，而不是一句「暂无数据」。
 */
export type EmptyKind = 'first-use' | 'empty' | 'no-permission' | 'offline' | 'unavailable';

export interface EmptyStateProps {
  kind: EmptyKind;
  title?: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
  /** 无权限时说明需要什么角色/配置 */
  requirement?: string;
  /** 服务不可用/未知错误时展示 request_id 与复制按钮 */
  requestId?: string | null;
  className?: string;
  compact?: boolean;
}

const defaults: Record<EmptyKind, { title: string; description: string }> = {
  'first-use': {
    title: '还没有配置任何内容',
    description: '按三步开始：添加提供商凭证 → 创建能力 → 跑一次真实测试。',
  },
  empty: {
    title: '没有符合条件的记录',
    description: '调整搜索词或筛选条件后重试；也可以清空全部筛选。',
  },
  'no-permission': {
    title: '没有查看该内容的权限',
    description: '当前会话缺少所需角色或作用域，请联系管理员授予后再试。',
  },
  offline: {
    title: '连接已断开',
    description: '正在自动退避重连。恢复后列表会自动刷新，无需手动操作。',
  },
  unavailable: {
    title: '服务暂时不可用',
    description: '平台可能正在维护或进程未启动。稍后重试；若持续失败请带上 request_id 报障。',
  },
};

const icons: Record<EmptyKind, React.ReactNode> = {
  'first-use': <PlugZap size={24} strokeWidth={1.75} aria-hidden />,
  empty: <Inbox size={24} strokeWidth={1.75} aria-hidden />,
  'no-permission': <Ban size={24} strokeWidth={1.75} aria-hidden />,
  offline: <CloudOff size={24} strokeWidth={1.75} aria-hidden />,
  unavailable: <ServerCrash size={24} strokeWidth={1.75} aria-hidden />,
};

export function EmptyState({
  kind,
  title,
  description,
  actionLabel,
  onAction,
  requirement,
  requestId,
  className,
  compact,
}: EmptyStateProps) {
  const fallback = defaults[kind];
  return (
    <div
      role="status"
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface text-center',
        compact ? 'px-4 py-6' : 'px-6 py-12',
        className,
      )}
    >
      <span className="text-content-muted">{icons[kind]}</span>
      <div className="max-w-[520px]">
        <p className="text-14 font-medium text-content">{title ?? fallback.title}</p>
        <p className="mt-1 text-13 text-content-muted">{description ?? fallback.description}</p>
        {requirement ? (
          <p className="mt-2 text-12 text-content-muted">需要：{requirement}</p>
        ) : null}
        {requestId ? <RequestIdCopy requestId={requestId} className="mt-2 justify-center" /> : null}
      </div>
      <div className="flex items-center gap-2">
        {onAction ? (
          <Button
            variant={kind === 'first-use' ? 'primary' : 'secondary'}
            size="sm"
            onClick={onAction}
            icon={kind === 'first-use' ? <Plus size={16} strokeWidth={1.75} /> : <RotateCw size={16} strokeWidth={1.75} />}
          >
            {actionLabel ?? (kind === 'first-use' ? '开始配置' : '重试')}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** request_id + 一键复制（错误定位必需） */
export function RequestIdCopy({
  requestId,
  className,
}: {
  requestId: string;
  className?: string;
}) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const copy = async (): Promise<void> => {
    const { copyText } = await import('@/lib/format');
    const ok = await copyText(requestId);
    setCopied(ok);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className={cn('flex flex-wrap items-center gap-2 text-12 text-content-muted', className)}>
      <span>
        request_id: <code className="sr-mono select-all text-content">{requestId}</code>
      </span>
      <button
        type="button"
        onClick={copy}
        aria-label="复制 request_id"
        className="rounded-sm border border-border px-1.5 py-0.5 text-12 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
      >
        {copied ? '已复制' : '复制'}
      </button>
      <span aria-live="polite" className="sr-only">
        {copied ? 'request_id 已复制到剪贴板' : ''}
      </span>
    </div>
  );
}
