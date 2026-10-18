'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { DataTable, useTableState, type DataTableColumn, type FilterChipGroup } from '@/components/common/DataTable';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Button } from '@/components/ui/button';
import { Drawer, DrawerContent } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { apiDelete, apiGet, apiPost, describeError, isApiError } from '@/lib/api';
import { formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Paginated, Provider } from '@/lib/types';

const FILTER_KEYS = ['status', 'category'] as const;

const providerSchema = z.object({
  name: z.string().min(1, '请输入名称').max(128),
  slug: z
    .string()
    .min(1, '请输入 slug')
    .max(64)
    .regex(/^[a-z0-9][a-z0-9-]*$/, '只允许小写字母、数字与连字符'),
  category: z.string().min(1),
  environment: z.string().min(1),
  base_url: z.string().url('必须是合法 URL').or(z.literal('')),
  docs_url: z.string().url('必须是合法 URL').or(z.literal('')),
  tags: z.string(),
  notes: z.string(),
});

type ProviderForm = z.infer<typeof providerSchema>;

/** 提供商列表（spec §13.2 `/providers`）：搜索 + 筛选 + 分页 + 创建抽屉 + 删除确认（含影响面）。 */
export default function ProvidersPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const table = useTableState({ defaultSort: '-created_at', filterKeys: FILTER_KEYS });

  const [createOpen, setCreateOpen] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState<Provider | null>(null);
  const [deleting, setDeleting] = React.useState(false);

  const list = useQuery<Paginated<Provider>>({
    queryKey: queryKeys.providers.list(table.requestParams),
    queryFn: () => apiGet<Paginated<Provider>>('/admin/providers', table.requestParams),
  });

  const form = useForm<ProviderForm>({
    resolver: zodResolver(providerSchema),
    defaultValues: {
      name: '',
      slug: '',
      category: 'llm',
      environment: 'prod',
      base_url: '',
      docs_url: '',
      tags: '',
      notes: '',
    },
    mode: 'onSubmit',
  });

  const createMutation = useMutation({
    mutationFn: async (values: ProviderForm) =>
      apiPost<Provider>('/admin/providers', {
        ...values,
        base_url: values.base_url || null,
        docs_url: values.docs_url || null,
        notes: values.notes || null,
        tags: values.tags
          .split(',')
          .map((tag) => tag.trim())
          .filter(Boolean),
      }),
    onSuccess: async (created) => {
      toast({ title: '提供商已创建', description: created.name, tone: 'ok' });
      setCreateOpen(false);
      form.reset();
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.all });
    },
    onError: (error) => {
      const info = describeError(error);
      toast({
        title: '创建失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (provider: Provider) => apiDelete<null>(`/admin/providers/${provider.id}`),
    onSuccess: async () => {
      toast({ title: '提供商已删除', tone: 'ok' });
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.all });
    },
    onError: (error) => {
      const info = describeError(error);
      toast({
        title: info.code === 'PROVIDER_IN_USE' ? '仍有关联对象' : '删除失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const filters: FilterChipGroup[] = [
    {
      id: 'status',
      label: '状态',
      value: table.filters['status'] ?? '',
      options: [
        { value: 'active', label: '启用' },
        { value: 'disabled', label: '已停用' },
      ],
    },
    {
      id: 'category',
      label: '分类',
      value: table.filters['category'] ?? '',
      options: [
        { value: 'llm', label: 'LLM' },
        { value: 'image', label: '图像' },
        { value: 'audio', label: '音频' },
        { value: 'video', label: '视频' },
      ],
    },
  ];

  const columns: DataTableColumn<Provider>[] = [
    {
      id: 'name',
      header: '名称',
      cardTitle: true,
      sortable: true,
      cell: (row) => (
        <Link href={`/providers/${row.id}`} className="flex flex-col hover:underline">
          <span className="text-14 text-content">{row.name}</span>
          <span className="sr-mono text-12 text-content-muted">{row.slug}</span>
        </Link>
      ),
    },
    {
      id: 'category',
      header: '分类',
      sortable: true,
      cell: (row) => <span className="text-13 text-content-muted">{row.category}</span>,
    },
    {
      id: 'environment',
      header: '环境',
      hideOnCard: true,
      cell: (row) => (
        <span className="text-13 text-content">{row.environment === 'prod' ? '生产' : row.environment}</span>
      ),
    },
    {
      id: 'credential_count',
      header: '凭证',
      align: 'right',
      cell: (row) => (
        <span className="text-13 text-content">
          {row.credential_count ?? 0}
          {row.active_credential_count !== undefined ? (
            <span className="text-content-muted"> / {row.active_credential_count} 有效</span>
          ) : null}
        </span>
      ),
    },
    {
      id: 'capability_count',
      header: '能力',
      align: 'right',
      cell: (row) => <span className="text-13 text-content">{row.capability_count ?? 0}</span>,
    },
    {
      id: 'status',
      header: '状态',
      sortable: true,
      cell: (row) => <StatusBadge kind="provider" status={row.status} />,
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
            <Link href={`/providers/${row.id}`}>详情</Link>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`删除提供商 ${row.name}`}
            onClick={(event) => {
              event.stopPropagation();
              setDeleteTarget(row);
            }}
            icon={<Trash2 size={16} strokeWidth={1.75} />}
          />
        </div>
      ),
    },
  ];

  const deleteImpacts = deleteTarget
    ? [
        {
          id: deleteTarget.id,
          label: deleteTarget.name,
          detail: `凭证 ${deleteTarget.credential_count ?? 0} 个 · 能力 ${deleteTarget.capability_count ?? 0} 个`,
        },
      ]
    : [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="提供商"
        description="上游服务与凭证的归属单位。删除提供商前必须先处理其凭证与能力路由。"
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)} icon={<Plus size={16} strokeWidth={1.75} />}>
            新建提供商
          </Button>
        }
      />

      <DataTable
        caption="提供商列表"
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
        emptyTitle="还没有提供商"
        emptyDescription="先添加一个提供商并录入凭证，能力路由才有可用的上游通道。"
        emptyActionLabel="新建提供商"
        onEmptyAction={() => setCreateOpen(true)}
        onRowClick={(row) => {
          window.location.assign(`/providers/${row.id}`);
        }}
      />

      {/* 创建抽屉 */}
      <Drawer open={createOpen} onOpenChange={setCreateOpen}>
        <DrawerContent title="新建提供商" description="slug 创建后不建议修改（能力路由会引用它）。">
          <form
            id="create-provider-form"
            noValidate
            className="flex flex-col gap-1"
            onSubmit={form.handleSubmit((values) => createMutation.mutate(values))}
          >
            <FormField id="name" label="名称" required error={form.formState.errors.name?.message}>
              <Input id="name" invalid={Boolean(form.formState.errors.name)} {...form.register('name')} />
            </FormField>
            <FormField
              id="slug"
              label="slug"
              required
              hint="小写字母/数字/连字符"
              error={form.formState.errors.slug?.message}
            >
              <Input id="slug" invalid={Boolean(form.formState.errors.slug)} {...form.register('slug')} />
            </FormField>
            <FormField id="category" label="分类" required error={form.formState.errors.category?.message}>
              <Select
                value={form.watch('category')}
                onValueChange={(value) => form.setValue('category', value)}
                options={[
                  { value: 'llm', label: 'LLM' },
                  { value: 'image', label: '图像' },
                  { value: 'audio', label: '音频' },
                  { value: 'video', label: '视频' },
                  { value: 'other', label: '其它' },
                ]}
              />
            </FormField>
            <FormField id="environment" label="环境" required error={form.formState.errors.environment?.message}>
              <Select
                value={form.watch('environment')}
                onValueChange={(value) => form.setValue('environment', value)}
                options={[
                  { value: 'prod', label: '生产（prod）' },
                  { value: 'staging', label: '预发（staging）' },
                ]}
              />
            </FormField>
            <FormField
              id="base_url"
              label="Base URL"
              hint="可选"
              error={form.formState.errors.base_url?.message}
            >
              <Input id="base_url" placeholder="https://api.openai.com" {...form.register('base_url')} />
            </FormField>
            <FormField id="docs_url" label="文档 URL" hint="可选" error={form.formState.errors.docs_url?.message}>
              <Input id="docs_url" placeholder="https://platform.openai.com/docs" {...form.register('docs_url')} />
            </FormField>
            <FormField id="tags" label="标签" hint="逗号分隔">
              <Input id="tags" placeholder="chat, cheap" {...form.register('tags')} />
            </FormField>
            <FormField id="notes" label="备注">
              <Textarea id="notes" rows={3} {...form.register('notes')} />
            </FormField>
            <div className="mt-2 flex items-center justify-end gap-2">
              <Button variant="secondary" size="sm" type="button" onClick={() => setCreateOpen(false)}>
                取消
              </Button>
              <Button variant="primary" size="sm" type="submit" loading={createMutation.isPending}>
                创建
              </Button>
            </div>
            {createMutation.isError && isApiError(createMutation.error) ? (
              <p className="text-12 text-danger">{createMutation.error.message}</p>
            ) : null}
          </form>
        </DrawerContent>
      </Drawer>

      {/* 删除确认（含影响面） */}
      <DangerConfirm
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title="删除提供商"
        action="删除"
        description="软删除后不再出现在列表；若仍有关联凭证或能力路由，接口会返回 PROVIDER_IN_USE。"
        impacts={deleteImpacts}
        loading={deleting || deleteMutation.isPending}
        onConfirm={async () => {
          if (!deleteTarget) return;
          setDeleting(true);
          try {
            await deleteMutation.mutateAsync(deleteTarget);
          } finally {
            setDeleting(false);
          }
        }}
      />
    </div>
  );
}
