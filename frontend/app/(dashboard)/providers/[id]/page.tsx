'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ExternalLink, Pencil, Plus, Power, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { useDeleteProviderMutation, useProviderQuery, useUpdateProviderMutation } from '@/lib/queries';
import type { ProviderDetail } from '@/lib/types';
import { formatDateTime, statusLabel } from '@/lib/format';
import { CredentialsSection } from '@/components/credentials/credentials-section';
import { ProviderFormDialog } from '@/components/providers/provider-form-dialog';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { useGuardedRouter } from '@/lib/navigation-guard';

function DeleteProviderDialog({
  provider,
  open,
  onClose,
}: {
  provider: ProviderDetail;
  open: boolean;
  onClose: () => void;
}) {
  const router = useGuardedRouter();
  const toast = useToast();
  const remove = useDeleteProviderMutation(provider.id);

  const blocking = provider.counts.credentials > 0 || provider.counts.capabilities > 0;

  const run = async () => {
    try {
      await remove.mutateAsync();
      toast.success({ title: 'Provider deleted', description: provider.name });
      router.push('/providers');
    } catch (error) {
      toast.apiError(error, 'Provider was not deleted');
    }
  };

  return (
    <ConfirmDialog
      open={open}
      onCancel={onClose}
      onConfirm={() => void run()}
      title={`Delete provider ${provider.name}?`}
      description="Providers are only removed when nothing references them. Disabling keeps the configuration."
      confirmLabel="Delete provider"
      loading={remove.isPending}
      impact={
        <div className="space-y-1">
          <p className="font-medium text-text">Impact</p>
          <p>
            {provider.counts.credentials} credential(s) · {provider.counts.capabilities} capability(ies)
            {typeof provider.counts.routes === 'number' ? ` · ${provider.counts.routes} route(s)` : ''}
          </p>
          {blocking ? (
            <p className="font-medium text-danger">
              The API answers <code>409 provider_in_use</code> while credentials or capabilities are attached. Delete or
              re-point them first, or disable the provider instead.
            </p>
          ) : (
            <p>The provider has no dependants, so deletion cannot break a published capability.</p>
          )}
        </div>
      }
    />
  );
}

export default function ProviderDetailPage() {
  const params = useParams<{ id: string }>();
  const providerId = params?.id;
  const toast = useToast();
  const query = useProviderQuery(providerId);
  const update = useUpdateProviderMutation(providerId ?? '');
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="Provider" backHref="/providers" backLabel="Providers" />
        <SkeletonBlock rows={4} />
      </div>
    );
  }

  if (query.isError || !query.data) {
    return (
      <div className="space-y-4">
        <PageHeader title="Provider" backHref="/providers" backLabel="Providers" />
        <Banner
          tone="danger"
          title="Could not load this provider"
          requestId={errorRequestId(query.error)}
          primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching, variant: 'primary' }}
        >
          {describeError(query.error)}
        </Banner>
      </div>
    );
  }

  const provider = query.data;

  const toggleStatus = async () => {
    const nextStatus = provider.status === 'active' ? 'disabled' : 'active';
    try {
      await update.mutateAsync({ status: nextStatus });
      toast.success({
        title: nextStatus === 'active' ? 'Provider enabled' : 'Provider disabled',
        description:
          nextStatus === 'active'
            ? 'Routes through this provider can be selected again.'
            : 'Routes are skipped while the provider is disabled; calls will fail over if another route exists.',
      });
    } catch (error) {
      toast.apiError(error, 'Provider status was not changed');
    }
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title={provider.name}
        backHref="/providers"
        backLabel="Providers"
        description={
          provider.notes ??
          `${statusLabel(provider.category)} provider in ${statusLabel(provider.environment)}. Used by ${provider.counts.capabilities} capability(ies).`
        }
        meta={
          <>
            <StatusBadge status={provider.status} />
            <Badge tone="info" dot={false}>
              {statusLabel(provider.category)}
            </Badge>
            <Badge tone="neutral" dot={false}>
              {statusLabel(provider.environment)}
            </Badge>
            <span className="font-mono text-[11px]">{provider.slug}</span>
          </>
        }
        actions={
          <>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void toggleStatus()}
              loading={update.isPending}
              iconLeft={<Power aria-hidden className="h-3.5 w-3.5" />}
            >
              {provider.status === 'active' ? 'Disable' : 'Enable'}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setEditOpen(true)}
              iconLeft={<Pencil aria-hidden className="h-3.5 w-3.5" />}
            >
              Edit
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => setDeleteOpen(true)}
              iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5" />}
            >
              Delete
            </Button>
          </>
        }
      />

      {update.isError ? (
        <Banner
          tone="danger"
          title="The last provider change failed"
          actions={['Retry the action or edit the provider again.']}
          requestId={errorRequestId(update.error)}
        >
          {describeError(update.error)}
        </Banner>
      ) : null}

      {provider.counts.capabilities > 0 ? (
        <Banner
          tone="warning"
          title="Deleting this provider is blocked while it is in use"
          primaryAction={{ label: 'Open capabilities', onClick: () => window.location.assign('/capabilities') }}
        >
          {provider.counts.capabilities} capability(ies) and {provider.counts.credentials} credential(s) point at this
          provider. The API refuses the delete with <code>409 provider_in_use</code>.
        </Banner>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Details" description="Non-secret provider metadata." />
          <CardBody className="grid gap-3 sm:grid-cols-2">
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Base URL</p>
              <p className="mt-0.5 break-all text-xs">
                {provider.base_url ? (
                  <a href={provider.base_url} target="_blank" rel="noreferrer noopener" className="hover:underline">
                    {provider.base_url}
                  </a>
                ) : (
                  '—'
                )}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Documentation</p>
              <p className="mt-0.5 text-xs">
                {provider.docs_url ? (
                  <a
                    href={provider.docs_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1 hover:underline"
                  >
                    Open docs
                    <ExternalLink aria-hidden className="h-3 w-3" />
                  </a>
                ) : (
                  '—'
                )}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Tags</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {provider.tags.length > 0 ? (
                  provider.tags.map((tag) => (
                    <Badge key={tag} tone="neutral" dot={false}>
                      {tag}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-muted">No tags</span>
                )}
              </div>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Timestamps</p>
              <p className="mt-0.5 text-xs">
                created {formatDateTime(provider.created_at)} · updated <TimeAgo value={provider.updated_at} />
              </p>
            </div>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Counts" description="What depends on this provider right now." />
          <CardBody className="space-y-2 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-muted">Credentials</span>
              <span className="tabular-nums">{provider.counts.credentials}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted">Capabilities</span>
              <span className="tabular-nums">{provider.counts.capabilities}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted">Routes</span>
              <span className="tabular-nums">{provider.counts.routes ?? provider.routes.length}</span>
            </div>
          </CardBody>
        </Card>
      </div>

      <CredentialsSection providerId={provider.id} providerName={provider.name} />

      <Card>
        <CardHeader
          title="Related capabilities"
          description="Capabilities routed to this provider."
          actions={
            <Link href={`/capabilities?provider_id=${provider.id}`}>
              <Button size="sm" variant="secondary" iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}>
                Manage capabilities
              </Button>
            </Link>
          }
        />
        <CardBody className="p-0">
          {provider.capabilities.length === 0 ? (
            <EmptyState
              title="No capability uses this provider"
              description="Create a capability and select this provider so routing can choose it."
              action={
                <Link href="/capabilities">
                  <Button size="sm" variant="primary">
                    New capability
                  </Button>
                </Link>
              }
            />
          ) : (
            <Table caption="Related capabilities">
              <THead sticky={false}>
                <TR>
                  <TH>Capability</TH>
                  <TH>Type</TH>
                  <TH>Status</TH>
                  <TH>Callable</TH>
                </TR>
              </THead>
              <TBody>
                {provider.capabilities.map((capability) => (
                  <TR key={capability.id}>
                    <TD>
                      <Link href={`/capabilities/${capability.id}`} className="font-medium hover:underline">
                        {capability.slug}
                      </Link>
                      <span className="block truncate text-[11px] text-muted">{capability.name}</span>
                    </TD>
                    <TD>
                      <Badge tone="info" dot={false}>
                        {statusLabel(capability.type)}
                      </Badge>
                    </TD>
                    <TD>
                      <StatusBadge status={capability.status} />
                    </TD>
                    <TD>
                      <StatusBadge
                        status={capability.callable ? 'valid' : 'unknown'}
                        label={capability.callable ? 'Callable' : 'Not callable'}
                      />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Routes" description="Route rows that use this provider (health is per route)." />
        <CardBody className="p-0">
          {provider.routes.length === 0 ? (
            <EmptyState title="No routes" description="Routes are created from a capability's Routes tab." />
          ) : (
            <Table caption="Routes using this provider">
              <THead sticky={false}>
                <TR>
                  <TH>Capability</TH>
                  <TH>Enabled</TH>
                  <TH>Health</TH>
                  <TH>Last check</TH>
                </TR>
              </THead>
              <TBody>
                {provider.routes.map((route) => (
                  <TR key={route.id}>
                    <TD>
                      <Link href={`/capabilities/${route.capability_id}?tab=routes`} className="hover:underline">
                        {route.capability_slug}
                      </Link>
                    </TD>
                    <TD>{route.enabled ? 'Enabled' : 'Disabled'}</TD>
                    <TD>
                      <StatusBadge status={route.health_status} />
                    </TD>
                    <TD>
                      <TimeAgo value={route.last_health_at} fallback="never" />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      <ProviderFormDialog open={editOpen} onClose={() => setEditOpen(false)} provider={provider} />
      <DeleteProviderDialog provider={provider} open={deleteOpen} onClose={() => setDeleteOpen(false)} />
    </div>
  );
}
