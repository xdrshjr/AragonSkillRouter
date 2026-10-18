'use client';

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import * as React from 'react';
import { cn } from './cn';

export const TooltipProvider = TooltipPrimitive.Provider;

export interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: 'top' | 'right' | 'bottom' | 'left';
  delayMs?: number;
  className?: string;
}

/** 提示气泡：同时可由键盘聚焦触发，内容不承载唯一关键信息。 */
export function Tooltip({ content, children, side = 'top', delayMs = 200, className }: TooltipProps) {
  return (
    <TooltipPrimitive.Root delayDuration={delayMs}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className={cn(
            'z-toast max-w-[280px] rounded-sm border border-border bg-surface px-2 py-1 text-12 text-content',
            'sr-enter',
            className,
          )}
        >
          {content}
          <TooltipPrimitive.Arrow className="fill-[var(--sr-border)]" width={8} height={4} />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
