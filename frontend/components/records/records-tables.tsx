'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { History, ListTree } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatCurrency, formatDuration, formatNumber } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Card } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import { Pagination } from '@/components/ui/pagination';
import { SearchInput } from '@/components/ui/search-input';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { SortableTH, Table, TBody, TD, TH, THead, TR, type SortState } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import {
  INVOCATION_STATUSES,
  TASK_STATUSES,
  useInvocationsQuery,
  useTasksQuery,
  type Task,
} from './api';
import { statusLabel } from '@/lib/format';
import type { Invocation } from '@/lib/types';

const PAGE_SIZE = 25;

function sortRows<T>(rows: T[], sort: SortState | null, pick: (row: T) => string | number | null): T[] {
  if (!sort) return rows;
  const factor = sort.direction === 'asc' ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = pick(left);
    const b = pick(right);
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * factor;
    return String(a).localeCompare(String(b)) * factor;
  });
}

function copyable(value: string | null | undefined): string {
  return value ?? '—';
}

function SortToggle({ sort, onSort }: { sort: SortState | null; onSort: (key: string) => void }) {
  return (
    <SortableTH label="Time" sortKey="created_at" sort={sort} onSort={onSort} align="right" />
  );
}

/* --------------------------------------------------------------- invocations -- */

export function InvocationsTable() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [capability, setCapability] = useState('');
  const [agent, setAgent] = useState('');
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<SortState | null>({ key: 'created_at', direction: 'desc' });

  const query = useInvocationsQuery({
    search,
    status,
    capability,
    agent,
    limit: PAGE_SIZE,
    offset,
  });

  const rows = useMemo(
    () => sortRows(query.data?.items ?? [], sort, (row: Invocation) => row.created_at),
    [query.data?.items, sort],
  );

  const onSort = (key: string) => {
    setSort((current) =>
      current?.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' },
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[220px] flex-1">
          <SearchInput
            value={search}
            onChange={(value) => {
              setSearch(value);
              setOffset(0);
            }}
            label="Search invocations"
            placeholder="request id, error code, capability…"
          />
        </div>
        <div className="w-40">
          <Select
            aria-label="Filter by status"
            value={status}
            placeholder="All statuses"
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
            options={INVOCATION_STATUSES.map((item) => ({ value: item, label: statusLabel(item) }))}
          />
        </div>
        <div className="w-40">
          <Input
            aria-label="Filter by capability"
            value={capability}
            placeholder="capability slug"
            onChange={(event) => {
              setCapability(event.target.value);
              setOffset(0);
            }}
          />
        </div>
        <div className="w-40">
          <Input
            aria-label="Filter by agent"
            value={agent}
            placeholder="agent id or name"
            onChange={(event) => {
              setAgent(event.target.value);
              setOffset(0);
            }}
          />
        </div>
      </div>

      {query.isLoading ? <SkeletonTable rows={6} columns={7} /> : null}

      {query.isError ? (
        <Banner
          tone="danger"
          title="Could not load invocations"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      ) : null}

      {!query.isLoading && !query.isError ? (
        (query.data?.items.length ?? 0) === 0 ? (
          <Card>
            <EmptyState
              icon={<History aria-hidden className="h-5 w-5" />}
              title={search || status || capability || agent ? 'No invocation matches these filters' : 'No invocation recorded yet'}
              description={
                search || status || capability || agent
                  ? 'Clear a filter to widen the search. Time is sorted client-side on the loaded page.'
                  : 'Every call that reaches the router writes one row here.'
              }
            />
          </Card>
        ) : (
          <>
            <Table caption="Invocations">
              <THead>
                <TR>
                  <TH>Request</TH>
                  <TH>Capability</TH>
                  <TH>Caller</TH>
                  <TH>Surface</TH>
                  <TH>Status</TH>
                  <TH align="right">Latency</TH>
                  <TH align="right">Cost</TH>
                  <SortToggle sort={sort} onSort={onSort} />
                </TR>
              </THead>
              <TBody>
                {rows.map((row) => (
                  <TR key={row.id}>
                    <TD>
                      <Link href={`/records/${row.id}`} className="font-mono text-[11px] hover:underline">
                        {row.request_id}
                      </Link>
                      <span className="block text-[11px] text-muted">{row.mode ?? '—'}</span>
                    </TD>
                    <TD>
                      <span className="font-mono text-xs">{row.capability?.slug ?? '—'}</span>
                      <span className="block text-[11px] text-muted">
                        {row.version ? `v${row.version}` : 'no version'} · {row.provider?.name ?? 'no provider'}
                      </span>
                    </TD>
                    <TD className="text-xs">
                      {row.agent?.name ?? row.caller_type ?? '—'}
                      <span className="block text-[11px] text-muted">{row.caller_type ?? '—'}</span>
                    </TD>
                    <TD>
                      <Badge tone="neutral" dot={false}>
                        {statusLabel(row.surface)}
                      </Badge>
                    </TD>
                    <TD>
                      <StatusBadge status={row.status} />
                      {row.http_status ? (
                        <span className="ml-1 text-[11px] text-muted">{row.http_status}</span>
                      ) : null}
                    </TD>
                    <TD align="right" className="text-xs tabular-nums">
                      {formatDuration(row.latency_ms)}
                      {row.retries > 0 ? (
                        <span className="block text-[11px] text-warning">{row.retries} retr.</span>
                      ) : null}
                    </TD>
                    <TD align="right" className="text-xs tabular-nums">
                      {row.cost?.actual_usd !== null && row.cost?.actual_usd !== undefined
                        ? formatCurrency(row.cost.actual_usd)
                        : row.cost?.estimated_usd !== null && row.cost?.estimated_usd !== undefined
                          ? `${formatCurrency(row.cost.estimated_usd)} (est.)`
                          : '—'}
                    </TD>
                    <TD align="right" className="text-xs text-muted">
                      <TimeAgo value={row.created_at} />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Pagination
                total={query.data?.total ?? 0}
                limit={query.data?.limit ?? PAGE_SIZE}
                offset={query.data?.offset ?? offset}
                onChange={setOffset}
                itemLabel="invocations"
              />
              <p className="text-[11px] text-muted">
                Sorting applies to the {rows.length} row(s) on this page; the API has no sort parameter.
              </p>
            </div>
          </>
        )
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------- tasks -- */

export function TasksTable() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [capability, setCapability] = useState('');
  const [agent, setAgent] = useState('');
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<SortState | null>({ key: 'created_at', direction: 'desc' });

  const query = useTasksQuery({ search, status, capability, agent, limit: PAGE_SIZE, offset });
  const rows = useMemo(
    () => sortRows(query.data?.items ?? [], sort, (row: Task) => row.created_at),
    [query.data?.items, sort],
  );

  const onSort = (key: string) => {
    setSort((current) =>
      current?.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' },
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[220px] flex-1">
          <SearchInput
            value={search}
            onChange={(value) => {
              setSearch(value);
              setOffset(0);
            }}
            label="Search tasks"
            placeholder="task id, request id, upstream id…"
          />
        </div>
        <div className="w-44">
          <Select
            aria-label="Filter by task status"
            value={status}
            placeholder="All statuses"
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
            options={TASK_STATUSES.map((item) => ({ value: item, label: statusLabel(item) }))}
          />
        </div>
        <div className="w-40">
          <Input
            aria-label="Filter tasks by capability"
            value={capability}
            placeholder="capability slug"
            onChange={(event) => {
              setCapability(event.target.value);
              setOffset(0);
            }}
          />
        </div>
        <div className="w-40">
          <Input
            aria-label="Filter tasks by agent"
            value={agent}
            placeholder="agent id or name"
            onChange={(event) => {
              setAgent(event.target.value);
              setOffset(0);
            }}
          />
        </div>
      </div>

      {query.isLoading ? <SkeletonTable rows={6} columns={7} /> : null}

      {query.isError ? (
        <Banner
          tone="danger"
          title="Could not load tasks"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      ) : null}

      {!query.isLoading && !query.isError ? (
        (query.data?.items.length ?? 0) === 0 ? (
          <Card>
            <EmptyState
              icon={<ListTree aria-hidden className="h-5 w-5" />}
              title={search || status || capability || agent ? 'No task matches these filters' : 'No async task yet'}
              description={
                search || status || capability || agent
                  ? 'Clear a filter to widen the search.'
                  : 'An async capability call creates a task row that the worker polls.'
              }
            />
          </Card>
        ) : (
          <>
            <Table caption="Async tasks">
              <THead>
                <TR>
                  <TH>Task</TH>
                  <TH>Capability</TH>
                  <TH>Status</TH>
                  <TH>Progress</TH>
                  <TH>Attempts</TH>
                  <TH>Upstream id</TH>
                  <TH align="right">Cost</TH>
                  <SortToggle sort={sort} onSort={onSort} />
                </TR>
              </THead>
              <TBody>
                {rows.map((row) => (
                  <TR key={row.id}>
                    <TD>
                      <Link href={`/records/${row.id}`} className="font-mono text-[11px] hover:underline">
                        {row.id}
                      </Link>
                      <span className="block text-[11px] text-muted">{row.request_id ?? '—'}</span>
                    </TD>
                    <TD>
                      <span className="font-mono text-xs">{row.capability?.slug ?? '—'}</span>
                      <span className="block text-[11px] text-muted">{row.provider?.name ?? 'no provider'}</span>
                    </TD>
                    <TD>
                      <StatusBadge status={row.status} />
                    </TD>
                    <TD className="min-w-[120px]">
                      <span className="flex items-center gap-2">
                        <span
                          role="progressbar"
                          aria-valuenow={row.progress}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          className="h-1.5 w-16 overflow-hidden rounded-pill bg-info-soft"
                        >
                          <span className="block h-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, row.progress))}%` }} />
                        </span>
                        <span className="text-[11px] tabular-nums text-muted">{row.progress}%</span>
                      </span>
                      {row.progress_note ? (
                        <span className="mt-0.5 block max-w-[220px] truncate text-[11px] text-muted">{row.progress_note}</span>
                      ) : null}
                    </TD>
                    <TD className="text-xs tabular-nums">
                      {row.attempts}/{row.max_attempts || '—'}
                    </TD>
                    <TD className="font-mono text-[11px] text-muted">{copyable(row.upstream_task_id)}</TD>
                    <TD align="right" className="text-xs tabular-nums">
                      {row.cost?.actual_usd !== null && row.cost?.actual_usd !== undefined
                        ? formatCurrency(row.cost.actual_usd, row.cost.currency)
                        : '—'}
                    </TD>
                    <TD align="right" className="text-xs text-muted">
                      <TimeAgo value={row.created_at} />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Pagination
                total={query.data?.total ?? 0}
                limit={query.data?.limit ?? PAGE_SIZE}
                offset={query.data?.offset ?? offset}
                onChange={setOffset}
                itemLabel="tasks"
              />
              <p className="text-[11px] text-muted">{formatNumber(query.data?.total ?? 0)} total task(s)</p>
            </div>
          </>
        )
      ) : null}
    </div>
  );
}
