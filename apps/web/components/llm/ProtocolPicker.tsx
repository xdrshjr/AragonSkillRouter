'use client';

import { Check, ExternalLink } from 'lucide-react';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Skeleton } from '@/components/ui/skeleton';
import type { LlmProtocolSpec } from '@/lib/types';

/**
 * 协议选择器（spec §3.8 ①）：2×2 卡片式 radio group。
 *
 * INV-1：卡片上的**每一个字**都来自 `GET /api/admin/llm/protocols`
 * （`label_zh` / `one_liner` / `default_base_url` / `base_url_hint` / `models_path` /
 *  `chat_path` / `paths_inherit_from_wire`）。本组件不认识 openai / anthropic / google，
 * 只认识「后端给了什么」。唯一的本地常量是**排序兜底**（见 `FALLBACK_PROTOCOL_ORDER`）。
 *
 * 键盘：roving tabindex + ↑↓←→ / Home / End，选中即跟随（ARIA radiogroup 标准行为）。
 */

/**
 * 排序兜底：**只**用于「后端列表顺序不稳定」与「列表为空时的占位卡片数量」。
 * 它不含任何协议差异（不带地址、不带路径、不带认证方式）——那些只能来自后端。
 */
const FALLBACK_PROTOCOL_ORDER: readonly string[] = ['openai', 'anthropic', 'google', 'custom'];

export interface ProtocolPickerProps {
  /** `GET /api/admin/llm/protocols` 的 `items` */
  protocols: LlmProtocolSpec[];
  /** 当前选中的协议 key；空串表示未选 */
  value: string;
  onChange: (protocolKey: string) => void;
  loading?: boolean;
  /** 协议列表拉取失败（此时只显示重试入口，不猜任何协议元数据） */
  error?: { code: string; message: string; hint?: string | null } | null;
  onRetry?: () => void;
  disabled?: boolean;
  /** radiogroup 的无障碍名字 */
  legend?: string;
  className?: string;
}

/** 卡片上的一行小字（路径 / 地址），保持等宽以免中文与 URL 混排跳动 */
function metaLine(label: string, value: string): React.ReactNode {
  return (
    <span className="flex min-w-0 items-baseline gap-1.5">
      <span className="shrink-0 text-content-muted">{label}</span>
      <span className="truncate font-mono text-12 text-content">{value}</span>
    </span>
  );
}

export function ProtocolPicker({
  protocols,
  value,
  onChange,
  loading,
  error,
  onRetry,
  disabled,
  legend = '协议',
  className,
}: ProtocolPickerProps) {
  const groupId = React.useId();
  const refs = React.useRef<(HTMLButtonElement | null)[]>([]);

  // 后端顺序即真相，但用兜底顺序做一层稳定排序：未知 key 排在已知 key 之后，保持后端相对顺序
  const ordered = React.useMemo(() => {
    const withIndex = protocols.map((spec, index) => ({ spec, index }));
    withIndex.sort((a, b) => {
      const ai = FALLBACK_PROTOCOL_ORDER.indexOf(a.spec.key);
      const bi = FALLBACK_PROTOCOL_ORDER.indexOf(b.spec.key);
      if (ai === -1 && bi === -1) return a.index - b.index;
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
    return withIndex.map((item) => item.spec);
  }, [protocols]);

  const activeIndex = ordered.findIndex((spec) => spec.key === value);
  const focusableIndex = activeIndex >= 0 ? activeIndex : 0;
  const selected = ordered.find((spec) => spec.key === value) ?? null;

  function moveFocus(from: number, delta: number): void {
    const count = ordered.length;
    if (count === 0) return;
    const next = (from + delta + count) % count;
    const target = ordered[next];
    if (!target) return;
    onChange(target.key);
    refs.current[next]?.focus();
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number): void {
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        event.preventDefault();
        moveFocus(index, 1);
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        event.preventDefault();
        moveFocus(index, -1);
        break;
      case 'Home':
        event.preventDefault();
        moveFocus(0, 0);
        break;
      case 'End':
        event.preventDefault();
        moveFocus(ordered.length - 1, 0);
        break;
      default:
        break;
    }
  }

  const placeholderCount = FALLBACK_PROTOCOL_ORDER.length;

  if (ordered.length === 0) {
    return (
      <div className={cn('flex flex-col gap-2', className)}>
        <p className="text-13 font-medium text-content">{legend}</p>
        <div
          className="grid grid-cols-1 gap-2 md:grid-cols-2"
          aria-busy={loading || undefined}
          aria-label={loading ? '协议列表加载中' : '协议列表不可用'}
        >
          {Array.from({ length: placeholderCount }).map((_, index) => (
            <Skeleton key={`protocol-placeholder-${index}`} height={104} rounded="md" />
          ))}
        </div>
        {loading ? (
          <p className="text-12 text-content-muted" role="status">
            正在读取协议注册表…
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-12 text-content-muted">
              协议列表暂时取不到{error?.message ? `：${error.message}` : ''}。
              不显示任何猜测的协议——先修好接口再配置，避免「看起来能用」的假成功。
            </p>
            {onRetry ? (
              <Button variant="secondary" size="sm" onClick={onRetry}>
                重新读取
              </Button>
            ) : null}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <p className="text-13 font-medium text-content" id={`${groupId}-label`}>
        {legend}
      </p>
      <div
        role="radiogroup"
        aria-labelledby={`${groupId}-label`}
        aria-disabled={disabled || undefined}
        className="grid grid-cols-1 gap-2 md:grid-cols-2"
      >
        {ordered.map((spec, index) => {
          const checked = spec.key === value;
          const needsBaseUrl = spec.default_base_url.trim() === '';
          const addressText = needsBaseUrl ? spec.base_url_hint : spec.default_base_url;
          const inheritPaths = spec.paths_inherit_from_wire === true;
          return (
            <button
              key={spec.key}
              ref={(node) => {
                refs.current[index] = node;
              }}
              type="button"
              role="radio"
              aria-checked={checked}
              aria-describedby={`${groupId}-${spec.key}-desc`}
              tabIndex={index === focusableIndex ? 0 : -1}
              disabled={disabled}
              onClick={() => onChange(spec.key)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={cn(
                'flex min-h-[104px] w-full flex-col items-start gap-1.5 rounded-md border p-3 text-left',
                'transition-colors duration-150 ease-sr',
                'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                'disabled:cursor-not-allowed disabled:opacity-55',
                checked
                  ? 'border-accent bg-accent-weak'
                  : 'border-border bg-surface hover:border-border-strong hover:bg-hover',
              )}
            >
              <span className="flex w-full items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate text-14 font-medium text-content">{spec.label_zh}</span>
                  <Badge tone="outline" mono>
                    {spec.key}
                  </Badge>
                </span>
                {checked ? (
                  <Check size={16} strokeWidth={2} aria-hidden className="shrink-0 text-accent" />
                ) : null}
              </span>
              <span className="text-12 text-content-muted">{spec.one_liner}</span>
              <span className="flex w-full flex-col gap-0.5">
                {metaLine(needsBaseUrl ? '地址示例' : '默认地址', addressText)}
                {inheritPaths ? (
                  metaLine('路径', '继承所选线协议')
                ) : (
                  <>
                    {spec.models_path ? metaLine('取模型', spec.models_path) : null}
                    {spec.chat_path ? metaLine('对话', spec.chat_path) : null}
                  </>
                )}
              </span>
              <span id={`${groupId}-${spec.key}-desc`} className="sr-only">
                {checked ? '当前已选中。' : ''}
                {needsBaseUrl
                  ? `该协议没有默认地址，必须填写地址；示例 ${spec.base_url_hint}。`
                  : `默认地址 ${spec.default_base_url}。`}
                {inheritPaths
                  ? '取模型与对话路径继承所选线协议，不是没有模型清单。'
                  : '取模型路径与对话路径见左。'}
                {spec.supports_models_list ? '支持一键获取模型清单。' : '不支持自动获取模型清单。'}
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-12 text-content-muted">
        {selected?.supports_models_list === false ? (
          <Badge tone="warn">该协议不提供模型清单，模型名需手填</Badge>
        ) : null}
        {selected && selected.default_base_url.trim() === '' ? (
          <Badge tone="warn">地址必填</Badge>
        ) : null}
        {selected?.docs_url ? (
          <a
            href={selected.docs_url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-accent hover:underline focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          >
            {selected.label_zh} 文档
            <ExternalLink size={12} strokeWidth={1.75} aria-hidden />
          </a>
        ) : null}
      </div>
    </div>
  );
}
