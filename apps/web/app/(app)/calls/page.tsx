'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';
import { DataTable, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Drawer, DrawerContent } from '@/components/ui/drawer';
import { Skeleton } from '@/components/ui/skeleton';
import { apiGet, describeError } from '@/lib/api';
import { formatDateTime, formatDuration, prettyJson } from '@/lib/format';
import { useTableState } from '@/lib/hooks/useTableState';
import { queryKeys } from '@/lib/query';
import type { CallDetail, CallLog, Paginated } from '@/lib/types';

/**
 * 筛选键即 URL 参数名（§A.3.11）：时间用 `date_from`/`date_to`（`from`/`to` 只是后端兼容别名，
 * 前端一律发新名字）；`needs_reconcile=1` 由概览「待对账调用」跳转带过来。
 */
const FILTER_KEYS = [
  'status',
  'capability',
  'provider',
  'caller',
  'date_from',
  'date_to',
  'min_latency',
  'needs_reconcile',
] as const;

/** `sort` 白名单（后端 `services/calls.py:14`）：越界会 400，因此只允许这三列可排序 */
const SORT_FIELDS = ['created_at', 'duration_ms', 'status'] as const;

/**
 * 调用记录（spec §13.2 `/calls`）：
 * 筛选（时间/状态/能力/提供商/调用方/最小耗时/待对账）+ 表格 + 详情抽屉
 * （路由决策 / 重试链 / 错误映射 / 耗时分解）。
 */
export default function CallsPage() {
  const table = useTableState({
    defaultSort: '-created_at',
    filterKeys: FILTER_KEYS,
    sortWhitelist: SORT_FIELDS,
    dateKeys: ['date_from', 'date_to'],
    booleanKeys: ['needs_reconcile'],
  });
  const [detailRequestId, setDetailRequestId] = React.useState<string | null>(null);
  const needsReconcile = table.filters['needs_reconcile'] === '1';

  const list = useQuery<Paginated<CallLog>>({
    queryKey: queryKeys.calls.list(table.requestParams),
    queryFn: () => apiGet<Paginated<CallLog>>('/admin/calls', table.requestParams),
    refetchInterval: 30_000,
  });

  const detail = useQuery<CallDetail>({
    queryKey: queryKeys.calls.detail(detailRequestId ?? 'none'),
    queryFn: () => apiGet<CallDetail>(`/admin/calls/${detailRequestId}`),
    enabled: Boolean(detailRequestId),
    retry: false,
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '状态',
      value: table.filters['status'] ?? '',
      options: [
        { value: 'success', label: '成功' },
        { value: 'failed', label: '失败' },
        { value: 'status_unknown', label: '状态未知' },
      ],
    },
    {
      id: 'needs_reconcile',
      label: '待对账',
      value: table.filters['needs_reconcile'] ?? '',
      options: [{ value: '1', label: '只看待对账' }],
    },
  ];

  const columns: DataTableColumn<CallLog>[] = [
    {
      id: 'created_at',
      header: '时间',
      sortable: true,
      cardTitle: true,
      cell: (row) => <span className="text-13 text-content">{formatDateTime(row.created_at)}</span>,
    },
    {
      id: 'capability_key',
      header: '能力',
      cell: (row) => <span className="sr-mono text-12 text-content">{row.capability_key}</span>,
    },
    {
      id: 'status',
      header: '状态',
      sortable: true,
      cell: (row) => (
        <div className="flex flex-wrap items-center gap-1">
          <StatusBadge kind="call" status={row.status} />
          {row.needs_reconcile ? <Badge tone="warn">待对账</Badge> : null}
          {row.error_code ? <code className="sr-mono text-12 text-danger">{row.error_code}</code> : null}
        </div>
      ),
    },
    {
      id: 'mode',
      header: '模式',
      cell: (row) => (
        <Badge tone="outline" mono>
          {row.mode}
        </Badge>
      ),
    },
    {
      id: 'provider_id',
      header: '提供商',
      cell: (row) => <span className="text-12 text-content-muted">{row.provider_id ?? '—'}</span>,
    },
    {
      id: 'caller_id',
      header: '调用方',
      hideOnCard: true,
      cell: (row) => (
        <span className="text-12 text-content-muted">
          {row.caller_type}
          {row.caller_id ? `:${row.caller_id}` : ''}
        </span>
      ),
    },
    {
      // 列 id 用后端排序字段名 `duration_ms`（值仍是 latency_ms/duration_ms 同源），否则表头箭头对不上
      id: 'duration_ms',
      header: '耗时',
      align: 'right',
      sortable: true,
      cell: (row) => (
        <span className="text-13 text-content">{formatDuration(row.duration_ms ?? row.latency_ms)}</span>
      ),
    },
    {
      id: 'retries',
      header: '重试',
      align: 'right',
      hideOnCard: true,
      cell: (row) => <span className="text-13 text-content-muted">{row.retries}</span>,
    },
    {
      id: 'request_id',
      header: 'request_id',
      cell: (row) => <span className="sr-mono text-12 text-content-muted">{row.request_id}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="调用记录"
        description="每次调用都记录路由决策与脱敏预览。点任意行查看路由、重试与耗时分解。"
      />

      {/* 额外筛选：时间范围 / 能力 / 提供商 / 调用方 / 最小耗时 */}
      <section aria-label="高级筛选" className="sr-card flex flex-wrap items-end gap-2 p-3">
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">起始时间</span>
          <input
            type="datetime-local"
            value={table.filters['date_from'] ?? ''}
            onChange={(event) => table.setFilter('date_from', event.target.value)}
            aria-label="起始时间"
            className="h-8 rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">结束时间</span>
          <input
            type="datetime-local"
            value={table.filters['date_to'] ?? ''}
            onChange={(event) => table.setFilter('date_to', event.target.value)}
            aria-label="结束时间"
            className="h-8 rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">能力 key</span>
          <input
            value={table.filters['capability'] ?? ''}
            onChange={(event) => table.setFilter('capability', event.target.value)}
            aria-label="能力 key"
            placeholder="text.generate.*"
            className="h-8 w-[180px] rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">提供商</span>
          <input
            value={table.filters['provider'] ?? ''}
            onChange={(event) => table.setFilter('provider', event.target.value)}
            aria-label="提供商 ID"
            placeholder="prv_openai"
            className="h-8 w-[140px] rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">调用方</span>
          <input
            value={table.filters['caller'] ?? ''}
            onChange={(event) => table.setFilter('caller', event.target.value)}
            aria-label="调用方"
            className="h-8 w-[140px] rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">最小耗时（ms）</span>
          <input
            type="number"
            value={table.filters['min_latency'] ?? ''}
            onChange={(event) => table.setFilter('min_latency', event.target.value)}
            aria-label="最小耗时"
            className="h-8 w-[120px] rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <Button variant="ghost" size="sm" onClick={table.resetFilters}>
          清空筛选
        </Button>
      </section>

      {needsReconcile ? (
        <div
          role="status"
          className="flex flex-wrap items-center gap-2 rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2 text-13 text-content"
        >
          <Badge tone="warn" dot>
            只显示待对账调用
          </Badge>
          <span className="text-content-muted">
            这些调用的结果不确定（可能已在上游执行），请结合任务对账后再决定是否重投。
          </span>
          <Button variant="ghost" size="sm" className="ml-auto" onClick={() => table.setFilter('needs_reconcile', '')}>
            显示全部
          </Button>
        </div>
      ) : null}

      <DataTable
        caption="调用记录"
        columns={columns}
        rows={list.data?.items}
        rowKey={(row) => row.id}
        total={list.data?.total}
        page={table.page}
        pageSize={table.pageSize}
        sort={table.sort}
        q={table.q}
        onPageChange={table.setPage}
        onPageSizeChange={table.setPageSize}
        onSortChange={table.setSort}
        onQChange={table.setQ}
        filters={filters}
        onFilterChange={table.setFilter}
        loading={list.isLoading}
        error={list.error}
        onRetry={() => void list.refetch()}
        emptyKind="empty"
        emptyTitle="还没有调用记录"
        emptyDescription="签发令牌并让外部 Agent 调用一次后，这里会出现真实记录。"
        onRowClick={(row) => setDetailRequestId(row.request_id)}
      />

      <Drawer
        open={Boolean(detailRequestId)}
        onOpenChange={(open) => {
          if (!open) setDetailRequestId(null);
        }}
      >
        <DrawerContent
          title="调用详情"
          description={detailRequestId ? `request_id ${detailRequestId}` : undefined}
          width={640}
        >
          {detail.isLoading ? (
            <div className="flex flex-col gap-2">
              <Skeleton height={24} />
              <Skeleton height={100} />
              <Skeleton height={100} />
            </div>
          ) : detail.isError ? (
            <div>
              <p className="text-13 text-content">{describeError(detail.error).message}</p>
              {describeError(detail.error).hint ? (
                <p className="mt-1 text-12 text-content-muted">{describeError(detail.error).hint}</p>
              ) : null}
              <RequestIdCopy requestId={describeError(detail.error).requestId ?? ''} className="mt-1" />
            </div>
          ) : detail.data ? (
            <div className="flex flex-col gap-4">
              <section aria-label="概览">
                <h3 className="text-13 font-medium text-content">概览</h3>
                <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-13">
                  <div>
                    <dt className="text-content-muted">能力</dt>
                    <dd className="sr-mono text-content">{detail.data.capability_key}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">状态</dt>
                    <dd>
                      <StatusBadge kind="call" status={detail.data.status} />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">HTTP</dt>
                    <dd className="text-content">{detail.data.http_status ?? '—'}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">模式</dt>
                    <dd className="text-content">{detail.data.mode}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">耗时</dt>
                    <dd className="text-content">{formatDuration(detail.data.latency_ms)}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">重试</dt>
                    <dd className="text-content">{detail.data.retries}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">时间</dt>
                    <dd className="text-content">{formatDateTime(detail.data.created_at)}</dd>
                  </div>
                </dl>
              </section>

              <section aria-label="耗时分解">
                <h3 className="text-13 font-medium text-content">耗时分解</h3>
                <dl className="mt-1 grid grid-cols-3 gap-2 text-13">
                  <div>
                    <dt className="text-content-muted">排队</dt>
                    <dd className="text-content">{formatDuration(detail.data.timings.queue_ms)}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">上游</dt>
                    <dd className="text-content">{formatDuration(detail.data.timings.upstream_ms)}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">总计</dt>
                    <dd className="text-content">{formatDuration(detail.data.timings.total_ms)}</dd>
                  </div>
                </dl>
              </section>

              <section aria-label="路由决策">
                <h3 className="text-13 font-medium text-content">路由决策</h3>
                {detail.data.route_decision ? (
                  <div className="mt-1 flex flex-col gap-1 text-13">
                    <p className="text-content">
                      选中 route：<code className="sr-mono">{detail.data.route_decision.chosen_route_id ?? '—'}</code>
                    </p>
                    <p className="text-content-muted">
                      提供商 {detail.data.route_decision.chosen_provider_id ?? '—'} · 原因{' '}
                      {detail.data.route_decision.reason ?? '—'}
                    </p>
                    {detail.data.route_decision.failover_from ? (
                      <p className="text-warn">由 {detail.data.route_decision.failover_from} 切换而来</p>
                    ) : null}
                    <details>
                      <summary className="cursor-pointer text-12 text-content-muted">
                        候选 {detail.data.route_decision.candidates.length} · 丢弃{' '}
                        {detail.data.route_decision.dropped.length}
                      </summary>
                      <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                        {prettyJson(
                          {
                            candidates: detail.data.route_decision.candidates,
                            dropped: detail.data.route_decision.dropped,
                          },
                          6000,
                        )}
                      </pre>
                    </details>
                  </div>
                ) : (
                  <p className="mt-1 text-13 text-content-muted">没有路由决策记录。</p>
                )}
              </section>

              <section aria-label="重试链与错误映射">
                <h3 className="text-13 font-medium text-content">重试链 / 错误映射</h3>
                <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                  {prettyJson({ retry_chain: detail.data.retry_chain, error_mapping: detail.data.error_mapping }, 6000)}
                </pre>
              </section>

              <section aria-label="请求与响应预览">
                <h3 className="text-13 font-medium text-content">请求 / 响应（{detail.data.payload_policy}）</h3>
                <p className="text-12 text-content-muted">按 payload_policy 脱敏；密钥永不出现在预览中。</p>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                  {detail.data.request_preview ?? '（无请求预览）'}
                </pre>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                  {detail.data.response_preview ?? '（无响应预览）'}
                </pre>
              </section>
            </div>
          ) : null}
        </DrawerContent>
      </Drawer>
    </div>
  );
}
