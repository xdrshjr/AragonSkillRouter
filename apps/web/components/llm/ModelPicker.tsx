'use client';

import { Check, ChevronDown, Search } from 'lucide-react';
import * as React from 'react';
import { FormField } from '@/components/common/FormField';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { formatRelative } from '@/lib/format';
import type { LlmModelEntry } from '@/lib/types';

/**
 * 模型选择器（spec §3.8 ④）：可搜索的 combobox + 手填回退。
 *
 * 契约（每条都能写成 Playwright 断言）：
 * - `role="combobox"` + `aria-expanded` + `aria-activedescendant`；
 * - ↑↓ 移动、Enter 选中、Esc 关闭；输入即过滤（`id` 与 `display_name` 子串，大小写不敏感）；
 * - 分组：`recommended` 一组、其余一组，推荐项带徽标与实心圆点；
 * - 展示 `context_window`（有值才展示，没有就不编）；
 * - 空结果显示「未匹配到模型 · 可直接手填 model id」；
 * - 手填回退：列表里给一行「未在目录中」，选中即把当前输入当作 model id。
 *
 * 组件不自己取数：清单来自页面（`POST /llm/models` 或 `GET /credentials/{id}/models`），
 * 这样「测试连接」与「选模型」用的是同一份数据，不会出现两个真相源。
 */

/** 清单来源（后端 `source`）→ 中文标签；未知值原样显示，不做猜测 */
const SOURCE_LABELS: Record<string, string> = {
  upstream: '实时拉取',
  cache: '缓存',
  'cache-stale': '缓存（降级）',
};

const SOURCE_TONES: Record<string, 'ok' | 'neutral' | 'warn'> = {
  upstream: 'ok',
  cache: 'neutral',
  'cache-stale': 'warn',
};

export interface ModelPickerProps {
  /** 归一化后的模型清单（后端已按 recommended → kind → id 排好） */
  models: LlmModelEntry[];
  /** 当前模型 id（可能不在清单里 —— 那就是「未在目录中」） */
  value: string;
  onChange: (modelId: string) => void;
  loading?: boolean;
  /** 清单来源：`upstream` / `cache` / `cache-stale` */
  source?: string | null;
  /** 降级时的说明（「上游暂时不可用，展示的是 12 分钟前的清单」） */
  warning?: string | null;
  fetchedAt?: string | null;
  /** 清单被 limit 截断 */
  truncated?: boolean;
  disabled?: boolean;
  /** 控件 id（交给 FormField 的 label htmlFor） */
  id?: string;
  label?: string;
  hint?: string;
  /** 字段级错误（保存前校验） */
  errorMessage?: string;
  className?: string;
}

interface ModelOption {
  /** 选中后写回的值 */
  id: string;
  /** null = 手填回退（「未在目录中」） */
  entry: LlmModelEntry | null;
}

function matches(entry: LlmModelEntry, needle: string): boolean {
  if (needle === '') return true;
  return (
    entry.id.toLowerCase().includes(needle) || (entry.display_name ?? '').toLowerCase().includes(needle)
  );
}

function contextText(entry: LlmModelEntry): string | null {
  if (typeof entry.context_window !== 'number' || entry.context_window <= 0) return null;
  return `${entry.context_window.toLocaleString('zh-CN')} ctx`;
}

export function ModelPicker({
  models,
  value,
  onChange,
  loading,
  source,
  warning,
  fetchedAt,
  truncated,
  disabled,
  id,
  label = '模型',
  hint = '搜索后点选；没有就手填 model id。',
  errorMessage,
  className,
}: ModelPickerProps) {
  const baseId = React.useId();
  const inputId = id ?? `${baseId}-input`;
  const listboxId = `${baseId}-listbox`;
  const helpId = `${baseId}-help`;

  /** null = 未在编辑（输入框显示已选值）；字符串 = 正在输入 / 过滤 */
  const [draft, setDraft] = React.useState<string | null>(null);
  /** 过滤词：打开列表时为空（先看全量），用户一输入才收窄 */
  const [query, setQuery] = React.useState('');
  const [activeIndex, setActiveIndex] = React.useState(0);
  const optionRefs = React.useRef<(HTMLDivElement | null)[]>([]);

  const open = draft !== null && !disabled;
  const needle = query.trim().toLowerCase();

  // 推荐 / 其余两段：分组顺序固定（推荐优先），组内保持后端顺序
  const groups = React.useMemo(() => {
    const matched = models.filter((entry) => matches(entry, needle));
    const recommended = matched.filter((entry) => entry.recommended);
    const rest = matched.filter((entry) => !entry.recommended);
    const result: { key: string; label: string; items: LlmModelEntry[] }[] = [];
    if (recommended.length > 0) result.push({ key: 'recommended', label: '推荐', items: recommended });
    if (rest.length > 0) result.push({ key: 'other', label: '其他模型', items: rest });
    return result;
  }, [models, needle]);

  // 手填回退只在「输入非空且与清单里的 id 不完全一致」时出现
  const manualId =
    draft !== null && needle !== '' && !models.some((entry) => entry.id === query.trim())
      ? query.trim()
      : null;

  const options = React.useMemo<ModelOption[]>(() => {
    const flat: ModelOption[] = groups.flatMap((group) =>
      group.items.map((entry) => ({ id: entry.id, entry })),
    );
    return manualId ? [{ id: manualId, entry: null }, ...flat] : flat;
  }, [groups, manualId]);

  const indexById = React.useMemo(() => {
    const map = new Map<string, number>();
    options.forEach((option, index) => {
      if (!map.has(option.id)) map.set(option.id, index);
    });
    return map;
  }, [options]);

  React.useEffect(() => {
    // 过滤条件变了，高亮回到第一项（否则可能指向一个已不存在的下标）
    setActiveIndex(0);
  }, [needle, open]);

  React.useEffect(() => {
    if (!open) return;
    optionRefs.current[activeIndex]?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open]);

  /**
   * 收起列表。
   *
   * `commit=true`（Enter / 失焦）：把输入框里的文字当成「手填的 model id」写回父级；
   * `commit=false`（Esc）：丢弃本次输入、回到原来的已选值 —— Esc 是取消，不是提交。
   * 之前每敲一个字符就直接写父级，于是「想搜一下别的模型」按 Esc 放弃后，
   * 已选模型会被半截搜索词静默替换掉（保存时才炸）。
   */
  function close(commit = false): void {
    const pending = draft;
    setDraft(null);
    setQuery('');
    if (!commit || pending === null) return;
    const next = pending.trim();
    if (next !== value) onChange(next);
  }

  function select(option: ModelOption): void {
    onChange(option.id);
    close();
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>): void {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (!open) {
        setDraft(value);
        setQuery('');
        setActiveIndex(0);
        return;
      }
      setActiveIndex((prev) => Math.min(prev + 1, Math.max(options.length - 1, 0)));
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (!open) {
        setDraft(value);
        setQuery('');
        return;
      }
      setActiveIndex((prev) => Math.max(prev - 1, 0));
      return;
    }
    if (event.key === 'Enter') {
      if (open) {
        const option = options[activeIndex];
        if (option) {
          event.preventDefault();
          select(option);
          return;
        }
      }
      // 没有可选项也接受当前输入（手填 model id）
      if (draft !== null) close(true);
      return;
    }
    if (event.key === 'Escape') {
      if (open) {
        event.preventDefault();
        close();
      }
      return;
    }
    if (event.key === 'Tab') {
      close(true);
    }
  }

  const selectedEntry = models.find((entry) => entry.id === value) ?? null;
  const outOfCatalog = value.trim() !== '' && selectedEntry === null;
  // 列表为空时不要指向一个不存在的 id（aria-activedescendant 必须指向真实节点）
  const activeOptionId = open && options.length > 0 ? `${baseId}-opt-${activeIndex}` : undefined;

  return (
    <FormField
      id={inputId}
      label={label}
      hint={hint}
      error={errorMessage}
      className={className}
      aside={
        source ? (
          <Badge tone={SOURCE_TONES[source] ?? 'neutral'}>{SOURCE_LABELS[source] ?? source}</Badge>
        ) : null
      }
    >
      <div
        className="relative"
        onBlur={(event) => {
          // 焦点离开整个控件才收起列表（选项本身不可聚焦，鼠标按下时已 preventDefault）
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close(true);
        }}
      >
        <Input
          id={inputId}
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={activeOptionId}
          aria-describedby={helpId}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          invalid={Boolean(errorMessage)}
          placeholder="搜索模型名，或直接粘贴 model id"
          leading={<Search size={14} strokeWidth={1.75} aria-hidden />}
          trailing={
            loading ? (
              <Spinner size={12} />
            ) : (
              <ChevronDown size={14} strokeWidth={1.75} aria-hidden />
            )
          }
          value={draft ?? value}
          onChange={(event) => {
            const next = event.target.value;
            // 只改本地草稿：值在「点选 / Enter / 失焦」时才提交，Esc 是取消
            setDraft(next);
            setQuery(next);
          }}
          onFocus={() => {
            if (disabled || draft !== null) return;
            // 打开时先看全量清单（过滤词为空），输入框仍显示当前已选值
            setDraft(value);
            setQuery('');
          }}
          onKeyDown={handleKeyDown}
        />

        {open ? (
          <div
            id={listboxId}
            role="listbox"
            aria-label="可选模型"
            className="sr-scroll absolute left-0 right-0 top-[calc(100%+4px)] z-panel max-h-72 overflow-auto rounded border border-border bg-surface p-1"
          >
            {loading ? (
              <div className="flex flex-col gap-1 p-1" aria-hidden>
                <Skeleton height={28} />
                <Skeleton height={28} />
                <Skeleton height={28} />
              </div>
            ) : options.length === 0 ? (
              <p className="px-2 py-1.5 text-13 text-content-muted">
                未匹配到模型 · 可直接手填 model id
              </p>
            ) : (
              <>
                {manualId ? (
                  <div
                    ref={(node) => {
                      optionRefs.current[0] = node;
                    }}
                    id={`${baseId}-opt-0`}
                    role="option"
                    aria-selected={false}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      select({ id: manualId, entry: null });
                    }}
                    className={cn(
                      'flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-13',
                      activeIndex === 0 ? 'bg-hover text-content' : 'text-content',
                    )}
                  >
                    <Badge tone="warn">未在目录中</Badge>
                    <span className="break-all">直接使用「{manualId}」</span>
                  </div>
                ) : null}
                {groups.map((group) => (
                  <React.Fragment key={group.key}>
                    <p
                      role="presentation"
                      className="px-2 pt-1.5 pb-0.5 text-12 font-medium text-content-muted"
                    >
                      {group.label}
                    </p>
                    {group.items.map((entry) => {
                      const index = indexById.get(entry.id) ?? -1;
                      const active = index === activeIndex;
                      const selected = entry.id === value;
                      const ctx = contextText(entry);
                      return (
                        <div
                          key={entry.id}
                          ref={(node) => {
                            if (index >= 0) optionRefs.current[index] = node;
                          }}
                          id={index >= 0 ? `${baseId}-opt-${index}` : undefined}
                          role="option"
                          aria-selected={selected}
                          onMouseDown={(event) => {
                            event.preventDefault();
                            select({ id: entry.id, entry });
                          }}
                          onMouseEnter={() => {
                            if (index >= 0) setActiveIndex(index);
                          }}
                          className={cn(
                            'flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-13',
                            active ? 'bg-hover' : 'bg-transparent',
                          )}
                        >
                          <span
                            aria-hidden
                            className={cn(
                              'h-1.5 w-1.5 shrink-0 rounded-full',
                              entry.recommended ? 'bg-accent' : 'border border-border-strong',
                            )}
                          />
                          <span className="min-w-0 flex-1 truncate text-content">
                            {entry.display_name || entry.id}
                            {entry.display_name && entry.display_name !== entry.id ? (
                              <span className="ml-1.5 font-mono text-12 text-content-muted">{entry.id}</span>
                            ) : null}
                          </span>
                          {entry.recommended ? <Badge tone="accent">推荐</Badge> : null}
                          {entry.kind !== 'chat' ? <Badge tone="outline">{entry.kind}</Badge> : null}
                          {ctx ? <span className="shrink-0 text-12 text-content-muted">{ctx}</span> : null}
                          {selected ? <Check size={14} strokeWidth={2} aria-hidden className="shrink-0 text-accent" /> : null}
                        </div>
                      );
                    })}
                  </React.Fragment>
                ))}
              </>
            )}
          </div>
        ) : null}
      </div>

      <div id={helpId} className="flex flex-wrap items-center gap-2 text-12 text-content-muted">
        {value.trim() !== '' ? (
          selectedEntry ? (
            <span className="flex min-w-0 items-center gap-1.5">
              <Check size={12} strokeWidth={2} aria-hidden className="text-ok" />
              <code className="sr-mono break-all text-content">{value}</code>
              {contextText(selectedEntry) ? <span>· {contextText(selectedEntry)}</span> : null}
            </span>
          ) : (
            <>
              <Badge tone="warn">未在目录中</Badge>
              <code className="sr-mono break-all text-content">{value}</code>
              <span>· 会按手填值保存，发轮次时仍可能被上游拒绝</span>
            </>
          )
        ) : models.length === 0 ? (
          <span>还没有模型清单：点上方「获取模型列表」拉一次，或直接手填 model id。</span>
        ) : (
          <span>未在目录中也可直接手填 model id（会标注「未在目录中」）。</span>
        )}
        {outOfCatalog && models.length > 0 ? (
          <span>· 当前值不在清单里（可能是旧模型名）</span>
        ) : null}
        {truncated ? <Badge tone="warn">清单已截断</Badge> : null}
        {fetchedAt ? <span>· 清单时间 {formatRelative(fetchedAt)}</span> : null}
      </div>

      {warning ? (
        <p className="text-12 text-warn" aria-live="polite">
          {warning}
        </p>
      ) : null}
    </FormField>
  );
}
