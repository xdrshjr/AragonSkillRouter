'use client';

import { useState } from 'react';
import { Bot, Pencil, Plus, Trash2 } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatCurrency, formatDateTimeShort } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { AgentDeleteDialog, AgentFormDialog, TokensSection } from '@/components/access/access-sections';
import { BudgetsSection } from '@/components/access/budgets-section';
import { InstallSection } from '@/components/access/install-section';
import { useAgentsQuery, useUsageLimitsQuery, type Agent } from '@/components/access/api';

export default function AccessPage() {
  const agents = useAgentsQuery();
  const limits = useUsageLimitsQuery();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Agent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);

  const items = agents.data?.items ?? [];
  const selected = items.find((agent) => agent.id === selectedId) ?? items[0] ?? null;

  const countersFor = (agentId: string) => limits.data?.agents.find((entry) => entry.agent.id === agentId) ?? null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Access"
        description="Agents hold tokens; tokens carry scopes, limits and an optional budget. Nothing here can exceed a budget policy."
        actions={
          <Button
            size="sm"
            variant="primary"
            onClick={() => setCreateOpen(true)}
            iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
          >
            New agent
          </Button>
        }
      />

      <Card>
        <CardHeader
          title="Agents"
          description="Token counts and the credit ceiling configured on their tokens (max budget_usd across active tokens)."
          actions={<Badge tone="neutral" dot={false}>{items.length} agent(s)</Badge>}
        />
        <CardBody className="p-0">
          {agents.isLoading ? (
            <div className="p-3">
              <SkeletonTable rows={3} columns={5} />
            </div>
          ) : agents.isError ? (
            <div className="p-3">
              <Banner
                tone="danger"
                title="Could not load agents"
                requestId={errorRequestId(agents.error)}
                primaryAction={{ label: 'Retry', onClick: () => void agents.refetch(), loading: agents.isFetching, variant: 'primary' }}
              >
                {describeError(agents.error)}
              </Banner>
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              icon={<Bot aria-hidden className="h-5 w-5" />}
              title="No agent yet"
              description="Create an agent, then issue a scoped token so it can call the router."
              action={
                <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
                  New agent
                </Button>
              }
            />
          ) : (
            <Table caption="Agents">
              <THead sticky={false}>
                <TR>
                  <TH>Agent</TH>
                  <TH>Status</TH>
                  <TH>Tokens</TH>
                  <TH>Credits</TH>
                  <TH>Current usage</TH>
                  <TH>Created</TH>
                  <TH align="right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {items.map((agent) => {
                  const counters = countersFor(agent.id);
                  return (
                    <TR
                      key={agent.id}
                      className={selected?.id === agent.id ? 'bg-accent-soft/40' : undefined}
                    >
                      <TD>
                        <button
                          type="button"
                          onClick={() => setSelectedId(agent.id)}
                          className="text-left font-medium text-text hover:underline"
                          aria-pressed={selected?.id === agent.id}
                        >
                          {agent.name}
                        </button>
                        <span className="block font-mono text-[11px] text-muted">{agent.slug}</span>
                        {agent.description ? (
                          <span className="block max-w-[320px] truncate text-[11px] text-muted">{agent.description}</span>
                        ) : null}
                      </TD>
                      <TD>
                        <StatusBadge status={agent.status} />
                      </TD>
                      <TD className="text-xs">
                        {agent.counts.active_tokens} active / {agent.counts.tokens} total
                      </TD>
                      <TD className="text-xs tabular-nums">
                        {counters?.limits.budget_usd === null || counters?.limits.budget_usd === undefined
                          ? 'no ceiling'
                          : formatCurrency(counters.limits.budget_usd)}
                      </TD>
                      <TD className="text-xs text-muted">
                        {counters
                          ? `${counters.calls_last_minute ?? 0} call(s)/min · conc ${counters.concurrency ?? 0}`
                          : limits.isError
                            ? 'unavailable'
                            : '—'}
                      </TD>
                      <TD className="text-xs text-muted">
                        <TimeAgo value={agent.created_at} />
                      </TD>
                      <TD align="right">
                        <span className="inline-flex items-center gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(event) => {
                              event.stopPropagation();
                              setEditTarget(agent);
                            }}
                            iconLeft={<Pencil aria-hidden className="h-3.5 w-3.5" />}
                          >
                            Edit
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDeleteTarget(agent);
                            }}
                            iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
                          >
                            Delete
                          </Button>
                        </span>
                      </TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
          )}
        </CardBody>
        {limits.isError ? (
          <div className="border-t border-border p-3">
            <Banner
              tone="warning"
              title="Live counters are unavailable"
              requestId={errorRequestId(limits.error)}
              primaryAction={{ label: 'Retry', onClick: () => void limits.refetch(), loading: limits.isFetching }}
            >
              {describeError(limits.error)} The token counts and credits above come from the agents endpoint and stay valid.
            </Banner>
          </div>
        ) : null}
      </Card>

      {selected ? <TokensSection agent={selected} /> : null}

      <BudgetsSection agents={items} />

      <InstallSection tokenPrefix={null} />

      <p className="text-[11px] text-muted">
        Signed-in administrator · tokens are hashed at rest · revocation is immediate ·{' '}
        {limits.data?.generated_at ? `counters as of ${formatDateTimeShort(limits.data.generated_at)}` : 'counters unavailable'}
      </p>

      <AgentFormDialog open={createOpen} onClose={() => setCreateOpen(false)} />
      <AgentFormDialog open={Boolean(editTarget)} onClose={() => setEditTarget(null)} agent={editTarget} />
      {deleteTarget ? (
        <AgentDeleteDialog open onClose={() => setDeleteTarget(null)} agent={deleteTarget} />
      ) : null}
    </div>
  );
}
