'use client';

/**
 * "Connect your agent" — pick a token, copy one command.
 *
 * The token is never typed or created by hand here:
 *   * the picker lists the agent tokens that already exist and preselects one
 *     (`default_token_id`), so the single-token case needs no interaction at all;
 *   * "Create a new token" is an explicit item, not the only option;
 *   * one button produces the command and copies it.
 *
 * A token's secret is only stored hashed, so choosing an existing token means the server
 * re-issues that token's secret to put it in the command. That is stated before the click
 * (the previous secret stops working) instead of being discovered afterwards.
 */

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { ArrowRight, Check, Copy, KeyRound, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react';

import { api, describeError, errorRequestId } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { Badge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { CodeBlock } from '@/components/ui/code-block';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';

const NEW_TOKEN = '__new__';

interface ConnectToken {
  id: string;
  name: string;
  agent: string;
  agent_id: string;
  prefix: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
}

interface UnpublishedCapability {
  slug: string;
  name: string;
  status: string;
  version?: number;
  blockers: string[];
  next_step: string;
  validation_errors?: { field: string; message: string }[];
}

interface ConnectStatus {
  installer: { available: boolean; cli_path?: string; note?: string };
  published_capabilities: string[];
  unpublished: UnpublishedCapability[];
  active_tokens: number;
  tokens: ConnectToken[];
  default_token_id: string | null;
  ready: boolean;
  blockers: string[];
  warnings: string[];
}

interface ConnectCommand {
  command: string;
  token: string;
  token_id: string;
  token_name: string;
  expires_at: string | null;
  agent: { id: string; name: string };
  scopes: string[];
  capabilities: { published: number; granted: number };
  rotated_existing: boolean;
  warning: string;
}

export function ConnectGuide() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string>('');
  const [issued, setIssued] = useState<ConnectCommand | null>(null);
  const [copied, setCopied] = useState(false);

  const status = useQuery({
    queryKey: ['connect-status'],
    queryFn: () => api.get<ConnectStatus>('/api/admin/connect/status'),
    staleTime: 15_000,
  });

  // Preselect: the server's suggestion, then "create one" when nothing exists.
  useEffect(() => {
    if (!status.data || selected) return;
    setSelected(status.data.default_token_id ?? NEW_TOKEN);
  }, [status.data, selected]);

  const create = useMutation({
    mutationFn: (tokenId: string) =>
      api.post<ConnectCommand>('/api/admin/connect/command', tokenId === NEW_TOKEN ? {} : { token_id: tokenId }),
    onSuccess: async (data) => {
      setIssued(data);
      void queryClient.invalidateQueries({ queryKey: ['connect-status'] });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
      // One click means one click: put the command on the clipboard now, and keep the
      // button below for a second copy (clipboards get overwritten by other windows).
      try {
        await navigator.clipboard.writeText(data.command);
        setCopied(true);
        toast.success({
          title: 'Command copied',
          description: 'Paste it into your agent; it installs the skill into its own skills folder.',
        });
      } catch {
        setCopied(false);
        toast.warning({
          title: 'Command created, copy it manually',
          description: 'The browser refused clipboard access. Use the Copy command button.',
        });
      }
    },
    onError: (error) => toast.apiError(error, 'Could not create the connect command'),
  });

  const copy = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.command);
      setCopied(true);
      toast.success({
        title: 'Command copied',
        description: 'Paste it into your agent; it installs the skill into its own skills folder.',
      });
    } catch {
      toast.error({ title: 'Copy failed', description: 'Select the command and copy it manually.' });
    }
  };

  const options = useMemo(() => {
    const tokens = status.data?.tokens ?? [];
    return [
      ...tokens.map((token) => ({
        value: token.id,
        label: `${token.agent} · ${token.name} · ${token.prefix} · ${
          token.scopes.includes('*') ? 'all capabilities' : `${token.scopes.length} scope(s)`
        }${token.expires_at ? ` · expires ${formatDateTime(token.expires_at)}` : ' · no expiry'}`,
      })),
      { value: NEW_TOKEN, label: '＋ Create a new token' },
    ];
  }, [status.data]);

  const chosen = status.data?.tokens.find((token) => token.id === selected) ?? null;
  const blockers = status.data?.blockers ?? [];
  const warnings = status.data?.warnings ?? [];
  const ready = status.data?.ready === true;
  const installerMissing = status.data?.installer.available === false;
  // The button is only disabled when the command could not work at all. "Nothing
  // published yet" is a warning: installing the skill first and publishing afterwards is
  // a valid order, and a refresh with `update` picks the capability up.
  const canGenerate = ready && selected !== '' && !create.isPending;

  return (
    <Card>
      <CardHeader
        title="Connect your agent"
        description="Pick a token, copy the command, give it to your agent. The agent installs the skill itself."
        actions={
          <Badge tone={installerMissing ? 'danger' : ready ? 'success' : 'warning'} dot={false}>
            {installerMissing
              ? 'installer unavailable'
              : (status.data?.published_capabilities.length ?? 0) > 0
                ? 'ready'
                : 'ready — no capabilities yet'}
          </Badge>
        }
      />
      <CardBody className="space-y-4">
        {status.isLoading ? <Skeleton className="h-20 w-full" /> : null}

        {status.isError ? (
          <Banner
            tone="danger"
            title="Could not read the connection state"
            requestId={errorRequestId(status.error)}
            primaryAction={{ label: 'Retry', onClick: () => void status.refetch(), loading: status.isFetching }}
          >
            {describeError(status.error)}
          </Banner>
        ) : null}

        {installerMissing ? (
          <InlineAlert tone="warning">
            {status.data?.installer.note ?? 'The connection CLI is not available on this machine.'}
          </InlineAlert>
        ) : null}

        {blockers.length > 0 ? (
          <InlineAlert tone="warning">
            {blockers.map((blocker) => (
              <span key={blocker} className="block">
                {blocker}
              </span>
            ))}
          </InlineAlert>
        ) : null}

        {warnings.length > 0 ? (
          <InlineAlert tone="warning">
            {warnings.map((warning) => (
              <span key={warning} className="block">
                {warning}{' '}
                <Link href="/capabilities" className="underline">
                  Open Capabilities
                </Link>
              </span>
            ))}
          </InlineAlert>
        ) : null}

        {/* Capabilities that exist but are invisible to agents, with the exact reason.
            "I added one, why can my agent not see it" is answered here rather than left to
            a guess. */}
        {(status.data?.unpublished ?? []).length > 0 ? (
          <div className="rounded-control border border-border bg-surface-muted/60 px-3 py-2">
            <p className="text-xs font-medium text-text">
              Added but not visible to agents yet ({status.data?.unpublished.length})
            </p>
            <ul className="mt-1 space-y-1.5">
              {(status.data?.unpublished ?? []).slice(0, 3).map((item) => (
                <li key={item.slug} className="text-xs text-muted">
                  <Link href={`/capabilities/${item.slug}`} className="font-mono text-text hover:underline">
                    {item.slug}
                  </Link>{' '}
                  — {item.blockers.join('; ') || item.status}.
                  <span className="block text-text/80">Next: {item.next_step}</span>
                  {item.validation_errors && item.validation_errors.length > 0 ? (
                    <span className="mt-0.5 block font-mono text-[11px] text-danger/90">
                      {item.validation_errors
                        .slice(0, 3)
                        .map((error) => error.field)
                        .join(', ')}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {!issued ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="min-w-[260px] flex-1">
                <label htmlFor="connect-token" className="mb-1 block text-xs font-medium text-muted">
                  Token the command will carry
                </label>
                <Select
                  id="connect-token"
                  options={options}
                  value={selected}
                  onChange={(event) => setSelected(event.target.value)}
                />
              </div>
              <Button
                variant="primary"
                size="sm"
                onClick={() => create.mutate(selected || NEW_TOKEN)}
                loading={create.isPending}
                disabled={!canGenerate}
                iconLeft={<KeyRound aria-hidden className="h-3.5 w-3.5" />}
              >
                Create &amp; copy command
              </Button>
            </div>

            {chosen ? (
              <p className="text-xs text-muted">
                The command will carry a <span className="font-medium text-text">new secret</span> for this token
                ({chosen.prefix}). Its current secret stops working, so re-run the command wherever that token is
                already installed.
              </p>
            ) : (
              <p className="text-xs text-muted">
                A new agent token will be created, scoped to the published capabilities and expiring in 30 days.
              </p>
            )}

            <p className="text-sm text-text/90">
              The command contains the token, so you copy it once and hand it over — no token to paste by hand.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium text-text">Give this to your agent</p>
                <p className="text-xs text-muted">
                  Agent <span className="font-mono">{issued.agent.name}</span> · token{' '}
                  <span className="font-mono">{issued.token_name}</span> · {issued.capabilities.granted} grant(s)
                  {issued.expires_at ? ` · expires ${formatDateTime(issued.expires_at)}` : ' · no expiry'}
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => void copy()}
                  iconLeft={copied ? <Check aria-hidden className="h-3.5 w-3.5" /> : <Copy aria-hidden className="h-3.5 w-3.5" />}
                >
                  {copied ? 'Copied' : 'Copy command'}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => {
                    setIssued(null);
                    setCopied(false);
                    void status.refetch();
                  }}
                  iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
                >
                  Choose another
                </Button>
              </div>
            </div>

            <CodeBlock code={issued.command} label="powershell" maxHeightClassName="max-h-none" wrap copyable />

            <div className="rounded-control border border-warning/30 bg-warning-soft px-3 py-2 text-xs text-text/90">
              <p className="flex items-start gap-1.5">
                <TriangleAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
                <span>{issued.warning}</span>
              </p>
            </div>

            <p className="text-xs text-muted">
              Your agent runs the command, detects its own skills folder (
              <span className="font-mono">~/.codex/skills/</span> or{' '}
              <span className="font-mono">~/.claude/skills/</span>), writes the skill there and replies
              &ldquo;installed&rdquo;. Afterwards ask it:{' '}
              <span className="italic">&ldquo;what Skill Router capabilities can you use?&rdquo;</span>
            </p>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3 text-xs text-muted">
          <span className="flex items-center gap-1">
            <ShieldCheck aria-hidden className="h-3.5 w-3.5 text-success" />
            Only the granted capabilities, only this platform.
          </span>
          <Link href="/access" className="inline-flex items-center gap-1 font-medium text-accent hover:underline">
            Manage tokens
            <ArrowRight aria-hidden className="h-3 w-3" />
          </Link>
          <span>{status.data ? `${status.data.active_tokens} active token(s)` : ''}</span>
        </div>
      </CardBody>
    </Card>
  );
}
