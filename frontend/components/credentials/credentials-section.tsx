'use client';

import { useState } from 'react';
import { KeyRound, Pencil, Plus, ShieldCheck, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import {
  useCredentialQuery,
  useCredentialsQuery,
  useDeleteCredentialMutation,
  useVerifyCredentialMutation,
} from '@/lib/queries';
import type { Credential } from '@/lib/types';
import { formatDateTime, statusLabel } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { CredentialFormDialog } from './credential-form-dialog';

function VerifyButton({ credential, onVerified }: { credential: Credential; onVerified: () => void }) {
  const toast = useToast();
  const verify = useVerifyCredentialMutation(credential.id);

  const run = async () => {
    try {
      const result = await verify.mutateAsync({});
      const tone = result.status === 'valid' ? 'success' : result.status === 'invalid' ? 'error' : 'warning';
      toast.push(tone, {
        title: `Verify: ${statusLabel(result.status)}`,
        description: `${result.message}${result.http_status ? ` (HTTP ${result.http_status})` : ''}`,
      });
      onVerified();
    } catch (error) {
      toast.apiError(error, 'Verification request failed');
    }
  };

  return (
    <Button
      size="sm"
      variant="secondary"
      onClick={() => void run()}
      loading={verify.isPending}
      iconLeft={<ShieldCheck aria-hidden className="h-3.5 w-3.5" />}
    >
      Verify
    </Button>
  );
}

function DeleteCredentialDialog({
  credential,
  open,
  onClose,
  onDeleted,
}: {
  credential: Credential;
  open: boolean;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const toast = useToast();
  const detail = useCredentialQuery(open ? credential.id : undefined);
  const remove = useDeleteCredentialMutation(credential.id);

  const impact = detail.data?.impact;

  const run = async (forceDelete: boolean) => {
    try {
      const result = await remove.mutateAsync({ force: forceDelete });
      toast.success({
        title: 'Credential deleted',
        description: `${credential.name} was removed. ${result.impact.capabilities.length} capability reference(s) were detached.`,
      });
      onDeleted();
      onClose();
    } catch (error) {
      toast.apiError(error, 'Credential was not deleted');
    }
  };

  return (
    <ConfirmDialog
      open={open}
      onCancel={onClose}
      onConfirm={() => void run(false)}
      title={`Delete credential ${credential.name}?`}
      description="The encrypted secret is destroyed. Any capability that authenticated with it stops working immediately."
      confirmLabel="Delete credential"
      loading={remove.isPending}
      secondaryAction={
        impact?.blocking
          ? {
              label: 'Force delete (detach capabilities)',
              onClick: () => void run(true),
              loading: remove.isPending,
            }
          : undefined
      }
      impact={
        !impact ? (
          <p>{detail.isLoading ? 'Computing impact…' : 'Impact could not be loaded — check the request id in the notification.'}</p>
        ) : (
          <div className="space-y-1">
            <p className="font-medium text-text">Impact</p>
            <p>
              {impact.capabilities.length} capability reference(s) · {impact.routes} route(s) · {impact.active_tasks} active
              task(s)
            </p>
            {impact.capabilities.length > 0 ? (
              <ul className="list-disc pl-4">
                {impact.capabilities.map((capability) => (
                  <li key={capability.id}>
                    {capability.slug} — {statusLabel(capability.status)}
                  </li>
                ))}
              </ul>
            ) : null}
            {impact.blocking ? (
              <p className="font-medium text-danger">
                A published or tested capability uses this credential; deleting it breaks those calls.
              </p>
            ) : null}
          </div>
        )
      }
    />
  );
}

export interface CredentialsSectionProps {
  providerId: string;
  providerName: string;
}

/** Credentials attached to one provider: masked list, verify, edit, delete. */
export function CredentialsSection({ providerId, providerName }: CredentialsSectionProps) {
  const toast = useToast();
  const query = useCredentialsQuery({ provider_id: providerId, limit: 100 });
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<Credential | null>(null);
  const [deleting, setDeleting] = useState<Credential | null>(null);

  const rows = query.data?.items ?? [];

  return (
    <Card>
      <CardHeader
        title="Credentials"
        description="Secrets are write-only. The list only ever shows a masked hint and the last verification result."
        actions={
          <Button
            size="sm"
            variant="primary"
            onClick={() => setCreateOpen(true)}
            iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
          >
            New credential
          </Button>
        }
      />
      <CardBody className="p-0">
        {query.isError ? (
          <div className="p-3">
            <Banner
              tone="danger"
              title="Could not load credentials"
              requestId={errorRequestId(query.error)}
              primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching }}
            >
              {describeError(query.error)}
            </Banner>
          </div>
        ) : query.isLoading ? (
          <div className="p-3">
            <SkeletonTable rows={2} columns={4} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<KeyRound aria-hidden className="h-5 w-5" />}
            title="No credential yet"
            description="A capability that authenticates needs an active credential on this provider."
            action={
              <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
                Add the first credential
              </Button>
            }
          />
        ) : (
          <Table caption="Credentials">
            <THead sticky={false}>
              <TR>
                <TH>Name</TH>
                <TH>Auth type</TH>
                <TH>Masked hint</TH>
                <TH>Status</TH>
                <TH>Last verification</TH>
                <TH>Expires</TH>
                <TH align="right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((credential) => (
                <TR key={credential.id}>
                  <TD className="max-w-[200px]">
                    <span className="block truncate font-medium">{credential.name}</span>
                    <span className="block truncate font-mono text-[11px] text-muted">{credential.id}</span>
                  </TD>
                  <TD>
                    <Badge tone="info" dot={false}>
                      {statusLabel(credential.auth_type)}
                    </Badge>
                  </TD>
                  <TD className="font-mono text-xs text-muted">{credential.hint}</TD>
                  <TD>
                    <div className="flex flex-wrap items-center gap-1">
                      <StatusBadge status={credential.status} />
                      {credential.expiring_soon ? <Badge tone="warning">Expiring</Badge> : null}
                    </div>
                  </TD>
                  <TD>
                    <div className="flex flex-col gap-0.5">
                      <StatusBadge status={credential.last_verify_status ?? 'unknown'} />
                      <span className="text-[11px] text-muted">
                        {credential.last_verified_at ? (
                          <>
                            <TimeAgo value={credential.last_verified_at} />
                            {credential.last_verify_message ? ` · ${credential.last_verify_message}` : ''}
                          </>
                        ) : (
                          'never verified'
                        )}
                      </span>
                    </div>
                  </TD>
                  <TD className="whitespace-nowrap text-xs">
                    {credential.expires_at ? formatDateTime(credential.expires_at) : '—'}
                  </TD>
                  <TD align="right">
                    <div className="flex items-center justify-end gap-1">
                      <VerifyButton credential={credential} onVerified={() => void query.refetch()} />
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setEditing(credential)}
                        aria-label={`Edit ${credential.name}`}
                        iconLeft={<Pencil aria-hidden className="h-3.5 w-3.5" />}
                      >
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDeleting(credential)}
                        aria-label={`Delete ${credential.name}`}
                        iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
                      >
                        Delete
                      </Button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>

      <CredentialFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        providerId={providerId}
        providerName={providerName}
        onSaved={() => {
          toast.info({
            title: 'Verify the credential',
            description: 'Verification probes the provider and records valid/invalid/unreachable honestly.',
          });
          void query.refetch();
        }}
      />

      <CredentialFormDialog
        open={editing !== null}
        onClose={() => setEditing(null)}
        providerId={providerId}
        providerName={providerName}
        credential={editing}
        onSaved={() => void query.refetch()}
      />

      {deleting ? (
        <DeleteCredentialDialog
          credential={deleting}
          open
          onClose={() => setDeleting(null)}
          onDeleted={() => void query.refetch()}
        />
      ) : null}
    </Card>
  );
}

