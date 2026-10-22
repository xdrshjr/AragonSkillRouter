'use client';

import { useEffect, useId, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { useFocusTrap } from '@/lib/hooks/use-focus-trap';
import { IconButton } from './button';

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  /** Panel width; the shell uses 440px for the agent panel. */
  widthClassName?: string;
  side?: 'right' | 'left';
  /**
   * Classes for the body. The agent panel passes
   * `flex min-h-0 flex-1 flex-col overflow-hidden p-0` so it can pin its own tab
   * strip, keep only the transcript scrolling, and hold the composer still.
   */
  contentClassName?: string;
  /** DOM id, so a trigger's `aria-controls` can point at the dialog. */
  id?: string;
}

/** Side panel with the same accessibility guarantees as Dialog. */
export function Drawer({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  widthClassName = 'w-full max-w-md',
  side = 'right',
  contentClassName,
  id,
}: DrawerProps) {
  const [mounted, setMounted] = useState(false);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => setMounted(true), []);
  const containerRef = useFocusTrap<HTMLDivElement>(open, onClose);

  if (!mounted || !open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50">
      <div aria-hidden onClick={onClose} className="absolute inset-0 bg-text/30 animate-fade-in" />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        id={id}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cn(
          // `min-h-0` matters: without it a flex child refuses to shrink and the
          // pinned footer is pushed off the bottom of the panel.
          'absolute top-0 flex h-full min-h-0 flex-col border-border bg-surface shadow-overlay outline-none animate-slide-in',
          side === 'right' ? 'right-0 border-l' : 'left-0 border-r',
          widthClassName,
        )}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h2 id={titleId} className="text-base font-semibold text-text">
              {title}
            </h2>
            {description ? (
              <p id={descriptionId} className="mt-0.5 text-xs text-muted">
                {description}
              </p>
            ) : null}
          </div>
          <IconButton label="Close panel" size="sm" onClick={onClose}>
            <X aria-hidden className="h-4 w-4" />
          </IconButton>
        </header>
        {/* `contentClassName` replaces the defaults rather than merging: a caller that
            pins its own regions must be able to drop `overflow-y-auto`, otherwise the
            body keeps scrolling and its inner scroll container never overflows. */}
        <div className={cn(contentClassName ?? 'min-h-0 flex-1 overflow-y-auto px-4 py-4')}>{children}</div>
        {footer ? <footer className="shrink-0 border-t border-border px-4 py-3">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  );
}
