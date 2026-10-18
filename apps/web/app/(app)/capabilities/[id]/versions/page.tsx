'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitCompare, RotateCcw } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { formatDateTime, prettyJson } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { CapabilityDiff, CapabilityImpact, CapabilityVersion, Paginated } from '@/lib/types';

/** 版本历史（spec §13.2）：时间线 + 字段级 diff（并排）+ 回退（二次确认说明影响的外部 Agent）。 */
export default function CapabilityVersionsPage() {
  const params = useParams<{ id: string }>();
  const capabilityId = params?.id ?? '';
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [fromVersion, setFromVersion] = React.useState('');
  const [toVersion, setToVersion] = React.useState('');
  const [rollbackTarget, setRollbackTarget] = React.useState<CapabilityVersion | null>(null);

  const versions = useQuery<Paginated<CapabilityVersion> | CapabilityVersion[]>({
    queryKey: queryKeys.capabilities.versions(capabilityId),
    queryFn: () =>
      apiGet<Paginated<CapabilityVersion> | CapabilityVersion[]>(`/admin/capabilities/${capabilityId}/versions`),
    enabled: Boolean(capabilityId),
  });

  const impact = useQuery<CapabilityImpact>({
    queryKey: queryKeys.capabilities.impact(capabilityId),
    queryFn: () => apiGet<CapabilityImpact>(`/admin/capabilities/${capabilityId}/impact`),
    enabled: Boolean(capabilityId),
  });

  const items = React.useMemo(() => {
    const data = versions.data;
    if (!data) return [];
    return Array.isArray(data) ? data : data.items;
  }, [versions.data]);

  // 默认比较最近两个版本
  React.useEffect(() => {
    if (items.length >= 2 && !fromVersion && !toVersion) {
      const sorted = [...items].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      setToVersion(sorted[0]?.version ?? '');
      setFromVersion(sorted[1]?.version ?? '');
    }
  }, [items, fromVersion, toVersion]);

  const diff = useQuery<CapabilityDiff>({
    queryKey: queryKeys.capabilities.diff(capabilityId, fromVersion, toVersion),
    queryFn: () =>
      apiGet<CapabilityDiff>(`/admin/capabilities/${capabilityId}/diff`, { from: fromVersion, to: toVersion }),
    enabled: Boolean(capabilityId && fromVersion && toVersion && fromVersion !== toVersion),
    retry: false,
  });

  const rollback = useMutation({
    mutationFn: async (version: string) =>
      apiPost(`/admin/capabilities/${capabilityId}/rollback`, { to_version: version }),
    onSuccess: async () => {
      toast({ title: '已回退', description: '创建了新的发布指针，历史版本行未被修改。', tone: 'ok' });
      setRollbackTarget(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.versions(capabilityId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.detail(capabilityId) });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '回退失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const versionOptions = items.map((version) => ({
    value: version.version,
    label: `${version.version}${version.published_at ? '（已发布）' : ''}`,
  }));

  if (versions.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} height={64} rounded="md" />
        ))}
      </div>
    );
  }

  if (versions.isError) {
    const info = describeError(versions.error);
    return (
      <EmptyState
        kind={info.code === 'NOT_FOUND' ? 'empty' : 'unavailable'}
        title={info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void versions.refetch()}
      />
    );
  }

  const impactedAgents = impact.data?.tokens ?? [];
  const impactedSkills = impact.data?.skills ?? [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="版本历史"
        breadcrumbs={[
          { label: '能力', href: '/capabilities' },
          { label: '能力详情', href: `/capabilities/${capabilityId}` },
        ]}
        description="已发布版本不可修改；回退只移动发布指针，不重写历史行。"
        actions={
          <Button asChild variant="secondary" size="sm">
            <Link href={`/capabilities/${capabilityId}`}>返回编辑器</Link>
          </Button>
        }
      />

      {items.length === 0 ? (
        <EmptyState
          kind="empty"
          title="还没有任何版本"
          description="先在编辑器里配置并跑一次通过的测试，然后发布第一个版本。"
          actionLabel="去编辑器"
          onAction={() => window.location.assign(`/capabilities/${capabilityId}`)}
        />
      ) : (
        <>
          {/* 时间线 */}
          <section aria-label="版本时间线" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">时间线</h2>
            <ol className="mt-3 flex flex-col gap-2">
              {items.map((version) => (
                <li key={version.id} className="flex flex-wrap items-start gap-2 border-l-2 border-border pl-3">
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-2 text-13">
                      <span className="sr-mono font-medium text-content">{version.version}</span>
                      <Badge tone={version.published_at ? 'ok' : 'neutral'} dot>
                        {version.published_at ? '已发布' : version.status}
                      </Badge>
                      {version.published_by ? (
                        <span className="text-12 text-content-muted">由 {version.published_by}</span>
                      ) : null}
                      <span className="text-12 text-content-muted">{formatDateTime(version.published_at ?? version.created_at)}</span>
                    </p>
                    {version.changelog ? (
                      <p className="mt-0.5 text-12 text-content-muted">{version.changelog}</p>
                    ) : null}
                    <p className="sr-mono mt-0.5 text-12 text-content-muted">
                      hash {version.content_hash.slice(0, 12)} · 测试证据 {version.test_run_ids.length} 条
                    </p>
                    <details className="mt-1">
                      <summary className="cursor-pointer text-12 text-content-muted">查看定义快照</summary>
                      <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                        {prettyJson(version.definition, 6000)}
                      </pre>
                    </details>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setRollbackTarget(version)}
                    icon={<RotateCcw size={14} strokeWidth={1.75} />}
                  >
                    回退到此版本
                  </Button>
                </li>
              ))}
            </ol>
          </section>

          {/* diff */}
          <section aria-label="版本差异" className="sr-card p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
                <GitCompare size={16} strokeWidth={1.75} aria-hidden />
                字段级 diff
              </h2>
              <div className="ml-auto flex items-center gap-2">
                <div className="w-[140px]">
                  <Select
                    size="sm"
                    aria-label="比较起点版本"
                    value={fromVersion}
                    onValueChange={setFromVersion}
                    options={versionOptions}
                    placeholder="从"
                  />
                </div>
                <span className="text-12 text-content-muted">→</span>
                <div className="w-[140px]">
                  <Select
                    size="sm"
                    aria-label="比较终点版本"
                    value={toVersion}
                    onValueChange={setToVersion}
                    options={versionOptions}
                    placeholder="到"
                  />
                </div>
              </div>
            </div>

            {fromVersion === toVersion ? (
              <p className="mt-2 text-13 text-content-muted">请选择两个不同的版本。</p>
            ) : diff.isLoading ? (
              <Skeleton height={120} className="mt-3" />
            ) : diff.isError ? (
              <p className="mt-2 text-13 text-content-muted">
                差异不可用：{describeError(diff.error).message}
                {describeError(diff.error).requestId ? (
                  <span className="ml-1 sr-mono text-12">（request_id {describeError(diff.error).requestId}）</span>
                ) : null}
              </p>
            ) : (diff.data?.entries.length ?? 0) === 0 ? (
              <p className="mt-2 text-13 text-content-muted">两个版本在该定义上没有字段级差异。</p>
            ) : (
              <div className="mt-3 overflow-x-auto sr-scroll">
                <table className="w-full text-13">
                  <caption className="px-1 py-1 text-left text-12 text-content-muted">
                    {fromVersion} → {toVersion} 的字段级差异（并排）
                  </caption>
                  <thead>
                    <tr className="border-b border-border">
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">字段路径</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">变更</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">{fromVersion}</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">{toVersion}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--sr-border)]">
                    {diff.data?.entries.map((entry) => (
                      <tr key={entry.path}>
                        <td className="px-2 py-1.5 sr-mono text-12 text-content">{entry.path}</td>
                        <td className="px-2 py-1.5">
                          <Badge tone={entry.kind === 'added' ? 'ok' : entry.kind === 'removed' ? 'danger' : 'warn'}>
                            {entry.kind === 'added' ? '新增' : entry.kind === 'removed' ? '删除' : '修改'}
                          </Badge>
                        </td>
                        <td className="px-2 py-1.5">
                          <pre className="max-h-32 max-w-[280px] overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-danger-weak)] p-1 sr-mono text-12 sr-scroll">
                            {prettyJson(entry.before, 1200)}
                          </pre>
                        </td>
                        <td className="px-2 py-1.5">
                          <pre className="max-h-32 max-w-[280px] overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-ok-weak)] p-1 sr-mono text-12 sr-scroll">
                            {prettyJson(entry.after, 1200)}
                          </pre>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      <DangerConfirm
        open={Boolean(rollbackTarget)}
        onOpenChange={(open) => {
          if (!open) setRollbackTarget(null);
        }}
        title="回退版本"
        action="回退"
        description={`将发布指针切回 ${rollbackTarget?.version ?? ''}；这会立即影响正在使用该能力的 SKILL 与外部 Agent。`}
        impacts={[
          ...(rollbackTarget
            ? [{ id: rollbackTarget.id, label: `目标版本 ${rollbackTarget.version}`, detail: rollbackTarget.changelog }]
            : []),
          ...impactedSkills.map((item) => ({ id: item.id, label: `依赖 SKILL：${item.name}` })),
          ...impactedAgents.map((item) => ({ id: item.id, label: `外部 Agent 令牌：${item.name}` })),
        ]}
        blockedReason={
          impactedSkills.length > 0 || impactedAgents.length > 0
            ? `有 ${impactedSkills.length} 个 SKILL 与 ${impactedAgents.length} 个令牌正在使用该能力，回退会改变它们的实际调用结果。`
            : null
        }
        loading={rollback.isPending}
        onConfirm={async () => {
          if (!rollbackTarget) return;
          await rollback.mutateAsync(rollbackTarget.version);
        }}
      />
    </div>
  );
}
