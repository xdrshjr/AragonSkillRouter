'use client';

import { ChevronLeft, ChevronRight } from 'lucide-react';

import { cn } from '@/lib/utils';
import { formatNumber } from '@/lib/format';
import { Button } from './button';

export interface PaginationProps {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
  /** Plural noun for the range label, e.g. "providers". */
  itemLabel?: string;
  disabled?: boolean;
  className?: string;
}

/** Range + prev/next pagination that stays honest about the total. */
export function Pagination({
  total,
  limit,
  offset,
  onChange,
  itemLabel = 'items',
  disabled,
  className,
}: PaginationProps) {
  if (total === 0) return null;
  const start = Math.min(offset + 1, total);
  const end = Math.min(offset + limit, total);
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <nav
      aria-label="Pagination"
      className={cn('flex flex-wrap items-center justify-between gap-2 py-2 text-xs text-muted', className)}
    >
      <p>
        {formatNumber(start)}–{formatNumber(end)} of {formatNumber(total)} {itemLabel}
      </p>
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
          iconLeft={<ChevronLeft aria-hidden className="h-3.5 w-3.5" />}
        >
          Previous
        </Button>
        <span aria-live="polite">
          Page {page} of {pages}
        </span>
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
          iconRight={<ChevronRight aria-hidden className="h-3.5 w-3.5" />}
        >
          Next
        </Button>
      </div>
    </nav>
  );
}
