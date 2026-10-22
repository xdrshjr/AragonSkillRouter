'use client';

import { useEffect, useMemo, useState } from 'react';
import { GitCompare, GitFork, History, RotateCcw, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import {
  useCreateVersionMutation,
  useDeprecateVersionMutation,
  useDiffQuery,
  useRollbackMutation,
} from '@/lib/queries';
import type { CapabilityVersion } from '@/lib/types';
import { formatDateTime, formatDateTimeShort, statusLabel, truncateMiddle } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { useToast } from '@/components/ui/toast';

function DiffDialog({
  open,
  onClose,
  capabilityId,
  versions,
  initialFrom,
  initialTo,
}: {
  open: boolean;
  onClose: () => void;
  capabilityId: string;
  versions: CapabilityVersion[];
  initialFrom?: string;
  initialTo?: string;
}) {
  const [from, setFrom] = useState(initialFrom ?? versions[0]?.id ?? '');
  const [to, setTo] = useState(initialTo ?? versions[1]?.id ?? '');

  useEffect(() => {
    if (!open) return;
    setFrom(initialFrom ?? versions[0]?.id ?? '');
    setTo(initialTo ?? versions[1]?.id ?? '');
  }, [open, initialFrom, initialTo, versions]);

  const diff = useDiffQuery(open ? capabilityId : undefined, from, to);

  return (
    <Dialog open={open} onClose={onClose} title="Compare versions" size="xl" description="Structural diff of two version configs.">
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="From version" htmlFor="diff-from">
            <Select
              id="diff-from"
              value={from}
              placeholder="Choose…"
              options={versions.map((version) => ({ value: version.id, label: `v${version.version} — ${statusLabel(version.status)}` }))}
              onChange={(event) => setFrom(event.target.value)}
            />
          </Field>
          <Field label="To version" htmlFor="diff-to">
            <Select
              id="diff-to"
              value={to}
              placeholder="Choose…"
              options={versions.map((version) => ({ value: version.id, label: `v${version.version} — ${statusLabel(version.status)}` }))}
              onChange={(event) => setTo(event.target.value)}
            />
          </Field>
        </div>

        {from && to && from === to ? (
          <InlineAlert tone="warning">Pick two different versions to compare.</InlineAlert>
        ) : null}

        {diff.isError ? (
          <Banner tone="danger" title="Diff failed" requestId={errorRequestId(diff.error)}>
            {describeError(diff.error)}
          </Banner>
        ) : null}

        {diff.isLoading ? <SkeletonTable rows={4} columns={3} /> : null}

        {diff.data ? (
          <div className="space-y-2">
            <p className="text-xs text-muted">
              v{diff.data.from.version} → v{diff.data.to.version} · added {diff.data.summary.added} · removed{' '}
              {diff.data.summary.removed} · changed {diff.data.summary.changed}
            </p>
            {diff.data.identical ? (
              <InlineAlert tone="info">The two versions have identical configuration.</InlineAlert>
            ) : (
              <div className="sr-scroll-x max-h-80 overflow-y-auto rounded-control border border-border">
                <table className="w-full text-left text-xs">
                  <caption className="sr-visually-hidden">Config changes</caption>
                  <thead className="bg-info-soft text-[11px] uppercase tracking-wide text-muted">
                    <tr>
                      <th scope="col" className="px-2 py-1.5">
                        Path
                      </th>
                      <th scope="col" className="px-2 py-1.5">
                        Kind
                      </th>
                      <th scope="col" className="px-2 py-1.5">
                        Before
                      </th>
                      <th scope="col" className="px-2 py-1.5">
                        After
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {diff.data.changes.map((change) => (
                      <tr key={`${change.path}-${change.kind}`}>
                        <td className="px-2 py-1.5 font-mono">{change.path}</td>
                        <td className="px-2 py-1.5">
                          <Badge tone={change.kind === 'added' ? 'success' : change.kind === 'removed' ? 'danger' : 'warning'}>
                            {change.kind}
                          </Badge>
                        </td>
                        <td className="px-2 py-1.5 font-mono text-muted">
                          {change.before === null || change.before === undefined ? '—' : JSON.stringify(change.before)}
                        </td>
                        <td className="px-2 py-1.5 font-mono">
                          {change.after === null || change.after === undefined ? '—' : JSON.stringify(change.after)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}

export interface VersionsPanelProps {
  capabilityId: string;
  versions: CapabilityVersion[];
  selectedVersionId: string | null;
  onSelectVersion: (versionId: string) => void;
  currentVersionId: string | null;
  publishedVersionId: string | null;
  onChanged?: () => void;
}

export function VersionsPanel({
  capabilityId,
  versions,
  selectedVersionId,
  onSelectVersion,
  currentVersionId,
  publishedVersionId,
  onChanged,
}: VersionsPanelProps) {
  const toast = useToast();
  const createVersion = useCreateVersionMutation(capabilityId);
  const rollback = useRollbackMutation(capabilityId);
  const deprecate = useDeprecateVersionMutation(capabilityId, selectedVersionId ?? '');
  const [diffOpen, setDiffOpen] = useState(false);
  const [newVersionOpen, setNewVersionOpen] = useState(false);
  const [rollbackTarget, setRollbackTarget] = useState<CapabilityVersion | null>(null);
  const [deprecateTarget, setDeprecateTarget] = useState<CapabilityVersion | null>(null);

  const selected = useMemo(
    () => versions.find((version) => version.id === selectedVersionId) ?? null,
    [versions, selectedVersionId],
  );

  const createFromSelected = async () => {
    try {
      const created = await createVersion.mutateAsync({
        from_version_id: selected?.id,
        notes: selected ? `Branched from v${selected.version}` : 'Initial version',
      });
      toast.success({ title: `Draft v${created.version} created`, description: 'A new version needs its own passing test.' });
      onSelectVersion(created.id);
      setNewVersionOpen(false);
      onChanged?.();
    } catch (error) {
      toast.apiError(error, 'Version was not created');
    }
  };

  const confirmRollback = async () => {
    if (!rollbackTarget) return;
    try {
      await rollback.mutateAsync({ version_id: rollbackTarget.id, reason: 'Rollback from the admin UI' });
      toast.success({
        title: `Rolled back to v${rollbackTarget.version}`,
        description: 'The config is republished as a new version; the old one stays immutable.',
      });
      setRollbackTarget(null);
      onChanged?.();
    } catch (error) {
      toast.apiError(error, 'Rollback failed');
    }
  };

  const confirmDeprecate = async () => {
    if (!deprecateTarget) return;
    try {
      await deprecate.mutateAsync();
      toast.success({ title: `v${deprecateTarget.version} deprecated` });
      setDeprecateTarget(null);
      onChanged?.();
    } catch (error) {
      toast.apiError(error, 'Deprecate failed');
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Versions"
          description="Published versions are immutable. Editing always happens on a draft or testing version."
          actions={
            <>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setDiffOpen(true)}
                disabled={versions.length < 2}
                iconLeft={<GitCompare aria-hidden className="h-3.5 w-3.5" />}
              >
                Compare
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => setNewVersionOpen(true)}
                iconLeft={<GitFork aria-hidden className="h-3.5 w-3.5" />}
              >
                New version
              </Button>
            </>
          }
        />
        <CardBody className="p-0">
          {versions.length === 0 ? (
            <EmptyState
              icon={<History aria-hidden className="h-5 w-5" />}
              title="No versions"
              description="Create the first draft to start configuring this capability."
              action={
                <Button size="sm" variant="primary" onClick={() => setNewVersionOpen(true)}>
                  Create draft v1
                </Button>
              }
            />
          ) : (
            <Table caption="Capability versions">
              <THead sticky={false}>
                <TR>
                  <TH>Version</TH>
                  <TH>Status</TH>
                  <TH>Notes</TH>
                  <TH>Checksum</TH>
                  <TH>Created</TH>
                  <TH>Published</TH>
                  <TH>Last test</TH>
                  <TH align="right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {versions.map((version) => {
                  const isSelected = version.id === selectedVersionId;
                  return (
                    <TR key={version.id} className={isSelected ? 'bg-accent-soft/40' : undefined}>
                      <TD>
                        <button
                          type="button"
                          onClick={() => onSelectVersion(version.id)}
                          className="font-medium text-accent hover:underline"
                          aria-current={isSelected ? 'true' : undefined}
                        >
                          v{version.version}
                          {version.id === currentVersionId ? ' (current)' : ''}
                          {version.id === publishedVersionId ? ' (published)' : ''}
                        </button>
                      </TD>
                      <TD>
                        <StatusBadge status={version.status} />
                      </TD>
                      <TD className="max-w-[200px]">
                        <span className="sr-truncate text-xs text-muted">{version.notes ?? '—'}</span>
                      </TD>
                      <TD className="font-mono text-[11px] text-muted">{truncateMiddle(version.checksum || '—', 8, 6)}</TD>
                      <TD>
                        <span className="whitespace-nowrap text-xs text-muted">{formatDateTimeShort(version.created_at)}</span>
                      </TD>
                      <TD>
                        <span className="whitespace-nowrap text-xs text-muted">
                          {version.published_at ? formatDateTimeShort(version.published_at) : '—'}
                        </span>
                      </TD>
                      <TD>
                        <StatusBadge status={version.last_test_status ?? 'unknown'} />
                      </TD>
                      <TD align="right">
                        <div className="flex items-center justify-end gap-1">
                          <Button size="sm" variant="ghost" onClick={() => onSelectVersion(version.id)}>
                            {isSelected ? 'Selected' : 'Open'}
                          </Button>
                          {version.status === 'published' && version.id !== currentVersionId ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setRollbackTarget(version)}
                              iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
                            >
                              Rollback
                            </Button>
                          ) : null}
                          {version.status === 'published' || version.status === 'testing' ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => {
                                onSelectVersion(version.id);
                                setDeprecateTarget(version);
                              }}
                              iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
                            >
                              Deprecate
                            </Button>
                          ) : null}
                        </div>
                      </TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      <DiffDialog
        open={diffOpen}
        onClose={() => setDiffOpen(false)}
        capabilityId={capabilityId}
        versions={versions}
        initialFrom={publishedVersionId ?? versions[0]?.id}
        initialTo={selectedVersionId ?? versions[1]?.id}
      />

      <ConfirmDialog
        open={newVersionOpen}
        tone="primary"
        title={selected ? `Branch v${selected.version} into a new draft?` : 'Create a new draft version?'}
        description="The new version copies the selected config and gets its own checksum. Publishing it requires a fresh passing test."
        confirmLabel="Create draft"
        cancelLabel="Cancel"
        loading={createVersion.isPending}
        onCancel={() => setNewVersionOpen(false)}
        onConfirm={() => void createFromSelected()}
      />

      <ConfirmDialog
        open={rollbackTarget !== null}
        title={`Roll back to v${rollbackTarget?.version ?? ''}?`}
        description="Rollback republishes that configuration as a new version. The currently published version is kept for history but stops being current."
        confirmLabel="Roll back"
        loading={rollback.isPending}
        onCancel={() => setRollbackTarget(null)}
        onConfirm={() => void confirmRollback()}
        impact={
          rollbackTarget ? (
            <ul className="list-disc pl-4">
              <li>Route health and credentials are unchanged.</li>
              <li>Config checksum: {truncateMiddle(rollbackTarget.checksum || '—', 10, 6)}</li>
              <li>Published at: {formatDateTime(rollbackTarget.published_at ?? rollbackTarget.created_at)}</li>
            </ul>
          ) : null
        }
      />

      <ConfirmDialog
        open={deprecateTarget !== null}
        title={`Deprecate v${deprecateTarget?.version ?? ''}?`}
        description="A deprecated version is no longer offered to agents. Calls already recorded are unaffected."
        confirmLabel="Deprecate version"
        loading={deprecate.isPending}
        onCancel={() => setDeprecateTarget(null)}
        onConfirm={() => void confirmDeprecate()}
      />
    </div>
  );
}
