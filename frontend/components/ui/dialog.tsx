'use client';

import { useEffect, useId, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { useFocusTrap } from '@/lib/hooks/use-focus-trap';
import { IconButton } from './button';

export type DialogSize = 'sm' | 'md' | 'lg' | 'xl';

const SIZES: Record<DialogSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
};

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: DialogSize;
  /** When false (e.g. a destructive confirm in flight) Esc/backdrop are ignored. */
  dismissible?: boolean;
}

/**
 * Modal dialog: portal, `role="dialog"` + `aria-modal`, labelled by its title,
 * focus trapped, Escape closes, focus returns to the trigger.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  dismissible = true,
}: DialogProps) {
  const [mounted, setMounted] = useState(false);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => setMounted(true), []);

  const close = () => {
    if (dismissible) onClose();
  };
  const containerRef = useFocusTrap<HTMLDivElement>(open, close);

  if (!mounted || !open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:items-center">
      <div
        aria-hidden
        onClick={close}
        className="fixed inset-0 bg-text/30 animate-fade-in"
      />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cn(
          'relative z-10 w-full rounded-card border border-border bg-surface shadow-overlay animate-slide-in',
          'my-8 flex max-h-[calc(100vh-4rem)] flex-col outline-none',
          SIZES[size],
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-border px-6 py-4">
          <div>
            <h2 id={titleId} className="text-lg font-semibold text-text">
              {title}
            </h2>
            {description ? (
              <div id={descriptionId} className="mt-1 text-sm text-muted">
                {description}
              </div>
            ) : null}
          </div>
          <IconButton label="Close dialog" size="sm" onClick={close} disabled={!dismissible}>
            <X aria-hidden className="h-4 w-4" />
          </IconButton>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-4">{children}</div>
        {footer ? (
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-6 py-3">{footer}</footer>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
