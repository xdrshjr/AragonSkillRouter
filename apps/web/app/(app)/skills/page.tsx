'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Plus } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { DataTable, useTableState, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Paginated, Skill } from '@/lib/types';

const FILTER_KEYS = ['status', 'enabled'] as const;

/**
 * SKILL 列表（spec §13.2 `/skills`）：
 * 「已发布」与「已启用」是两个独立列、独立操作；停用不改变版本。
 */
export default function SkillsPage() {
  const table = useTableState({ defaultSort: '-updated_at', filterKeys: FILTER_KEYS });
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [busyId, setBusyId] = React.useState<string | null>(null);

  const list = useQuery<Paginated<Skill>>({
    queryKey: queryKeys.skills.list(table.requestParams),
    queryFn: () => apiGet<Paginated<Skill>>('/admin/skills', table.requestParams),
  });

  const toggle = useMutation({
    mutationFn: async ({ skill, enable }: { skill: Skill; enable: boolean }) =>
      apiPost<Skill>(`/admin/skills/${skill.id}/${enable ? 'enable' : 'disable'}`, {}),
    onMutate: ({ skill }) => setBusyId(skill.id),
    onSuccess: async (_data, { skill, enable }) => {
      toast({
        title: enable ? 'SKILL 已启用' : 'SKILL 已停用',
        description: enable
          ? `「${skill.name}」重新对外可见并可执行。`
          : `「${skill.name}」立即对外不可见；版本号不变（${skill.latest_version ?? '无'}）。`,
        tone: 'ok',
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.skills.all });
      await queryClient.invalidateQueries({ queryKey: queryKeys.skills.detail(skill.id) });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '操作失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
    onSettled: () => setBusyId(null),
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '发布状态',
      value: table.filters['status'] ?? '',
      options: [
        { value: 'draft', label: '草稿' },
        { value: 'published', label: '已发布' },
        { value: 'deprecated', label: '已停用' },
      ],
    },
    {
      id: 'enabled',
      label: '启用',
      value: table.filters['enabled'] ?? '',
      options: [
        { value: 'true', label: '已启用' },
        { value: 'false', label: '已停用' },
      ],
    },
  ];

  const columns: DataTableColumn<Skill>[] = [
    {
      id: 'title',
      header: '名称',
      sortable: true,
      cardTitle: true,
      cell: (row) => (
        <div className="flex flex-col">
          <Link href={`/skills/${row.id}`} className="text-14 text-content hover:underline">
            {row.title}
          </Link>
          <span className="sr-mono text-12 text-content-muted">{row.name}</span>
        </div>
      ),
    },
    {
      id: 'category',
      header: '分类',
      sortable: true,
      cell: (row) => <span className="text-13 text-content-muted">{row.category ?? '—'}</span>,
    },
    {
      id: 'latest_version',
      header: '版本',
      cell: (row) => <span className="sr-mono text-13 text-content">{row.latest_version ?? '未发布'}</span>,
    },
    {
      id: 'dependency_count',
      header: '依赖',
      align: 'right',
      cell: (row) => <span className="text-13 text-content">{row.dependency_count ?? 0}</span>,
    },
    {
      id: 'status',
      header: '发布状态',
      sortable: true,
      cell: (row) => <StatusBadge kind="skill" status={row.status} />,
    },
    {
      id: 'enabled',
      header: '启用开关',
      cell: (row) => (
        <div className="flex items-center gap-2">
          <Switch
            id={`skill-enabled-${row.id}`}
            checked={row.enabled}
            disabled={busyId === row.id}
            onCheckedChange={(checked) => toggle.mutate({ skill: row, enable: checked })}
            label={`${row.enabled ? '停用' : '启用'} SKILL ${row.name}`}
          />
          <Badge tone={row.enabled ? 'ok' : 'danger'} dot>
            {row.enabled ? '已启用' : '已停用'}
          </Badge>
        </div>
      ),
    },
    {
      id: 'source',
      header: '来源',
      hideOnCard: true,
      cell: (row) => (
        <span className="text-12 text-content-muted">
          {row.source_type}
          {row.source_ref ? ` · ${row.source_ref}` : ''}
        </span>
      ),
    },
    {
      id: 'updated_at',
      header: '更新时间',
      sortable: true,
      hideOnCard: true,
      cell: (row) => <span className="text-13 text-content-muted">{formatRelative(row.updated_at)}</span>,
    },
    {
      id: 'actions',
      header: '操作',
      align: 'right',
      cell: (row) => (
        <div className="flex items-center justify-end gap-1">
          <Button asChild variant="ghost" size="sm">
            <Link href={`/skills/${row.id}`}>详情</Link>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`导出 ${row.name}`}
            onClick={async () => {
              try {
                const result = await apiPost<{ file_id?: string; url?: string }>(`/admin/skills/${row.id}/export`, {
                  format: 'zip',
                });
                toast({
                  title: '导出已生成',
                  description: result.file_id ? `产物 ${result.file_id}` : '请到详情页下载',
                  tone: 'ok',
                });
              } catch (cause) {
                const info = describeError(cause);
                toast({ title: '导出失败', description: info.message, tone: 'danger', requestId: info.requestId });
              }
            }}
            icon={<Download size={16} strokeWidth={1.75} />}
          />
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="SKILL"
        description="对外分发单元。「已发布」决定版本，「已启用」决定当前是否对外可见与可执行——两者独立。"
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href="/skills/new" className="inline-flex items-center gap-1">
              <Plus size={16} strokeWidth={1.75} aria-hidden />
              新建 / 导入
            </Link>
          </Button>
        }
      />

      <DataTable
        caption="SKILL 列表"
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
        emptyKind="first-use"
        emptyTitle="还没有 SKILL"
        emptyDescription="可以从 git / URL 导入，或手工创建。导入会先给预览，不直接落地。"
        emptyActionLabel="新建 / 导入"
        onEmptyAction={() => window.location.assign('/skills/new')}
        onRowClick={(row) => window.location.assign(`/skills/${row.id}`)}
      />
    </div>
  );
}
