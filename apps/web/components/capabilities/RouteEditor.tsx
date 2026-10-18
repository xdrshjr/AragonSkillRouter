'use client';

import { ArrowDown, ArrowUp, CircleAlert, Plus, Star, Trash2 } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Select, type SelectOption } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { formatDuration } from '@/lib/format';
import type { CapabilityRoute } from '@/lib/types';

/**
 * 多 route 编辑（spec §13.2 `/capabilities/[id]` 路由 Tab，v2）：
 * 每条 route 明确展示 提供商 / 凭证 / 优先级 / 启用状态，
 * 并标出「谁是当前主力」（priority 最小且 enabled 的那条）与故障切换顺序。
 */

export interface RouteEditorProvider {
  id: string;
  name: string;
  credentials: { id: string; name: string; status: string }[];
}

export interface RouteEditorProps {
  routes: CapabilityRoute[];
  providers: RouteEditorProvider[];
  onChange: (routes: CapabilityRoute[]) => void;
  disabled?: boolean;
  className?: string;
}

function primaryRouteId(routes: CapabilityRoute[]): string | null {
  const enabled = routes.filter((route) => route.enabled);
  if (enabled.length === 0) return null;
  const sorted = [...enabled].sort((a, b) => a.priority - b.priority);
  return sorted[0]?.id ?? null;
}

export function failoverOrder(routes: CapabilityRoute[]): CapabilityRoute[] {
  return [...routes.filter((route) => route.enabled)].sort((a, b) => a.priority - b.priority);
}

export function RouteEditor({ routes, providers, onChange, disabled, className }: RouteEditorProps) {
  const primaryId = primaryRouteId(routes);
  const order = failoverOrder(routes);

  const providerOptions: SelectOption[] = providers.map((provider) => ({
    value: provider.id,
    label: provider.name,
  }));

  const patch = (id: string, changes: Partial<CapabilityRoute>): void => {
    onChange(routes.map((route) => (route.id === id ? { ...route, ...changes } : route)));
  };

  const remove = (id: string): void => {
    onChange(routes.filter((route) => route.id !== id));
  };

  const addRoute = (): void => {
    const provider = providers[0];
    const credential = provider?.credentials[0];
    const now = new Date().toISOString();
    const next: CapabilityRoute = {
      id: `rt_new_${routes.length + 1}`,
      capability_id: '',
      provider_id: provider?.id ?? '',
      credential_id: credential?.id ?? '',
      route_key: `route_${routes.length + 1}`,
      priority: (routes.length === 0 ? 0 : Math.max(...routes.map((r) => r.priority)) + 10),
      enabled: true,
      model_hint: null,
      request: { method: 'POST', url_template: '{base_url}/v1/…', encoding: 'json' },
      param_map: {},
      response_map: {},
      stream: null,
      async_spec: null,
      limits: {},
      errors: [],
      timeout_ms: 60000,
      retry: { max_attempts: 3, base_ms: 500, max_ms: 8000 },
      health: {},
      created_at: now,
      updated_at: now,
    };
    onChange([...routes, next]);
  };

  const move = (id: string, direction: 'up' | 'down'): void => {
    const sorted = [...routes].sort((a, b) => a.priority - b.priority);
    const index = sorted.findIndex((route) => route.id === id);
    if (index < 0) return;
    const swapIndex = direction === 'up' ? index - 1 : index + 1;
    const current = sorted[index];
    const other = sorted[swapIndex];
    if (!current || !other) return;
    patch(current.id, { priority: other.priority });
    patch(other.id, { priority: current.priority });
  };

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-13 font-medium text-content">路由（{routes.length}）</h3>
          <p className="text-12 text-content-muted">
            优先级数字越小越先被选中；固定指定 = 只保留一条启用的 route。仅在两条以上启用时才发生故障切换。
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={addRoute}
          disabled={disabled}
          icon={<Plus size={14} strokeWidth={1.75} />}
        >
          新增 route
        </Button>
      </div>

      {/* 故障切换顺序 */}
      <section aria-label="故障切换顺序" className="rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2">
        <h4 className="text-12 font-medium text-content-muted">故障切换顺序</h4>
        {order.length === 0 ? (
          <p className="mt-1 text-12 text-danger">没有任何启用的 route —— 能力不可调用。</p>
        ) : (
          <ol className="mt-1 flex flex-wrap items-center gap-1.5 text-12">
            {order.map((route, index) => {
              const provider = providers.find((p) => p.id === route.provider_id);
              return (
                <li key={route.id} className="flex items-center gap-1.5">
                  {index > 0 ? <span aria-hidden className="text-content-muted">→</span> : null}
                  <Badge tone={index === 0 ? 'accent' : 'neutral'} dot={index === 0}>
                    {index === 0 ? '主力：' : ''}
                    {provider?.name ?? route.provider_id}
                  </Badge>
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <ul className="flex flex-col gap-2">
        {routes.map((route) => {
          const provider = providers.find((item) => item.id === route.provider_id);
          const credentialOptions: SelectOption[] = (provider?.credentials ?? []).map((credential) => ({
            value: credential.id,
            label: `${credential.name}（${credential.status}）`,
            disabled: credential.status !== 'active',
          }));
          const isPrimary = route.id === primaryId;
          return (
            <li key={route.id} className="rounded-md border border-border bg-surface">
              <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
                <span className="sr-mono text-12 text-content-muted">{route.route_key || route.id}</span>
                {isPrimary ? (
                  <Badge tone="accent">
                    <Star size={12} strokeWidth={1.75} aria-hidden />
                    当前主力
                  </Badge>
                ) : null}
                {!route.enabled ? <Badge tone="danger">已停用</Badge> : null}
                <div className="ml-auto flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="上移优先级"
                    onClick={() => move(route.id, 'up')}
                    disabled={disabled}
                    icon={<ArrowUp size={16} strokeWidth={1.75} />}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="下移优先级"
                    onClick={() => move(route.id, 'down')}
                    disabled={disabled}
                    icon={<ArrowDown size={16} strokeWidth={1.75} />}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`删除 route ${route.route_key || route.id}`}
                    onClick={() => remove(route.id)}
                    disabled={disabled || routes.length <= 1}
                    icon={<Trash2 size={16} strokeWidth={1.75} />}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 gap-2 px-3 py-2 md:grid-cols-2">
                <label className="flex flex-col gap-1">
                  <span className="text-12 text-content-muted">提供商</span>
                  <Select
                    size="sm"
                    aria-label="提供商"
                    value={route.provider_id || undefined}
                    disabled={disabled}
                    placeholder="选择提供商"
                    onValueChange={(value) => {
                      const nextProvider = providers.find((item) => item.id === value);
                      patch(route.id, {
                        provider_id: value,
                        credential_id: nextProvider?.credentials[0]?.id ?? '',
                      });
                    }}
                    options={providerOptions}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-12 text-content-muted">凭证</span>
                  <Select
                    size="sm"
                    aria-label="凭证"
                    value={route.credential_id || undefined}
                    disabled={disabled || credentialOptions.length === 0}
                    placeholder={credentialOptions.length === 0 ? '该提供商暂无凭证' : '选择凭证'}
                    onValueChange={(value) => patch(route.id, { credential_id: value })}
                    options={credentialOptions}
                  />
                  {credentialOptions.length === 0 ? (
                    <span className="flex items-center gap-1 text-12 text-warn">
                      <CircleAlert size={12} strokeWidth={1.75} aria-hidden />
                      先到提供商页面添加凭证
                    </span>
                  ) : null}
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-12 text-content-muted">优先级（越小越优先）</span>
                  <input
                    type="number"
                    value={route.priority}
                    disabled={disabled}
                    aria-label="优先级"
                    onChange={(event) => patch(route.id, { priority: Number(event.target.value) })}
                    className="h-8 w-full rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-12 text-content-muted">模型提示（model_hint）</span>
                  <input
                    value={route.model_hint ?? ''}
                    disabled={disabled}
                    aria-label="模型提示"
                    placeholder="gpt-4o-mini"
                    onChange={(event) => patch(route.id, { model_hint: event.target.value || null })}
                    className="h-8 w-full rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                  />
                </label>
                <label className="flex flex-col gap-1 md:col-span-2">
                  <span className="text-12 text-content-muted">请求地址模板（必须含 {'{base_url}'} 或白名单内的绝对 URL）</span>
                  <input
                    value={route.request.url_template}
                    disabled={disabled}
                    aria-label="请求地址模板"
                    onChange={(event) =>
                      patch(route.id, { request: { ...route.request, url_template: event.target.value } })
                    }
                    className="h-8 w-full rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-12 text-content-muted">超时</span>
                  <input
                    type="number"
                    value={route.timeout_ms}
                    disabled={disabled}
                    aria-label="超时（毫秒）"
                    onChange={(event) => patch(route.id, { timeout_ms: Number(event.target.value) })}
                    className="h-8 w-full rounded border border-border bg-surface px-2 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                  />
                  <span className="text-12 text-content-muted">{formatDuration(route.timeout_ms)}</span>
                </label>
                <div className="flex items-end">
                  <Switch
                    id={`route-enabled-${route.id}`}
                    checked={route.enabled}
                    disabled={disabled}
                    onCheckedChange={(checked) => patch(route.id, { enabled: checked })}
                    label={`启用 route ${route.route_key || route.id}`}
                    description={route.enabled ? '参与路由候选' : '不参与路由（历史调用仍可查）'}
                  />
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {routes.length > 1 ? (
        <p className="text-12 text-content-muted">
          启用 {order.length} 条中优先级最高的是「{providers.find((p) => p.id === order[0]?.provider_id)?.name ?? '—'}」；
          其余仅在主力失败且错误可重试时按顺序切换。
        </p>
      ) : null}
    </div>
  );
}
