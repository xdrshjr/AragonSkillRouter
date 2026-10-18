'use client';

import { ChevronLeft, ChevronRight, Search, X } from 'lucide-react';
import * as React from 'react';
import { EmptyState, RequestIdCopy, type EmptyKind } from '@/components/common/EmptyState';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { TableSkeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from '@/components/ui/table';
import { describeError } from '@/lib/api';
import type { PageSize } from '@/lib/types';

/**
 * 统一列表组件（spec §13.5）：
 * 搜索 300ms 防抖 + 筛选 chips + 排序表头 + 分页（20/50/100）+ URL 状态同步 +
 * 加载骨架 / 空态 / 错误（带 request_id 复制）/ 窄窗卡片列表。
 */

export interface DataTableColumn<T> {
  id: string;
  header: string;
  cell: (row: T) => React.ReactNode;
  sortable?: boolean;
  align?: 'left' | 'right';
  width?: string;
  /** 窄窗卡片列表：主标题字段 */
  cardTitle?: boolean;
  /** 窄窗卡片列表：次要信息 */
  cardMeta?: boolean;
  /** 窄窗不展示 */
  hideOnCard?: boolean;
}

export interface FilterChipOption {
  value: string;
  label: string;
  count?: number;
}

export interface FilterChipGroup {
  id: string;
  label: string;
  value: string;
  options: FilterChipOption[];
}

export interface DataTableProps<T> {
  /** 供读屏与 SEO 的表格说明（渲染 <caption>） */
  caption: string;
  columns: DataTableColumn<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  total?: number;

  page: number;
  pageSize: PageSize;
  sort: string;
  q: string;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: PageSize) => void;
  onSortChange: (sort: string) => void;
  onQChange: (q: string) => void;

  filters?: FilterChipGroup[];
  onFilterChange?: (id: string, value: string) => void;

  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;

  onRowClick?: (row: T) => void;
  selectedKey?: string | null;

  emptyKind?: EmptyKind;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyActionLabel?: string;
  onEmptyAction?: () => void;

  toolbar?: React.ReactNode;
  className?: string;
}

export const PAGE_SIZES: PageSize[] = [20, 50, 100];

/* ── URL 状态同步 ─────────────────────────────────────── */

interface TableState {
  page: number;
  page_size: PageSize;
  sort: string;
  q: string;
  filters: Record<string, string>;
}

function readUrlState(defaults: TableState): TableState {
  if (typeof window === 'undefined') return defaults;
  const params = new URLSearchParams(window.location.search);
  const sizeRaw = Number(params.get('page_size'));
  const size: PageSize = PAGE_SIZES.includes(sizeRaw as PageSize) ? (sizeRaw as PageSize) : defaults.page_size;
  const page = Math.max(1, Number(params.get('page')) || defaults.page);
  const filters: Record<string, string> = {};
  for (const key of Object.keys(defaults.filters)) {
    const value = params.get(key);
    if (value) filters[key] = value;
  }
  return {
    page,
    page_size: size,
    sort: params.get('sort') || defaults.sort,
    q: params.get('q') || '',
    filters,
  };
}

function writeUrlState(state: TableState): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams(window.location.search);
  if (state.page > 1) params.set('page', String(state.page));
  else params.delete('page');
  if (state.page_size !== 20) params.set('page_size', String(state.page_size));
  else params.delete('page_size');
  if (state.sort) params.set('sort', state.sort);
  else params.delete('sort');
  if (state.q) params.set('q', state.q);
  else params.delete('q');
  for (const [key, value] of Object.entries(state.filters)) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  const qs = params.toString();
  const next = `${window.location.pathname}${qs ? `?${qs}` : ''}`;
  window.history.replaceState(null, '', next);
}

export interface UseTableStateOptions {
  defaultSort: string;
  defaultPageSize?: PageSize;
  /** 额外筛选（键即 URL 参数名） */
  filterKeys?: readonly string[];
  defaultFilters?: Record<string, string>;
}

export interface UseTableStateResult {
  page: number;
  pageSize: PageSize;
  sort: string;
  q: string;
  /** 已防抖的搜索词（用于请求） */
  debouncedQ: string;
  filters: Record<string, string>;
  setPage: (page: number) => void;
  setPageSize: (size: PageSize) => void;
  setSort: (sort: string) => void;
  setQ: (q: string) => void;
  setFilter: (id: string, value: string) => void;
  resetFilters: () => void;
  requestParams: { page: number; page_size: PageSize; sort: string; q: string } & Record<
    string,
    string | number | boolean | undefined
  >;
}

/**
 * 列表状态（可分享 URL + 刷新保持）。
 * 读写在客户端进行（不用 useSearchParams，避免静态预渲染需要 Suspense 包裹）。
 */
export function useTableState({
  defaultSort,
  defaultPageSize = 20,
  filterKeys = [],
  defaultFilters = {},
}: UseTableStateOptions): UseTableStateResult {
  const filterKeySig = filterKeys.join('|');
  const defaultFilterSig = JSON.stringify(defaultFilters);

  const defaults = React.useMemo<TableState>(() => {
    const filters: Record<string, string> = {};
    for (const key of filterKeySig ? filterKeySig.split('|') : []) {
      const seed = (JSON.parse(defaultFilterSig) as Record<string, string>)[key];
      filters[key] = seed ?? '';
    }
    return { page: 1, page_size: defaultPageSize, sort: defaultSort, q: '', filters };
  }, [defaultSort, defaultPageSize, filterKeySig, defaultFilterSig]);

  const [state, setState] = React.useState<TableState>(defaults);
  const [hydrated, setHydrated] = React.useState(false);

  React.useEffect(() => {
    setState(readUrlState(defaults));
    setHydrated(true);
    const onPop = (): void => setState(readUrlState(defaults));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [defaults]);

  React.useEffect(() => {
    if (hydrated) writeUrlState(state);
  }, [state, hydrated]);

  const [debouncedQ, setDebouncedQ] = React.useState(state.q);
  React.useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(state.q), 300);
    return () => clearTimeout(timer);
  }, [state.q]);

  const setPage = React.useCallback((page: number) => setState((s) => ({ ...s, page })), []);
  const setPageSize = React.useCallback(
    (page_size: PageSize) => setState((s) => ({ ...s, page_size, page: 1 })),
    [],
  );
  const setSort = React.useCallback((sort: string) => setState((s) => ({ ...s, sort })), []);
  const setQ = React.useCallback((q: string) => setState((s) => ({ ...s, q, page: 1 })), []);
  const setFilter = React.useCallback(
    (id: string, value: string) =>
      setState((s) => ({ ...s, page: 1, filters: { ...s.filters, [id]: value } })),
    [],
  );
  const resetFilters = React.useCallback(
    () => setState((s) => ({ ...s, page: 1, q: '', filters: defaults.filters })),
    [defaults],
  );

  const requestParams = React.useMemo(() => {
    const extra: Record<string, string> = {};
    for (const [key, value] of Object.entries(state.filters)) {
      if (value) extra[key] = value;
    }
    return {
      page: state.page,
      page_size: state.page_size,
      sort: state.sort,
      q: debouncedQ,
      ...extra,
    } as { page: number; page_size: PageSize; sort: string; q: string } & Record<
      string,
      string | number | boolean | undefined
    >;
  }, [state.filters, state.page, state.page_size, state.sort, debouncedQ]);

  return {
    page: state.page,
    pageSize: state.page_size,
    sort: state.sort,
    q: state.q,
    debouncedQ,
    filters: state.filters,
    setPage,
    setPageSize,
    setSort,
    setQ,
    setFilter,
    resetFilters,
    requestParams,
  };
}

/* ── 组件 ─────────────────────────────────────────────── */

function useSlowHint(loading: boolean): boolean {
  const [slow, setSlow] = React.useState(false);
  React.useEffect(() => {
    if (!loading) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), 2000);
    return () => clearTimeout(timer);
  }, [loading]);
  return slow;
}

export function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  total,
  page,
  pageSize,
  sort,
  q,
  onPageChange,
  onPageSizeChange,
  onSortChange,
  onQChange,
  filters,
  onFilterChange,
  loading,
  error,
  onRetry,
  onRowClick,
  selectedKey,
  emptyKind = 'empty',
  emptyTitle,
  emptyDescription,
  emptyActionLabel,
  onEmptyAction,
  toolbar,
  className,
}: DataTableProps<T>) {
  const slow = useSlowHint(Boolean(loading));
  const [searchValue, setSearchValue] = React.useState(q);
  React.useEffect(() => setSearchValue(q), [q]);

  const safeTotal = total ?? rows?.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(safeTotal / pageSize));
  const showEmpty = !loading && !error && (rows?.length ?? 0) === 0;
  const hasActiveFilters = Boolean(q) || Object.values(filters ?? {}).some((f) => f.value);

  const sortState = (columnId: string): 'asc' | 'desc' | null => {
    const normalized = sort.startsWith('-') ? sort.slice(1) : sort;
    if (normalized !== columnId) return null;
    return sort.startsWith('-') ? 'desc' : 'asc';
  };

  const toggleSort = (columnId: string): void => {
    const current = sortState(columnId);
    if (current === null) onSortChange(columnId);
    else if (current === 'asc') onSortChange(`-${columnId}`);
    else onSortChange('');
  };

  const errorInfo = error ? describeError(error) : null;

  const cardColumns = columns.filter((column) => !column.hideOnCard);

  return (
    <section className={cn('flex flex-col gap-3', className)} aria-label={caption}>
      {/* 工具栏：搜索 + 筛选 chips + 自定义操作 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="w-[260px] max-w-full">
          <Input
            type="search"
            inputSize="sm"
            value={searchValue}
            placeholder="搜索…"
            aria-label={`搜索${caption}`}
            onChange={(event) => {
              setSearchValue(event.target.value);
              onQChange(event.target.value);
            }}
            leading={<Search size={16} strokeWidth={1.75} aria-hidden />}
            trailing={
              searchValue ? (
                <button
                  type="button"
                  aria-label="清空搜索"
                  onClick={() => {
                    setSearchValue('');
                    onQChange('');
                  }}
                  className="rounded-sm p-0.5 hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                >
                  <X size={14} strokeWidth={1.75} aria-hidden />
                </button>
              ) : null
            }
          />
        </div>
        {filters?.map((group) => (
          <div
            key={group.id}
            role="group"
            aria-label={group.label}
            className="flex flex-wrap items-center gap-1"
          >
            {group.options.map((option) => {
              const active = group.value === option.value;
              return (
                <button
                  key={`${group.id}-${option.value}`}
                  type="button"
                  aria-pressed={active}
                  onClick={() => onFilterChange?.(group.id, active ? '' : option.value)}
                  className={cn(
                    'rounded-sm border px-2 py-1 text-12 transition-colors duration-150 ease-sr',
                    'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                    active
                      ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
                      : 'border-border bg-surface text-content-muted hover:bg-hover hover:text-content',
                  )}
                >
                  {option.label}
                  {option.count !== undefined ? (
                    <span className="ml-1 text-content-muted">{option.count}</span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ))}
        <div className="ml-auto flex items-center gap-2">{toolbar}</div>
      </div>

      {slow ? (
        <p role="status" className="text-12 text-content-muted">
          仍在处理，可继续其它操作。
        </p>
      ) : null}

      {/* 错误态 */}
      {errorInfo ? (
        <EmptyState
          kind={
            errorInfo.code === 'NETWORK_OFFLINE'
              ? 'offline'
              : errorInfo.code === 'FORBIDDEN' ||
                  errorInfo.code === 'TOKEN_SCOPE_DENIED' ||
                  errorInfo.code === 'PASSWORD_CHANGE_REQUIRED'
                ? 'no-permission'
                : 'unavailable'
          }
          title={errorInfo.message}
          description={errorInfo.hint ?? undefined}
          requirement={
            errorInfo.code === 'FORBIDDEN' || errorInfo.code === 'TOKEN_SCOPE_DENIED'
              ? '管理员角色，或对应作用域'
              : undefined
          }
          requestId={errorInfo.requestId}
          actionLabel="重试"
          onAction={onRetry}
        />
      ) : null}

      {/* 加载骨架 */}
      {loading && !errorInfo ? <TableSkeleton rows={Math.min(pageSize, 6)} cols={columns.length} /> : null}

      {/* 空态 */}
      {showEmpty && !errorInfo ? (
        <EmptyState
          kind={hasActiveFilters ? 'empty' : emptyKind}
          title={hasActiveFilters ? '没有匹配的记录' : emptyTitle}
          description={hasActiveFilters ? '调整搜索词或清空筛选后重试。' : emptyDescription}
          actionLabel={hasActiveFilters ? undefined : emptyActionLabel}
          onAction={hasActiveFilters ? undefined : onEmptyAction}
        />
      ) : null}

      {/* 表格（≥768px） */}
      {!loading && !errorInfo && (rows?.length ?? 0) > 0 ? (
        <>
          <div className="hidden md:block">
            <Table>
              <TableCaption>{caption}（共 {safeTotal} 条，第 {page} / {pageCount} 页）</TableCaption>
              <TableHead>
                <TableRow>
                  {columns.map((column) => (
                    <TableHeaderCell
                      key={column.id}
                      sortable={column.sortable}
                      sortDir={column.sortable ? sortState(column.id) : null}
                      onSort={() => toggleSort(column.id)}
                      style={column.width ? { width: column.width } : undefined}
                      className={column.align === 'right' ? 'text-right' : undefined}
                    >
                      {column.header}
                    </TableHeaderCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {rows?.map((row) => {
                  const key = rowKey(row);
                  return (
                    <TableRow
                      key={key}
                      interactive={Boolean(onRowClick)}
                      selected={selectedKey === key}
                      onClick={onRowClick ? () => onRowClick(row) : undefined}
                      tabIndex={onRowClick ? 0 : undefined}
                      onKeyDown={
                        onRowClick
                          ? (event) => {
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                onRowClick(row);
                              }
                            }
                          : undefined
                      }
                    >
                      {columns.map((column) => (
                        <TableCell
                          key={column.id}
                          className={column.align === 'right' ? 'text-right' : undefined}
                        >
                          {column.cell(row)}
                        </TableCell>
                      ))}
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>

          {/* 卡片列表（<768px）*/}
          <ul className="flex flex-col gap-2 md:hidden">
            {rows?.map((row) => {
              const key = rowKey(row);
              const titleCol = cardColumns.find((c) => c.cardTitle) ?? cardColumns[0];
              const metaCols = cardColumns.filter((c) => c !== titleCol && !c.cardMeta);
              return (
                <li key={key} className="rounded-md border border-border bg-surface p-3">
                  {onRowClick ? (
                    <button
                      type="button"
                      onClick={() => onRowClick(row)}
                      className="w-full text-left focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                    >
                      <span className="block text-14 font-medium text-content">
                        {titleCol ? titleCol.cell(row) : key}
                      </span>
                    </button>
                  ) : (
                    <p className="text-14 font-medium text-content">
                      {titleCol ? titleCol.cell(row) : key}
                    </p>
                  )}
                  <dl className="mt-2 grid grid-cols-2 gap-1">
                    {metaCols.map((column) => (
                      <React.Fragment key={column.id}>
                        <dt className="text-12 text-content-muted">{column.header}</dt>
                        <dd className="text-right text-13 text-content">{column.cell(row)}</dd>
                      </React.Fragment>
                    ))}
                  </dl>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}

      {/* 分页 */}
      {!loading && !errorInfo && (rows?.length ?? 0) > 0 ? (
        <nav aria-label="分页" className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
          <p className="text-12 text-content-muted" aria-live="polite">
            共 {safeTotal} 条 · 第 {page} / {pageCount} 页
          </p>
          <div className="flex items-center gap-2">
            <div className="w-[92px]">
              <Select
                size="sm"
                aria-label="每页条数"
                value={String(pageSize)}
                onValueChange={(value) => onPageSizeChange(Number(value) as PageSize)}
                options={PAGE_SIZES.map((size) => ({ value: String(size), label: `每页 ${size}` }))}
              />
            </div>
            <Button
              variant="secondary"
              size="sm"
              aria-label="上一页"
              disabled={page <= 1}
              onClick={() => onPageChange(page - 1)}
              icon={<ChevronLeft size={16} strokeWidth={1.75} />}
            >
              上一页
            </Button>
            <Button
              variant="secondary"
              size="sm"
              aria-label="下一页"
              disabled={page >= pageCount}
              onClick={() => onPageChange(page + 1)}
            >
              下一页
              <ChevronRight size={16} strokeWidth={1.75} aria-hidden />
            </Button>
          </div>
        </nav>
      ) : null}
    </section>
  );
}

/** 行内 request_id 复制（详情抽屉里也复用） */
export function InlineRequestId({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-content-muted">—</span>;
  return <RequestIdCopy requestId={value} />;
}
