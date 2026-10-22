import type { ReactNode } from 'react';
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { StatusTone } from '@/lib/format';

export interface StatProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: StatusTone;
  icon?: ReactNode;
  /** Optional trend indicator; `null` renders a neutral dash, never a fake 0. */
  trend?: 'up' | 'down' | 'flat' | null;
  className?: string;
}

const TONE_TEXT: Record<StatusTone, string> = {
  neutral: 'text-text',
  accent: 'text-accent-strong',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  info: 'text-text',
};

/** Compact metric tile used on the overview. */
export function Stat({ label, value, hint, tone = 'neutral', icon, trend = null, className }: StatProps) {
  return (
    <div className={cn('rounded-card border border-border bg-surface p-4', className)}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
        {icon ? <span className="text-muted">{icon}</span> : null}
      </div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span className={cn('text-xl font-semibold tabular-nums', TONE_TEXT[tone])}>{value}</span>
        {trend === 'up' ? (
          <ArrowUpRight aria-hidden className="h-4 w-4 text-success" />
        ) : trend === 'down' ? (
          <ArrowDownRight aria-hidden className="h-4 w-4 text-danger" />
        ) : trend === 'flat' ? (
          <Minus aria-hidden className="h-4 w-4 text-muted" />
        ) : null}
      </div>
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4', className)}>{children}</div>
  );
}
