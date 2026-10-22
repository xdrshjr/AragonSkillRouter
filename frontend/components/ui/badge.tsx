import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/utils';
import { statusLabel, statusTone, type StatusTone } from '@/lib/format';

const TONE_CLASSES: Record<StatusTone, string> = {
  neutral: 'border-border-strong bg-info-soft text-muted',
  accent: 'border-accent/30 bg-accent-soft text-accent-strong',
  success: 'border-success/25 bg-success-soft text-success',
  warning: 'border-warning/25 bg-warning-soft text-warning',
  danger: 'border-danger/25 bg-danger-soft text-danger',
  info: 'border-border bg-surface text-text',
};

const DOT_CLASSES: Record<StatusTone, string> = {
  neutral: 'bg-muted',
  accent: 'bg-accent',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-muted',
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: StatusTone;
  dot?: boolean;
  children: ReactNode;
}

export function Badge({ tone = 'neutral', dot = true, className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-pill border px-2 py-0.5 text-xs font-medium leading-4',
        TONE_CLASSES[tone],
        className,
      )}
      {...rest}
    >
      {dot ? <span aria-hidden className={cn('h-1.5 w-1.5 rounded-pill', DOT_CLASSES[tone])} /> : null}
      {children}
    </span>
  );
}

export interface StatusBadgeProps {
  /** Any status string from CONTRACT §4. */
  status: string | null | undefined;
  /** Overrides the human label derived from the status. */
  label?: string;
  dot?: boolean;
  className?: string;
}

/** Badge whose colour and label come from the shared status vocabulary. */
export function StatusBadge({ status, label, dot = true, className }: StatusBadgeProps) {
  return (
    <Badge tone={statusTone(status)} dot={dot} className={className}>
      {label ?? statusLabel(status)}
    </Badge>
  );
}
