import * as React from 'react';
import { cn } from './cn';

export function Table({
  className,
  children,
  ...props
}: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto sr-scroll">
      <table className={cn('w-full text-14', className)} {...props}>
        {children}
      </table>
    </div>
  );
}

export function TableCaption({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLTableCaptionElement>) {
  return (
    <caption className={cn('px-3 py-2 text-left text-12 text-content-muted', className)} {...props}>
      {children}
    </caption>
  );
}

export function TableHead({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn('border-b border-border', className)} {...props} />;
}

export function TableBody({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn('divide-y divide-[var(--sr-border)]', className)} {...props} />;
}

export interface TableRowProps extends React.HTMLAttributes<HTMLTableRowElement> {
  interactive?: boolean;
  selected?: boolean;
}

export function TableRow({ className, interactive, selected, ...props }: TableRowProps) {
  return (
    <tr
      data-selected={selected || undefined}
      className={cn(
        // 无斑马纹：靠 1px 分隔线 + 悬停底色
        interactive && 'cursor-pointer transition-colors duration-150 ease-sr hover:bg-hover',
        selected && 'bg-[var(--sr-accent-weak)]',
        className,
      )}
      {...props}
    />
  );
}

export interface TableHeaderCellProps extends React.ThHTMLAttributes<HTMLTableCellElement> {
  sortable?: boolean;
  sortDir?: 'asc' | 'desc' | null;
  onSort?: () => void;
}

export function TableHeaderCell({
  className,
  sortable,
  sortDir,
  onSort,
  children,
  scope = 'col',
  ...props
}: TableHeaderCellProps) {
  return (
    <th
      scope={scope}
      aria-sort={sortDir === 'asc' ? 'ascending' : sortDir === 'desc' ? 'descending' : undefined}
      className={cn(
        'whitespace-nowrap px-3 py-2 text-left text-12 font-medium uppercase tracking-wide text-content-muted',
        className,
      )}
      {...props}
    >
      {sortable ? (
        <button
          type="button"
          onClick={onSort}
          className="inline-flex items-center gap-1 rounded-sm hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
        >
          {children}
          <span aria-hidden className="text-12">
            {sortDir === 'asc' ? '↑' : sortDir === 'desc' ? '↓' : '↕'}
          </span>
        </button>
      ) : (
        children
      )}
    </th>
  );
}

export function TableCell({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn('px-3 py-2.5 align-middle text-content', className)} {...props} />;
}
