'use client';

import { MessageCircle, X } from 'lucide-react';
import * as React from 'react';
import { cn } from '@/components/ui/cn';
import { Tooltip } from '@/components/ui/tooltip';
import { useAgentHotkey, useAgentSession } from './useAgentSession';

/**
 * 固定入口（spec §13.3）：56×56，距边 24px，aria-label="打开 Agent"，Alt+J 开关。
 * 不覆盖表格操作区/分页控件：主内容区已预留 24px 安全边距（见 styles/globals.css）。
 */
export function AgentDock() {
  const { panelOpen, togglePanel, openPanel, approvals, phase } = useAgentSession();
  useAgentHotkey();

  const pending = approvals.filter((approval) => approval.status === 'pending').length;
  const running = phase === 'running' || phase === 'queued';

  if (panelOpen) {
    // 面板打开时，用一个等大的关闭按钮（保持位置稳定，避免布局跳动）
    return (
      <button
        type="button"
        onClick={togglePanel}
        aria-label="关闭 Agent（Alt+J）"
        className={cn(
          'fixed bottom-6 right-6 z-dock flex h-14 w-14 items-center justify-center rounded-full',
          'border border-border bg-surface text-content-muted hover:bg-hover hover:text-content',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
          'transition-colors duration-150 ease-sr',
        )}
      >
        <X size={24} strokeWidth={1.75} aria-hidden />
      </button>
    );
  }

  return (
    <Tooltip content="打开 Agent（Alt+J）" side="left">
      <button
        type="button"
        onClick={openPanel}
        aria-label="打开 Agent"
        aria-keyshortcuts="Alt+J"
        className={cn(
          'fixed bottom-6 right-6 z-dock flex h-14 w-14 items-center justify-center rounded-full',
          'border border-accent bg-accent text-white',
          'hover:bg-[color-mix(in_srgb,var(--sr-accent)_88%,black)]',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
          'transition-colors duration-150 ease-sr',
        )}
      >
        <MessageCircle size={24} strokeWidth={1.75} aria-hidden />
        {running ? (
          <span
            aria-hidden
            className="absolute right-1 top-1 h-2.5 w-2.5 rounded-full border border-surface bg-info"
          />
        ) : null}
        {pending > 0 ? (
          <>
            <span
              aria-hidden
              className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full border border-surface bg-danger px-1 text-12 font-medium text-white"
            >
              {pending}
            </span>
            <span className="sr-only">有 {pending} 个待审批请求</span>
          </>
        ) : null}
      </button>
    </Tooltip>
  );
}
