'use client';

import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  ArrowRight,
  Bot,
  CircleAlert,
  Cpu,
  ListChecks,
  Plug,
  Wrench,
} from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Skeleton } from '@/components/ui/skeleton';
import { apiGet, describeError } from '@/lib/api';
import { formatCount, formatDuration, formatPercent, formatRelative, statusLabel } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { CallLog, Overview, Paginated } from '@/lib/types';

/**
 * 概览（spec §13.2 `/`）：
 * 指标卡 + 接入状态流水线 + 待办 + 最近调用 + 引擎卡。
 * 每个区块独立查询，**单卡失败不拖垮整页**（partial failure）。
 */
export default function OverviewPage() {
  const overview = useQuery<Overview>({
    queryKey: queryKeys.overview(),
    queryFn: () => apiGet<Overview>('/admin/overview'),
    refetchInterval: 60_000,
  });

  const recentCalls = useQuery<Paginated<CallLog>>({
    queryKey: queryKeys.calls.list({ page: 1, page_size: 20, sort: '-created_at' }),
    queryFn: () =>
      apiGet<Paginated<CallLog>>('/admin/calls', { page: 1, page_size: 20, sort: '-created_at' }),
    refetchInterval: 30_000,
  });

  const data = overview.data;
  const errorInfo = overview.isError ? describeError(overview.error) : null;

  const isFirstUse =
    data &&
    data.capabilities.published === 0 &&
    data.capabilities.draft === 0 &&
    data.providers.total === 0;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="概览"
        description="能力、凭证、SKILL 与引擎的当前状态。每张卡片与每条待办都可以直接跳到对应详情。"
        actions={
          <>
            <Button asChild variant="secondary" size="sm">
              <Link href="/providers" className="inline-flex items-center gap-1">
                <Plug size={16} strokeWidth={1.75} aria-hidden />
                添加提供商
              </Link>
            </Button>
            <Button asChild variant="primary" size="sm">
              <Link href="/capabilities/new" className="inline-flex items-center gap-1">
                <Wrench size={16} strokeWidth={1.75} aria-hidden />
                新建能力
              </Link>
            </Button>
          </>
        }
      />

      {overview.isLoading ? (
        <OverviewSkeleton />
      ) : errorInfo ? (
        <EmptyState
          kind={errorInfo.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
          title={errorInfo.message}
          description={errorInfo.hint ?? undefined}
          requestId={errorInfo.requestId}
          actionLabel="重试"
          onAction={() => void overview.refetch()}
        />
      ) : isFirstUse ? (
        <FirstUseGuide />
      ) : data ? (
        <>
          {/* 指标卡 */}
          <section aria-label="关键指标" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricCard
              label="能力"
              icon={<Wrench size={16} strokeWidth={1.75} aria-hidden />}
              value={formatCount(data.capabilities.published)}
              unit="已发布"
              detail={`草稿 ${data.capabilities.draft} · 异常 ${data.capabilities.failing}`}
              href="/capabilities"
            />
            <MetricCard
              label="24h 成功率"
              icon={<Activity size={16} strokeWidth={1.75} aria-hidden />}
              value={formatPercent(data.success_rate_24h)}
              detail={`调用 ${formatCount(data.calls_24h)} 次`}
              href="/calls"
              tone={data.success_rate_24h < 0.95 ? 'warn' : 'ok'}
            />
            <MetricCard
              label="耗时 P50 / P95"
              icon={<Activity size={16} strokeWidth={1.75} aria-hidden />}
              value={formatDuration(data.latency.p95_ms)}
              unit="P95"
              detail={`P50 ${formatDuration(data.latency.p50_ms)}`}
              href="/calls"
            />
            <MetricCard
              label="失败任务 / 队列"
              icon={<ListChecks size={16} strokeWidth={1.75} aria-hidden />}
              value={formatCount(data.failed_tasks)}
              unit="失败"
              detail={`队列深度 ${data.queue_depth}`}
              href="/tasks"
              tone={data.failed_tasks > 0 ? 'danger' : 'neutral'}
            />
          </section>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* 接入状态流水线 */}
            <section aria-label="接入状态" className="sr-card p-4 lg:col-span-2">
              <h2 className="text-16 font-semibold text-content">接入状态</h2>
              <p className="mt-0.5 text-12 text-content-muted">
                从凭证到可调用的四步；点任一步进入对应页面。
              </p>
              <ol className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-4">
                <PipelineStep
                  index={1}
                  title="提供商"
                  value={`${data.providers.with_active_credential} / ${data.providers.total}`}
                  detail="有有效凭证的提供商"
                  href="/providers"
                  done={data.providers.with_active_credential > 0}
                />
                <PipelineStep
                  index={2}
                  title="能力"
                  value={`${data.capabilities.published}`}
                  detail="已发布能力"
                  href="/capabilities"
                  done={data.capabilities.published > 0}
                />
                <PipelineStep
                  index={3}
                  title="SKILL"
                  value="目录"
                  detail="对外分发单元"
                  href="/skills"
                  done
                />
                <PipelineStep
                  index={4}
                  title="外部 Agent"
                  value="令牌"
                  detail="作用域与预算"
                  href="/access"
                  done
                />
              </ol>
            </section>

            {/* 引擎卡 */}
            <section aria-label="引擎状态" className="sr-card p-4">
              <div className="flex items-center justify-between">
                <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
                  <Cpu size={16} strokeWidth={1.75} aria-hidden />
                  Agent 引擎
                </h2>
                <StatusBadge kind="engine" status={data.engine.status} />
              </div>
              <dl className="mt-3 flex flex-col gap-1.5 text-13">
                <div className="flex justify-between">
                  <dt className="text-content-muted">当前版本</dt>
                  <dd className="sr-mono text-content">{data.engine.version ?? '—'}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-content-muted">升级策略</dt>
                  <dd className="text-content">{statusLabel(data.engine.policy ?? 'manual')}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-content-muted">候选版本</dt>
                  <dd className="sr-mono text-content">{data.engine.candidate ?? '无'}</dd>
                </div>
              </dl>
              <Button asChild variant="secondary" size="sm" className="mt-3 w-full">
                <Link href="/settings/engine" className="inline-flex items-center justify-center gap-1">
                  查看升级与回滚
                  <ArrowRight size={14} strokeWidth={1.75} aria-hidden />
                </Link>
              </Button>
            </section>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* 待办 */}
            <section aria-label="待办" className="sr-card p-4">
              <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
                <CircleAlert size={16} strokeWidth={1.75} aria-hidden />
                待办（{data.attention.length}）
              </h2>
              {data.attention.length === 0 ? (
                <p className="mt-2 text-13 text-content-muted">没有需要处理的事项。</p>
              ) : (
                <ul className="mt-2 divide-y divide-[var(--sr-border)]">
                  {data.attention.map((item) => (
                    <li key={`${item.code}-${item.target}`}>
                      <Link
                        href={item.target}
                        className="flex flex-col gap-0.5 py-2 hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                      >
                        <span className="flex items-center gap-1.5 text-13 text-content">
                          <Badge
                            tone={item.severity === 'error' ? 'danger' : item.severity === 'warning' ? 'warn' : 'info'}
                            dot
                          >
                            {item.severity === 'error' ? '错误' : item.severity === 'warning' ? '警告' : '提示'}
                          </Badge>
                          {item.title}
                        </span>
                        <span className="sr-mono text-12 text-content-muted">{item.target}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* 最近调用 */}
            <section aria-label="最近调用" className="sr-card p-4 lg:col-span-2">
              <div className="flex items-center justify-between">
                <h2 className="text-16 font-semibold text-content">最近调用</h2>
                <Link href="/calls" className="text-12 text-accent hover:underline">
                  查看全部
                </Link>
              </div>
              {recentCalls.isLoading ? (
                <div className="mt-3 flex flex-col gap-2">
                  {Array.from({ length: 4 }).map((_, index) => (
                    <Skeleton key={index} height={20} />
                  ))}
                </div>
              ) : recentCalls.isError ? (
                // 单卡失败：不影响其它区块
                <div className="mt-2 rounded border border-border px-3 py-2">
                  <p className="text-13 text-content-muted">
                    最近调用加载失败：{describeError(recentCalls.error).message}
                  </p>
                  <RequestIdCopy requestId={describeError(recentCalls.error).requestId ?? ''} className="mt-1" />
                  <Button variant="secondary" size="sm" className="mt-2" onClick={() => void recentCalls.refetch()}>
                    重试
                  </Button>
                </div>
              ) : (recentCalls.data?.items.length ?? 0) === 0 ? (
                <p className="mt-2 text-13 text-content-muted">还没有调用记录。</p>
              ) : (
                <div className="mt-2 overflow-x-auto sr-scroll">
                  <table className="w-full text-13">
                    <caption className="sr-only">最近 5 条调用记录</caption>
                    <thead>
                      <tr className="border-b border-border">
                        <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                          时间
                        </th>
                        <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                          能力
                        </th>
                        <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                          状态
                        </th>
                        <th scope="col" className="px-2 py-1.5 text-right text-12 font-medium text-content-muted">
                          耗时
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--sr-border)]">
                      {recentCalls.data?.items.slice(0, 5).map((call) => (
                        <tr key={call.id}>
                          <td className="px-2 py-1.5 text-content-muted">{formatRelative(call.created_at)}</td>
                          <td className="px-2 py-1.5">
                            <span className="sr-mono text-content">{call.capability_key}</span>
                          </td>
                          <td className="px-2 py-1.5">
                            <StatusBadge kind="call" status={call.status} />
                          </td>
                          <td className="px-2 py-1.5 text-right text-content-muted">
                            {formatDuration(call.latency_ms)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}

function MetricCard({
  label,
  value,
  unit,
  detail,
  href,
  icon,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  unit?: string;
  detail?: string;
  href: string;
  icon: React.ReactNode;
  tone?: 'neutral' | 'ok' | 'warn' | 'danger';
}) {
  const toneClass =
    tone === 'danger' ? 'text-danger' : tone === 'warn' ? 'text-warn' : tone === 'ok' ? 'text-ok' : 'text-content';
  return (
    <Link
      href={href}
      className="sr-card flex flex-col gap-1 p-4 transition-colors duration-150 ease-sr hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
    >
      <span className="flex items-center gap-1.5 text-12 text-content-muted">
        {icon}
        {label}
      </span>
      <span className="flex items-baseline gap-1">
        <span className={cn('text-24 font-semibold tracking-tight', toneClass)}>{value}</span>
        {unit ? <span className="text-12 text-content-muted">{unit}</span> : null}
      </span>
      {detail ? <span className="text-12 text-content-muted">{detail}</span> : null}
    </Link>
  );
}

function PipelineStep({
  index,
  title,
  value,
  detail,
  href,
  done,
}: {
  index: number;
  title: string;
  value: string;
  detail: string;
  href: string;
  done: boolean;
}) {
  return (
    <li>
      <Link
        href={href}
        className="flex h-full flex-col gap-1 rounded border border-border px-3 py-2 hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
      >
        <span className="flex items-center gap-1.5 text-12 text-content-muted">
          <span aria-hidden className="flex h-4 w-4 items-center justify-center rounded-full border border-border text-12">
            {index}
          </span>
          {title}
          {done ? (
            <Badge tone="ok">就绪</Badge>
          ) : (
            <Badge tone="warn">待配置</Badge>
          )}
        </span>
        <span className="text-16 font-medium text-content">{value}</span>
        <span className="text-12 text-content-muted">{detail}</span>
      </Link>
    </li>
  );
}

function FirstUseGuide() {
  const steps = [
    {
      title: '添加提供商与凭证',
      detail: '凭证密文只在服务端解密；页面只显示掩码。',
      href: '/providers',
      action: '去添加',
    },
    {
      title: '创建能力并跑一次真实测试',
      detail: '测试通过后才有资格发布（24h 证据有效）。',
      href: '/capabilities/new',
      action: '新建能力',
    },
    {
      title: '签发令牌给外部 Agent',
      detail: '令牌只在创建时完整显示一次，随后不可再读。',
      href: '/access',
      action: '签发令牌',
    },
  ];
  return (
    <section className="sr-card p-6" aria-label="首次使用引导">
      <h2 className="text-20 font-semibold text-content">三步接入第一个能力</h2>
      <p className="mt-1 text-13 text-content-muted">
        当前没有任何提供商与能力。按下面顺序操作，每一步完成都能看到真实状态（不是假成功）。
      </p>
      <ol className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
        {steps.map((step, index) => (
          <li key={step.href} className="flex flex-col gap-2 rounded-md border border-border p-3">
            <span className="text-12 text-content-muted">第 {index + 1} 步</span>
            <span className="text-14 font-medium text-content">{step.title}</span>
            <span className="text-12 text-content-muted">{step.detail}</span>
            <Button asChild variant="secondary" size="sm" className="mt-auto">
              <Link href={step.href}>{step.action}</Link>
            </Button>
          </li>
        ))}
      </ol>
      <p className="mt-4 flex items-center gap-1.5 text-12 text-content-muted">
        <Bot size={14} strokeWidth={1.75} aria-hidden />
        也可以按 Alt+J 打开 Agent 面板，让它读接口文档并生成可复核的草稿（仍需要你批准）。
      </p>
    </section>
  );
}

function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} height={92} rounded="md" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Skeleton height={168} rounded="md" className="lg:col-span-2" />
        <Skeleton height={168} rounded="md" />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Skeleton height={220} rounded="md" />
        <Skeleton height={220} rounded="md" className="lg:col-span-2" />
      </div>
    </div>
  );
}
