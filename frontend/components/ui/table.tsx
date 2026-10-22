'use client';

import type { HTMLAttributes, ReactNode, ThHTMLAttributes } from 'react';
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react';

import { cn } from '@/lib/utils';

export type SortDirection = 'asc' | 'desc';

export interface SortState {
  key: string;
  direction: SortDirection;
}

export interface TableProps {
  children: ReactNode;
  /** Accessible table caption, visually hidden. */
  caption: string;
  className?: string;
  /** Max height of the scroll container (horizontal scrolling is always on). */
  maxHeightClassName?: string;
}

/** Table shell: horizontally scrollable with an accessible caption. */
export function Table({ children, caption, className, maxHeightClassName }: TableProps) {
  return (
    <div className={cn('sr-scroll-x rounded-card border border-border bg-surface', maxHeightClassName, className)}>
      <table className="w-full min-w-[720px] border-collapse text-left text-sm">
        <caption className="sr-visually-hidden">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function THead({ children, sticky = true }: { children: ReactNode; sticky?: boolean }) {
  return (
    <thead className={cn('bg-info-soft text-xs uppercase tracking-wide text-muted', sticky && 'sticky top-0 z-10')}>
      {children}
    </thead>
  );
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody className="divide-y divide-border">{children}</tbody>;
}

export function TR({ className, children, ...rest }: HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr className={cn('align-middle transition-colors duration-150 hover:bg-info-soft/60', className)} {...rest}>
      {children}
    </tr>
  );
}

export interface THProps extends ThHTMLAttributes<HTMLTableCellElement> {
  align?: 'left' | 'right' | 'center';
}

export function TH({ align = 'left', className, children, ...rest }: THProps) {
  return (
    <th
      scope="col"
      className={cn(
        'whitespace-nowrap border-b border-border px-3 py-2 font-medium',
        align === 'right' && 'text-right',
        align === 'center' && 'text-center',
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

export function TD({ align = 'left', className, children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn('px-3 py-2 text-text', align === 'right' && 'text-right', align === 'center' && 'text-center', className)}
      {...rest}
    >
      {children}
    </td>
  );
}

export interface SortableTHProps {
  label: string;
  sortKey: string;
  sort: SortState | null;
  onSort: (key: string) => void;
  align?: 'left' | 'right';
  className?: string;
}

/** Sortable column header: a real button, with `aria-sort` on the cell. */
export function SortableTH({ label, sortKey, sort, onSort, align = 'left', className }: SortableTHProps) {
  const active = sort?.key === sortKey;
  const direction = active ? sort?.direction : undefined;
  const ariaSort = active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none';
  return (
    <th
      scope="col"
      aria-sort={ariaSort}
      className={cn(
        'whitespace-nowrap border-b border-border px-3 py-2 font-medium',
        align === 'right' && 'text-right',
        className,
      )}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 rounded-control px-1 py-0.5 transition-colors duration-150',
          'hover:text-text focus-visible:text-text',
          active ? 'text-text' : 'text-muted',
        )}
      >
        {label}
        {direction === 'asc' ? (
          <ArrowUp aria-hidden className="h-3 w-3" />
        ) : direction === 'desc' ? (
          <ArrowDown aria-hidden className="h-3 w-3" />
        ) : (
          <ChevronsUpDown aria-hidden className="h-3 w-3 opacity-60" />
        )}
        <span className="sr-visually-hidden">
          {active ? `sorted ${direction === 'asc' ? 'ascending' : 'descending'}, activate to reverse` : 'activate to sort'}
        </span>
      </button>
    </th>
  );
}

/** Grid used instead of a table for stacked key/value detail lists. */
export function TableEmpty({ colSpan, children }: { colSpan: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-8 text-center text-sm text-muted">
        {children}
      </td>
    </tr>
  );
}
