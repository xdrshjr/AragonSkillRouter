'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleAlert, Cpu, RefreshCw, Rocket, RotateCcw, ShieldCheck } from 'lucide-react';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { formatDateTime, formatRelative, statusLabel } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type {
  EngineCandidate,
  EngineCapabilityGroups,
  EngineCapabilityState,
  EngineStatus,
  EngineUpgradeRun,
  Paginated,
} from '@/lib/types';

/**
 * 引擎设置（spec §13.2 `/settings/engine`）：
 * 当前版本与 pin、候选版本、验证结果明细、升级策略、升级记录、能力状态表。
 * 「已可用 / 待适配 / 受权限限制」三色标注；升级中展示实时进度；失败展示失败检查项。
 */
export default function EngineSettingsPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [targetVersion, setTargetVersion] = React.useState('');
  const [mode, setMode] = React.useState<'verify_only' | 'deploy'>('verify_only');
  const [upgradeOpen, setUpgradeOpen] = React.useState(false);
  const [rollbackOpen, setRollbackOpen] = React.useState(false);

  const status = useQuery<EngineStatus>({
    queryKey: queryKeys.engine.status(),
    queryFn: () => apiGet<EngineStatus>('/admin/engine/status'),
    refetchInterval: 30_000,
  });

  const candidates = useQuery<EngineCandidate[] | Paginated<EngineCandidate>>({
    queryKey: queryKeys.engine.candidates(),
    queryFn: () => apiGet<EngineCandidate[] | Paginated<EngineCandidate>>('/admin/engine/candidates'),
  });

  const runs = useQuery<EngineUpgradeRun[] | Paginated<EngineUpgradeRun>>({
    queryKey: queryKeys.engine.runs(),
    queryFn: () => apiGet<EngineUpgradeRun[] | Paginated<EngineUpgradeRun>>('/admin/engine/runs', { page: 1, page_size: 20 }),
    refetchInterval: 20_000,
  });

  const capabilityStatus = useQuery<EngineCapabilityGroups>({
    queryKey: queryKeys.engine.capabilities(),
    queryFn: () => apiGet<EngineCapabilityGroups>('/admin/engine/capabilities'),
  });

  const candidateList = React.useMemo(() => {
    const data = candidates.data;
    if (!data) return [];
    return Array.isArray(data) ? data : data.items;
  }, [candidates.data]);

  const runList = React.useMemo(() => {
    const data = runs.data;
    if (!data) return [];
    return Array.isArray(data) ? data : data.items;
  }, [runs.data]);

  // /admin/engine/capabilities 返回按状态分组的结构：{groups: {state: [...]}, note}
  const capabilityList = React.useMemo(() => {
    const groups = capabilityStatus.data?.groups;
    if (!groups) return [];
    const stateOrder: EngineCapabilityState[] = ['available', 'needs_adapter', 'permission_limited', 'unsupported'];
    return stateOrder.flatMap((state) =>
      (groups[state] ?? []).map((item) => ({
        capability_key: item.key,
        state,
        detail: item.detail,
        since_version: item.since_version,
      })),
    );
  }, [capabilityStatus.data]);

  React.useEffect(() => {
    if (!targetVersion) {
      const first = candidateList[0];
      if (first) setTargetVersion(first.version);
    }
  }, [candidateList, targetVersion]);

  const check = useMutation({
    mutationFn: async () => apiPost<{ found?: number }>('/admin/engine/check', {}),
    onSuccess: async () => {
      toast({ title: '已触发检测', description: '双通道检测结果会更新候选列表。', tone: 'info' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.engine.candidates() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.engine.status() });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '检测失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const upgrade = useMutation({
    mutationFn: async () => apiPost<{ run_id: string; status: string; mode: string }>('/admin/engine/upgrade', { version: targetVersion, mode }),
    onSuccess: async (run) => {
      toast({
        title: mode === 'deploy' ? '升级已开始' : '验证已开始',
        description: `运行 ${run.run_id}（${run.status}）：${targetVersion}`,
        tone: 'info',
      });
      setUpgradeOpen(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.engine.runs() });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: info.code === 'UPGRADE_IN_PROGRESS' ? '已有升级在运行' : '升级失败',
        description: info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const rollback = useMutation({
    mutationFn: async () => apiPost<EngineUpgradeRun>('/admin/engine/rollback', { to_version: targetVersion }),
    onSuccess: async () => {
      toast({ title: '回滚已开始', description: `目标版本 ${targetVersion}`, tone: 'info' });
      setRollbackOpen(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.engine.runs() });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '回滚失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  if (status.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        <Skeleton height={200} rounded="md" />
        <Skeleton height={260} rounded="md" />
      </div>
    );
  }

  if (status.isError) {
    const info = describeError(status.error);
    return (
      <EmptyState
        kind={info.code === 'ENGINE_UNAVAILABLE' ? 'unavailable' : info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
        title={info.code === 'ENGINE_UNAVAILABLE' ? 'Agent 引擎网关不可用' : info.message}
        description={info.hint ?? '检查网关进程是否运行（agents/aragon-gateway）。'}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void status.refetch()}
      />
    );
  }

  const data = status.data;
  if (!data) return null;

  const runningRun = runList.find((run) => !run.finished_at && run.result === null);
  const failedRun = runList.find((run) => run.result === 'failed');

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="引擎与升级"
        breadcrumbs={[{ label: '设置', href: '/settings' }]}
        description="AragonAgent 引擎版本、候选与验证结果。升级采用隔离构建 → 验证 → 切换 → 可回滚。"
        status={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge kind="engine" status={data.engine.status} />
            <Badge tone="outline" mono>
              {data.engine.version ?? '未知版本'}
            </Badge>
            <Badge tone="outline" mono>
              pin {data.engine_pin}
            </Badge>
            <Badge tone="neutral">适配层 {data.adapter_version}</Badge>
            <Badge tone="neutral">策略 {statusLabel(data.engine_policy)}</Badge>
            {data.engine.reachable === false ? <Badge tone="danger" dot>网关不可达</Badge> : null}
          </div>
        }
        actions={
          <>
            <Button variant="secondary" size="sm" loading={check.isPending} onClick={() => check.mutate()} icon={<RefreshCw size={16} strokeWidth={1.75} />}>
              立即检测
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!targetVersion}
              onClick={() => setUpgradeOpen(true)}
              icon={<Rocket size={16} strokeWidth={1.75} />}
            >
              升级 / 验证
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!targetVersion}
              onClick={() => setRollbackOpen(true)}
              icon={<RotateCcw size={16} strokeWidth={1.75} />}
            >
              回滚
            </Button>
          </>
        }
      />

      {/* 升级中：实时进度 */}
      {runningRun ? (
        <section aria-label="升级进度" className="sr-card p-4">
          <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
            <Cpu size={16} strokeWidth={1.75} aria-hidden />
            正在{runningRun.mode === 'deploy' ? '升级' : '验证'}：{runningRun.from_version} → {runningRun.to_version}
          </h2>
          <ol className="mt-2 flex flex-col gap-1">
            {runningRun.steps.map((step) => (
              <li key={step.name} className="flex items-center gap-2 text-13">
                <StatusBadge kind="step" status={step.status} />
                <span className="text-content">{step.name}</span>
                {step.detail ? <span className="text-12 text-content-muted">{step.detail}</span> : null}
              </li>
            ))}
          </ol>
          <p className="mt-1 text-12 text-content-muted">开始于 {formatRelative(runningRun.started_at)}（每 20s 自动刷新）</p>
        </section>
      ) : null}

      {/* 失败检查项 */}
      {failedRun ? (
        <section aria-label="最近失败" className="rounded-md border border-danger bg-[var(--sr-danger-weak)] p-4">
          <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
            <CircleAlert size={16} strokeWidth={1.75} className="text-danger" aria-hidden />
            最近一次升级失败（{failedRun.from_version} → {failedRun.to_version}）
          </h2>
          <ul className="mt-2 flex flex-col gap-1">
            {failedRun.steps
              .filter((step) => step.status === 'failed')
              .map((step) => (
                <li key={step.name} className="text-13 text-content">
                  · <span className="font-medium">{step.name}</span>
                  {step.detail ? <span className="text-content-muted">：{step.detail}</span> : null}
                </li>
              ))}
          </ul>
          {failedRun.error ? <p className="mt-1 text-12 text-content-muted">{failedRun.error}</p> : null}
          {failedRun.rollback_of ? (
            <p className="mt-1 text-12 text-content-muted">该运行是回滚运行（{failedRun.rollback_of}）。</p>
          ) : null}
        </section>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 候选版本 */}
        <section aria-label="候选版本" className="sr-card p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-16 font-semibold text-content">候选版本（{candidateList.length}）</h2>
            <span className="text-12 text-content-muted">最近检测：{data.last_checked_at ? formatRelative(data.last_checked_at) : '未知'}</span>
          </div>
          {candidates.isLoading ? (
            <Skeleton height={100} className="mt-2" />
          ) : candidateList.length === 0 ? (
            <p className="mt-2 text-13 text-content-muted">没有发现候选版本。可点「立即检测」。</p>
          ) : (
            <ul className="mt-2 divide-y divide-[var(--sr-border)]">
              {candidateList.map((candidate) => (
                <li key={candidate.id} className="py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="sr-mono text-13 font-medium text-content">{candidate.version}</span>
                    <Badge tone="outline" mono>
                      {candidate.provenance}
                    </Badge>
                    <Badge
                      tone={
                        candidate.verification_status === 'passed'
                          ? 'ok'
                          : candidate.verification_status === 'failed'
                            ? 'danger'
                            : 'neutral'
                      }
                      dot
                    >
                      验证{statusLabel(candidate.verification_status)}
                    </Badge>
                    {candidate.channel ? <Badge tone="neutral">{candidate.channel}</Badge> : null}
                    <span className="text-12 text-content-muted">
                      发布 {candidate.published_at ? formatDateTime(candidate.published_at) : '未知'}
                    </span>
                  </div>
                  {candidate.notes ? <p className="mt-0.5 text-12 text-content-muted">{candidate.notes}</p> : null}
                  {Object.keys(candidate.verification_report).length > 0 ? (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-12 text-content-muted">验证结果明细</summary>
                      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                        {JSON.stringify(candidate.verification_report, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                  <div className="mt-1 flex items-center gap-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setTargetVersion(candidate.version);
                        setUpgradeOpen(true);
                      }}
                    >
                      选择此版本
                    </Button>
                    {candidate.id === candidateList[0]?.id ? <span className="text-12 text-content-muted">最新候选</span> : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* 能力状态表 */}
        <section aria-label="引擎能力状态" className="sr-card p-4">
          <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
            <ShieldCheck size={16} strokeWidth={1.75} aria-hidden />
            能力状态（{capabilityList.length}）
          </h2>
          <p className="mt-0.5 text-12 text-content-muted">
            升级不会自动把新能力标为可用；状态由验证流程重算并记录 since_version。
          </p>
          {capabilityStatus.isLoading ? (
            <Skeleton height={120} className="mt-2" />
          ) : capabilityList.length === 0 ? (
            <p className="mt-2 text-13 text-content-muted">还没有能力状态记录。</p>
          ) : (
            <div className="mt-2 overflow-x-auto sr-scroll">
              <table className="w-full text-13">
                <caption className="sr-only">引擎能力状态（已可用 / 待适配 / 受权限限制 / 不支持）</caption>
                <thead>
                  <tr className="border-b border-border">
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">能力</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">状态</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">自版本</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">说明</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--sr-border)]">
                  {capabilityList.map((item) => (
                    <tr key={item.capability_key}>
                      <td className="px-2 py-1.5 sr-mono text-12 text-content">{item.capability_key}</td>
                      <td className="px-2 py-1.5">
                        <StatusBadge kind="engine" status={item.state} />
                      </td>
                      <td className="px-2 py-1.5 sr-mono text-12 text-content-muted">{item.since_version ?? '—'}</td>
                      <td className="px-2 py-1.5 text-12 text-content-muted">{item.detail ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {/* 升级记录 */}
      <section aria-label="升级记录" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">升级记录（{runList.length}）</h2>
        {runs.isLoading ? (
          <Skeleton height={120} className="mt-2" />
        ) : runList.length === 0 ? (
          <p className="mt-2 text-13 text-content-muted">还没有升级或验证记录。</p>
        ) : (
          <ul className="mt-2 divide-y divide-[var(--sr-border)]">
            {runList.map((run) => (
              <li key={run.id} className="py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="sr-mono text-13 text-content">
                    {run.from_version} → {run.to_version}
                  </span>
                  <Badge tone={run.mode === 'deploy' ? 'accent' : 'neutral'}>{run.mode === 'deploy' ? '部署' : '仅验证'}</Badge>
                  <Badge
                    tone={run.result === 'passed' ? 'ok' : run.result === 'failed' ? 'danger' : run.result === 'rolled_back' ? 'warn' : 'neutral'}
                    dot
                  >
                    {run.result ? statusLabel(run.result) : '进行中'}
                  </Badge>
                  <span className="text-12 text-content-muted">
                    {formatDateTime(run.started_at)}
                    {run.finished_at ? ` → ${formatDateTime(run.finished_at)}` : ''}
                  </span>
                  <span className="text-12 text-content-muted">策略 {run.policy}</span>
                </div>
                <details className="mt-1">
                  <summary className="cursor-pointer text-12 text-content-muted">检查项明细</summary>
                  <ol className="mt-1 flex flex-col gap-1">
                    {run.steps.map((step) => (
                      <li key={step.name} className="flex items-center gap-2 text-12">
                        <StatusBadge kind="step" status={step.status} />
                        <span className="text-content">{step.name}</span>
                        {step.detail ? <span className="text-content-muted">{step.detail}</span> : null}
                      </li>
                    ))}
                  </ol>
                  {run.backup_file_id ? (
                    <p className="mt-1 sr-mono text-12 text-content-muted">备份产物 {run.backup_file_id}</p>
                  ) : null}
                </details>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 升级确认 */}
      <DangerConfirm
        open={upgradeOpen}
        onOpenChange={setUpgradeOpen}
        title={mode === 'deploy' ? '部署引擎升级' : '仅验证候选版本'}
        action={mode === 'deploy' ? '升级' : '验证'}
        description="升级会隔离构建 → 验证 → 切换 → 健康检查；失败会自动回滚到上一版本。"
        impacts={[
          { id: targetVersion || 'none', label: `目标版本 ${targetVersion || '未选择'}`, detail: `模式 ${mode}` },
          { id: 'current', label: `当前版本 ${data.engine.version ?? '未知'}`, detail: `策略 ${data.engine_policy}` },
        ]}
        confirmLabel={mode === 'deploy' ? `升级到 ${targetVersion}` : `验证 ${targetVersion}`}
        loading={upgrade.isPending}
        onConfirm={() => upgrade.mutateAsync()}
      />

      {/* 回滚确认 */}
      <DangerConfirm
        open={rollbackOpen}
        onOpenChange={setRollbackOpen}
        title="回滚引擎版本"
        action="回滚"
        description="回滚会切回指定版本并重新健康检查；不影响数据库结构（schema_version 只增不减）。"
        impacts={[{ id: targetVersion || 'none', label: `回滚到 ${targetVersion || '未选择'}` }]}
        loading={rollback.isPending}
        onConfirm={() => rollback.mutateAsync()}
      />

      {/* 模式选择（升级弹窗的前置） */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-12 text-content-muted">升级模式：</span>
        <div className="w-[160px]">
          <Select
            size="sm"
            aria-label="升级模式"
            value={mode}
            onValueChange={(value) => setMode(value as 'verify_only' | 'deploy')}
            options={[
              { value: 'verify_only', label: '仅验证' },
              { value: 'deploy', label: '验证并部署' },
            ]}
          />
        </div>
        <span className="text-12 text-content-muted">目标版本：</span>
        <div className="w-[160px]">
          <Select
            size="sm"
            aria-label="目标版本"
            value={targetVersion || undefined}
            onValueChange={setTargetVersion}
            placeholder="选择候选"
            options={candidateList.map((candidate) => ({
              value: candidate.version,
              label: candidate.version,
              hint: candidate.provenance,
            }))}
          />
        </div>
      </div>
    </div>
  );
}
