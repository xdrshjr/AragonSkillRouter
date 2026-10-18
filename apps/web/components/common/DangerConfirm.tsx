'use client';

import { ChevronDown, TriangleAlert } from 'lucide-react';
import * as React from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { cn } from '@/components/ui/cn';

/**
 * 危险操作二次确认（spec §13.5）。
 * - 必须列出影响对象（可展开清单）
 * - 确认按钮文案包含**动作 + 对象数量**，例如「停用 3 个能力」
 * - 需要用户输入指定关键词（默认即动作文案）才解锁，避免误点
 */
export interface DangerConfirmProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** 动作名，用于按钮文案与确认词，如「删除」「停用」「撤销」 */
  action: string;
  /** 影响对象清单 */
  impacts: { id: string; label: string; detail?: string | null }[];
  /** 影响说明 */
  description?: string;
  /** 无法执行时的解释（如「有关联凭证，删除已禁用」） */
  blockedReason?: string | null;
  confirmLabel?: string;
  onConfirm: () => void | Promise<unknown>;
  loading?: boolean;
  /** 对话框内的附加内容（如版本号输入） */
  extra?: React.ReactNode;
}

export function DangerConfirm({
  open,
  onOpenChange,
  title,
  action,
  impacts,
  description,
  blockedReason,
  confirmLabel,
  onConfirm,
  loading,
  extra,
}: DangerConfirmProps) {
  const [expanded, setExpanded] = React.useState(false);
  const [typed, setTyped] = React.useState('');
  const count = impacts.length;
  const confirmText = confirmLabel ?? `${action} ${count} 个对象`;
  const requiredWord = action;
  const blocked = Boolean(blockedReason) || count === 0;
  const canConfirm = !blocked && typed.trim() === requiredWord && !loading;

  React.useEffect(() => {
    if (!open) {
      setTyped('');
      setExpanded(false);
    }
  }, [open]);

  const handleConfirm = async (): Promise<void> => {
    if (!canConfirm) return;
    await onConfirm();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={title}
        description={description ?? `该操作不可撤销，请确认影响范围。`}
        width="sm"
        footer={
          <>
            <DialogClose asChild>
              <Button variant="secondary" size="sm">
                取消
              </Button>
            </DialogClose>
            <Button
              variant="danger"
              size="sm"
              onClick={handleConfirm}
              loading={loading}
              disabled={!canConfirm}
            >
              {confirmText}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-2 rounded border border-[color-mix(in_srgb,var(--sr-danger)_35%,transparent)] bg-[var(--sr-danger-weak)] px-3 py-2">
            <TriangleAlert size={16} strokeWidth={1.75} className="mt-0.5 shrink-0 text-danger" aria-hidden />
            <p className="text-13 text-content">
              {blockedReason ?? `将影响 ${count} 个对象，其中部分可能正被外部 Agent 引用。`}
            </p>
          </div>

          <section aria-labelledby="danger-impact-title">
            <div className="flex items-center justify-between">
              <h3 id="danger-impact-title" className="text-13 font-medium text-content">
                影响面（{count}）
              </h3>
              {count > 0 ? (
                <button
                  type="button"
                  onClick={() => setExpanded((v) => !v)}
                  aria-expanded={expanded}
                  className="inline-flex items-center gap-1 rounded-sm text-12 text-content-muted hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                >
                  {expanded ? '收起清单' : '展开清单'}
                  <ChevronDown
                    size={14}
                    strokeWidth={1.75}
                    aria-hidden
                    className={cn('transition-transform duration-150 ease-sr', expanded && 'rotate-180')}
                  />
                </button>
              ) : null}
            </div>
            {count === 0 ? (
              <p className="mt-1 text-13 text-content-muted">没有可影响的对象。</p>
            ) : expanded ? (
              <ul className="mt-2 max-h-48 divide-y divide-[var(--sr-border)] overflow-y-auto rounded border border-border sr-scroll">
                {impacts.map((item) => (
                  <li key={item.id} className="px-3 py-2">
                    <p className="text-13 text-content">{item.label}</p>
                    {item.detail ? (
                      <p className="text-12 text-content-muted">{item.detail}</p>
                    ) : null}
                    <p className="sr-mono text-12 text-content-muted">{item.id}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <ul className="mt-2 space-y-0.5 text-13 text-content-muted">
                {impacts.slice(0, 3).map((item) => (
                  <li key={item.id} className="truncate">
                    · {item.label}
                  </li>
                ))}
                {count > 3 ? <li>· 其余 {count - 3} 项</li> : null}
              </ul>
            )}
          </section>

          {extra ? <div className="border-t border-border pt-3">{extra}</div> : null}

          <div className="flex flex-col gap-1.5">
            <label htmlFor="danger-confirm-word" className="text-13 text-content">
              输入 <code className="sr-mono text-danger">{requiredWord}</code> 以确认
            </label>
            <input
              id="danger-confirm-word"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              disabled={blocked}
              autoComplete="off"
              aria-describedby="danger-confirm-help"
              className={cn(
                'h-9 w-full rounded border border-border bg-surface px-3 text-14 text-content',
                'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] disabled:opacity-55',
              )}
            />
            <p id="danger-confirm-help" className="text-12 text-content-muted">
              按钮将显示「{confirmText}」。
            </p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
