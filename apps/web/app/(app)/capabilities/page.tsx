'use client';

import { useQuery } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { DataTable, useTableState, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { apiGet } from '@/lib/api';
import { formatPercent, formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Capability, Paginated } from '@/lib/types';

const FILTER_KEYS = ['status', 'kind'] as const;

/** 能力列表（spec §13.2）：四态徽标区分草稿/测试中/已发布/已停用。 */
export default function CapabilitiesPage() {
  const table = useTableState({ defaultSort: '-updated_at', filterKeys: FILTER_KEYS });

  const list = useQuery<Paginated<Capability>>({
    queryKey: queryKeys.capabilities.list(table.requestParams),
    queryFn: () => apiGet<Paginated<Capability>>('/admin/capabilities', table.requestParams),
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '状态',
      value: table.filters['status'] ?? '',
      options: [
        { value: 'draft', label: '草稿' },
        { value: 'published', label: '已发布' },
        { value: 'deprecated', label: '已停用' },
      ],
    },
    {
      id: 'kind',
      label: '类型',
      value: table.filters['kind'] ?? '',
      options: [
        { value: 'text.generate', label: '文本' },
        { value: 'image.generate', label: '图像' },
        { value: 'video.generate', label: '视频' },
        { value: 'audio.speech', label: '语音合成' },
        { value: 'audio.transcribe', label: '语音转写' },
        { value: 'generic.http', label: '通用 HTTP' },
      ],
    },
  ];

  const columns: DataTableColumn<Capability>[] = [
    {
      id: 'key',
      header: '能力 key',
      sortable: true,
      cardTitle: true,
      cell: (row) => (
        <div className="flex flex-col">
          <Link href={`/capabilities/${row.id}`} className="sr-mono text-13 text-content hover:underline">
            {row.key}
          </Link>
          <span className="text-12 text-content-muted">{row.name}</span>
        </div>
      ),
    },
    {
      id: 'kind',
      header: '类型',
      sortable: true,
      cell: (row) => <span className="text-13 text-content-muted">{row.kind}</span>,
    },
    {
      id: 'modes',
      header: '模式',
      hideOnCard: false,
      cell: (row) => (
        <span className="flex flex-wrap gap-1">
          {(row.modes ?? []).map((mode) => (
            <Badge key={mode} tone="outline" mono>
              {mode}
            </Badge>
          ))}
        </span>
      ),
    },
    {
      id: 'status',
      header: '状态',
      sortable: true,
      cell: (row) => (
        <div className="flex flex-wrap items-center gap-1">
          <StatusBadge kind="capability" status={row.status} />
          {!row.enabled ? <Badge tone="danger">已停用</Badge> : null}
          {row.requires_agent_reasoning ? <Badge tone="info">需 Agent 推理</Badge> : null}
        </div>
      ),
    },
    {
      id: 'latest_version',
      header: '发布版本',
      cell: (row) => (
        <span className="sr-mono text-13 text-content">
          {row.latest_version ?? <span className="text-content-muted">未发布</span>}
        </span>
      ),
    },
    {
      id: 'success_rate_24h',
      header: '24h 成功率',
      align: 'right',
      cell: (row) =>
        row.success_rate_24h === undefined || row.success_rate_24h === null ? (
          <span className="text-13 text-content-muted">—</span>
        ) : (
          <span className={row.success_rate_24h < 0.95 ? 'text-13 text-warn' : 'text-13 text-content'}>
            {formatPercent(row.success_rate_24h)}
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
            <Link href={`/capabilities/${row.id}`}>编辑</Link>
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link href={`/capabilities/${row.id}/versions`}>版本</Link>
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="能力"
        description="标准能力是对外契约，routes 是各提供商通道。「草稿 / 测试通过 / 已发布 / 可调用」是四个独立状态。"
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href="/capabilities/new" className="inline-flex items-center gap-1">
              <Plus size={16} strokeWidth={1.75} aria-hidden />
              新建能力
            </Link>
          </Button>
        }
      />

      <DataTable
        caption="能力列表"
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
        emptyTitle="还没有能力"
        emptyDescription="从手工配置、OpenAPI 导入或交给 Agent 三条路开始。"
        emptyActionLabel="新建能力"
        onEmptyAction={() => window.location.assign('/capabilities/new')}
        onRowClick={(row) => window.location.assign(`/capabilities/${row.id}`)}
      />
    </div>
  );
}
