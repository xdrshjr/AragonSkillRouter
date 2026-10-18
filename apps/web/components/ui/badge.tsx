import * as React from 'react';
import { cn } from './cn';

export type BadgeTone =
  | 'neutral'
  | 'accent'
  | 'ok'
  | 'warn'
  | 'danger'
  | 'info'
  | 'outline';

const tones: Record<BadgeTone, string> = {
  neutral: 'bg-[var(--sr-surface-sunken)] text-content-muted border-border',
  accent: 'bg-[var(--sr-accent-weak)] text-accent border-[color-mix(in_srgb,var(--sr-accent)_35%,transparent)]',
  ok: 'bg-[var(--sr-ok-weak)] text-ok border-[color-mix(in_srgb,var(--sr-ok)_35%,transparent)]',
  warn: 'bg-[var(--sr-warn-weak)] text-warn border-[color-mix(in_srgb,var(--sr-warn)_35%,transparent)]',
  danger: 'bg-[var(--sr-danger-weak)] text-danger border-[color-mix(in_srgb,var(--sr-danger)_35%,transparent)]',
  info: 'bg-[var(--sr-info-weak)] text-info border-[color-mix(in_srgb,var(--sr-info)_35%,transparent)]',
  outline: 'bg-transparent text-content-muted border-border',
};

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  /** 状态点（颜色之外的第二种编码，避免只靠颜色） */
  dot?: boolean;
  mono?: boolean;
}

const dotColor: Record<BadgeTone, string> = {
  neutral: 'bg-content-muted',
  accent: 'bg-accent',
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  info: 'bg-info',
  outline: 'bg-content-muted',
};

export function Badge({ className, tone = 'neutral', dot, mono, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm border px-1.5 py-0.5 text-12 leading-5',
        tones[tone],
        mono && 'font-mono',
        className,
      )}
      {...props}
    >
      {dot ? (
        <span aria-hidden className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dotColor[tone])} />
      ) : null}
      {children}
    </span>
  );
}
