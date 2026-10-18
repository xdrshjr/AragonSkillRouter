'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleAlert, RefreshCw, XCircle } from 'lucide-react';
import * as React from 'react';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { cn } from '@/components/ui/cn';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { formatDateTime, formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { CancelResult, Paginated, Task, TaskStatus } from '@/lib/types';

/**
 * 任务（spec §13.2 `/tasks`）：
 * 状态分组（进行中 / 待确认 / 已完成 / 失败）+ 进度条 + 取消与对账。
 * `status_unknown` 单独区块解释「可能仍在远端执行」——不假装已完成或失败。
 */

const GROUPS: { id: string; title: string; statuses: TaskStatus[]; hint: string }[] = [
  { id: 'active', title: '进行中', statuses: ['queued', 'running', 'canceling'], hint: 'worker 正在处理' },
  {
    id: 'unknown',
    title: '待确认',
    statuses: ['status_unknown'],
    hint: '无法判定上游是否已受理：可能仍在远端执行，请先对账再决定是否重投',
  },
  { id: 'done', title: '已完成', statuses: ['succeeded', 'canceled'], hint: '终态' },
  { id: 'failed', title: '失败', statuses: ['failed'], hint: '可按错误码决定是否重试' },
];

export default function TasksPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [cancelTarget, setCancelTarget] = React.useState<Task | null>(null);
  const [cancelResult, setCancelResult] = React.useState<CancelResult | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);

  const tasks = useQuery<Paginated<Task>>({
    queryKey: queryKeys.tasks.list({ page: 1, page_size: 100, sort: '-updated_at' }),
    queryFn: () => apiGet<Paginated<Task>>('/admin/tasks', { page: 1, page_size: 100, sort: '-updated_at' }),
    refetchInterval: 15_000,
  });

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasks.list({ page: 1, page_size: 100, sort: '-updated_at' }) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.overview() });
  };

  const cancelTask = useMutation({
    mutationFn: async (taskId: string) => apiPost<CancelResult>(`/admin/tasks/${taskId}/cancel`, {}),
    onMutate: (taskId) => setBusyId(taskId),
    onSuccess: async (result) => {
      setCancelResult(result);
      toast({
        title: result.cancel_scope === 'remote' ? '已请求远端取消' : '仅停止了本地等待',
        description:
          result.cancel_scope === 'remote'
            ? '上游已受理取消请求。'
            : '上游不支持取消，远端可能仍在执行——请到任务里对账确认。',
        tone: result.cancel_scope === 'remote' ? 'ok' : 'warn',
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
    mutationFn: async (taskId: string) => apiPost<Task>(`/admin/tasks/${taskId}/reconcile`, {}),
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

  const items = tasks.data?.items ?? [];

  if (tasks.isLoading) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton height={56} rounded="md" />
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} height={72} rounded="md" />
        ))}
      </div>
    );
  }

  if (tasks.isError) {
    const info = describeError(tasks.error);
    return (
      <EmptyState
        kind={info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
        title={info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void tasks.refetch()}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="任务"
        description="异步任务的真实状态。「待确认」表示无法判定上游是否已受理——可能仍在远端执行。"
        actions={
          <Button variant="secondary" size="sm" onClick={() => void tasks.refetch()} icon={<RefreshCw size={16} strokeWidth={1.75} />}>
            刷新
          </Button>
        }
      />

      {items.length === 0 ? (
        <EmptyState
          kind="empty"
          title="还没有任务"
          description="以 async 模式调用能力后，任务会出现在这里。"
        />
      ) : (
        GROUPS.map((group) => {
          const groupTasks = items.filter((task) => group.statuses.includes(task.status));
          if (groupTasks.length === 0 && group.id !== 'unknown') return null;
          return (
            <section key={group.id} aria-label={group.title} className="sr-card p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="flex items-center gap-2 text-16 font-semibold text-content">
                  {group.title}
                  <Badge tone={group.id === 'failed' ? 'danger' : group.id === 'unknown' ? 'warn' : 'neutral'}>
                    {groupTasks.length}
                  </Badge>
                </h2>
                <p className="text-12 text-content-muted">{group.hint}</p>
              </div>

              {group.id === 'unknown' && groupTasks.length === 0 ? (
                <p className="mt-2 text-13 text-content-muted">
                  目前没有需要确认的任务。出现 `status_unknown` 时，这里会解释「可能仍在远端执行」并提供对账入口。
                </p>
              ) : (
                <ul className="mt-2 divide-y divide-[var(--sr-border)]">
                  {groupTasks.map((task) => (
                    <li key={task.id} className="flex flex-col gap-2 py-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusBadge kind="task" status={task.status} />
                        <span className="sr-mono text-13 text-content">{task.capability_key}</span>
                        <Badge tone="outline" mono>
                          {task.mode}
                        </Badge>
                        <span className="text-12 text-content-muted">
                          更新 {formatRelative(task.updated_at)} · 尝试 {task.attempts}/{task.max_attempts}
                        </span>
                        {task.upstream_task_id ? (
                          <span className="sr-mono text-12 text-content-muted">上游 {task.upstream_task_id}</span>
                        ) : null}
                        <span className="ml-auto flex items-center gap-1">
                          <Button
                            variant="secondary"
                            size="sm"
                            loading={busyId === task.id}
                            disabled={task.status !== 'status_unknown'}
                            onClick={() => reconcile.mutate(task.id)}
                          >
                            对账
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            loading={busyId === task.id}
                            disabled={['succeeded', 'failed', 'canceled'].includes(task.status)}
                            onClick={() => {
                              setCancelResult(null);
                              setCancelTarget(task);
                            }}
                            icon={<XCircle size={14} strokeWidth={1.75} />}
                          >
                            取消
                          </Button>
                        </span>
                      </div>

                      {/* 进度条 */}
                      <div className="flex items-center gap-2">
                        <div
                          role="progressbar"
                          aria-valuenow={task.progress_percent}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-label={`${task.capability_key} 进度`}
                          className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--sr-surface-sunken)]"
                        >
                          <div
                            className={cn(
                              'h-full rounded-full',
                              task.status === 'failed'
                                ? 'bg-danger'
                                : task.status === 'status_unknown'
                                  ? 'bg-warn'
                                  : 'bg-accent',
                            )}
                            style={{ width: `${Math.min(100, Math.max(0, task.progress_percent))}%` }}
                          />
                        </div>
                        <span className="text-12 text-content-muted">
                          {task.progress_percent}%{task.progress_stage ? ` · ${task.progress_stage}` : ''}
                        </span>
                      </div>

                      {task.status === 'status_unknown' ? (
                        <div className="rounded border border-warn bg-[var(--sr-warn-weak)] px-2 py-1.5">
                          <p className="flex items-start gap-1.5 text-12 text-content">
                            <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5 text-warn" aria-hidden />
                            可能仍在远端执行：请求已发出但未收到响应。请先「对账」确认上游状态，避免重复提交产生双份费用。
                          </p>
                        </div>
                      ) : null}

                      {task.error_code ? (
                        <p className="text-12 text-content-muted">
                          错误码 <code className="sr-mono text-danger">{task.error_code}</code>
                          {task.error_detail_redacted ? ` · ${task.error_detail_redacted}` : ''}
                          {task.failure_class ? ` · 分类 ${task.failure_class}` : ''}
                        </p>
                      ) : null}

                      <p className="text-12 text-content-muted">
                        request_id <code className="sr-mono select-all">{task.request_id}</code> · 创建 {formatDateTime(task.created_at)}
                        {task.finished_at ? ` · 完成 ${formatDateTime(task.finished_at)}` : ''}
                      </p>

                      {task.output_files.length > 0 ? (
                        <ul className="flex flex-wrap gap-1">
                          {task.output_files.map((file) => (
                            <li key={file.file_id}>
                              <Badge tone="outline" mono>
                                {file.file_id} · {file.mime}
                              </Badge>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          );
        })
      )}

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
          description={
            cancelTarget
              ? `${cancelTarget.capability_key} · ${cancelTarget.status}`
              : undefined
          }
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
                loading={cancelTask.isPending}
                disabled={!cancelTarget || Boolean(cancelResult)}
                onClick={() => {
                  if (cancelTarget) cancelTask.mutate(cancelTarget.id);
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
                若仅能停止本地等待，远端可能继续执行并产生费用。
              </p>
              {cancelResult ? (
                <div
                  className={cn(
                    'rounded px-2 py-1.5',
                    cancelResult.cancel_scope === 'remote'
                      ? 'border border-ok bg-[var(--sr-ok-weak)]'
                      : 'border border-warn bg-[var(--sr-warn-weak)]',
                  )}
                >
                  <p className="text-content">
                    取消范围：{cancelResult.cancel_scope === 'remote' ? '远端已受理' : '仅本地'}
                  </p>
                  <p className="mt-0.5 text-12 text-content-muted">当前状态：{cancelResult.status}</p>
                  {cancelResult.cancel_scope === 'local_only' ? (
                    <p className="mt-1 text-12 text-content-muted">
                      建议到「任务」里对该任务执行「对账」，确认上游是否仍在运行。
                    </p>
                  ) : null}
                </div>
              ) : null}
              {cancelTask.isError ? (
                <RequestIdCopy requestId={describeError(cancelTask.error).requestId ?? ''} />
              ) : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <p className="text-12 text-content-muted">
        提示：任务列表按更新时间倒序，最多展示 100 条；历史数据受「数据保留」设置约束（见设置页）。
      </p>
    </div>
  );
}
