'use client';

import { useQuery } from '@tanstack/react-query';
import { ChevronRight, History, RotateCw } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Skeleton } from '@/components/ui/skeleton';
import { describeError } from '@/lib/api';
import { formatDuration, formatRelative, formatShortTime } from '@/lib/format';
import { getProbes } from '@/lib/llm';
import { queryKeys } from '@/lib/query';
import type { LlmProbeHistoryItem, LlmProbeHistoryResponse } from '@/lib/types';
import { LLM_STAGE_LABELS } from './ConnectionTester';

/**
 * 探测历史（spec §3.8 ⑥）：`GET /admin/llm/probes` 的最近 N 条。
 *
 * **没有详情端点**（spec §5.5 / R-14）：列表项已经带了完整的 `stages`
 * （一次探测最多 2 个阶段），所以「点开看阶段」是纯前端就地展开，
 * 不要为一次点击再打一次请求，也不要为此新增 `GET /llm/probes/{id}`。
 *
 * `probe_id` 的唯一用途是和 `var/logs` 里的 `llm_probe_*` 事件对齐，
 * 所以这里把它等宽展示、可复制文本。
 */

/** 模式 → 中文（`both` 必须点明计费，避免「测试了一次」被误读成免费） */
const MODE_LABELS: Record<string, string> = {
  auth: '鉴权（不计费）',
  completion: '生成（计费）',
  both: '鉴权 + 生成（计费）',
};

export interface ProbeHistoryProps {
  /** 只看某条凭证的探测记录（草稿测试没有凭证 id） */
  credentialId?: string | null;
  /** 最近 N 条，默认 10（后端 `page_size` 上限 100） */
  limit?: number;
  enabled?: boolean;
  className?: string;
  /** 空态补充说明 */
  emptyHint?: string;
}

function StageRows({ item }: { item: LlmProbeHistoryItem }) {
  return (
    <ul className="mt-1 flex flex-col gap-1 border-t border-border pt-2">
      {item.stages.length === 0 ? (
        <li className="text-12 text-content-muted">这次探测没有记录到阶段</li>
      ) : null}
      {item.stages.map((stage, index) => (
        <li
          key={`${item.id}-${stage.stage}-${index}`}
          className="flex flex-wrap items-baseline gap-x-3 gap-y-1"
        >
          <span className="min-w-[120px] text-13 text-content">
            {LLM_STAGE_LABELS[stage.stage] ?? stage.stage}
          </span>
          <Badge tone={stage.ok ? 'ok' : 'danger'}>{stage.ok ? '通过' : '失败'}</Badge>
          <span className="text-12 text-content-muted">
            HTTP {stage.http_status ?? '—'} · {formatDuration(stage.latency_ms)}
          </span>
          {stage.model_count !== undefined && stage.model_count !== null ? (
            <span className="text-12 text-content-muted">{stage.model_count} 个模型</span>
          ) : null}
          {stage.model ? (
            <span className="break-all text-12 text-content-muted">
              模型 <code className="sr-mono">{stage.model}</code>
            </span>
          ) : null}
          {stage.detail ? (
            <span className="basis-full break-words text-12 text-content-muted">{stage.detail}</span>
          ) : null}
        </li>
      ))}
      {item.detail_redacted ? (
        <li className="basis-full break-words text-12 text-content-muted">
          摘要（已脱敏）：{item.detail_redacted}
        </li>
      ) : null}
    </ul>
  );
}

export function ProbeHistory({
  credentialId,
  limit = 10,
  enabled = true,
  className,
  emptyHint,
}: ProbeHistoryProps) {
  const [expanded, setExpanded] = React.useState<string | null>(null);

  const params = React.useMemo(
    () => ({ page: 1, page_size: limit, credential_id: credentialId ?? undefined }),
    [limit, credentialId],
  );

  const query = useQuery<LlmProbeHistoryResponse>({
    queryKey: queryKeys.llm.probes(params),
    queryFn: ({ signal }) => getProbes(params, signal),
    enabled,
    staleTime: 10_000,
  });

  const items = query.data?.items ?? [];

  return (
    <section className={cn('flex flex-col gap-2', className)} aria-label="探测历史">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
          <History size={16} strokeWidth={1.75} aria-hidden />
          探测历史
        </h2>
        <div className="flex items-center gap-2">
          <span className="text-12 text-content-muted">
            最近 {Math.min(limit, 100)} 条 · 列表已含完整阶段，点开就地展开
          </span>
          <Button
            variant="ghost"
            size="sm"
            loading={query.isFetching}
            onClick={() => void query.refetch()}
            icon={<RotateCw size={14} strokeWidth={1.75} />}
          >
            刷新
          </Button>
        </div>
      </div>

      {query.isLoading ? (
        <div className="flex flex-col gap-1" aria-busy="true">
          <Skeleton height={32} />
          <Skeleton height={32} />
          <Skeleton height={32} />
        </div>
      ) : query.isError ? (
        (() => {
          const info = describeError(query.error);
          return (
            <div className="flex flex-wrap items-center gap-2 text-12" aria-live="polite">
              <span className="text-content">
                读取探测历史失败：{info.message}
                {info.hint ? `（${info.hint}）` : ''}
              </span>
              {info.requestId ? (
                <code className="sr-mono break-all text-content-muted">{info.requestId}</code>
              ) : null}
              <Button variant="secondary" size="sm" onClick={() => void query.refetch()}>
                重试
              </Button>
            </div>
          );
        })()
      ) : items.length === 0 ? (
        <p className="text-12 text-content-muted" aria-live="polite">
          还没有探测记录：点「测试连接」跑一次真实请求，结论会出现在这里。
          {emptyHint ? ` ${emptyHint}` : ''}
        </p>
      ) : (
        <ul className="flex flex-col" aria-live="polite">
          {items.map((item) => {
            const open = expanded === item.id;
            return (
              <li key={item.id} className="border-b border-border last:border-0">
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : item.id)}
                  className={cn(
                    'flex min-h-9 w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-sm px-1 py-1.5 text-left',
                    'transition-colors duration-150 ease-sr hover:bg-hover',
                    'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                  )}
                >
                  <ChevronRight
                    size={14}
                    strokeWidth={1.75}
                    aria-hidden
                    className={cn(
                      'shrink-0 text-content-muted transition-transform duration-150 ease-sr',
                      open && 'rotate-90',
                    )}
                  />
                  <span className="min-w-[92px] text-12 text-content" title={item.at}>
                    {formatShortTime(item.at)}
                    <span className="ml-1 text-content-muted">{formatRelative(item.at)}</span>
                  </span>
                  <Badge tone="outline" mono>
                    {item.protocol}
                  </Badge>
                  <span className="text-12 text-content-muted">
                    {MODE_LABELS[item.mode] ?? item.mode}
                  </span>
                  <Badge tone={item.ok ? 'ok' : 'danger'} dot>
                    {item.ok ? '通过' : '失败'}
                  </Badge>
                  <span className="text-12 text-content-muted">
                    HTTP {item.http_status ?? '—'} · {formatDuration(item.latency_ms)}
                  </span>
                  {item.error_code ? (
                    <Badge tone="danger" mono>
                      {item.error_code}
                    </Badge>
                  ) : null}
                  <span className="ml-auto break-all text-12 text-content-muted">
                    <code className="sr-mono">{item.id}</code>
                  </span>
                </button>
                {open ? (
                  <div className="px-1 pb-2 pl-6">
                    {item.base_url ? (
                      <p className="mb-1 break-all text-12 text-content-muted">
                        地址 <code className="sr-mono text-content">{item.base_url}</code>
                      </p>
                    ) : null}
                    <StageRows item={item} />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
