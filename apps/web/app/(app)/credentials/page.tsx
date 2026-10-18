'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, Plus } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { DataTable, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { listCredentials } from '@/lib/api';
import { formatDateTime, formatRelative } from '@/lib/format';
import { useTableState } from '@/lib/hooks/useTableState';
import { queryKeys } from '@/lib/query';
import type { Credential, CredentialStatus } from '@/lib/types';

/**
 * 凭证列表（spec §A.3.11 / §13.2，F-19）：
 * `DataTable` + 搜索（name/kind/provider_name）+ 状态筛选（含 `expiring`）+ 排序 + 分页，
 * 行内入口跳提供商详情 / 凭证详情。
 *
 * 排序白名单与后端严格一致（`:26 CREDENTIAL_SORT_FIELDS`）：`name/created_at/expires_at/last_verified_at`。
 * 越界字段后端会回 400，所以这里宁可不可排序，也不发一个「点了没反应」的假排序。
 */

const FILTER_KEYS = ['status'] as const;
const SORT_FIELDS = ['name', 'created_at', 'expires_at', 'last_verified_at'] as const;
/** 与后端 `EXPIRING_DAYS = 7` 对齐（`status=expiring` 的定义） */
const EXPIRING_DAYS = 7;
const DAY_MS = 86_400_000;

/** 距离过期的天数（负值 = 已过期）；无过期时间返回 null */
function daysUntil(expiresAt: string | null): number | null {
  if (!expiresAt) return null;
  const parsed = Date.parse(expiresAt);
  if (!Number.isFinite(parsed)) return null;
  return Math.ceil((parsed - Date.now()) / DAY_MS);
}

function ExpiryCell({ expiresAt }: { expiresAt: string | null }) {
  if (!expiresAt) return <span className="text-13 text-content-muted">不自动过期</span>;
  const days = daysUntil(expiresAt);
  return (
    <span className="flex flex-col gap-0.5">
      <span className="text-13 text-content">{formatDateTime(expiresAt)}</span>
      <span className="flex flex-wrap items-center gap-1">
        {days !== null && days < 0 ? (
          <Badge tone="danger" dot>
            已过期 {Math.abs(days)} 天
          </Badge>
        ) : days !== null && days <= EXPIRING_DAYS ? (
          <Badge tone="warn" dot>
            即将过期 · 剩余 {days} 天
          </Badge>
        ) : (
          <span className="text-12 text-content-muted">剩余 {days ?? '—'} 天</span>
        )}
      </span>
    </span>
  );
}

export default function CredentialsPage() {
  const table = useTableState({
    defaultSort: 'name',
    filterKeys: FILTER_KEYS,
    sortWhitelist: SORT_FIELDS,
    fallbackSort: 'name',
  });

  const list = useQuery({
    queryKey: [...queryKeys.credentials.all, 'list', table.requestParams] as const,
    queryFn: () =>
      listCredentials({
        page: table.requestParams.page,
        page_size: table.requestParams.page_size,
        sort: table.requestParams.sort,
        q: table.requestParams.q,
        status: (table.filters['status'] ?? '') as CredentialStatus | 'expiring' | '',
      }),
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '状态',
      value: table.filters['status'] ?? '',
      options: [
        { value: 'active', label: '启用' },
        { value: 'disabled', label: '停用' },
        { value: 'expired', label: '已过期' },
        { value: 'expiring', label: `即将过期（≤${EXPIRING_DAYS} 天）` },
      ],
    },
  ];

  const columns: DataTableColumn<Credential>[] = [
    {
      id: 'name',
      header: '名称',
      sortable: true,
      cardTitle: true,
      cell: (row) => (
        <div className="flex flex-col">
          <Link
            href={`/providers/${row.provider_id}/credentials/${row.id}`}
            className="text-13 text-content hover:underline"
          >
            {row.name}
          </Link>
          <span className="sr-mono text-12 text-content-muted">{row.kind}</span>
        </div>
      ),
    },
    {
      id: 'provider_name',
      header: '提供商',
      hideOnCard: false,
      cell: (row) => (
        <Link href={`/providers/${row.provider_id}`} className="text-13 text-content hover:underline">
          {row.provider_name || row.provider_id}
        </Link>
      ),
    },
    {
      id: 'status',
      header: '状态',
      cell: (row) => (
        <div className="flex flex-wrap items-center gap-1">
          <StatusBadge kind="credential" status={row.status} />
          {row.last_verify_ok === false ? <Badge tone="danger">最近验证失败</Badge> : null}
        </div>
      ),
    },
    {
      id: 'secret',
      header: '掩码 / 指纹',
      hideOnCard: true,
      cell: (row) => (
        <span className="flex flex-col">
          <span className="sr-mono text-12 text-content">{row.secret_mask || '—'}</span>
          <span className="sr-mono text-12 text-content-muted">{row.secret_fingerprint || '—'}</span>
        </span>
      ),
    },
    {
      id: 'expires_at',
      header: '过期时间',
      sortable: true,
      cell: (row) => <ExpiryCell expiresAt={row.expires_at} />,
    },
    {
      id: 'last_verified_at',
      header: '最近验证',
      sortable: true,
      hideOnCard: true,
      cell: (row) =>
        row.last_verified_at ? (
          <span className="text-13 text-content-muted">{formatRelative(row.last_verified_at)}</span>
        ) : (
          <span className="text-13 text-content-muted">未验证</span>
        ),
    },
    {
      id: 'created_at',
      header: '创建时间',
      sortable: true,
      align: 'right',
      hideOnCard: true,
      cell: (row) => <span className="text-13 text-content-muted">{formatRelative(row.created_at)}</span>,
    },
    {
      id: 'actions',
      header: '操作',
      align: 'right',
      cell: (row) => (
        <div className="flex items-center justify-end gap-1">
          <Button asChild variant="ghost" size="sm">
            <Link href={`/providers/${row.provider_id}/credentials/${row.id}`}>详情</Link>
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link href={`/providers/${row.provider_id}`} aria-label={`打开提供商 ${row.provider_name || row.provider_id}`}>
              <ExternalLink size={14} strokeWidth={1.75} aria-hidden />
              提供商
            </Link>
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="凭证"
        description="所有提供商的凭证集中在这里：掩码与指纹可见，明文永不回显；「即将过期」按 ≤7 天计算。"
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href="/providers" className="inline-flex items-center gap-1">
              <Plus size={16} strokeWidth={1.75} aria-hidden />
              到提供商里新增
            </Link>
          </Button>
        }
      />

      <DataTable
        caption="凭证列表"
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
        emptyTitle="还没有凭证"
        emptyDescription="凭证挂在提供商下：先创建提供商，再添加第一把密钥。"
        emptyActionLabel="去提供商"
        onEmptyAction={() => window.location.assign('/providers')}
        onRowClick={(row) => window.location.assign(`/providers/${row.provider_id}/credentials/${row.id}`)}
      />

      <p className="text-12 text-content-muted">
        搜索覆盖名称、类型与提供商名称；排序支持 名称 / 创建时间 / 过期时间 / 最近验证。
        <Link href="/credentials?status=expiring" className="ml-1 text-accent hover:underline">
          只看即将过期的凭证 →
        </Link>
      </p>
    </div>
  );
}
