'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, KeyRound, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { Drawer, DrawerContent } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiDelete, apiGet, apiPost, describeError } from '@/lib/api';
import { copyText, formatCost, formatDateTime, formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { CreatedToken, ExternalAgent, Paginated, PlatformToken } from '@/lib/types';

interface AgentDetail extends ExternalAgent {
  tokens: PlatformToken[];
}

/**
 * 接入与令牌（spec §13.2 `/access`）：
 * Agent 列表 + 令牌表（前缀/作用域/限流/预算/最近使用）+ 创建令牌弹窗。
 * 令牌**仅创建时完整展示一次**，随后只能看前缀；撤销即时生效并说明影响。
 */
export default function AccessPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [selectedAgentId, setSelectedAgentId] = React.useState<string | null>(null);
  const [agentDrawerOpen, setAgentDrawerOpen] = React.useState(false);
  const [tokenDialogOpen, setTokenDialogOpen] = React.useState(false);
  const [createdToken, setCreatedToken] = React.useState<CreatedToken | null>(null);
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [revokeTarget, setRevokeTarget] = React.useState<PlatformToken | null>(null);

  // 新建 agent 表单
  const [agentName, setAgentName] = React.useState('');
  const [agentDescription, setAgentDescription] = React.useState('');
  const [agentContact, setAgentContact] = React.useState('');

  // 新建令牌表单
  const [tokenName, setTokenName] = React.useState('');
  const [tokenScopes, setTokenScopes] = React.useState('*');
  const [tokenRpm, setTokenRpm] = React.useState('');
  const [tokenConcurrency, setTokenConcurrency] = React.useState('');
  const [tokenDailyCalls, setTokenDailyCalls] = React.useState('');
  const [tokenBudget, setTokenBudget] = React.useState('');
  const [tokenExpires, setTokenExpires] = React.useState('');

  const agents = useQuery<Paginated<ExternalAgent> | ExternalAgent[]>({
    queryKey: queryKeys.access.agents({ page: 1, page_size: 100 }),
    queryFn: () => apiGet<Paginated<ExternalAgent> | ExternalAgent[]>('/admin/access/agents', { page: 1, page_size: 100 }),
  });

  const agentItems = React.useMemo(() => {
    const data = agents.data;
    if (!data) return [];
    return Array.isArray(data) ? data : data.items;
  }, [agents.data]);

  React.useEffect(() => {
    if (!selectedAgentId && agentItems[0]) setSelectedAgentId(agentItems[0].id);
  }, [agentItems, selectedAgentId]);

  const selectedAgent = useQuery<AgentDetail>({
    queryKey: queryKeys.access.agent(selectedAgentId ?? 'none'),
    queryFn: () => apiGet<AgentDetail>(`/admin/access/agents/${selectedAgentId}`),
    enabled: Boolean(selectedAgentId),
  });

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.access.agents({ page: 1, page_size: 100 }) });
    if (selectedAgentId) {
      await queryClient.invalidateQueries({ queryKey: queryKeys.access.agent(selectedAgentId) });
    }
  };

  const createAgent = useMutation({
    mutationFn: async () =>
      apiPost<ExternalAgent>('/admin/access/agents', {
        name: agentName,
        description: agentDescription || null,
        contact: agentContact || null,
      }),
    onSuccess: async (agent) => {
      toast({ title: 'Agent 已创建', description: '下一步为它签发令牌。', tone: 'ok' });
      setAgentDrawerOpen(false);
      setAgentName('');
      setAgentDescription('');
      setAgentContact('');
      await invalidate();
      setSelectedAgentId(agent.id);
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '创建失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const createToken = useMutation({
    mutationFn: async () =>
      apiPost<CreatedToken>(`/admin/access/agents/${selectedAgentId}/tokens`, {
        name: tokenName,
        scopes: tokenScopes
          .split(',')
          .map((scope) => scope.trim())
          .filter(Boolean),
        rpm: tokenRpm ? Number(tokenRpm) : null,
        concurrency: tokenConcurrency ? Number(tokenConcurrency) : null,
        daily_calls: tokenDailyCalls ? Number(tokenDailyCalls) : null,
        budget_usd: tokenBudget ? Number(tokenBudget) : null,
        expires_at: tokenExpires || null,
      }),
    onSuccess: async (token) => {
      setCreatedToken(token);
      setAcknowledged(false);
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '签发失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const revoke = useMutation({
    mutationFn: async (tokenId: string) => apiDelete<null>(`/admin/access/tokens/${tokenId}`),
    onSuccess: async () => {
      toast({ title: '令牌已撤销', description: '该令牌的下一次请求即被拒绝（401 TOKEN_REVOKED）。', tone: 'ok' });
      setRevokeTarget(null);
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '撤销失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const tokens = selectedAgent.data?.tokens ?? [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="接入与令牌"
        description="外部 Agent 通过 Bearer 令牌访问 /api/v1。令牌只在创建时完整显示一次，之后只保留前缀。"
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={() => setAgentDrawerOpen(true)} icon={<Plus size={16} strokeWidth={1.75} />}>
              新建 Agent
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!selectedAgentId}
              onClick={() => {
                setCreatedToken(null);
                setTokenDialogOpen(true);
              }}
              icon={<KeyRound size={16} strokeWidth={1.75} />}
            >
              签发令牌
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        {/* Agent 列表 */}
        <section aria-label="外部 Agent" className="sr-card p-3">
          <h2 className="px-1 text-13 font-medium text-content">外部 Agent（{agentItems.length}）</h2>
          {agents.isLoading ? (
            <div className="mt-2 flex flex-col gap-1">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} height={36} />
              ))}
            </div>
          ) : agents.isError ? (
            <EmptyState
              kind="unavailable"
              compact
              title={describeError(agents.error).message}
              requestId={describeError(agents.error).requestId}
              actionLabel="重试"
              onAction={() => void agents.refetch()}
            />
          ) : agentItems.length === 0 ? (
            <EmptyState
              kind="first-use"
              compact
              title="还没有外部 Agent"
              description="先创建 Agent，再为它签发令牌。"
              actionLabel="新建 Agent"
              onAction={() => setAgentDrawerOpen(true)}
            />
          ) : (
            <ul className="mt-1 flex flex-col gap-0.5">
              {agentItems.map((agent) => {
                const active = agent.id === selectedAgentId;
                return (
                  <li key={agent.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedAgentId(agent.id)}
                      aria-current={active ? 'true' : undefined}
                      className={
                        active
                          ? 'w-full rounded-sm bg-[var(--sr-accent-weak)] px-2 py-1.5 text-left'
                          : 'w-full rounded-sm px-2 py-1.5 text-left hover:bg-hover'
                      }
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className={active ? 'text-13 text-accent' : 'text-13 text-content'}>{agent.name}</span>
                        <Badge tone={agent.status === 'active' ? 'ok' : 'neutral'} dot>
                          {agent.status === 'active' ? '启用' : '停用'}
                        </Badge>
                      </span>
                      <span className="block text-12 text-content-muted">
                        最后使用 {agent.last_seen_at ? formatRelative(agent.last_seen_at) : '从未'}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {/* 令牌表 */}
        <section aria-label="令牌" className="sr-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-16 font-semibold text-content">
              {selectedAgent.data ? `${selectedAgent.data.name} 的令牌` : '令牌'}（{tokens.length}）
            </h2>
            <span className="text-12 text-content-muted">撤销后立即生效</span>
          </div>

          {selectedAgent.isLoading ? (
            <Skeleton height={120} className="mt-3" />
          ) : selectedAgent.isError ? (
            <EmptyState
              kind="unavailable"
              compact
              title={describeError(selectedAgent.error).message}
              requestId={describeError(selectedAgent.error).requestId}
              actionLabel="重试"
              onAction={() => void selectedAgent.refetch()}
            />
          ) : tokens.length === 0 ? (
            <EmptyState
              kind="empty"
              compact
              title="该 Agent 还没有令牌"
              description="没有令牌的 Agent 无法调用 /api/v1。"
              actionLabel="签发令牌"
              onAction={() => {
                setCreatedToken(null);
                setTokenDialogOpen(true);
              }}
            />
          ) : (
            <div className="mt-3 overflow-x-auto sr-scroll">
              <table className="w-full text-13">
                <caption className="px-1 py-1 text-left text-12 text-content-muted">
                  令牌列表（前缀/作用域/限流/预算/最近使用）
                </caption>
                <thead>
                  <tr className="border-b border-border">
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">名称 / 前缀</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">作用域</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">限流</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">预算</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">最近使用</th>
                    <th scope="col" className="px-2 py-1.5 text-right text-12 font-medium text-content-muted">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--sr-border)]">
                  {tokens.map((token) => {
                    const revoked = Boolean(token.revoked_at);
                    return (
                      <tr key={token.id} className={revoked ? 'opacity-60' : undefined}>
                        <td className="px-2 py-2">
                          <span className="flex flex-col">
                            <span className="text-content">{token.name}</span>
                            <span className="sr-mono text-12 text-content-muted">
                              {token.prefix}…{revoked ? '（已撤销）' : ''}
                            </span>
                          </span>
                        </td>
                        <td className="px-2 py-2">
                          <span className="flex flex-wrap gap-1">
                            {token.scopes.slice(0, 3).map((scope) => (
                              <Badge key={scope} tone="outline" mono>
                                {scope}
                              </Badge>
                            ))}
                            {token.scopes.length > 3 ? <Badge tone="neutral">+{token.scopes.length - 3}</Badge> : null}
                          </span>
                          <span className="mt-0.5 block text-12 text-content-muted">
                            模式：{token.allowed_modes.join(' / ')}
                          </span>
                        </td>
                        <td className="px-2 py-2 text-12 text-content-muted">
                          {token.rpm ?? '—'} rpm · {token.concurrency ?? '—'} 并发 · {token.daily_calls ?? '—'} 次/日
                        </td>
                        <td className="px-2 py-2 text-12 text-content-muted">
                          {token.budget_usd !== null ? formatCost(token.budget_usd) : '不限'}
                        </td>
                        <td className="px-2 py-2 text-12 text-content-muted">
                          {token.last_used_at ? formatRelative(token.last_used_at) : '从未'}
                          {token.expires_at ? (
                            <span className="block">到期 {formatDateTime(token.expires_at)}</span>
                          ) : null}
                        </td>
                        <td className="px-2 py-2 text-right">
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`撤销令牌 ${token.name}`}
                            disabled={revoked}
                            onClick={() => setRevokeTarget(token)}
                            icon={<Trash2 size={16} strokeWidth={1.75} />}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {/* 新建 Agent */}
      <Drawer open={agentDrawerOpen} onOpenChange={setAgentDrawerOpen}>
        <DrawerContent title="新建外部 Agent" description="Agent 是令牌的归属主体，便于按调用方统计与限流。">
          <form
            noValidate
            className="flex flex-col gap-1"
            onSubmit={(event) => {
              event.preventDefault();
              createAgent.mutate();
            }}
          >
            <FormField id="agent-name" label="名称" required>
              <Input value={agentName} onChange={(event) => setAgentName(event.target.value)} />
            </FormField>
            <FormField id="agent-description" label="说明">
              <Input value={agentDescription} onChange={(event) => setAgentDescription(event.target.value)} />
            </FormField>
            <FormField id="agent-contact" label="联系人">
              <Input value={agentContact} onChange={(event) => setAgentContact(event.target.value)} />
            </FormField>
            <div className="mt-2 flex justify-end gap-2">
              <Button variant="secondary" size="sm" type="button" onClick={() => setAgentDrawerOpen(false)}>
                取消
              </Button>
              <Button variant="primary" size="sm" type="submit" loading={createAgent.isPending} disabled={!agentName}>
                创建
              </Button>
            </div>
          </form>
        </DrawerContent>
      </Drawer>

      {/* 创建令牌 */}
      <Dialog
        open={tokenDialogOpen}
        onOpenChange={(open) => {
          setTokenDialogOpen(open);
          if (!open) {
            setCreatedToken(null);
            setAcknowledged(false);
          }
        }}
      >
        <DialogContent
          title={createdToken ? '令牌已签发（仅显示一次）' : '签发令牌'}
          description={
            createdToken
              ? '关闭后无法再次查看完整令牌。请立即复制并保存到目标 Agent 的凭据存储。'
              : '作用域与限流会立即生效；令牌只在创建响应中出现一次。'
          }
          width="md"
          footer={
            createdToken ? (
              <>
                <DialogClose asChild>
                  <Button variant="secondary" size="sm" disabled={!acknowledged}>
                    我已保存，关闭
                  </Button>
                </DialogClose>
              </>
            ) : (
              <>
                <DialogClose asChild>
                  <Button variant="secondary" size="sm">
                    取消
                  </Button>
                </DialogClose>
                <Button
                  variant="primary"
                  size="sm"
                  loading={createToken.isPending}
                  disabled={!tokenName || !selectedAgentId}
                  onClick={() => createToken.mutate()}
                >
                  签发
                </Button>
              </>
            )
          }
        >
          {createdToken ? (
            <div className="flex flex-col gap-3">
              <div className="rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2 text-12 text-content">
                完整令牌只显示这一次。离开后列表只显示前缀 <code className="sr-mono">{createdToken.prefix}…</code>。
              </div>
              <div>
                <p className="text-12 text-content-muted">令牌</p>
                <div className="mt-1 flex items-center gap-2">
                  <code className="sr-mono flex-1 overflow-x-auto whitespace-pre rounded bg-[var(--sr-surface-sunken)] px-2 py-1.5 text-13 sr-scroll">
                    {createdToken.token}
                  </code>
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={<Copy size={14} strokeWidth={1.75} />}
                    onClick={async () => {
                      const ok = await copyText(createdToken.token);
                      toast({ title: ok ? '已复制令牌' : '复制失败，请手动选择', tone: ok ? 'ok' : 'warn' });
                    }}
                  >
                    复制
                  </Button>
                </div>
              </div>
              <div>
                <p className="text-12 text-content-muted">安装命令</p>
                <div className="mt-1 flex items-center gap-2">
                  <code className="sr-mono flex-1 overflow-x-auto whitespace-pre rounded bg-[var(--sr-surface-sunken)] px-2 py-1.5 text-12 sr-scroll">
                    {createdToken.install_command}
                  </code>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={async () => {
                      const ok = await copyText(createdToken.install_command);
                      toast({ title: ok ? '已复制安装命令' : '复制失败', tone: ok ? 'ok' : 'warn' });
                    }}
                  >
                    复制
                  </Button>
                </div>
              </div>
              <label className="flex items-center gap-2 text-13 text-content">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  className="h-4 w-4"
                />
                我已把令牌保存到安全位置
              </label>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <FormField id="token-name" label="令牌名称" required>
                <Input value={tokenName} onChange={(event) => setTokenName(event.target.value)} placeholder="prod-agent-a" />
              </FormField>
              <FormField id="token-scopes" label="作用域" hint="逗号分隔；* 表示全部">
                <Input value={tokenScopes} onChange={(event) => setTokenScopes(event.target.value)} className="sr-mono" />
              </FormField>
              <div className="grid grid-cols-2 gap-2">
                <FormField id="token-rpm" label="RPM">
                  <Input type="number" value={tokenRpm} onChange={(event) => setTokenRpm(event.target.value)} />
                </FormField>
                <FormField id="token-concurrency" label="并发上限">
                  <Input type="number" value={tokenConcurrency} onChange={(event) => setTokenConcurrency(event.target.value)} />
                </FormField>
                <FormField id="token-daily" label="每日调用上限">
                  <Input type="number" value={tokenDailyCalls} onChange={(event) => setTokenDailyCalls(event.target.value)} />
                </FormField>
                <FormField id="token-budget" label="预算（USD）">
                  <Input type="number" value={tokenBudget} onChange={(event) => setTokenBudget(event.target.value)} />
                </FormField>
              </div>
              <FormField id="token-expires" label="过期时间" hint="留空为不过期">
                <Input type="datetime-local" value={tokenExpires} onChange={(event) => setTokenExpires(event.target.value)} />
              </FormField>
              {createToken.isError ? (
                <>
                  <p className="text-12 text-danger">{describeError(createToken.error).message}</p>
                  <RequestIdCopy requestId={describeError(createToken.error).requestId ?? ''} />
                </>
              ) : null}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <DangerConfirm
        open={Boolean(revokeTarget)}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
        title="撤销令牌"
        action="撤销"
        description="撤销即时生效：该令牌的后续请求会返回 401 TOKEN_REVOKED。"
        impacts={
          revokeTarget
            ? [
                { id: revokeTarget.id, label: revokeTarget.name, detail: `前缀 ${revokeTarget.prefix}…` },
                { id: `${revokeTarget.id}-agent`, label: `影响 Agent：${selectedAgent.data?.name ?? revokeTarget.agent_id}` },
              ]
            : []
        }
        confirmLabel={`撤销 ${revokeTarget?.name ?? '令牌'}`}
        loading={revoke.isPending}
        onConfirm={async () => {
          if (revokeTarget) await revoke.mutateAsync(revokeTarget.id);
        }}
      />
    </div>
  );
}
