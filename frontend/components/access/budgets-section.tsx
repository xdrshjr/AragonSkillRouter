'use client';

import { useEffect, useMemo, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';

import { api, describeError, errorRequestId } from '@/lib/api';
import { formatCurrency, formatNumber } from '@/lib/format';
import type { ListEnvelope } from '@/lib/types';
import { Badge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, FormRow } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { useCapabilityOptionsQuery } from '@/components/skills/api';
import { useQuery } from '@tanstack/react-query';
import {
  useBudgetsQuery,
  useCreateBudgetMutation,
  useDeleteBudgetMutation,
  useUpdateBudgetMutation,
  useUsageLimitsQuery,
  type Agent,
  type BudgetPolicy,
  type BudgetScope,
  type BudgetWindow,
} from './api';

function BudgetDialog({ open, onClose, agents }: { open: boolean; onClose: () => void; agents: Agent[] }) {
  const toast = useToast();
  const create = useCreateBudgetMutation();
  const capabilities = useCapabilityOptionsQuery();
  const providers = useQuery<ListEnvelope<{ id: string; name: string; slug: string }>, Error>({
    queryKey: ['budget-provider-options'],
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<{ id: string; name: string; slug: string }>>('/api/admin/providers', {
        query: { limit: 200 },
        signal,
      }),
  });

  const [scopeType, setScopeType] = useState<BudgetScope>('global');
  const [scopeId, setScopeId] = useState('');
  const [window, setWindow] = useState<BudgetWindow>('day');
  const [maxCalls, setMaxCalls] = useState('');
  const [maxCost, setMaxCost] = useState('');
  const [rate, setRate] = useState('');
  const [concurrency, setConcurrency] = useState('');
  const [enabled, setEnabled] = useState(true);

  useEffect(() => {
    if (!open) return;
    setScopeType('global');
    setScopeId('');
    setWindow('day');
    setMaxCalls('');
    setMaxCost('');
    setRate('');
    setConcurrency('');
    setEnabled(true);
  }, [open]);

  const scopeOptions = useMemo(() => {
    if (scopeType === 'agent') return agents.map((agent) => ({ value: agent.id, label: `${agent.name} (${agent.id})` }));
    if (scopeType === 'capability') {
      return (capabilities.data ?? []).map((option) => ({ value: option.slug, label: `${option.slug} · ${option.name}` }));
    }
    if (scopeType === 'provider') {
      return (providers.data?.items ?? []).map((provider) => ({ value: provider.id, label: `${provider.name} (${provider.slug})` }));
    }
    return [];
  }, [scopeType, agents, capabilities.data, providers.data]);

  const submit = async () => {
    try {
      await create.mutateAsync({
        scope_type: scopeType,
        scope_id: scopeType === 'global' ? null : scopeId || null,
        window,
        max_calls: maxCalls ? Number.parseInt(maxCalls, 10) : null,
        max_cost_usd: maxCost ? Number.parseFloat(maxCost) : null,
        rate_per_min: rate ? Number.parseInt(rate, 10) : null,
        max_concurrency: concurrency ? Number.parseInt(concurrency, 10) : null,
        enabled,
      });
      toast.success({ title: 'Budget policy created', description: `${scopeType} · per ${window}` });
      onClose();
    } catch (cause) {
      toast.apiError(cause, 'The budget policy was not created');
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New budget policy"
      description="Limits are enforced by the router before a call leaves the platform. Blank fields mean 'not limited by this policy'."
      size="md"
      dismissible={!create.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            loading={create.isPending}
            disabled={scopeType !== 'global' && !scopeId}
          >
            Create policy
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {create.isError ? (
          <Banner tone="danger" title="The policy was not created" requestId={errorRequestId(create.error)}>
            {describeError(create.error)}
          </Banner>
        ) : null}
        <FormRow columns={2}>
          <Field label="Scope" htmlFor="budget-scope" required>
            <Select
              id="budget-scope"
              value={scopeType}
              onChange={(event) => {
                setScopeType(event.target.value as BudgetScope);
                setScopeId('');
              }}
              options={[
                { value: 'global', label: 'Global' },
                { value: 'provider', label: 'Per provider' },
                { value: 'capability', label: 'Per capability' },
                { value: 'agent', label: 'Per agent' },
              ]}
            />
          </Field>
          <Field label="Window" htmlFor="budget-window" required>
            <Select
              id="budget-window"
              value={window}
              onChange={(event) => setWindow(event.target.value as BudgetWindow)}
              options={[
                { value: 'minute', label: 'Per minute' },
                { value: 'day', label: 'Per day' },
                { value: 'month', label: 'Per month' },
              ]}
            />
          </Field>
        </FormRow>

        {scopeType !== 'global' ? (
          <Field
            label="Target"
            htmlFor="budget-target"
            required
            hint={
              scopeOptions.length === 0
                ? 'Nothing to pick yet — type the id or slug manually.'
                : 'Pick the record this policy applies to.'
            }
          >
            <Input
              id="budget-target"
              list={`budget-targets-${scopeType}`}
              value={scopeId}
              onChange={(event) => setScopeId(event.target.value)}
              placeholder={scopeType === 'capability' ? 'text.summarize' : `${scopeType}_…`}
            />
            <datalist id={`budget-targets-${scopeType}`}>
              {scopeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </datalist>
          </Field>
        ) : null}

        <FormRow columns={2}>
          <Field label="Max calls" htmlFor="budget-calls">
            <Input id="budget-calls" value={maxCalls} inputMode="numeric" onChange={(event) => setMaxCalls(event.target.value)} placeholder="1000" />
          </Field>
          <Field label="Max cost (USD)" htmlFor="budget-cost">
            <Input id="budget-cost" value={maxCost} inputMode="decimal" onChange={(event) => setMaxCost(event.target.value)} placeholder="50" />
          </Field>
        </FormRow>
        <FormRow columns={2}>
          <Field label="Rate / minute" htmlFor="budget-rate">
            <Input id="budget-rate" value={rate} inputMode="numeric" onChange={(event) => setRate(event.target.value)} placeholder="60" />
          </Field>
          <Field label="Max concurrency" htmlFor="budget-concurrency">
            <Input
              id="budget-concurrency"
              value={concurrency}
              inputMode="numeric"
              onChange={(event) => setConcurrency(event.target.value)}
              placeholder="4"
            />
          </Field>
        </FormRow>
        <Field label="Enabled" htmlFor="budget-enabled" hint="A disabled policy is stored but never enforced.">
          <Select
            id="budget-enabled"
            value={enabled ? 'yes' : 'no'}
            onChange={(event) => setEnabled(event.target.value === 'yes')}
            options={[
              { value: 'yes', label: 'Enabled' },
              { value: 'no', label: 'Disabled' },
            ]}
          />
        </Field>
      </div>
    </Dialog>
  );
}

export function BudgetsSection({ agents }: { agents: Agent[] }) {
  const toast = useToast();
  const budgets = useBudgetsQuery();
  const limits = useUsageLimitsQuery();
  const update = useUpdateBudgetMutation();
  const remove = useDeleteBudgetMutation();
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<BudgetPolicy | null>(null);

  const toggle = async (policy: BudgetPolicy) => {
    try {
      await update.mutateAsync({ id: policy.id, body: { enabled: !policy.enabled } });
      toast.success({
        title: policy.enabled ? 'Policy disabled' : 'Policy enabled',
        description: policy.enabled ? 'It is no longer enforced.' : 'It is enforced from the next call.',
      });
    } catch (cause) {
      toast.apiError(cause, 'The policy was not changed');
    }
  };

  const runDelete = async () => {
    if (!deleteTarget) return;
    try {
      await remove.mutateAsync(deleteTarget.id);
      toast.success({ title: 'Budget policy deleted' });
      setDeleteTarget(null);
    } catch (cause) {
      toast.apiError(cause, 'The policy was not deleted');
    }
  };

  return (
    <Card>
      <CardHeader
        title="Budget policies"
        description="Call, cost, rate and concurrency ceilings per scope and window."
        actions={
          <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)} iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}>
            New policy
          </Button>
        }
      />
      <CardBody className="p-0">
        {budgets.isLoading ? (
          <div className="p-3">
            <SkeletonTable rows={3} columns={6} />
          </div>
        ) : budgets.isError ? (
          <div className="p-3">
            <Banner
              tone="danger"
              title="Could not load budget policies"
              requestId={errorRequestId(budgets.error)}
              primaryAction={{ label: 'Retry', onClick: () => void budgets.refetch(), loading: budgets.isFetching }}
            >
              {describeError(budgets.error)}
            </Banner>
          </div>
        ) : (budgets.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title="No budget policy"
            description="Without a policy, spend and concurrency are only limited by the per-token values."
            action={
              <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
                New policy
              </Button>
            }
          />
        ) : (
          <Table caption="Budget policies">
            <THead sticky={false}>
              <TR>
                <TH>Scope</TH>
                <TH>Window</TH>
                <TH>Calls</TH>
                <TH>Cost</TH>
                <TH>Rate / concurrency</TH>
                <TH>State</TH>
                <TH align="right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {budgets.data?.items.map((policy) => (
                <TR key={policy.id}>
                  <TD>
                    <span className="text-sm">{policy.scope_type}</span>
                    <span className="block font-mono text-[11px] text-muted">{policy.scope_id ?? '—'}</span>
                  </TD>
                  <TD className="text-xs">per {policy.window}</TD>
                  <TD className="text-xs tabular-nums">{policy.max_calls === null ? '—' : formatNumber(policy.max_calls)}</TD>
                  <TD className="text-xs tabular-nums">
                    {policy.max_cost_usd === null ? '—' : formatCurrency(policy.max_cost_usd)}
                  </TD>
                  <TD className="text-xs text-muted">
                    {policy.rate_per_min === null ? '—' : `${policy.rate_per_min}/min`} ·{' '}
                    {policy.max_concurrency === null ? '—' : `conc ${policy.max_concurrency}`}
                  </TD>
                  <TD>
                    <Badge tone={policy.enabled ? 'success' : 'neutral'} dot={false}>
                      {policy.enabled ? 'enforced' : 'not enforced'}
                    </Badge>
                  </TD>
                  <TD align="right">
                    <span className="inline-flex items-center gap-1">
                      <Button size="sm" variant="ghost" onClick={() => void toggle(policy)} loading={update.isPending}>
                        {policy.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDeleteTarget(policy)}
                        iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
                      >
                        Delete
                      </Button>
                    </span>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>
      <div className="border-t border-border px-4 py-2 text-[11px] text-muted">
        Current counters: {limits.data ? `${limits.data.global.running_invocations ?? 0} running invocation(s)` : 'unavailable'} ·{' '}
        {limits.data?.generated_at ? <TimeAgo value={limits.data.generated_at} /> : null}
      </div>

      <BudgetDialog open={createOpen} onClose={() => setCreateOpen(false)} agents={agents} />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void runDelete()}
        title="Delete this budget policy?"
        description="Deleting removes the ceiling; the next call is no longer limited by it."
        confirmLabel="Delete policy"
        loading={remove.isPending}
        impact={
          <div className="space-y-1">
            <p>
              Scope: {deleteTarget?.scope_type} / {deleteTarget?.scope_id ?? 'global'} · per {deleteTarget?.window}
            </p>
          </div>
        }
      />
    </Card>
  );
}
