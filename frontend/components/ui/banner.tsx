'use client';

import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Button } from './button';

export type BannerTone = 'info' | 'success' | 'warning' | 'danger';

export interface BannerAction {
  label: string;
  onClick: () => void;
  loading?: boolean;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
}

export interface BannerProps {
  tone?: BannerTone;
  title: ReactNode;
  children?: ReactNode;
  /** Actionable next steps straight from the error envelope. */
  actions?: string[];
  requestId?: string | null;
  primaryAction?: BannerAction;
  onDismiss?: () => void;
  className?: string;
}

const TONES: Record<BannerTone, { wrapper: string; icon: ReactNode }> = {
  info: { wrapper: 'border-border-strong bg-accent-soft text-text', icon: <Info aria-hidden className="h-4 w-4 text-accent" /> },
  success: {
    wrapper: 'border-success/30 bg-success-soft text-text',
    icon: <CheckCircle2 aria-hidden className="h-4 w-4 text-success" />,
  },
  warning: {
    wrapper: 'border-warning/30 bg-warning-soft text-text',
    icon: <AlertTriangle aria-hidden className="h-4 w-4 text-warning" />,
  },
  danger: {
    wrapper: 'border-danger/30 bg-danger-soft text-text',
    icon: <XCircle aria-hidden className="h-4 w-4 text-danger" />,
  },
};

/** Inline alert used for load failures, publish blockers and first-run notices. */
export function Banner({
  tone = 'info',
  title,
  children,
  actions,
  requestId,
  primaryAction,
  onDismiss,
  className,
}: BannerProps) {
  const style = TONES[tone];
  return (
    <div
      role={tone === 'danger' || tone === 'warning' ? 'alert' : 'status'}
      className={cn('flex items-start gap-3 rounded-card border p-3 text-sm', style.wrapper, className)}
    >
      <span className="mt-0.5 shrink-0">{style.icon}</span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="font-medium">{title}</div>
        {children ? <div className="text-xs text-text/80">{children}</div> : null}
        {actions && actions.length > 0 ? (
          <ul className="list-disc space-y-0.5 pl-4 text-xs text-text/80">
            {actions.map((action) => (
              <li key={action}>{action}</li>
            ))}
          </ul>
        ) : null}
        {requestId ? <p className="select-all font-mono text-[11px] text-muted">request {requestId}</p> : null}
        {primaryAction ? (
          <div className="pt-1">
            <Button
              size="sm"
              variant={primaryAction.variant ?? 'secondary'}
              onClick={primaryAction.onClick}
              loading={primaryAction.loading}
            >
              {primaryAction.label}
            </Button>
          </div>
        ) : null}
      </div>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss message"
          className="rounded-control p-1 text-muted transition-colors duration-150 hover:bg-surface hover:text-text"
        >
          <X aria-hidden className="h-3.5 w-3.5" />
        </button>
      ) : null}
    </div>
  );
}

/** Compact single-line variant for form-level problems. */
export function InlineAlert({
  tone = 'danger',
  children,
  className,
}: {
  tone?: BannerTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <p
      role={tone === 'danger' ? 'alert' : 'status'}
      className={cn(
        'rounded-control border px-2 py-1 text-xs',
        tone === 'danger' && 'border-danger/30 bg-danger-soft text-danger',
        tone === 'warning' && 'border-warning/30 bg-warning-soft text-warning',
        tone === 'success' && 'border-success/30 bg-success-soft text-success',
        tone === 'info' && 'border-border-strong bg-info-soft text-muted',
        className,
      )}
    >
      {children}
    </p>
  );
}
