'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Package, Plus, Upload } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatDateTimeShort } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { Pagination } from '@/components/ui/pagination';
import { SearchInput } from '@/components/ui/search-input';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR, TableEmpty } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { formatNumber } from '@/lib/format';
import { ImportSkillDialog, SkillFormDialog } from '@/components/skills/skill-dialogs';
import { useImportHistoryQuery, useSkillsQuery, type Skill } from '@/components/skills/api';

const PAGE_SIZE = 20;

export default function SkillsPage() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [category, setCategory] = useState('');
  const [offset, setOffset] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const filters = { search, status: status as Skill['status'] | '', category, limit: PAGE_SIZE, offset };
  const query = useSkillsQuery(filters);
  const imports = useImportHistoryQuery(true);

  const items = query.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="SKILLs"
        description="A SKILL composes capability calls into a plan an external agent can follow. Per-step inputs use {{ input.x }} and {{ steps.id.x }} templates."
        actions={
          <>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setImportOpen(true)}
              iconLeft={<Upload aria-hidden className="h-3.5 w-3.5" />}
            >
              Import
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => setCreateOpen(true)}
              iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
            >
              New SKILL
            </Button>
          </>
        }
      />

      <Banner tone="info" title="Imported bundles are reviewed, never executed">
        Scripts, shebang files and install hooks found in an import are listed for review with the file that needs
        attention. Skill Router never runs them — the import creates a draft manifest and instructions only.
      </Banner>

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[220px] flex-1">
          <SearchInput
            value={search}
            onChange={(value) => {
              setSearch(value);
              setOffset(0);
            }}
            label="Search SKILLs"
            placeholder="Search name, slug or description…"
          />
        </div>
        <div className="w-40">
          <Select
            aria-label="Filter by status"
            value={status}
            placeholder="All statuses"
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
            options={[
              { value: 'draft', label: 'Draft' },
              { value: 'published', label: 'Published' },
              { value: 'disabled', label: 'Disabled' },
            ]}
          />
        </div>
        <div className="w-44">
          <label htmlFor="skill-category-filter" className="sr-visually-hidden">
            Filter by category
          </label>
          <input
            id="skill-category-filter"
            type="text"
            value={category}
            placeholder="Category"
            onChange={(event) => {
              setCategory(event.target.value);
              setOffset(0);
            }}
            className="h-10 w-full rounded-control border border-border-strong bg-surface px-3 text-sm text-text placeholder:text-muted"
          />
        </div>
      </div>

      {query.isLoading ? <SkeletonTable rows={5} columns={6} /> : null}

      {query.isError ? (
        <Banner
          tone="danger"
          title="Could not load SKILLs"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      ) : null}

      {!query.isLoading && !query.isError ? (
        items.length === 0 ? (
          <Card>
            <EmptyState
              icon={<Package aria-hidden className="h-5 w-5" />}
              title={search || status || category ? 'No SKILL matches these filters' : 'No SKILL yet'}
              description={
                search || status || category
                  ? 'Clear the filters, or create a SKILL that matches what you are looking for.'
                  : 'Create a SKILL from scratch, or import an existing bundle (scripts are never executed).'
              }
              action={
                <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
                  New SKILL
                </Button>
              }
              secondaryAction={
                <Button size="sm" variant="secondary" onClick={() => setImportOpen(true)}>
                  Import a bundle
                </Button>
              }
            />
          </Card>
        ) : (
          <>
            <Table caption="SKILLs">
              <THead>
                <TR>
                  <TH>SKILL</TH>
                  <TH>Category</TH>
                  <TH>Status</TH>
                  <TH>Version</TH>
                  <TH>Live agent</TH>
                  <TH>Updated</TH>
                </TR>
              </THead>
              <TBody>
                {items.map((skill) => (
                  <TR key={skill.id}>
                    <TD>
                      <Link href={`/skills/${skill.id}`} className="font-medium hover:underline">
                        {skill.name}
                      </Link>
                      <span className="block max-w-[320px] truncate font-mono text-[11px] text-muted">{skill.slug}</span>
                    </TD>
                    <TD>
                      <Badge tone="neutral" dot={false}>
                        {skill.category || '—'}
                      </Badge>
                    </TD>
                    <TD>
                      <StatusBadge status={skill.status} />
                    </TD>
                    <TD>
                      {skill.current_version ? (
                        <span className="flex items-center gap-1.5">
                          <span className="font-mono text-xs">v{skill.current_version.version}</span>
                          <StatusBadge status={skill.current_version.status} />
                        </span>
                      ) : (
                        <span className="text-xs text-muted">no version</span>
                      )}
                    </TD>
                    <TD>
                      {skill.requires_live_agent ? (
                        <Badge tone="warning" dot={false}>
                          Needs engine
                        </Badge>
                      ) : (
                        <span className="text-xs text-muted">No</span>
                      )}
                    </TD>
                    <TD>
                      <TimeAgo value={skill.updated_at} className="text-xs" />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            <Pagination
              total={query.data?.total ?? 0}
              limit={query.data?.limit ?? PAGE_SIZE}
              offset={query.data?.offset ?? offset}
              onChange={setOffset}
              itemLabel="SKILLs"
            />
          </>
        )
      ) : null}

      <Card>
        <CardHeader
          title="Recent imports"
          description="Every staged bundle and what it contained. Flagged files were never executed."
          actions={
            <Badge tone="neutral" dot={false}>
              {formatNumber(imports.data?.total ?? 0)} total
            </Badge>
          }
        />
        <CardBody className="p-0">
          {imports.isLoading ? (
            <div className="p-3 text-xs text-muted">Loading imports…</div>
          ) : imports.isError ? (
            <div className="p-3">
              <Banner
                tone="danger"
                title="Could not load the import history"
                requestId={errorRequestId(imports.error)}
                primaryAction={{ label: 'Retry', onClick: () => void imports.refetch(), variant: 'secondary' }}
              >
                {describeError(imports.error)}
              </Banner>
            </div>
          ) : (imports.data?.items.length ?? 0) === 0 ? (
            <EmptyState
              title="No import has been staged yet"
              description="Use Import to stage a git, web or uploaded bundle for review."
            />
          ) : (
            <Table caption="Recent SKILL imports">
              <THead sticky={false}>
                <TR>
                  <TH>Source</TH>
                  <TH>Status</TH>
                  <TH>Files</TH>
                  <TH>Flagged</TH>
                  <TH>Scripts executed</TH>
                  <TH>When</TH>
                </TR>
              </THead>
              <TBody>
                {imports.data?.items.map((record) => (
                  <TR key={record.id}>
                    <TD>
                      <span className="block max-w-[320px] truncate text-xs">{record.source_uri}</span>
                      <span className="text-[11px] text-muted">{record.source_type}</span>
                    </TD>
                    <TD>
                      <StatusBadge status={record.status} />
                    </TD>
                    <TD className="text-xs tabular-nums">{record.file_count}</TD>
                    <TD>
                      <Badge tone={record.requires_review.length > 0 ? 'warning' : 'neutral'} dot={false}>
                        {record.requires_review.length}
                      </Badge>
                    </TD>
                    <TD>
                      <Badge tone={record.scripts_executed ? 'danger' : 'success'} dot={false}>
                        {record.scripts_executed ? 'yes' : 'no'}
                      </Badge>
                    </TD>
                    <TD className="text-xs text-muted">{formatDateTimeShort(record.created_at)}</TD>
                  </TR>
                ))}
                {(imports.data?.items.length ?? 0) === 0 ? <TableEmpty colSpan={6}>No imports.</TableEmpty> : null}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      <SkillFormDialog open={createOpen} onClose={() => setCreateOpen(false)} />
      <ImportSkillDialog open={importOpen} onClose={() => setImportOpen(false)} />
    </div>
  );
}
