'use client';

import { useId, useState, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: 'top' | 'bottom';
  className?: string;
}

/**
 * Hover/focus tooltip. The trigger is wrapped in a focusable span so keyboard
 * users get the same hint: `aria-describedby` points at the tooltip element.
 */
export function Tooltip({ content, children, side = 'top', className }: TooltipProps) {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();
  return (
    <span
      className={cn('relative inline-flex', className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <span tabIndex={0} aria-describedby={open ? tooltipId : undefined} className="inline-flex rounded-control">
        {children}
      </span>
      {open ? (
        <span
          role="tooltip"
          id={tooltipId}
          className={cn(
            'pointer-events-none absolute z-40 w-max max-w-[260px] rounded-control border border-border bg-surface px-2 py-1',
            'text-xs leading-4 text-text shadow-overlay animate-fade-in',
            side === 'top' ? 'bottom-full left-1/2 mb-1.5 -translate-x-1/2' : 'top-full left-1/2 mt-1.5 -translate-x-1/2',
          )}
        >
          {content}
        </span>
      ) : null}
    </span>
  );
}
