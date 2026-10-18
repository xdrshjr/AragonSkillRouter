'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleAlert, RefreshCw, XCircle } from 'lucide-react';
import * as React from 'react';
import { DataTable, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { useToast } from '@/components/ui/toast';
import { apiPost, cancelTask, describeError, listTasks } from '@/lib/api';
import { formatDateTime, formatRelative, statusLabel } from '@/lib/format';
import { useTableState } from '@/lib/hooks/useTableState';
import { queryKeys } from '@/lib/query';
import type { CancelResult, CancelScope, Task, TaskStatus } from '@/lib/types';

/**
 * 任务（spec §A.3.11 / §13.2 / F-07、F-18）：
 * 与 `/calls` 完全一致的交互（`DataTable` + 搜索 + 筛选 + 排序 + 分页），
 * `status_unknown` 与 `canceling` 都必须**在行内**解释清楚：
 * 「可能仍在远端执行，请先对账」/「已请求取消（范围）」—— 不假装已完成或已失败。
 */

const FILTER_KEYS = ['status', 'capability_key'] as const;
/** `sort` 白名单（后端 `services/tasks.py:25`）：`created_at / updated_at / status` */
const SORT_FIELDS = ['created_at', 'updated_at', 'status'] as const;

const STATUS_OPTIONS: { value: TaskStatus; label: string }[] = [
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '进行中' },
  { value: 'canceling', label: '取消中' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'canceled', label: '已取消' },
  { value: 'status_unknown', label: '状态未知' },
];

const CANCEL_SCOPE_LABEL: Record<CancelScope, string> = {
  none: '未请求取消',
  pending_submit: '提交中取消，拿到上游 id 后会追取消',
  remote: '上游已受理，等上游确认',
  local_only: '上游不支持取消，仅停止本地等待（远端可能仍在执行）',
};

function ProgressCell({ task }: { task: Task }) {
  return (
    <div className="flex flex-col gap-1">
      <div
        role="progressbar"
        aria-valuenow={task.progress_percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${task.capability_key} 进度`}
        className="h-1.5 w-full min-w-[80px] overflow-hidden rounded-full bg-[var(--sr-surface-sunken)]"
      >
        <div
          className={cn(
            'h-full rounded-full',
            task.status === 'failed'
              ? 'bg-danger'
              : task.status === 'status_unknown' || task.status === 'canceling'
                ? 'bg-warn'
                : 'bg-accent',
          )}
          style={{ width: `${Math.min(100, Math.max(0, task.progress_percent))}%` }}
        />
      </div>
      <span className="text-12 text-content-muted">
        {task.progress_percent}%
        {task.progress_stage ? ` · ${task.progress_stage}` : ''}
        {` · 尝试 ${task.attempts}/${task.max_attempts}`}
      </span>
    </div>
  );
}

export default function TasksPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [cancelTarget, setCancelTarget] = React.useState<Task | null>(null);
  const [cancelResult, setCancelResult] = React.useState<CancelResult | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);

  const table = useTableState({
    defaultSort: '-updated_at',
    filterKeys: FILTER_KEYS,
    sortWhitelist: SORT_FIELDS,
  });

  const tasks = useQuery({
    queryKey: queryKeys.tasks.list(table.requestParams),
    queryFn: () =>
      listTasks({
        page: table.requestParams.page,
        page_size: table.requestParams.page_size,
        sort: table.requestParams.sort,
        q: table.requestParams.q,
        status: (table.filters['status'] ?? '') as TaskStatus | '',
        capability_key: table.filters['capability_key'] ?? '',
      }),
    refetchInterval: 15_000,
  });

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: ['tasks'] });
    await queryClient.invalidateQueries({ queryKey: queryKeys.overview() });
  };

  const cancel = useMutation({
    mutationFn: async (taskId: string) => cancelTask(taskId),
    onMutate: (taskId) => setBusyId(taskId),
    onSuccess: async (result) => {
      setCancelResult(result);
      toast({
        title:
          result.cancel_scope === 'remote'
            ? '已请求远端取消'
            : result.cancel_scope === 'pending_submit'
              ? '已请求取消（提交中）'
              : result.cancel_scope === 'local_only'
                ? '仅停止了本地等待'
                : '任务已是终态',
        description:
          result.cancel_scope === 'none'
            ? '该任务已结束，无需取消。'
            : CANCEL_SCOPE_LABEL[result.cancel_scope],
        tone: result.cancel_scope === 'remote' || result.cancel_scope === 'pending_submit' ? 'ok' : 'warn',
      });
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: info.code === 'CANCEL_UNSUPPORTED' ? '上游不支持取消' : '取消失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
    onSettled: () => setBusyId(null),
  });

  const reconcile = useMutation({
    mutationFn: async (taskId: string) => reconcileTask(taskId),
    onMutate: (taskId) => setBusyId(taskId),
    onSuccess: async () => {
      toast({ title: '已对账', description: '状态已按上游结果更新。', tone: 'ok' });
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '对账失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
    onSettled: () => setBusyId(null),
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '状态',
      value: table.filters['status'] ?? '',
      options: STATUS_OPTIONS.map((option) => ({ value: option.value, label: option.label })),
    },
  ];

  const pendingUnknown = (tasks.data?.items ?? []).filter((task) => task.status === 'status_unknown').length;

  const columns: DataTableColumn<Task>[] = [
    {
      id: 'capability_key',
      header: '能力',
      cardTitle: true,
      cell: (row) => (
        <div className="flex flex-col">
          <span className="sr-mono text-13 text-content">{row.capability_key}</span>
          <span className="flex items-center gap-1">
            <Badge tone="outline" mono>
              {row.mode}
            </Badge>
            <span className="text-12 text-content-muted">{statusLabel(row.status)}</span>
          </span>
        </div>
      ),
    },
    {
      id: 'status',
      header: '状态',
      sortable: true,
      cell: (row) => (
        <div className="flex flex-col gap-1">
          <StatusBadge kind="task" status={row.status} />
          {row.cancel_requested_at && row.status !== 'canceled' ? (
            <span className="flex flex-wrap items-center gap-1 text-12">
              <Badge tone="warn" dot>
                已请求取消
              </Badge>
              <span className="text-content-muted">
                {formatRelative(row.cancel_requested_at)} · {CANCEL_SCOPE_LABEL[row.cancel_scope]}
              </span>
            </span>
          ) : null}
          {row.status === 'status_unknown' ? (
            <span className="flex items-start gap-1 text-12 text-warn">
              <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden />
              结果不确定：可能仍在远端执行，请先「对账」
            </span>
          ) : null}
          {row.error_code ? (
            <span className="text-12 text-content-muted">
              错误码 <code className="sr-mono text-danger">{row.error_code}</code>
              {row.error_detail_redacted ? ` · ${row.error_detail_redacted}` : ''}
              {row.failure_class ? ` · 分类 ${row.failure_class}` : ''}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      id: 'progress',
      header: '进度',
      hideOnCard: true,
      cell: (row) => <ProgressCell task={row} />,
    },
    {
      id: 'upstream_task_id',
      header: '上游任务',
      hideOnCard: true,
      cell: (row) =>
        row.upstream_task_id ? (
          <span className="sr-mono text-12 text-content-muted">{row.upstream_task_id}</span>
        ) : (
          <span className="text-12 text-content-muted">—</span>
        ),
    },
    {
      id: 'request_id',
      header: 'request_id',
      hideOnCard: true,
      cell: (row) => <span className="sr-mono text-12 text-content-muted">{row.request_id}</span>,
    },
    {
      id: 'updated_at',
      header: '更新时间',
      sortable: true,
      align: 'right',
      cell: (row) => (
        <span className="flex flex-col items-end">
          <span className="text-13 text-content-muted">{formatRelative(row.updated_at)}</span>
          <span className="text-12 text-content-muted">
            {row.finished_at ? `完成 ${formatDateTime(row.finished_at)}` : `创建 ${formatDateTime(row.created_at)}`}
          </span>
        </span>
      ),
    },
    {
      id: 'actions',
      header: '操作',
      align: 'right',
      cell: (row) => (
        <div className="flex items-center justify-end gap-1">
          <Button
            variant="secondary"
            size="sm"
            loading={busyId === row.id}
            disabled={row.status !== 'status_unknown'}
            onClick={(event) => {
              event.stopPropagation();
              reconcile.mutate(row.id);
            }}
          >
            对账
          </Button>
          <Button
            variant="secondary"
            size="sm"
            loading={busyId === row.id}
            disabled={
              ['succeeded', 'failed', 'canceled'].includes(row.status) || Boolean(row.cancel_requested_at)
            }
            onClick={(event) => {
              event.stopPropagation();
              setCancelResult(null);
              setCancelTarget(row);
            }}
            icon={<XCircle size={14} strokeWidth={1.75} />}
          >
            {row.cancel_requested_at ? '已请求取消' : '取消'}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="任务"
        description="异步任务的真实状态。搜索覆盖 request_id / 能力 key / 上游任务号；「状态未知」表示可能仍在远端执行。"
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void tasks.refetch()}
            icon={<RefreshCw size={16} strokeWidth={1.75} />}
          >
            刷新
          </Button>
        }
      />

      <section aria-label="任务筛选" className="sr-card flex flex-wrap items-end gap-2 p-3">
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">能力 key</span>
          <input
            value={table.filters['capability_key'] ?? ''}
            onChange={(event) => table.setFilter('capability_key', event.target.value)}
            aria-label="能力 key"
            placeholder="text.chat"
            className="h-8 w-[180px] rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          />
        </label>
        <Button variant="ghost" size="sm" onClick={table.resetFilters}>
          清空筛选
        </Button>
        <p className="ml-auto text-12 text-content-muted">
          status_unknown 与 canceling 都是「未终结」状态，不能当作成功或失败。
        </p>
      </section>

      {/* status_unknown 是独立语义：可能仍在远端执行，绝不能读成失败或成功 */}
      {pendingUnknown > 0 ? (
        <section
          aria-label="待确认任务"
          className="flex flex-wrap items-start gap-2 rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2 text-13 text-content"
        >
          <CircleAlert size={16} strokeWidth={1.75} className="mt-0.5 shrink-0 text-warn" aria-hidden />
          <div>
            <p className="font-medium">当前页有 {pendingUnknown} 个「状态未知」任务</p>
            <p className="mt-0.5 text-12 text-content-muted">
              请求已发出但没有拿到确定结果，可能仍在远端执行。请先点该行「对账」确认上游状态，
              再决定是否重投——直接重投可能产生双份费用。
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            className="ml-auto"
            onClick={() => table.setFilter('status', 'status_unknown')}
          >
            只看状态未知
          </Button>
        </section>
      ) : null}

      <DataTable
        caption="任务列表"
        columns={columns}
        rows={tasks.data?.items}
        rowKey={(row) => row.id}
        total={tasks.data?.total}
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
        loading={tasks.isLoading}
        error={tasks.error}
        onRetry={() => void tasks.refetch()}
        emptyKind="first-use"
        emptyTitle="还没有任务"
        emptyDescription="以 async 模式调用能力后，任务会出现在这里。"
      />

      <Dialog
        open={Boolean(cancelTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setCancelTarget(null);
            setCancelResult(null);
          }
        }}
      >
        <DialogContent
          title="取消任务"
          description={cancelTarget ? `${cancelTarget.capability_key} · ${cancelTarget.status}` : undefined}
          width="sm"
          footer={
            <>
              <DialogClose asChild>
                <Button variant="secondary" size="sm">
                  关闭
                </Button>
              </DialogClose>
              <Button
                variant="danger"
                size="sm"
                loading={cancel.isPending}
                disabled={!cancelTarget || Boolean(cancelResult)}
                onClick={() => {
                  if (cancelTarget) cancel.mutate(cancelTarget.id);
                }}
              >
                确认取消
              </Button>
            </>
          }
        >
          {cancelTarget ? (
            <div className="flex flex-col gap-2 text-13">
              <p className="text-content">
                上游能力 <code className="sr-mono">{cancelTarget.capability_key}</code>
                {cancelTarget.upstream_task_id ? (
                  <>
                    （上游任务 <code className="sr-mono">{cancelTarget.upstream_task_id}</code>）
                  </>
                ) : null}
              </p>
              <p className="text-content-muted">
                取消是否触及远端取决于上游支持情况（当前 cancel_capability: {cancelTarget.cancel_capability}）。
                提交过程中取消只会记下取消意图（`cancel_scope = pending_submit`，返回 202 + `canceling`），
                上游可能继续执行并产生费用。
              </p>
              {cancelResult ? (
                <div
                  className={cn(
                    'rounded px-2 py-1.5',
                    cancelResult.cancel_scope === 'remote' || cancelResult.cancel_scope === 'pending_submit'
                      ? 'border border-ok bg-[var(--sr-ok-weak)]'
                      : 'border border-warn bg-[var(--sr-warn-weak)]',
                  )}
                >
                  <p className="text-content">
                    取消范围：{CANCEL_SCOPE_LABEL[cancelResult.cancel_scope]}
                  </p>
                  <p className="mt-0.5 text-12 text-content-muted">
                    当前状态：{statusLabel(cancelResult.status)}
                    {cancelResult.note ? ` · ${cancelResult.note}` : ''}
                  </p>
                  {cancelResult.cancel_scope === 'local_only' ? (
                    <p className="mt-1 text-12 text-content-muted">
                      建议在上表对该任务执行「对账」，确认上游是否仍在运行。
                    </p>
                  ) : null}
                </div>
              ) : null}
              {cancel.isError ? <RequestIdCopy requestId={describeError(cancel.error).requestId ?? ''} /> : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <p className="text-12 text-content-muted">
        提示：默认按更新时间倒序；历史数据受「数据保留」设置约束（见设置页）。
      </p>
    </div>
  );
}

/** 对账（`POST /admin/tasks/{id}/reconcile`）：结果不确定时按上游重算状态 */
async function reconcileTask(taskId: string): Promise<Task> {
  return apiPost<Task>(`/admin/tasks/${taskId}/reconcile`, {});
}
