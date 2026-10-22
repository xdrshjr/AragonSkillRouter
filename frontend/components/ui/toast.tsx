'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { cn } from '@/lib/utils';

export type ToastTone = 'success' | 'error' | 'warning' | 'info';

export interface ToastInput {
  title: string;
  description?: string;
  /** The backend request id — always shown when the backend supplied one. */
  requestId?: string | null;
  /** Actionable next steps from the error envelope. */
  actions?: string[];
  /** 0 keeps the toast until dismissed (default for errors). */
  durationMs?: number;
}

interface ToastRecord extends ToastInput {
  id: string;
  tone: ToastTone;
}

export interface ToastApi {
  push: (tone: ToastTone, input: ToastInput | string) => string;
  success: (input: ToastInput | string) => string;
  error: (input: ToastInput | string) => string;
  warning: (input: ToastInput | string) => string;
  info: (input: ToastInput | string) => string;
  /** Render a normalised API failure: message + request id + actions. */
  apiError: (error: unknown, title: string) => string;
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_STYLES: Record<ToastTone, string> = {
  success: 'border-success/30 bg-surface',
  error: 'border-danger/40 bg-surface',
  warning: 'border-warning/40 bg-surface',
  info: 'border-border-strong bg-surface',
};

const TONE_ICON: Record<ToastTone, ReactNode> = {
  success: <CheckCircle2 aria-hidden className="h-4 w-4 text-success" />,
  error: <XCircle aria-hidden className="h-4 w-4 text-danger" />,
  warning: <AlertTriangle aria-hidden className="h-4 w-4 text-warning" />,
  info: <Info aria-hidden className="h-4 w-4 text-accent" />,
};

function normalise(input: ToastInput | string): ToastInput {
  return typeof input === 'string' ? { title: input } : input;
}

let toastCounter = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastRecord[]>([]);
  const timers = useRef(new Map<string, number>());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer) window.clearTimeout(timer);
    timers.current.delete(id);
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const push = useCallback(
    (tone: ToastTone, input: ToastInput | string) => {
      toastCounter += 1;
      const id = `toast-${toastCounter}`;
      const record: ToastRecord = { id, tone, ...normalise(input) };
      setToasts((current) => [...current.slice(-3), record]);
      const duration = record.durationMs ?? (tone === 'error' ? 0 : 5000);
      if (duration > 0) {
        const timer = window.setTimeout(() => dismiss(id), duration);
        timers.current.set(id, timer);
      }
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const registered = timers.current;
    return () => {
      registered.forEach((timer) => window.clearTimeout(timer));
      registered.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      push,
      dismiss,
      success: (input) => push('success', input),
      error: (input) => push('error', input),
      warning: (input) => push('warning', input),
      info: (input) => push('info', input),
      apiError: (error, title) =>
        push('error', {
          title,
          description: describeError(error),
          requestId: errorRequestId(error),
          actions: errorActions(error),
        }),
    }),
    [push, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        aria-live="polite"
        aria-relevant="additions text"
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(400px,calc(100vw-2rem))] flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.tone === 'error' || toast.tone === 'warning' ? 'alert' : 'status'}
            className={cn(
              'pointer-events-auto rounded-card border p-3 shadow-overlay animate-slide-in',
              TONE_STYLES[toast.tone],
            )}
          >
            <div className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0">{TONE_ICON[toast.tone]}</span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-text">{toast.title}</p>
                {toast.description ? (
                  <p className="mt-0.5 break-words text-xs text-muted">{toast.description}</p>
                ) : null}
                {toast.requestId ? (
                  <p className="mt-1 select-all font-mono text-[11px] text-muted">request {toast.requestId}</p>
                ) : null}
                {toast.actions && toast.actions.length > 0 ? (
                  <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-xs text-text">
                    {toast.actions.map((action) => (
                      <li key={action}>{action}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <button
                type="button"
                onClick={() => dismiss(toast.id)}
                aria-label="Dismiss notification"
                className="rounded-control p-1 text-muted transition-colors duration-150 hover:bg-info-soft hover:text-text"
              >
                <X aria-hidden className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext);
  if (!context) throw new Error('useToast must be used inside <ToastProvider>.');
  return context;
}
