'use client';

import { useEffect, useMemo, useState } from 'react';
import { Activity, GitBranch, Pencil, Plus, Route as RouteIcon, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import {
  useCreateRouteMutation,
  useDeleteRouteMutation,
  useHealthCheckMutation,
  useProvidersQuery,
  useCredentialsQuery,
  useRoutesQuery,
  useRoutePreviewMutation,
  useUpdateRouteMutation,
} from '@/lib/queries';
import { ROUTE_STRATEGIES, type CapabilityVersion, type Route, type RouteStrategy } from '@/lib/types';
import { formatDuration, statusLabel } from '@/lib/format';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, FormRow } from '@/components/ui/field';
import { Switch } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { JsonEditor } from '@/components/ui/json-editor';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { StatusBadge } from '@/components/ui/badge';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';

interface RouteFormState {
  capability_version_id: string;
  provider_id: string;
  credential_id: string;
  strategy: RouteStrategy;
  priority: string;
  weight: string;
  enabled: boolean;
}

function RouteFormDialog({
  open,
  onClose,
  capabilityId,
  versions,
  route,
}: {
  open: boolean;
  onClose: () => void;
  capabilityId: string;
  versions: CapabilityVersion[];
  route?: Route | null;
}) {
  const toast = useToast();
  const providers = useProvidersQuery({ limit: 200 });
  const create = useCreateRouteMutation(capabilityId);
  const update = useUpdateRouteMutation(capabilityId);
  const isEdit = Boolean(route);

  const [form, setForm] = useState<RouteFormState>({
    capability_version_id: route?.capability_version_id ?? versions.find((v) => v.status === 'published')?.id ?? versions[0]?.id ?? '',
    provider_id: route?.provider_id ?? '',
    credential_id: route?.credential_id ?? '',
    strategy: route?.strategy ?? 'priority',
    priority: String(route?.priority ?? 100),
    weight: String(route?.weight ?? 1),
    enabled: route?.enabled ?? true,
  });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setForm({
      capability_version_id:
        route?.capability_version_id ?? versions.find((v) => v.status === 'published')?.id ?? versions[0]?.id ?? '',
      provider_id: route?.provider_id ?? '',
      credential_id: route?.credential_id ?? '',
      strategy: route?.strategy ?? 'priority',
      priority: String(route?.priority ?? 100),
      weight: String(route?.weight ?? 1),
      enabled: route?.enabled ?? true,
    });
    setError(null);
  }, [open, route, versions]);

  const credentials = useCredentialsQuery({ provider_id: form.provider_id || undefined, limit: 200 }, Boolean(form.provider_id));
  const credentialOptions = useMemo(
    () => (credentials.data?.items ?? []).map((credential) => ({ value: credential.id, label: credential.name })),
    [credentials.data?.items],
  );

  const submit = async () => {
    if (!form.capability_version_id) {
      setError('Pick the capability version this route serves.');
      return;
    }
    const priority = Number(form.priority);
    const weight = Number(form.weight);
    if (!Number.isInteger(priority) || priority < 0 || priority > 10000) {
      setError('Priority must be an integer between 0 and 10000.');
      return;
    }
    if (!Number.isInteger(weight) || weight < 1 || weight > 1000) {
      setError('Weight must be an integer between 1 and 1000.');
      return;
    }
    setError(null);
    const body = {
      capability_version_id: form.capability_version_id,
      provider_id: form.provider_id || null,
      credential_id: form.credential_id || null,
      strategy: form.strategy,
      priority,
      weight,
      enabled: form.enabled,
    };
    try {
      if (isEdit && route) {
        await update.mutateAsync({ routeId: route.id, body });
        toast.success({ title: 'Route updated', description: route.id });
      } else {
        await create.mutateAsync(body);
        toast.success({ title: 'Route created', description: 'Health is unknown until a check runs.' });
      }
      onClose();
    } catch (cause) {
      toast.apiError(cause, isEdit ? 'Route was not updated' : 'Route was not created');
    }
  };

  const pending = create.isPending || update.isPending;
  const mutationError = create.error ?? update.error;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={isEdit ? 'Edit route' : 'New route'}
      description="A route tells the router which provider and credential serve a published version."
      size="lg"
      dismissible={!pending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={pending}>
            {isEdit ? 'Save route' : 'Create route'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {mutationError ? (
          <Banner
            tone="danger"
            title={isEdit ? 'Route was not updated' : 'Route was not created'}
            requestId={errorRequestId(mutationError)}
          >
            {describeError(mutationError)}
          </Banner>
        ) : null}
        {error ? <InlineAlert tone="danger">{error}</InlineAlert> : null}

        <FormRow columns={2}>
          <Field label="Capability version" htmlFor="route-version" required hint="Only published versions can serve external calls.">
            <Select
              id="route-version"
              value={form.capability_version_id}
              placeholder="Choose a version"
              options={versions.map((version) => ({
                value: version.id,
                label: `v${version.version} — ${statusLabel(version.status)}`,
              }))}
              onChange={(event) => setForm((current) => ({ ...current, capability_version_id: event.target.value }))}
            />
          </Field>
          <Field label="Provider" htmlFor="route-provider" hint="Empty means the capability's own provider.">
            <Select
              id="route-provider"
              value={form.provider_id}
              placeholder="No provider"
              options={(providers.data?.items ?? []).map((provider) => ({ value: provider.id, label: provider.name }))}
              onChange={(event) =>
                setForm((current) => ({ ...current, provider_id: event.target.value, credential_id: '' }))
              }
            />
          </Field>
        </FormRow>

        <FormRow columns={2}>
          <Field label="Credential" htmlFor="route-credential" hint={form.provider_id ? undefined : 'Pick a provider first.'}>
            <Select
              id="route-credential"
              value={form.credential_id}
              placeholder="No credential"
              disabled={!form.provider_id}
              options={credentialOptions}
              onChange={(event) => setForm((current) => ({ ...current, credential_id: event.target.value }))}
            />
          </Field>
          <Field label="Strategy" htmlFor="route-strategy" required>
            <Select
              id="route-strategy"
              value={form.strategy}
              options={ROUTE_STRATEGIES.map((value) => ({ value, label: statusLabel(value) }))}
              onChange={(event) => setForm((current) => ({ ...current, strategy: event.target.value as RouteStrategy }))}
            />
          </Field>
        </FormRow>

        <FormRow columns={2}>
          <Field label="Priority" htmlFor="route-priority" required hint="Lower wins. 0–10000.">
            <Input
              id="route-priority"
              type="number"
              min={0}
              max={10000}
              value={form.priority}
              onChange={(event) => setForm((current) => ({ ...current, priority: event.target.value }))}
            />
          </Field>
          <Field label="Weight" htmlFor="route-weight" required hint="Relative weight for the same priority. 1–1000.">
            <Input
              id="route-weight"
              type="number"
              min={1}
              max={1000}
              value={form.weight}
              onChange={(event) => setForm((current) => ({ ...current, weight: event.target.value }))}
            />
          </Field>
        </FormRow>

        <Switch
          label="Enabled"
          hint="Disabled routes stay configured but are never selected."
          checked={form.enabled}
          onCheckedChange={(checked) => setForm((current) => ({ ...current, enabled: checked }))}
        />
      </div>
    </Dialog>
  );
}

function HealthCheckButton({ capabilityId, route }: { capabilityId: string; route: Route }) {
  const toast = useToast();
  const check = useHealthCheckMutation(capabilityId);

  const run = async () => {
    try {
      const result = await check.mutateAsync(route.id);
      const tone = result.status === 'healthy' ? 'success' : result.status === 'unknown' ? 'info' : 'warning';
      toast.push(tone, {
        title: `Health: ${statusLabel(result.status)}`,
        description: `${result.message}${result.latency_ms ? ` (${formatDuration(result.latency_ms)})` : ''}`,
      });
    } catch (error) {
      toast.apiError(error, 'Health check failed');
    }
  };

  return (
    <Button
      size="sm"
      variant="ghost"
      onClick={() => void run()}
      loading={check.isPending}
      iconLeft={<Activity aria-hidden className="h-3.5 w-3.5" />}
    >
      Health check
    </Button>
  );
}

export interface RoutesPanelProps {
  capabilityId: string;
  versions: CapabilityVersion[];
}

export function RoutesPanel({ capabilityId, versions }: RoutesPanelProps) {
  const toast = useToast();
  const query = useRoutesQuery(capabilityId);
  const update = useUpdateRouteMutation(capabilityId);
  const remove = useDeleteRouteMutation(capabilityId);
  const preview = useRoutePreviewMutation(capabilityId);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Route | null>(null);
  const [deleting, setDeleting] = useState<Route | null>(null);
  const [previewInput, setPreviewInput] = useState('{}');

  const rows = query.data?.items ?? [];

  const toggleEnabled = async (route: Route) => {
    try {
      await update.mutateAsync({ routeId: route.id, body: { enabled: !route.enabled } });
      toast.success({ title: route.enabled ? 'Route disabled' : 'Route enabled', description: route.id });
    } catch (error) {
      toast.apiError(error, 'Route was not updated');
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.id);
      toast.success({ title: 'Route deleted', description: deleting.id });
      setDeleting(null);
    } catch (error) {
      toast.apiError(error, 'Route was not deleted');
    }
  };

  const runPreview = async () => {
    try {
      const parsed = JSON.parse(previewInput) as unknown;
      const input = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
      const result = await preview.mutateAsync({ input, mode: 'sync' });
      toast.info({
        title: 'Route preview (no network call)',
        description: result.reason || 'See the detail below.',
      });
    } catch (error) {
      toast.apiError(error, 'Route preview failed');
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Routes"
          description="Priority first, then weight. A published version without a route is not callable."
          actions={
            <Button
              size="sm"
              variant="primary"
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
              iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
            >
              Add route
            </Button>
          }
        />
        <CardBody className="p-0">
          {query.isError ? (
            <div className="p-3">
              <Banner
                tone="danger"
                title="Could not load routes"
                requestId={errorRequestId(query.error)}
                primaryAction={{ label: 'Retry', onClick: () => void query.refetch(), loading: query.isFetching }}
              >
                {describeError(query.error)}
              </Banner>
            </div>
          ) : query.isLoading ? (
            <div className="p-3">
              <SkeletonTable rows={3} columns={5} />
            </div>
          ) : rows.length === 0 ? (
            <EmptyState
              icon={<RouteIcon aria-hidden className="h-5 w-5" />}
              title="No route configured"
              description="Without a route the router cannot select a provider, so a published capability stays uncallable."
              action={
                <Button size="sm" variant="primary" onClick={() => setFormOpen(true)}>
                  Add the first route
                </Button>
              }
            />
          ) : (
            <Table caption="Routes">
              <THead sticky={false}>
                <TR>
                  <TH>Version</TH>
                  <TH>Provider</TH>
                  <TH>Credential</TH>
                  <TH>Strategy</TH>
                  <TH align="right">Priority</TH>
                  <TH align="right">Weight</TH>
                  <TH>Enabled</TH>
                  <TH>Health</TH>
                  <TH>Last check</TH>
                  <TH align="right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {rows.map((route) => (
                  <TR key={route.id}>
                    <TD>v{route.version ?? '—'}</TD>
                    <TD>{route.provider_name ?? '—'}</TD>
                    <TD>{route.credential_name ?? '—'}</TD>
                    <TD>{statusLabel(route.strategy)}</TD>
                    <TD align="right" className="tabular-nums">
                      {route.priority}
                    </TD>
                    <TD align="right" className="tabular-nums">
                      {route.weight}
                    </TD>
                    <TD>
                      <Switch
                        label={<span className="sr-visually-hidden">Enabled</span>}
                        checked={route.enabled}
                        onCheckedChange={() => void toggleEnabled(route)}
                      />
                    </TD>
                    <TD>
                      <StatusBadge status={route.health_status} />
                    </TD>
                    <TD>
                      <TimeAgo value={route.last_health_at} fallback="never" />
                    </TD>
                    <TD align="right">
                      <div className="flex items-center justify-end gap-1">
                        <HealthCheckButton capabilityId={capabilityId} route={route} />
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Edit route ${route.id}`}
                          onClick={() => {
                            setEditing(route);
                            setFormOpen(true);
                          }}
                          iconLeft={<Pencil aria-hidden className="h-3.5 w-3.5" />}
                        >
                          Edit
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Delete route ${route.id}`}
                          onClick={() => setDeleting(route)}
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
      </Card>

      <Card>
        <CardHeader
          title="Route preview"
          description="Explains which route would be chosen for a sample input. Pure database work, no network call."
          actions={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void runPreview()}
              loading={preview.isPending}
              iconLeft={<GitBranch aria-hidden className="h-3.5 w-3.5" />}
            >
              Preview selection
            </Button>
          }
        />
        <CardBody className="space-y-3">
          <JsonEditor
            label="Preview input (JSON)"
            value={previewInput}
            onChange={setPreviewInput}
            rows={4}
            requireObject
            hint="A minimal payload that satisfies the required inputs."
          />
          {preview.isError ? (
            <Banner tone="danger" title="Preview failed" requestId={errorRequestId(preview.error)}>
              {describeError(preview.error)}
            </Banner>
          ) : null}
          {preview.data ? (
            <div className="space-y-2 text-xs">
              <InlineAlert tone="info">{preview.data.reason}</InlineAlert>
              <p className="text-muted">
                candidates: {preview.data.candidates.length} · rejected: {preview.data.rejected.length}
              </p>
              <pre className="sr-scroll-x max-h-56 overflow-y-auto rounded-control border border-border bg-info-soft/60 p-2 font-mono text-[11px]">
                {JSON.stringify(preview.data, null, 2)}
              </pre>
            </div>
          ) : null}
        </CardBody>
      </Card>

      <RouteFormDialog
        open={formOpen}
        onClose={() => {
          setFormOpen(false);
          setEditing(null);
        }}
        capabilityId={capabilityId}
        versions={versions}
        route={editing}
      />

      <ConfirmDialog
        open={deleting !== null}
        title="Delete this route?"
        description="Traffic stops using this provider/credential combination. If no other route remains the capability becomes uncallable."
        confirmLabel="Delete route"
        loading={remove.isPending}
        onCancel={() => setDeleting(null)}
        onConfirm={() => void confirmDelete()}
        impact={
          deleting ? (
            <ul className="list-disc pl-4">
              <li>Version v{deleting.version ?? '—'} on {deleting.provider_name ?? 'no provider'}</li>
              <li>Health status: {statusLabel(deleting.health_status)}</li>
            </ul>
          ) : null
        }
      />
    </div>
  );
}
