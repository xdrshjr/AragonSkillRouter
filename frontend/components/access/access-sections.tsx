'use client';

import { useEffect, useState } from 'react';
import { AlertTriangle, Copy, KeyRound, Plus, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatCurrency, formatDateTimeShort } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { CodeBlock } from '@/components/ui/code-block';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, FormRow } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { useCapabilityOptionsQuery } from '@/components/skills/api';
import {
  useAgentTokensQuery,
  useCreateAgentMutation,
  useCreateTokenMutation,
  useDeleteAgentMutation,
  useRevokeTokenMutation,
  useUpdateAgentMutation,
  type Agent,
  type CreatedToken,
} from './api';

/* ------------------------------------------------------------- agent dialogs -- */

export function AgentFormDialog({
  open,
  onClose,
  agent,
}: {
  open: boolean;
  onClose: () => void;
  agent?: Agent | null;
}) {
  const toast = useToast();
  const create = useCreateAgentMutation();
  const update = useUpdateAgentMutation(agent?.id ?? '');
  const editing = Boolean(agent);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [status, setStatus] = useState<'active' | 'disabled'>('active');

  useEffect(() => {
    if (!open) return;
    setName(agent?.name ?? '');
    setDescription(agent?.description ?? '');
    setStatus(agent?.status ?? 'active');
  }, [open, agent]);

  const pending = create.isPending || update.isPending;
  const mutation = editing ? update : create;

  const submit = async () => {
    if (!name.trim()) return;
    try {
      if (editing && agent) {
        await update.mutateAsync({ name: name.trim(), description, status });
        toast.success({ title: 'Agent saved', description: status === 'disabled' ? 'Its active tokens were revoked.' : agent.name });
        onClose();
      } else {
        const created = await create.mutateAsync({ name: name.trim(), description });
        toast.success({ title: 'Agent created', description: `${created.name} can now receive a scoped token.` });
        onClose();
      }
    } catch (cause) {
      toast.apiError(cause, editing ? 'The agent was not saved' : 'The agent was not created');
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={editing ? `Edit ${agent?.name}` : 'New agent'}
      description="An agent is the identity an external caller uses; tokens belong to it."
      size="md"
      dismissible={!pending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={pending} disabled={!name.trim()}>
            {editing ? 'Save agent' : 'Create agent'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {mutation.isError ? (
          <Banner tone="danger" title="The last attempt failed" requestId={errorRequestId(mutation.error)}>
            {describeError(mutation.error)}
          </Banner>
        ) : null}
        <Field label="Name" htmlFor="agent-name" required>
          <Input id="agent-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={120} />
        </Field>
        <Field label="Description" htmlFor="agent-description" hint="Who runs it and what it is allowed to do.">
          <Textarea
            id="agent-description"
            value={description}
            rows={3}
            maxLength={1000}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        {editing ? (
          <Field
            label="Status"
            htmlFor="agent-status"
            hint="Disabling an agent revokes every active token immediately."
          >
            <Select
              id="agent-status"
              value={status}
              onChange={(event) => setStatus(event.target.value as 'active' | 'disabled')}
              options={[
                { value: 'active', label: 'Active' },
                { value: 'disabled', label: 'Disabled (revokes tokens)' },
              ]}
            />
          </Field>
        ) : null}
      </div>
    </Dialog>
  );
}

export function AgentDeleteDialog({ open, onClose, agent }: { open: boolean; onClose: () => void; agent: Agent }) {
  const toast = useToast();
  const remove = useDeleteAgentMutation(agent.id);

  const run = async () => {
    try {
      const result = await remove.mutateAsync();
      toast.success({
        title: 'Agent deleted',
        description: `The row is kept for its records; ${result.revoked_tokens} active token(s) were revoked.`,
      });
      onClose();
    } catch (cause) {
      toast.apiError(cause, 'The agent could not be deleted');
    }
  };

  return (
    <ConfirmDialog
      open={open}
      onCancel={onClose}
      onConfirm={() => void run()}
      title={`Delete agent ${agent.name}?`}
      description="The agent is disabled and its tokens are revoked. Invocation records keep pointing at it."
      confirmLabel="Delete agent"
      loading={remove.isPending}
      impact={
        <div className="space-y-1">
          <p className="font-medium text-text">Impact</p>
          <p>
            {agent.counts.active_tokens} active token(s) of {agent.counts.tokens} total stop working on the next request.
          </p>
          <p>Existing invocation rows keep their agent name.</p>
        </div>
      }
    />
  );
}

/* --------------------------------------------------------------- token table -- */

function CopyOnceDialog({ created, onClose }: { created: CreatedToken | null; onClose: () => void }) {
  return (
    <Dialog
      open={Boolean(created?.token)}
      onClose={onClose}
      title="Copy this token now"
      description="The API returns the plaintext token exactly once. Skill Router stores only a hash."
      size="lg"
      footer={
        <Button variant="primary" onClick={onClose}>
          I stored the token
        </Button>
      }
    >
      <div className="space-y-3">
        <Banner tone="warning" title="This is the only time the token is shown">
          {created?.warning ??
            'Copy it into the agent configuration now; it cannot be displayed again, only revoked and replaced.'}
        </Banner>
        <CodeBlock code={created?.token ?? ''} label="agent token (shown once)" copyable maxHeightClassName="max-h-32" />
        <div className="text-xs text-muted">
          <p>
            Name: {created?.token_record?.name ?? created?.name} · prefix{' '}
            <span className="font-mono">{created?.token_record?.token_prefix ?? created?.token_prefix}</span>
          </p>
          <p>
            Scopes: {created?.token_record?.scopes?.join(', ') || created?.scopes?.join(', ') || 'none'} · expires{' '}
            {created?.token_record?.expires_at ?? created?.expires_at ?? 'never'}
          </p>
        </div>
      </div>
    </Dialog>
  );
}

function CreateTokenDialog({
  open,
  onClose,
  agentId,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  agentId: string;
  onCreated: (created: CreatedToken) => void;
}) {
  const toast = useToast();
  const create = useCreateTokenMutation(agentId);
  const capabilities = useCapabilityOptionsQuery();

  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresInDays, setExpiresInDays] = useState('30');
  const [rateLimit, setRateLimit] = useState('');
  const [concurrency, setConcurrency] = useState('');
  const [budget, setBudget] = useState('');

  useEffect(() => {
    if (!open) return;
    setName('');
    setScopes([]);
    setExpiresInDays('30');
    setRateLimit('');
    setConcurrency('');
    setBudget('');
  }, [open]);

  const options = (capabilities.data ?? []).filter((item) => item.status === 'published');

  const toggleScope = (slug: string) => {
    setScopes((current) => (current.includes(slug) ? current.filter((item) => item !== slug) : [...current, slug]));
  };

  const submit = async () => {
    try {
      const created = await create.mutateAsync({
        name: name.trim(),
        scopes,
        expires_in_days: expiresInDays ? Number.parseInt(expiresInDays, 10) : null,
        rate_limit_per_min: rateLimit ? Number.parseInt(rateLimit, 10) : null,
        max_concurrency: concurrency ? Number.parseInt(concurrency, 10) : null,
        budget_usd: budget ? Number.parseFloat(budget) : null,
      });
      onCreated(created);
      onClose();
      toast.success({ title: 'Token issued', description: 'Copy it from the dialog — it is not shown again.' });
    } catch (cause) {
      toast.apiError(cause, 'The token was not issued');
    }
  };

  const invalidBudget = budget !== '' && Number.isNaN(Number.parseFloat(budget));

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New agent token"
      description="Scopes restrict which capabilities this token may call. Everything else is refused."
      size="lg"
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
            disabled={!name.trim() || invalidBudget}
          >
            Issue token
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {create.isError ? (
          <Banner tone="danger" title="The token was not issued" requestId={errorRequestId(create.error)}>
            {describeError(create.error)}
          </Banner>
        ) : null}

        <FormRow columns={2}>
          <Field label="Token name" htmlFor="token-name" required hint="What this token is used for.">
            <Input id="token-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={120} />
          </Field>
          <Field label="Expires in (days)" htmlFor="token-expiry" hint="Blank means no expiry.">
            <Input
              id="token-expiry"
              value={expiresInDays}
              inputMode="numeric"
              onChange={(event) => setExpiresInDays(event.target.value)}
              placeholder="30"
            />
          </Field>
        </FormRow>

        <fieldset className="rounded-card border border-border p-3">
          <legend className="px-1 text-xs font-medium text-text">Scopes</legend>
          <p className="mb-2 text-xs text-muted">
            Only published capabilities are listed. Wildcards (<code>slug:*</code>, <code>*</code>) are accepted by the
            API but this UI offers exact slugs so the grant is obvious.
          </p>
          {capabilities.isLoading ? (
            <p className="text-xs text-muted">Loading capabilities…</p>
          ) : options.length === 0 ? (
            <InlineAlert tone="warning">
              No published capability exists yet, so a token could not call anything. Publish a capability first.
            </InlineAlert>
          ) : (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {options.map((option) => (
                <Checkbox
                  key={option.id}
                  checked={scopes.includes(option.slug)}
                  onChange={() => toggleScope(option.slug)}
                  label={<span className="font-mono text-xs">{option.slug}</span>}
                  hint={option.name}
                />
              ))}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2">
            <Checkbox
              checked={scopes.includes('*')}
              onChange={() => toggleScope('*')}
              label="Grant every capability (*)"
              hint="Includes capabilities published later."
            />
          </div>
          <p className="mt-1 text-[11px] text-muted">{scopes.length} scope(s) selected.</p>
        </fieldset>

        <FormRow columns={3}>
          <Field label="Rate / minute" htmlFor="token-rate" hint="Blank uses the default.">
            <Input
              id="token-rate"
              value={rateLimit}
              inputMode="numeric"
              onChange={(event) => setRateLimit(event.target.value)}
              placeholder="60"
            />
          </Field>
          <Field label="Max concurrency" htmlFor="token-concurrency">
            <Input
              id="token-concurrency"
              value={concurrency}
              inputMode="numeric"
              onChange={(event) => setConcurrency(event.target.value)}
              placeholder="4"
            />
          </Field>
          <Field label="Budget (USD)" htmlFor="token-budget" error={invalidBudget ? 'Enter a number.' : null}>
            <Input
              id="token-budget"
              value={budget}
              inputMode="decimal"
              onChange={(event) => setBudget(event.target.value)}
              placeholder="25"
            />
          </Field>
        </FormRow>
      </div>
    </Dialog>
  );
}

export function TokensSection({ agent }: { agent: Agent }) {
  const toast = useToast();
  const tokens = useAgentTokensQuery(agent.id);
  const revoke = useRevokeTokenMutation(agent.id);
  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<{ id: string; name: string; prefix: string } | null>(null);

  const runRevoke = async () => {
    if (!revokeTarget) return;
    try {
      const result = await revoke.mutateAsync(revokeTarget.id);
      toast.success({
        title: 'Token revoked',
        description: result.already_revoked
          ? 'It was already revoked; nothing changed.'
          : `Effective ${result.effective ?? 'immediately'} — the next request with it is refused.`,
      });
      setRevokeTarget(null);
    } catch (cause) {
      toast.apiError(cause, 'The token was not revoked');
    }
  };

  return (
    <Card>
      <CardHeader
        title={`Tokens · ${agent.name}`}
        description="A token is the only credential an external agent holds. The plaintext is shown once."
        actions={
          <Button
            size="sm"
            variant="primary"
            onClick={() => setCreateOpen(true)}
            disabled={agent.status !== 'active'}
            iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
          >
            New token
          </Button>
        }
      />
      <CardBody className="p-0">
        {agent.status !== 'active' ? (
          <div className="p-3">
            <InlineAlert tone="warning">
              This agent is disabled, so the API refuses to issue new tokens. Re-enable it in Edit first.
            </InlineAlert>
          </div>
        ) : null}

        {tokens.isLoading ? (
          <div className="p-3">
            <SkeletonTable rows={3} columns={5} />
          </div>
        ) : tokens.isError ? (
          <div className="p-3">
            <Banner
              tone="danger"
              title="Could not load the tokens"
              requestId={errorRequestId(tokens.error)}
              primaryAction={{ label: 'Retry', onClick: () => void tokens.refetch(), loading: tokens.isFetching }}
            >
              {describeError(tokens.error)}
            </Banner>
          </div>
        ) : (tokens.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            icon={<KeyRound aria-hidden className="h-5 w-5" />}
            title="This agent has no token"
            description="Issue a scoped token to connect it. You will see the plaintext exactly once."
            action={
              <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)} disabled={agent.status !== 'active'}>
                New token
              </Button>
            }
          />
        ) : (
          <Table caption={`Tokens for agent ${agent.name}`}>
            <THead sticky={false}>
              <TR>
                <TH>Token</TH>
                <TH>Status</TH>
                <TH>Scopes</TH>
                <TH>Limits</TH>
                <TH>Expires</TH>
                <TH>Last used</TH>
                <TH align="right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {tokens.data?.items.map((token) => (
                <TR key={token.id}>
                  <TD>
                    <span className="text-sm text-text">{token.name}</span>
                    <span className="block font-mono text-[11px] text-muted">{token.token_prefix}…</span>
                  </TD>
                  <TD>
                    <StatusBadge status={token.status} />
                    {token.expiring_soon ? (
                      <span className="ml-1">
                        <Badge tone="warning" dot={false}>
                          expires in {token.days_until_expiry}d
                        </Badge>
                      </span>
                    ) : null}
                  </TD>
                  <TD>
                    <span className="flex max-w-[220px] flex-wrap gap-1">
                      {token.scopes.length === 0 ? (
                        <span className="text-xs text-muted">none</span>
                      ) : (
                        token.scopes.map((scope) => (
                          <Badge key={scope} tone="neutral" dot={false}>
                            {scope}
                          </Badge>
                        ))
                      )}
                    </span>
                  </TD>
                  <TD className="text-xs text-muted">
                    {token.rate_limit_per_min ? `${token.rate_limit_per_min}/min` : 'default rate'} ·{' '}
                    {token.max_concurrency ? `conc ${token.max_concurrency}` : 'default concurrency'} ·{' '}
                    {token.budget_usd !== null ? formatCurrency(token.budget_usd) : 'no budget'}
                  </TD>
                  <TD className="text-xs text-muted">
                    {token.expires_at ? formatDateTimeShort(token.expires_at) : 'never'}
                  </TD>
                  <TD className="text-xs text-muted">
                    {token.last_used_at ? <TimeAgo value={token.last_used_at} /> : 'never'}
                  </TD>
                  <TD align="right">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={token.status === 'revoked'}
                      onClick={() => setRevokeTarget({ id: token.id, name: token.name, prefix: token.token_prefix })}
                      iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
                    >
                      Revoke
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>

      <div className="border-t border-border px-4 py-2 text-[11px] text-muted">
        <Copy aria-hidden className="mr-1 inline h-3 w-3" />
        The list never contains the secret: only the display prefix is stored.
      </div>

      <CreateTokenDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        agentId={agent.id}
        onCreated={(value) => setCreated(value)}
      />
      <CopyOnceDialog created={created} onClose={() => setCreated(null)} />
      <ConfirmDialog
        open={Boolean(revokeTarget)}
        onCancel={() => setRevokeTarget(null)}
        onConfirm={() => void runRevoke()}
        title={`Revoke token ${revokeTarget?.name ?? ''}?`}
        description="Revocation is immediate: the next request that presents this token is refused. The token row is kept for the audit trail."
        confirmLabel="Revoke token"
        loading={revoke.isPending}
        tone="danger"
        impact={
          <div className="space-y-1">
            <p>
              Prefix <span className="font-mono">{revokeTarget?.prefix}…</span>
            </p>
            <p className="flex items-start gap-1 text-danger">
              <AlertTriangle aria-hidden className="mt-0.5 h-3 w-3 shrink-0" />
              An agent using it stops working immediately; issue a replacement token first if it must keep running.
            </p>
          </div>
        }
      />
    </Card>
  );
}
