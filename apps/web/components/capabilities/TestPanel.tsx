'use client';

import { FlaskConical, History, Play } from 'lucide-react';
import * as React from 'react';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Select } from '@/components/ui/select';
import { formatDateTime, formatDuration, prettyJson } from '@/lib/format';
import type { CallMode, CapabilityRoute, CapabilityTestResult, CapabilityTestRun, JsonObject } from '@/lib/types';

/**
 * 测试面板（spec §8.4 / §9.2 `POST /capabilities/{id}/test`）：
 * 请求预览 → 发送真实调用 → 响应（脱敏）/ HTTP 状态 / 耗时 / 实际使用的 route。
 * 明确区分「测试通过」与「已发布」；测试记录只读回放。
 */

export interface TestPanelProps {
  capabilityId: string;
  modes: CallMode[];
  routes: CapabilityRoute[];
  examples: { title?: string; input: JsonObject }[];
  /** 由页面提供的测试动作（走 lib/api.ts） */
  onRun: (payload: {
    input: JsonObject;
    route_id?: string;
    mode?: CallMode;
    save_payload: boolean;
  }) => Promise<CapabilityTestResult>;
  history?: CapabilityTestRun[];
  onHistoryRefresh?: () => void;
  disabled?: boolean;
  className?: string;
}

export function TestPanel({
  capabilityId,
  modes,
  routes,
  examples,
  onRun,
  history,
  disabled,
  className,
}: TestPanelProps) {
  const firstExample = examples[0];
  const [inputText, setInputText] = React.useState<string>(() =>
    JSON.stringify(firstExample?.input ?? {}, null, 2),
  );
  const [routeId, setRouteId] = React.useState<string>('');
  const [mode, setMode] = React.useState<CallMode>(modes[0] ?? 'sync');
  const [running, setRunning] = React.useState(false);
  const [result, setResult] = React.useState<CapabilityTestResult | null>(null);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [networkError, setNetworkError] = React.useState<{ message: string; requestId: string | null } | null>(null);

  const run = async (): Promise<void> => {
    setParseError(null);
    setNetworkError(null);
    let parsed: JsonObject;
    try {
      const value = JSON.parse(inputText) as unknown;
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        setParseError('输入必须是 JSON 对象');
        return;
      }
      parsed = value as JsonObject;
    } catch (cause) {
      setParseError(cause instanceof Error ? cause.message : 'JSON 解析失败');
      return;
    }
    setRunning(true);
    try {
      const response = await onRun({
        input: parsed,
        route_id: routeId || undefined,
        mode,
        save_payload: false,
      });
      setResult(response);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '测试调用失败';
      const requestId =
        typeof cause === 'object' && cause !== null && 'requestId' in cause
          ? String((cause as { requestId?: string | null }).requestId ?? '')
          : null;
      setNetworkError({ message, requestId: requestId || null });
    } finally {
      setRunning(false);
    }
  };

  const routeOptions = [
    { value: '', label: '按路由规则自动选择（推荐）' },
    ...routes.map((route) => ({
      value: route.id,
      label: `${route.route_key || route.id} · 优先级 ${route.priority}${route.enabled ? '' : '（已停用）'}`,
      disabled: !route.enabled,
    })),
  ];

  return (
    <section className={cn('flex flex-col gap-3', className)} aria-label="测试面板">
      <header className="flex items-center justify-between gap-2">
        <div>
          <h3 className="flex items-center gap-1.5 text-13 font-medium text-content">
            <FlaskConical size={16} strokeWidth={1.75} aria-hidden />
            真实调用测试
          </h3>
          <p className="text-12 text-content-muted">
            测试会真实打到上游并计入用量与审计；结果只保留脱敏预览。
          </p>
        </div>
        {result ? (
          <Badge tone={result.ok ? 'ok' : 'danger'} dot>
            {result.ok ? '测试通过' : '测试未通过'}
          </Badge>
        ) : null}
      </header>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">模式</span>
          <Select
            size="sm"
            aria-label="调用模式"
            value={mode}
            onValueChange={(value) => setMode(value as CallMode)}
            options={modes.map((item) => ({ value: item, label: item }))}
            disabled={disabled || modes.length === 0}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-12 text-content-muted">指定 route</span>
          <Select
            size="sm"
            aria-label="指定 route"
            value={routeId}
            onValueChange={setRouteId}
            options={routeOptions}
            disabled={disabled}
          />
        </label>
      </div>

      {examples.length > 1 ? (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-12 text-content-muted">示例：</span>
          {examples.map((example, index) => (
            <button
              key={`${example.title ?? 'example'}-${index}`}
              type="button"
              onClick={() => setInputText(JSON.stringify(example.input, null, 2))}
              className="rounded-sm border border-border px-1.5 py-0.5 text-12 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            >
              {example.title ?? `示例 ${index + 1}`}
            </button>
          ))}
        </div>
      ) : null}

      <label className="flex flex-col gap-1">
        <span className="text-12 text-content-muted">请求输入（JSON，不含密钥；密钥由凭证注入）</span>
        <textarea
          value={inputText}
          onChange={(event) => setInputText(event.target.value)}
          rows={8}
          spellCheck={false}
          aria-label="测试输入 JSON"
          aria-invalid={parseError ? true : undefined}
          className={cn(
            'w-full rounded border bg-surface px-3 py-2 text-12 sr-mono text-content',
            'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
            parseError ? 'border-danger' : 'border-border',
          )}
        />
        {parseError ? <span className="text-12 text-danger">{parseError}</span> : null}
      </label>

      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={running}
          onClick={() => void run()}
          disabled={disabled || modes.length === 0}
          icon={<Play size={14} strokeWidth={1.75} />}
        >
          发送测试
        </Button>
        {modes.length === 0 ? <span className="text-12 text-warn">能力未声明任何模式，无法测试</span> : null}
        <span className="sr-mono ml-auto text-12 text-content-muted">{capabilityId}</span>
      </div>

      {networkError ? (
        <div role="alert" className="rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2">
          <p className="text-13 text-content">{networkError.message}</p>
          {networkError.requestId ? <RequestIdCopy requestId={networkError.requestId} className="mt-1" /> : null}
        </div>
      ) : null}

      {result ? (
        <div className="flex flex-col gap-2 rounded border border-border bg-surface px-3 py-2">
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-12 sm:grid-cols-4">
            <div>
              <dt className="text-content-muted">状态</dt>
              <dd className="text-content">{result.status}</dd>
            </div>
            <div>
              <dt className="text-content-muted">HTTP</dt>
              <dd className="text-content">{result.http_status ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-content-muted">耗时</dt>
              <dd className="text-content">{formatDuration(result.latency_ms)}</dd>
            </div>
            <div>
              <dt className="text-content-muted">实际 route</dt>
              <dd className="truncate text-content">
                {result.route?.provider_name ?? result.route?.id ?? '—'}
              </dd>
            </div>
          </dl>
          {result.error ? (
            <div className="rounded border border-danger bg-[var(--sr-danger-weak)] px-2 py-1.5">
              <p className="text-12 text-content">
                <code className="sr-mono">{result.error.code}</code> {result.error.message}
              </p>
              {result.error.hint ? <p className="text-12 text-content-muted">{result.error.hint}</p> : null}
              {result.error.request_id ? (
                <RequestIdCopy requestId={result.error.request_id} className="mt-1" />
              ) : null}
            </div>
          ) : null}
          <div>
            <h4 className="text-12 font-medium text-content-muted">响应预览（脱敏）</h4>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
              {result.response_preview ?? '（无响应体）'}
            </pre>
          </div>
          <p className="text-12 text-content-muted">
            测试通过 ≠ 已发布：发布还需版本号、变更说明与 24h 内的测试证据。
          </p>
        </div>
      ) : null}

      {history && history.length > 0 ? (
        <section aria-label="最近测试记录" className="flex flex-col gap-1">
          <h4 className="flex items-center gap-1.5 text-12 font-medium text-content-muted">
            <History size={14} strokeWidth={1.75} aria-hidden />
            最近测试
          </h4>
          <ul className="divide-y divide-[var(--sr-border)] rounded border border-border">
            {history.slice(0, 5).map((run) => (
              <li key={run.id} className="flex flex-wrap items-center gap-2 px-2 py-1.5 text-12">
                <Badge tone={run.ok ? 'ok' : 'danger'} dot>
                  {run.ok ? '通过' : '失败'}
                </Badge>
                <span className="text-content-muted">{formatDateTime(run.at)}</span>
                <span className="text-content-muted">{formatDuration(run.latency_ms)}</span>
                <span className="text-content-muted">{run.mode}</span>
                {run.error_code ? <code className="sr-mono text-danger">{run.error_code}</code> : null}
                {run.response_preview_redacted ? (
                  <details className="ml-auto">
                    <summary className="cursor-pointer text-content-muted">响应</summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 sr-mono sr-scroll">
                      {prettyJson(run.response_preview_redacted, 4000)}
                    </pre>
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
