'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ExternalLink, Plus, RotateCcw, Server } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { useProvidersQuery } from '@/lib/queries';
import {
  PROVIDER_CATEGORIES,
  PROVIDER_ENVIRONMENTS,
  type Provider,
  type ProviderCategory,
  type ProviderEnvironment,
  type ProviderFilters,
} from '@/lib/types';
import { formatDateTimeShort, formatNumber, statusLabel } from '@/lib/format';
import { ProviderFormDialog } from '@/components/providers/provider-form-dialog';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { Pagination } from '@/components/ui/pagination';
import { SearchInput } from '@/components/ui/search-input';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { SortableTH, Table, TBody, TD, TH, THead, TR, type SortState } from '@/components/ui/table';
import { useToast } from '@/components/ui/toast';

const PAGE_SIZE = 25;

const SORT_GETTERS: Record<string, (provider: Provider) => string | number> = {
  name: (provider) => provider.name.toLowerCase(),
  category: (provider) => provider.category,
  environment: (provider) => provider.environment,
  status: (provider) => provider.status,
  credentials: (provider) => provider.counts.credentials,
  capabilities: (provider) => provider.counts.capabilities,
  updated_at: (provider) => provider.updated_at,
};

export default function ProvidersPage() {
  const router = useRouter();
  const toast = useToast();
  const [filters, setFilters] = useState<ProviderFilters>({
    search: '',
    category: '',
    environment: '',
    limit: PAGE_SIZE,
    offset: 0,
  });
  const [sort, setSort] = useState<SortState>({ key: 'name', direction: 'asc' });
  const [createOpen, setCreateOpen] = useState(false);

  const query = useProvidersQuery(filters);

  const updateFilters = (patch: Partial<ProviderFilters>) => {
    setFilters((current) => ({ ...current, ...patch, offset: 0 }));
  };

  const onSort = (key: string) => {
    setSort((current) =>
      current.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' },
    );
  };

  const rows = useMemo(() => {
    const items = query.data?.items ?? [];
    const getter = SORT_GETTERS[sort.key] ?? SORT_GETTERS.name;
    const copy = [...items];
    copy.sort((a, b) => {
      const left = getter(a);
      const right = getter(b);
      if (left === right) return 0;
      const result = left < right ? -1 : 1;
      return sort.direction === 'asc' ? result : -result;
    });
    return copy;
  }, [query.data?.items, sort]);

  const hasFilters = Boolean(filters.search || filters.category || filters.environment || filters.status);
  const total = query.data?.total ?? 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Providers"
        description="Upstream services a capability can call. Credentials and capabilities attach to a provider."
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)} iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}>
            New provider
          </Button>
        }
      />

      <div className="flex flex-wrap items-end gap-2">
        <div className="w-full sm:w-64">
          <SearchInput
            label="Search providers"
            value={filters.search ?? ''}
            placeholder="Search name, slug, tag or notes"
            onChange={(value) => updateFilters({ search: value })}
          />
        </div>
        <div className="w-full sm:w-40">
          <label htmlFor="provider-filter-category" className="mb-1 block text-xs text-muted">
            Category
          </label>
          <Select
            id="provider-filter-category"
            value={filters.category ?? ''}
            placeholder="All categories"
            onChange={(event) => updateFilters({ category: event.target.value as ProviderCategory | '' })}
            options={PROVIDER_CATEGORIES.map((value) => ({ value, label: statusLabel(value) }))}
          />
        </div>
        <div className="w-full sm:w-40">
          <label htmlFor="provider-filter-environment" className="mb-1 block text-xs text-muted">
            Environment
          </label>
          <Select
            id="provider-filter-environment"
            value={filters.environment ?? ''}
            placeholder="All environments"
            onChange={(event) => updateFilters({ environment: event.target.value as ProviderEnvironment | '' })}
            options={PROVIDER_ENVIRONMENTS.map((value) => ({ value, label: statusLabel(value) }))}
          />
        </div>
        <div className="w-full sm:w-36">
          <label htmlFor="provider-filter-status" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select
            id="provider-filter-status"
            value={filters.status ?? ''}
            placeholder="All statuses"
            onChange={(event) => updateFilters({ status: event.target.value as ProviderFilters['status'] })}
            options={[
              { value: 'active', label: 'Active' },
              { value: 'disabled', label: 'Disabled' },
            ]}
          />
        </div>
        {hasFilters ? (
          <Button
            variant="ghost"
            size="sm"
            iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
            onClick={() => setFilters({ search: '', category: '', environment: '', status: '', limit: PAGE_SIZE, offset: 0 })}
          >
            Reset filters
          </Button>
        ) : null}
      </div>

      {query.isError ? (
        <Banner
          tone="danger"
          title="Could not load providers"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      ) : null}

      {query.isLoading ? (
        <SkeletonTable rows={6} columns={6} />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Server aria-hidden className="h-5 w-5" />}
          title={hasFilters ? 'No provider matches these filters' : 'No providers yet'}
          description={
            hasFilters
              ? 'Clear the filters to see every provider, or create a new one.'
              : 'Create the first provider, then attach a credential so capabilities can authenticate against it.'
          }
          action={
            hasFilters ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setFilters({ search: '', category: '', environment: '', status: '', limit: PAGE_SIZE, offset: 0 })}
              >
                Clear filters
              </Button>
            ) : (
              <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>
                New provider
              </Button>
            )
          }
        />
      ) : (
        <>
          <Table caption="Providers" maxHeightClassName="max-h-[560px]">
            <THead>
              <TR>
                <SortableTH label="Name" sortKey="name" sort={sort} onSort={onSort} />
                <SortableTH label="Category" sortKey="category" sort={sort} onSort={onSort} />
                <SortableTH label="Environment" sortKey="environment" sort={sort} onSort={onSort} />
                <SortableTH label="Status" sortKey="status" sort={sort} onSort={onSort} />
                <SortableTH label="Credentials" sortKey="credentials" sort={sort} onSort={onSort} align="right" />
                <SortableTH label="Capabilities" sortKey="capabilities" sort={sort} onSort={onSort} align="right" />
                <SortableTH label="Updated" sortKey="updated_at" sort={sort} onSort={onSort} />
                <TH align="right">
                  <span className="sr-visually-hidden">Actions</span>
                </TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((provider) => (
                <TR key={provider.id}>
                  <TD className="max-w-[260px]">
                    <Link href={`/providers/${provider.id}`} className="font-medium hover:underline">
                      {provider.name}
                    </Link>
                    <span className="block truncate text-[11px] text-muted">{provider.slug}</span>
                  </TD>
                  <TD>
                    <Badge tone="info" dot={false}>
                      {statusLabel(provider.category)}
                    </Badge>
                  </TD>
                  <TD>
                    <Badge tone="neutral" dot={false}>
                      {statusLabel(provider.environment)}
                    </Badge>
                  </TD>
                  <TD>
                    <StatusBadge status={provider.status} />
                  </TD>
                  <TD align="right" className="tabular-nums">
                    {formatNumber(provider.counts.credentials)}
                  </TD>
                  <TD align="right" className="tabular-nums">
                    {formatNumber(provider.counts.capabilities)}
                  </TD>
                  <TD>
                    <span className="text-xs text-muted">{formatDateTimeShort(provider.updated_at)}</span>
                  </TD>
                  <TD align="right">
                    <div className="flex items-center justify-end gap-1">
                      {provider.docs_url ? (
                        <a
                          href={provider.docs_url}
                          target="_blank"
                          rel="noreferrer noopener"
                          aria-label={`Open documentation for ${provider.name}`}
                          className="rounded-control p-1 text-muted transition-colors duration-150 hover:bg-info-soft hover:text-text"
                        >
                          <ExternalLink aria-hidden className="h-3.5 w-3.5" />
                        </a>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => router.push(`/providers/${provider.id}`)}
                      >
                        Open
                      </Button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[11px] text-muted">
              Sorting applies to the {rows.length} rows on this page — the list endpoint has no sort parameter.
            </p>
            <Pagination
              total={total}
              limit={filters.limit ?? PAGE_SIZE}
              offset={filters.offset ?? 0}
              itemLabel="providers"
              disabled={query.isFetching}
              onChange={(offset) => setFilters((current) => ({ ...current, offset }))}
            />
          </div>
        </>
      )}

      <ProviderFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSaved={(provider) => {
          toast.info({ title: 'Add a credential next', description: `${provider.name} has no credential yet.` });
          router.push(`/providers/${provider.id}`);
        }}
      />
    </div>
  );
}
