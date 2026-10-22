'use client';

import { Suspense, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Boxes, CheckCircle2, CircleDashed, Plus, RotateCcw } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { useCapabilitiesQuery, useProvidersQuery } from '@/lib/queries';
import {
  CAPABILITY_STATUSES,
  CAPABILITY_TYPES,
  type CapabilityDetail,
  type CapabilityFilters,
  type CapabilityStatus,
  type CapabilityType,
} from '@/lib/types';
import { formatDateTimeShort, statusLabel } from '@/lib/format';
import { CapabilityFormDialog } from '@/components/capabilities/capability-form-dialog';
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
import { Tooltip } from '@/components/ui/tooltip';
import { useToast } from '@/components/ui/toast';

const PAGE_SIZE = 25;

const SORT_GETTERS: Record<string, (row: CapabilityDetail) => string | number> = {
  slug: (row) => row.slug,
  type: (row) => row.type,
  status: (row) => row.status,
  provider: (row) => row.provider_name ?? '',
  test: (row) => row.last_test_status ?? 'none',
  published: (row) => row.published_version ?? -1,
  callable: (row) => (row.callable ? 1 : 0),
  updated_at: (row) => row.updated_at,
};

function LifecycleMark({ done, label, detail }: { done: boolean; label: string; detail: string }) {
  return (
    <Tooltip content={detail}>
      <span className="inline-flex items-center gap-1 text-xs">
        {done ? (
          <CheckCircle2 aria-hidden className="h-3.5 w-3.5 text-success" />
        ) : (
          <CircleDashed aria-hidden className="h-3.5 w-3.5 text-muted" />
        )}
        <span className={done ? 'text-text' : 'text-muted'}>{label}</span>
      </span>
    </Tooltip>
  );
}

function CapabilitiesList() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const toast = useToast();
  const providerFromUrl = searchParams.get('provider_id') ?? '';

  const [filters, setFilters] = useState<CapabilityFilters>({
    search: '',
    type: '',
    status: '',
    provider_id: providerFromUrl,
    limit: PAGE_SIZE,
    offset: 0,
  });
  const [sort, setSort] = useState<SortState>({ key: 'updated_at', direction: 'desc' });
  const [createOpen, setCreateOpen] = useState(false);

  const query = useCapabilitiesQuery(filters);
  const providers = useProvidersQuery({ limit: 200 });

  const updateFilters = (patch: Partial<CapabilityFilters>) =>
    setFilters((current) => ({ ...current, ...patch, offset: 0 }));

  const onSort = (key: string) => {
    setSort((current) =>
      current.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' },
    );
  };

  const rows = useMemo(() => {
    const items = query.data?.items ?? [];
    const getter = SORT_GETTERS[sort.key] ?? SORT_GETTERS.updated_at;
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

  const hasFilters = Boolean(filters.search || filters.type || filters.status || filters.provider_id);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Capabilities"
        description="Each capability has its own saved / tested / published / callable state. Open one to configure and publish it."
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)} iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}>
            New capability
          </Button>
        }
      />

      <div className="flex flex-wrap items-end gap-2">
        <div className="w-full sm:w-64">
          <SearchInput
            label="Search capabilities"
            value={filters.search ?? ''}
            placeholder="Search name, slug or description"
            onChange={(value) => updateFilters({ search: value })}
          />
        </div>
        <div className="w-full sm:w-36">
          <label htmlFor="capability-filter-type" className="mb-1 block text-xs text-muted">
            Type
          </label>
          <Select
            id="capability-filter-type"
            value={filters.type ?? ''}
            placeholder="All types"
            options={CAPABILITY_TYPES.map((value) => ({ value, label: statusLabel(value) }))}
            onChange={(event) => updateFilters({ type: event.target.value as CapabilityType | '' })}
          />
        </div>
        <div className="w-full sm:w-36">
          <label htmlFor="capability-filter-status" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select
            id="capability-filter-status"
            value={filters.status ?? ''}
            placeholder="All statuses"
            options={CAPABILITY_STATUSES.map((value) => ({ value, label: statusLabel(value) }))}
            onChange={(event) => updateFilters({ status: event.target.value as CapabilityStatus | '' })}
          />
        </div>
        <div className="w-full sm:w-48">
          <label htmlFor="capability-filter-provider" className="mb-1 block text-xs text-muted">
            Provider
          </label>
          <Select
            id="capability-filter-provider"
            value={filters.provider_id ?? ''}
            placeholder="All providers"
            options={(providers.data?.items ?? []).map((provider) => ({ value: provider.id, label: provider.name }))}
            onChange={(event) => updateFilters({ provider_id: event.target.value })}
          />
        </div>
        {hasFilters ? (
          <Button
            variant="ghost"
            size="sm"
            iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
            onClick={() =>
              setFilters({ search: '', type: '', status: '', provider_id: '', limit: PAGE_SIZE, offset: 0 })
            }
          >
            Reset filters
          </Button>
        ) : null}
      </div>

      {query.isError ? (
        <Banner
          tone="danger"
          title="Could not load capabilities"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      ) : null}

      {query.isLoading ? (
        <SkeletonTable rows={6} columns={7} />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Boxes aria-hidden className="h-5 w-5" />}
          title={hasFilters ? 'No capability matches these filters' : 'No capabilities yet'}
          description={
            hasFilters
              ? 'Clear the filters, or create a capability that matches them.'
              : 'A capability wraps one upstream call: request, extraction, modes, errors and limits. Start with a draft.'
          }
          action={
            hasFilters ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setFilters({ search: '', type: '', status: '', provider_id: '', limit: PAGE_SIZE, offset: 0 })}
              >
                Clear filters
              </Button>
            ) : (
              <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>
                New capability
              </Button>
            )
          }
        />
      ) : (
        <>
          <Table caption="Capabilities" maxHeightClassName="max-h-[560px]">
            <THead>
              <TR>
                <SortableTH label="Capability" sortKey="slug" sort={sort} onSort={onSort} />
                <SortableTH label="Type" sortKey="type" sort={sort} onSort={onSort} />
                <SortableTH label="Status" sortKey="status" sort={sort} onSort={onSort} />
                <SortableTH label="Provider" sortKey="provider" sort={sort} onSort={onSort} />
                <TH>Saved</TH>
                <SortableTH label="Test" sortKey="test" sort={sort} onSort={onSort} />
                <SortableTH label="Published" sortKey="published" sort={sort} onSort={onSort} />
                <SortableTH label="Callable" sortKey="callable" sort={sort} onSort={onSort} />
                <SortableTH label="Updated" sortKey="updated_at" sort={sort} onSort={onSort} />
              </TR>
            </THead>
            <TBody>
              {rows.map((row) => (
                <TR key={row.id}>
                  <TD className="max-w-[240px]">
                    <Link href={`/capabilities/${row.id}`} className="font-medium hover:underline">
                      {row.slug}
                    </Link>
                    <span className="block truncate text-[11px] text-muted">{row.name}</span>
                  </TD>
                  <TD>
                    <Badge tone="info" dot={false}>
                      {statusLabel(row.type)}
                    </Badge>
                  </TD>
                  <TD>
                    <StatusBadge status={row.status} />
                  </TD>
                  <TD className="max-w-[140px]">
                    <span className="sr-truncate text-xs">{row.provider_name ?? '—'}</span>
                  </TD>
                  <TD>
                    <LifecycleMark
                      done={row.saved}
                      label={row.saved ? `v${row.current_version ?? '?'}` : 'no'}
                      detail="Saved means the config is persisted, not that it works."
                    />
                  </TD>
                  <TD>
                    <LifecycleMark
                      done={row.last_test_status === 'passed'}
                      label={row.last_test_status ? statusLabel(row.last_test_status) : 'none'}
                      detail="A test is a real upstream call whose evidence is stored."
                    />
                  </TD>
                  <TD>
                    <LifecycleMark
                      done={row.published_version !== null}
                      label={row.published_version !== null ? `v${row.published_version}` : 'no'}
                      detail="Published means an immutable version is live."
                    />
                  </TD>
                  <TD>
                    <LifecycleMark
                      done={row.callable}
                      label={row.callable ? 'yes' : 'no'}
                      detail={row.verification.callable_reason}
                    />
                  </TD>
                  <TD>
                    <span className="whitespace-nowrap text-xs text-muted">{formatDateTimeShort(row.updated_at)}</span>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[11px] text-muted">
              Sorting applies to the rows on this page — the list endpoint has no sort parameter.
            </p>
            <Pagination
              total={query.data?.total ?? 0}
              limit={filters.limit ?? PAGE_SIZE}
              offset={filters.offset ?? 0}
              itemLabel="capabilities"
              disabled={query.isFetching}
              onChange={(offset) => setFilters((current) => ({ ...current, offset }))}
            />
          </div>
        </>
      )}

      <CapabilityFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        defaultProviderId={providerFromUrl || undefined}
        onCreated={(capability) => {
          toast.info({ title: 'Draft v1 created', description: 'Fill in the request, then validate and test it.' });
          router.push(`/capabilities/${capability.id}`);
        }}
      />

    </div>
  );
}

export default function CapabilitiesPage() {
  return (
    <Suspense fallback={<SkeletonTable rows={6} columns={7} />}>
      <CapabilitiesList />
    </Suspense>
  );
}
