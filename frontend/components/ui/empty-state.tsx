import type { ReactNode } from 'react';
import { Inbox } from 'lucide-react';

import { cn } from '@/lib/utils';

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  secondaryAction?: ReactNode;
  className?: string;
}

/** Shown instead of an empty table body — always offers the next step. */
export function EmptyState({ title, description, icon, action, secondaryAction, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <span className="flex h-10 w-10 items-center justify-center rounded-pill bg-info-soft text-muted">
        {icon ?? <Inbox aria-hidden className="h-5 w-5" />}
      </span>
      <p className="text-sm font-medium text-text">{title}</p>
      {description ? <p className="max-w-md text-xs text-muted">{description}</p> : null}
      {action || secondaryAction ? (
        <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
          {action}
          {secondaryAction}
        </div>
      ) : null}
    </div>
  );
}
