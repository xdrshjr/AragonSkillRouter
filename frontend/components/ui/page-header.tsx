import Link from 'next/link';
import type { ReactNode } from 'react';
import { ChevronLeft } from 'lucide-react';

import { cn } from '@/lib/utils';

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  /** Primary action(s); the first one should be the main call to action. */
  actions?: ReactNode;
  meta?: ReactNode;
  backHref?: string;
  backLabel?: string;
  className?: string;
}

/** Title block used by every page: title, description, primary action, breadcrumb. */
export function PageHeader({ title, description, actions, meta, backHref, backLabel, className }: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-3 pb-4', className)}>
      {backHref ? (
        <Link
          href={backHref}
          className="inline-flex w-fit items-center gap-1 text-xs text-muted transition-colors duration-150 hover:text-text hover:no-underline"
        >
          <ChevronLeft aria-hidden className="h-3.5 w-3.5" />
          {backLabel ?? 'Back'}
        </Link>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-text">{title}</h1>
          {description ? <div className="mt-1 max-w-3xl text-sm text-muted">{description}</div> : null}
          {meta ? <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">{meta}</div> : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}

export function SectionHeading({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-wrap items-end justify-between gap-2', className)}>
      <div>
        <h2 className="text-base font-semibold text-text">{title}</h2>
        {description ? <p className="mt-0.5 text-xs text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}
