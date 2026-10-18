'use client';

import { Circle, CircleCheck, CircleDot, CircleSlash, Clock, TriangleAlert, XCircle } from 'lucide-react';
import * as React from 'react';
import { Badge, type BadgeTone } from '@/components/ui/badge';
import { Tooltip } from '@/components/ui/tooltip';

/**
 * 统一状态语义（spec §8.4 / §13.2）。
 *
 * 关键点：**已保存 / 测试通过 / 已发布 / 可调用是四个独立状态**，绝不合并成一个「成功」；
 * 「已停用」是第五个独立语义。状态不仅靠颜色，同时有文字与图标。
 */

export type StatusKind =
  | 'capability'
  | 'skill'
  | 'provider'
  | 'credential'
  | 'task'
  | 'call'
  | 'step'
  | 'engine'
  | 'agent'
  | 'generic';

interface Descriptor {
  label: string;
  tone: BadgeTone;
  icon?: React.ReactNode;
  hint?: string;
}

const ICON = { size: 12, strokeWidth: 1.75, 'aria-hidden': true } as const;

const capabilityMap: Record<string, Descriptor> = {
  draft: { label: '草稿', tone: 'neutral', icon: <CircleDot {...ICON} />, hint: '尚未发布，外部不可见' },
  // `POST /capabilities/{id}/validate` 通过后后端会把 draft 推进成 testing（capabilities.validate）；
  // 缺这一项时列表与详情会直接显示英文 `testing`，与「四态徽标」的约定不一致。
  testing: { label: '测试中', tone: 'info', icon: <CircleDot {...ICON} />, hint: '结构校验已通过，尚缺发布证据' },
  published: { label: '已发布', tone: 'ok', icon: <CircleCheck {...ICON} />, hint: '已有不可变版本，外部可见' },
  deprecated: { label: '已停用', tone: 'danger', icon: <CircleSlash {...ICON} />, hint: '不再参与路由，历史调用可查' },
  failing: { label: '连续失败', tone: 'warn', icon: <TriangleAlert {...ICON} />, hint: '最近测试未通过' },
};

const skillMap: Record<string, Descriptor> = {
  draft: { label: '草稿', tone: 'neutral', icon: <CircleDot {...ICON} /> },
  published: { label: '已发布', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  deprecated: { label: '已停用', tone: 'danger', icon: <CircleSlash {...ICON} /> },
  disabled: { label: '已停用', tone: 'danger', icon: <CircleSlash {...ICON} /> },
};

const providerMap: Record<string, Descriptor> = {
  active: { label: '启用', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  disabled: { label: '已停用', tone: 'danger', icon: <CircleSlash {...ICON} /> },
};

const credentialMap: Record<string, Descriptor> = {
  active: { label: '启用', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  disabled: { label: '已停用', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  expired: { label: '已过期', tone: 'warn', icon: <Clock {...ICON} />, hint: '需要替换或轮换凭证' },
};

const taskMap: Record<string, Descriptor> = {
  queued: { label: '排队中', tone: 'neutral', icon: <Clock {...ICON} /> },
  running: { label: '进行中', tone: 'info', icon: <CircleDot {...ICON} /> },
  succeeded: { label: '成功', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  failed: { label: '失败', tone: 'danger', icon: <XCircle {...ICON} /> },
  canceling: { label: '取消中', tone: 'warn', icon: <Clock {...ICON} /> },
  canceled: { label: '已取消', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  status_unknown: {
    label: '状态未知',
    tone: 'warn',
    icon: <TriangleAlert {...ICON} />,
    hint: '可能仍在远端执行，建议对账',
  },
};

const callMap: Record<string, Descriptor> = {
  success: { label: '成功', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  failed: { label: '失败', tone: 'danger', icon: <XCircle {...ICON} /> },
  canceled: { label: '已取消', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  status_unknown: { label: '状态未知', tone: 'warn', icon: <TriangleAlert {...ICON} /> },
};

const stepMap: Record<string, Descriptor> = {
  pending: { label: '待执行', tone: 'neutral', icon: <Circle {...ICON} /> },
  running: { label: '进行中', tone: 'info', icon: <CircleDot {...ICON} /> },
  passed: { label: '通过', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  failed: { label: '失败', tone: 'danger', icon: <XCircle {...ICON} /> },
  skipped: { label: '已跳过', tone: 'outline', icon: <CircleSlash {...ICON} /> },
};

const engineMap: Record<string, Descriptor> = {
  available: { label: '已可用', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  degraded: { label: '降级', tone: 'warn', icon: <TriangleAlert {...ICON} /> },
  unavailable: { label: '不可用', tone: 'danger', icon: <XCircle {...ICON} /> },
  needs_adapter: { label: '待适配', tone: 'warn', icon: <Clock {...ICON} /> },
  permission_limited: { label: '受权限限制', tone: 'info', icon: <CircleSlash {...ICON} /> },
  unsupported: { label: '不支持', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  pending: { label: '待验证', tone: 'neutral', icon: <Clock {...ICON} /> },
};

const agentMap: Record<string, Descriptor> = {
  active: { label: '启用', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  archived: { label: '已归档', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  running: { label: '运行中', tone: 'info', icon: <CircleDot {...ICON} /> },
  waiting_approval: { label: '待审批', tone: 'warn', icon: <Clock {...ICON} /> },
  stopped: { label: '已停止', tone: 'neutral', icon: <CircleSlash {...ICON} /> },
  approved: { label: '已批准', tone: 'ok', icon: <CircleCheck {...ICON} /> },
  rejected: { label: '已拒绝', tone: 'danger', icon: <XCircle {...ICON} /> },
  expired: { label: '已过期', tone: 'neutral', icon: <Clock {...ICON} /> },
  pending_review: { label: '待复核', tone: 'warn', icon: <Clock {...ICON} /> },
};

const genericMap: Record<string, Descriptor> = {
  ok: { label: '正常', tone: 'ok' },
  warn: { label: '警告', tone: 'warn' },
  error: { label: '错误', tone: 'danger' },
  info: { label: '提示', tone: 'info' },
};

function resolve(kind: StatusKind, status: string): Descriptor {
  const table =
    kind === 'capability'
      ? capabilityMap
      : kind === 'skill'
        ? skillMap
        : kind === 'provider'
          ? providerMap
          : kind === 'credential'
            ? credentialMap
            : kind === 'task'
              ? taskMap
              : kind === 'call'
                ? callMap
                : kind === 'step'
                  ? stepMap
                  : kind === 'engine'
                    ? engineMap
                    : kind === 'agent'
                      ? agentMap
                      : genericMap;
  return table[status] ?? { label: status, tone: 'neutral' };
}

export interface StatusBadgeProps {
  kind: StatusKind;
  status: string | null | undefined;
  /** 显示状态点（颜色 + 文字双编码） */
  dot?: boolean;
  className?: string;
}

export function StatusBadge({ kind, status, dot = true, className }: StatusBadgeProps) {
  if (!status) return <span className="text-13 text-content-muted">—</span>;
  const descriptor = resolve(kind, status);
  const badge = (
    <Badge tone={descriptor.tone} dot={dot && !descriptor.icon} className={className}>
      {descriptor.icon}
      {descriptor.label}
    </Badge>
  );
  if (!descriptor.hint) return badge;
  return (
    <Tooltip content={descriptor.hint}>
      <span tabIndex={0} className="inline-flex rounded-sm">
        {badge}
      </span>
    </Tooltip>
  );
}

/* ── 能力发布流水线的四个独立状态 ─────────────────────── */

export interface PublishStateProps {
  saved: boolean;
  testedOk: boolean;
  published: boolean;
  callable: boolean;
  enabled?: boolean;
  className?: string;
}

/**
 * 「已保存」「测试通过」「已发布」「可调用」分别展示，不合并。
 * 同时给出「已停用」的独立提示。
 */
export function PublishStateBadges({
  saved,
  testedOk,
  published,
  callable,
  enabled = true,
  className,
}: PublishStateProps) {
  return (
    <div className={className} aria-label="能力状态">
      <ul className="flex flex-wrap items-center gap-2">
        <li>
          <Badge tone={saved ? 'outline' : 'neutral'} dot>
            {saved ? '已保存' : '未保存'}
          </Badge>
        </li>
        <li>
          <Badge tone={testedOk ? 'info' : 'neutral'} dot>
            {testedOk ? '测试通过' : '未测试'}
          </Badge>
        </li>
        <li>
          <Badge tone={published ? 'ok' : 'neutral'} dot>
            {published ? '已发布' : '未发布'}
          </Badge>
        </li>
        <li>
          <Badge tone={callable ? 'accent' : 'neutral'} dot>
            {callable ? '可调用' : '不可调用'}
          </Badge>
        </li>
        {!enabled ? (
          <li>
            <Badge tone="danger" dot>
              已停用
            </Badge>
          </li>
        ) : null}
      </ul>
    </div>
  );
}
