import * as React from 'react';
import { cn } from '@/components/ui/cn';

export interface PageHeaderProps {
  title: string;
  description?: string;
  /** 面包屑（不含当前页） */
  breadcrumbs?: { label: string; href?: string }[];
  actions?: React.ReactNode;
  /** 状态徽标区（如已保存/已发布） */
  status?: React.ReactNode;
  className?: string;
}

export function PageHeader({
  title,
  description,
  breadcrumbs,
  actions,
  status,
  className,
}: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-2 border-b border-border pb-4', className)}>
      {breadcrumbs && breadcrumbs.length > 0 ? (
        <nav aria-label="面包屑" className="text-12 text-content-muted">
          <ol className="flex flex-wrap items-center gap-1">
            {breadcrumbs.map((crumb, index) => (
              <li key={`${crumb.label}-${index}`} className="flex items-center gap-1">
                {crumb.href ? (
                  <a href={crumb.href} className="hover:text-content">
                    {crumb.label}
                  </a>
                ) : (
                  <span>{crumb.label}</span>
                )}
                <span aria-hidden className="text-border-strong">
                  /
                </span>
              </li>
            ))}
            <li aria-current="page" className="text-content">
              {title}
            </li>
          </ol>
        </nav>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-24 font-semibold tracking-tight text-content">{title}</h1>
          {description ? (
            <p className="mt-1 max-w-[720px] text-13 text-content-muted">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {status ? <div>{status}</div> : null}
    </header>
  );
}
