'use client';

import { CheckCircle2, Circle, FlaskConical, ListChecks, PlugZap, XCircle } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Spinner } from '@/components/ui/spinner';
import { formatDuration, formatRelative } from '@/lib/format';
import type { LlmProbeMode, LlmProbeResponse, LlmStageResult } from '@/lib/types';

/**
 * 实时验证（spec §3.8 ③）：三个按钮 + 阶段时间线 + 失败解释。
 *
 * 三个按钮与 `mode` 的对应关系是**契约**（spec §3.8 / §3.4 的 mode 表）：
 * - 「测试连接」            → `auth`（默认焦点、不计费）
 * - 「获取模型列表」        → `auth`（同一次探测，结果拿去填 ④）
 * - 「测试生成（计费 1 次）」→ `both`（**计费**，文案里必须出现「计费」二字）
 *
 * 本组件只渲染、不发请求：`onRun` 由页面实现（页面要同时更新 ④ 的模型清单，
 * 并在配置变动时把上一次结论置空——「测试过但配置又改了」留着绿标就是撒谎）。
 * 无障碍：整页只允许页面自己那一个 `role="alert"`，这里一律用 `aria-live="polite"`。
 */

export type LlmTesterAction = 'test' | 'models' | 'completion';

/** 动作 → 请求 `mode`（契约见上） */
export const LLM_TESTER_ACTION_MODE: Record<LlmTesterAction, LlmProbeMode> = {
  test: 'auth',
  models: 'auth',
  completion: 'both',
};

/** 后端阶段名（`services/llm/probe.py`）→ 中文行标签；未知阶段原样显示 */
export const LLM_STAGE_LABELS: Record<string, string> = {
  auth: '鉴权与模型清单',
  completion: '生成请求',
};

/** 阶段展示顺序：先鉴权后生成（这是探测引擎的执行顺序，不是协议差异） */
const STAGE_ORDER: readonly string[] = ['auth', 'completion'];

/** 传输层错误（`ApiError` 经 `describeError()` 之后的人话形态） */
export interface LlmProbeTransportError {
  code: string;
  message: string;
  hint?: string | null;
  requestId?: string | null;
}

export interface ConnectionTesterProps {
  /** 最近一次探测结果；配置一改动页面应传 null 让结论作废 */
  result: LlmProbeResponse | null;
  /** 请求没发出去 / 被平台拒绝（400/404/409/429/5xx），与「上游返回 ok=false」分开渲染 */
  error?: LlmProbeTransportError | null;
  /** 正在进行的动作；null 表示空闲 */
  runningAction?: LlmTesterAction | null;
  onRun: (action: LlmTesterAction, mode: LlmProbeMode) => void;
  /** 必填项是否齐全（缺项时三个按钮都禁用） */
  canTest?: boolean;
  /** 缺什么（中文人话），显示在按钮下方 */
  blockedReason?: string | null;
  disabled?: boolean;
  /** 主按钮默认焦点：页面首次进入时传 true */
  autoFocusPrimary?: boolean;
  className?: string;
}

function stageOf(result: LlmProbeResponse, name: string): LlmStageResult | null {
  return result.stages.find((stage) => stage.stage === name) ?? null;
}

function StageIcon({ state }: { state: 'ok' | 'failed' | 'skipped' | 'running' }) {
  if (state === 'ok') {
    return <CheckCircle2 size={16} strokeWidth={1.75} aria-hidden className="shrink-0 text-ok" />;
  }
  if (state === 'failed') {
    return <XCircle size={16} strokeWidth={1.75} aria-hidden className="shrink-0 text-danger" />;
  }
  if (state === 'running') {
    return <Spinner size={14} />;
  }
  return <Circle size={16} strokeWidth={1.75} aria-hidden className="shrink-0 text-content-muted" />;
}

/** 「未执行」的原因必须写清楚，不能留空白（spec §3.8 阶段可见） */
function skippedReason(result: LlmProbeResponse, stage: string): string {
  const auth = stageOf(result, 'auth');
  if (stage === 'completion') {
    if (auth && !auth.ok) return '未执行（鉴权未通过）';
    if (result.mode === 'completion') return '未执行（本次只测生成，未拉清单）';
    if (result.mode === 'auth') return '未执行（点「测试生成（计费 1 次）」才会发计费请求）';
    return '未执行';
  }
  if (result.mode === 'completion') return '未执行（本次只测生成）';
  return '未执行';
}

export function ConnectionTester({
  result,
  error,
  runningAction = null,
  onRun,
  canTest = true,
  blockedReason,
  disabled,
  autoFocusPrimary = false,
  className,
}: ConnectionTesterProps) {
  const busy = runningAction !== null;
  const blocked = disabled || !canTest;

  const stageNames = React.useMemo(() => {
    const extra = (result?.stages ?? [])
      .map((stage) => stage.stage)
      .filter((stage) => !STAGE_ORDER.includes(stage));
    return [...STAGE_ORDER, ...extra];
  }, [result]);

  return (
    <section className={cn('flex flex-col gap-3', className)} aria-label="实时验证">
      <div className="flex flex-col gap-1">
        <h2 className="text-16 font-semibold text-content">实时验证</h2>
        <p className="text-12 text-content-muted">
          测试用的是当前表单值；保存后才对 Agent 生效。鉴权阶段只拉模型清单（不计费），
          「测试生成」才会真的发一次计费请求。
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          autoFocus={autoFocusPrimary}
          disabled={blocked || busy}
          loading={runningAction === 'test'}
          onClick={() => onRun('test', LLM_TESTER_ACTION_MODE.test)}
          icon={<PlugZap size={16} strokeWidth={1.75} />}
        >
          {runningAction === 'test' ? '测试中…' : '测试连接'}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={blocked || busy}
          loading={runningAction === 'models'}
          onClick={() => onRun('models', LLM_TESTER_ACTION_MODE.models)}
          icon={<ListChecks size={16} strokeWidth={1.75} />}
        >
          {runningAction === 'models' ? '获取中…' : '获取模型列表'}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={blocked || busy}
          loading={runningAction === 'completion'}
          onClick={() => onRun('completion', LLM_TESTER_ACTION_MODE.completion)}
          icon={<FlaskConical size={16} strokeWidth={1.75} />}
        >
          {runningAction === 'completion' ? '生成请求中…' : '测试生成（计费 1 次）'}
        </Button>
        {busy ? (
          <span className="flex items-center gap-1.5 text-12 text-content-muted" role="status">
            <Spinner size={12} />
            正在请求上游…
          </span>
        ) : null}
      </div>

      {blocked && blockedReason ? (
        <p className="text-12 text-content-muted">{blockedReason}</p>
      ) : null}

      {/* 阶段时间线：aria-live=polite，不用 role="alert"（整页只允许页面自己一个 alert） */}
      <div aria-live="polite" className="flex flex-col gap-2">
        {error ? (
          <div className="rounded-sm border border-danger bg-danger-weak p-2 text-12">
            <p className="text-13 font-medium text-content">
              请求没有完成
              <Badge tone="danger" mono className="ml-2 align-middle">
                {error.code}
              </Badge>
            </p>
            <p className="mt-1 text-content">{error.message}</p>
            {error.hint ? <p className="mt-0.5 text-content-muted">下一步：{error.hint}</p> : null}
            {error.requestId ? (
              <p className="mt-0.5 break-all text-content-muted">
                request_id: <code className="sr-mono">{error.requestId}</code>
              </p>
            ) : null}
          </div>
        ) : null}

        {result ? (
          <div className="rounded-sm border border-border bg-surface p-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={result.ok ? 'ok' : 'danger'} dot>
                {result.ok ? '测试通过' : '测试失败'}
              </Badge>
              <Badge tone="outline" mono>
                mode={result.mode}
              </Badge>
              {result.protocol ? (
                <Badge tone="outline" mono>
                  {result.wire && result.wire !== result.protocol
                    ? `${result.protocol} → ${result.wire}`
                    : result.protocol}
                </Badge>
              ) : null}
              <span className="text-12 text-content-muted">
                {result.checked_at ? formatRelative(result.checked_at) : '刚刚'}
              </span>
              {result.probe_id ? (
                <span className="break-all text-12 text-content-muted">
                  probe_id: <code className="sr-mono">{result.probe_id}</code>
                </span>
              ) : null}
            </div>
            {result.resolved_url ? (
              <p className="mt-1 break-all text-12 text-content-muted">
                实际请求地址：<code className="sr-mono text-content">{result.resolved_url}</code>
              </p>
            ) : null}

            <ul className="mt-2 flex flex-col">
              {stageNames.map((stageName) => {
                const stage = stageOf(result, stageName);
                const state = stage ? (stage.ok ? 'ok' : 'failed') : 'skipped';
                return (
                  <li
                    key={stageName}
                    className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border py-2 last:border-0"
                  >
                    <span className="flex min-w-[132px] items-center gap-1.5 text-13 text-content">
                      <StageIcon state={runningAction !== null && !stage ? 'running' : state} />
                      {LLM_STAGE_LABELS[stageName] ?? stageName}
                    </span>
                    {stage ? (
                      <>
                        <Badge tone={stage.ok ? 'ok' : 'danger'}>{stage.ok ? '通过' : '失败'}</Badge>
                        <span className="text-12 text-content-muted">
                          HTTP {stage.http_status ?? '—'} · {formatDuration(stage.latency_ms)}
                        </span>
                      </>
                    ) : (
                      <span className="text-12 text-content-muted">{skippedReason(result, stageName)}</span>
                    )}
                    {stage?.model_count !== undefined && stage.model_count !== null ? (
                      <span className="text-12 text-content-muted">
                        识别到 {stage.model_count} 个模型
                      </span>
                    ) : null}
                    {stage?.model ? (
                      <span className="break-all text-12 text-content-muted">
                        模型 <code className="sr-mono">{stage.model}</code>
                      </span>
                    ) : null}
                    {stage?.text_chars !== undefined && stage.text_chars !== null ? (
                      <span className="text-12 text-content-muted">回话 {stage.text_chars} 字符</span>
                    ) : null}
                    {stage?.detail ? (
                      <span className="basis-full break-words text-12 text-content-muted">
                        {stage.detail}
                      </span>
                    ) : null}
                  </li>
                );
              })}
            </ul>

            {!result.ok ? (
              <div className="mt-2 flex flex-col gap-1 border-t border-border pt-2">
                <p className="flex flex-wrap items-center gap-2 text-13 text-content">
                  失败原因
                  {result.error_code ? (
                    <Badge tone="danger" mono>
                      {result.error_code}
                    </Badge>
                  ) : null}
                </p>
                {result.hint ? (
                  <p className="text-12 text-content">
                    下一步：<span className="text-content-muted">{result.hint}</span>
                  </p>
                ) : null}
                {result.problems.length > 0 ? (
                  <ul className="flex flex-col gap-0.5 text-12 text-content-muted">
                    {result.problems.map((problem) => (
                      <li key={problem}>· {problem}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}

            {result.notes && result.notes.length > 0 ? (
              <ul className="mt-2 flex flex-col gap-0.5 border-t border-border pt-2 text-12 text-content-muted">
                {result.notes.map((note) => (
                  <li key={note}>· {note}</li>
                ))}
              </ul>
            ) : null}

            {result.inferred ? (
              <p className="mt-1 text-12 text-content-muted">
                协议族是根据提供商名推断的；建议在凭证里显式写入协议，避免推断漂移。
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-12 text-content-muted">
            {busy ? '还没有结果。' : '点「测试连接」用当前表单值打一次真实请求（不计费）。'}
          </p>
        )}
      </div>
    </section>
  );
}
