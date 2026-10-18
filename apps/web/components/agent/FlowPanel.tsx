'use client';

import { ChevronDown, CircleAlert, Play, ShieldCheck, ThumbsDown, ThumbsUp } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { StatusBadge } from '@/components/common/StatusBadge';
import { formatDateTime, formatRelative, prettyJson } from '@/lib/format';
import type { AgentApproval, AgentFlow, AgentFlowStep, FlowStep } from '@/lib/types';

/**
 * 接入流程七步（spec §7.12 / §13.2 `/agent`）：
 * parse → draft → validate → test → sample → check → publish
 * - 每步可展开证据（测试记录/文档/工具调用）
 * - 失败可从该步继续（复用已通过步骤的证据）
 * - 审批卡片显示具体影响并支持批准/拒绝
 */

export const FLOW_STEPS: { step: FlowStep; label: string; description: string }[] = [
  { step: 'parse', label: '解析', description: '读取接口文档/凭证，抽取端点与参数' },
  { step: 'draft', label: '生成草稿', description: '产出能力定义与路由（含参数映射）' },
  { step: 'validate', label: '校验', description: '结构与安全校验，输出 issues' },
  { step: 'test', label: '测试', description: '真实调用一次，记录耗时与响应摘要' },
  { step: 'sample', label: '样例', description: '补齐可直接复用的调用样例' },
  { step: 'check', label: '检查', description: '错误映射、限制、证据时效检查' },
  { step: 'publish', label: '发布', description: '冻结版本并把证据写入审计' },
];

function findStep(steps: AgentFlowStep[], step: FlowStep): AgentFlowStep | null {
  return steps.find((item) => item.step === step) ?? null;
}

function EvidenceBlock({ step }: { step: AgentFlowStep }) {
  const [open, setOpen] = React.useState(false);
  const evidenceText = prettyJson(step.evidence);
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 rounded-sm text-12 text-content-muted hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
      >
        {open ? '收起证据' : '展开证据'}
        <ChevronDown
          size={12}
          strokeWidth={1.75}
          aria-hidden
          className={cn('transition-transform duration-150 ease-sr', open && 'rotate-180')}
        />
      </button>
      {open ? (
        <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
          {evidenceText || '（该步骤尚未产生证据）'}
        </pre>
      ) : null}
    </div>
  );
}

export interface FlowPanelProps {
  flow: AgentFlow | null;
  approvals: AgentApproval[];
  onRunStep: (flowId: string, step: string) => Promise<void>;
  onPublish: (flowId: string) => Promise<void>;
  onDecide: (id: string, approve: boolean) => Promise<void>;
  /** 面板内紧凑模式（浮窗） */
  compact?: boolean;
  className?: string;
}

export function FlowPanel({
  flow,
  approvals,
  onRunStep,
  onPublish,
  onDecide,
  compact,
  className,
}: FlowPanelProps) {
  const [busyStep, setBusyStep] = React.useState<string | null>(null);
  const [busyApproval, setBusyApproval] = React.useState<string | null>(null);

  if (!flow) {
    return (
      <div className={cn('rounded-sm border border-dashed border-border p-3 text-13 text-content-muted', className)}>
        还没有接入流程。在对话里说「把这个接口接成能力」，Agent 会创建七步流程并逐步留下证据。
      </div>
    );
  }

  const steps = flow.steps ?? [];
  const allPassed = FLOW_STEPS.every(({ step }) => findStep(steps, step)?.status === 'passed');
  const pendingApprovals = approvals.filter((approval) => approval.status === 'pending');

  const handleRun = async (step: FlowStep): Promise<void> => {
    setBusyStep(step);
    try {
      await onRunStep(flow.id, step);
    } finally {
      setBusyStep(null);
    }
  };

  const handleDecide = async (id: string, approve: boolean): Promise<void> => {
    setBusyApproval(id);
    try {
      await onDecide(id, approve);
    } finally {
      setBusyApproval(null);
    }
  };

  return (
    <section className={cn('flex flex-col gap-3', className)} aria-label="接入流程步骤">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-14 font-medium text-content">{flow.title}</h3>
          <p className="text-12 text-content-muted">
            当前步骤：{FLOW_STEPS.find((s) => s.step === flow.current_step)?.label ?? flow.current_step} · 更新于{' '}
            {formatRelative(flow.updated_at)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge kind="agent" status={flow.status} />
          {flow.auto_publish ? <Badge tone="info">自动发布</Badge> : null}
        </div>
      </header>

      <ol className="flex flex-col gap-1.5">
        {FLOW_STEPS.map((definition, index) => {
          const step = findStep(steps, definition.step);
          const status = step?.status ?? 'pending';
          const canResume = status === 'failed' || (status === 'pending' && !allPassed);
          return (
            <li key={definition.step} className="rounded-sm border border-border bg-surface px-2 py-2">
              <div className="flex items-start gap-2">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-border text-12 text-content-muted"
                >
                  {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-13 font-medium text-content">{definition.label}</span>
                    <StatusBadge kind="step" status={status} />
                    {step?.finished_at && step.started_at ? (
                      <span className="text-12 text-content-muted">
                        {formatDateTime(step.started_at)} 起
                      </span>
                    ) : null}
                  </div>
                  {!compact ? (
                    <p className="mt-0.5 text-12 text-content-muted">{definition.description}</p>
                  ) : null}
                  {step?.detail ? (
                    <p className="mt-1 break-words text-12 text-content-muted">{step.detail}</p>
                  ) : null}
                  {step?.error_code ? (
                    <p className="mt-1 flex items-center gap-1 text-12 text-danger">
                      <CircleAlert size={12} strokeWidth={1.75} aria-hidden />
                      {step.error_code}
                    </p>
                  ) : null}
                  {step ? <EvidenceBlock step={step} /> : null}
                </div>
                {canResume ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={busyStep === definition.step}
                    onClick={() => void handleRun(definition.step)}
                    icon={<Play size={14} strokeWidth={1.75} />}
                  >
                    {status === 'failed' ? '从此步继续' : '执行'}
                  </Button>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2">
        <Button
          variant="primary"
          size="sm"
          disabled={!allPassed || flow.status === 'published'}
          onClick={() => void onPublish(flow.id)}
          icon={<ShieldCheck size={16} strokeWidth={1.75} />}
        >
          汇总证据并发布
        </Button>
        {!allPassed ? (
          <p className="text-12 text-content-muted">全部步骤通过后才能发布（当前未全部通过）。</p>
        ) : null}
      </div>

      {/* 审批卡片 */}
      <section aria-label="待审批请求" className="flex flex-col gap-2">
        <h4 className="text-13 font-medium text-content">
          待审批（{pendingApprovals.length}）
        </h4>
        {pendingApprovals.length === 0 ? (
          <p className="text-12 text-content-muted">没有待审批请求。</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {pendingApprovals.map((approval) => (
              <li key={approval.id} className="rounded-sm border border-warn bg-[var(--sr-warn-weak)] px-2 py-2">
                <p className="text-13 font-medium text-content">{approval.action}</p>
                {approval.target ? (
                  <p className="sr-mono mt-0.5 break-all text-12 text-content-muted">{approval.target}</p>
                ) : null}
                <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-surface p-2 text-12 sr-mono sr-scroll">
                  {prettyJson(approval.impact)}
                </pre>
                <p className="mt-1 text-12 text-content-muted">
                  到期：{formatDateTime(approval.expires_at)}
                </p>
                <div className="mt-1.5 flex items-center gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    loading={busyApproval === approval.id}
                    onClick={() => void handleDecide(approval.id, true)}
                    icon={<ThumbsUp size={14} strokeWidth={1.75} />}
                  >
                    批准
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={busyApproval === approval.id}
                    onClick={() => void handleDecide(approval.id, false)}
                    icon={<ThumbsDown size={14} strokeWidth={1.75} />}
                  >
                    拒绝
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
