'use client';

import * as ToastPrimitive from '@radix-ui/react-toast';
import { CheckCircle2, Info, TriangleAlert, X, XCircle } from 'lucide-react';
import * as React from 'react';
import { cn } from './cn';

export type ToastTone = 'ok' | 'warn' | 'danger' | 'info';

export interface ToastInput {
  title: string;
  description?: string;
  tone?: ToastTone;
  /** 展示 request_id 与复制按钮（错误类必备） */
  requestId?: string | null;
  /** 毫秒，错误默认更久 */
  durationMs?: number;
  action?: { label: string; onClick: () => void };
}

interface ToastRecord extends ToastInput {
  id: number;
  open: boolean;
}

interface ToastContextValue {
  toast: (input: ToastInput) => number;
  dismiss: (id: number) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

const toneIcon: Record<ToastTone, React.ReactNode> = {
  ok: <CheckCircle2 size={20} strokeWidth={1.75} className="text-ok" aria-hidden />,
  warn: <TriangleAlert size={20} strokeWidth={1.75} className="text-warn" aria-hidden />,
  danger: <XCircle size={20} strokeWidth={1.75} className="text-danger" aria-hidden />,
  info: <Info size={20} strokeWidth={1.75} className="text-info" aria-hidden />,
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastRecord[]>([]);
  const counter = React.useRef(0);

  const dismiss = React.useCallback((id: number) => {
    setItems((prev) => prev.map((t) => (t.id === id ? { ...t, open: false } : t)));
  }, []);

  const toast = React.useCallback((input: ToastInput) => {
    counter.current += 1;
    const id = counter.current;
    setItems((prev) => [...prev.slice(-4), { ...input, id, open: true }]);
    return id;
  }, []);

  const value = React.useMemo<ToastContextValue>(() => ({ toast, dismiss }), [toast, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      <ToastPrimitive.Provider swipeDirection="right" duration={6000}>
        {children}
        {items.map((item) => (
          <ToastPrimitive.Root
            key={item.id}
            open={item.open}
            duration={item.durationMs ?? (item.tone === 'danger' ? 12_000 : 5_000)}
            onOpenChange={(open) => {
              if (!open) dismiss(item.id);
            }}
            className={cn(
              'flex w-[380px] max-w-[calc(100vw-32px)] items-start gap-3 rounded-md border border-border bg-surface p-3',
              'sr-enter data-[state=closed]:opacity-0',
            )}
          >
            <span className="mt-0.5 shrink-0">{toneIcon[item.tone ?? 'info']}</span>
            <div className="min-w-0 flex-1">
              <ToastPrimitive.Title className="text-14 font-medium text-content">
                {item.title}
              </ToastPrimitive.Title>
              {item.description ? (
                <ToastPrimitive.Description className="mt-1 break-words text-13 text-content-muted">
                  {item.description}
                </ToastPrimitive.Description>
              ) : null}
              {item.requestId ? (
                <p className="mt-1 break-all text-12 text-content-muted">
                  request_id: <code className="sr-mono">{item.requestId}</code>
                </p>
              ) : null}
              {item.action ? (
                <ToastPrimitive.Action
                  altText={item.action.label}
                  onClick={item.action.onClick}
                  className="mt-2 rounded-sm border border-border px-2 py-1 text-12 text-content hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                >
                  {item.action.label}
                </ToastPrimitive.Action>
              ) : null}
            </div>
            <ToastPrimitive.Close
              aria-label="关闭提示"
              className="rounded-sm p-1 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            >
              <X size={16} strokeWidth={1.75} aria-hidden />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        ))}
        <ToastPrimitive.Viewport className="fixed bottom-4 right-4 z-toast flex max-h-screen w-auto flex-col-reverse gap-2 outline-none" />
      </ToastPrimitive.Provider>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext);
  if (!ctx) {
    throw new Error('useToast 必须在 <ToastProvider> 内使用');
  }
  return ctx;
}
