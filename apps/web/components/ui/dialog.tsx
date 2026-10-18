'use client';

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import * as React from 'react';
import { cn } from './cn';

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export interface DialogContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  title: string;
  description?: string;
  /** 隐藏标题（视觉隐藏但保留可访问名） */
  hideTitle?: boolean;
  footer?: React.ReactNode;
  width?: 'sm' | 'md' | 'lg';
}

const widths = {
  sm: 'max-w-[420px]',
  md: 'max-w-[560px]',
  lg: 'max-w-[820px]',
} as const;

export const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(function DialogContent(
  { className, children, title, description, hideTitle, footer, width = 'md', ...props },
  ref,
) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-overlay bg-[rgba(28,26,23,0.36)]" />
      <DialogPrimitive.Content
        ref={ref}
        className={cn(
          'fixed left-1/2 top-1/2 z-overlay w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2',
          'rounded-md border border-border bg-surface p-0 focus:outline-none sr-enter',
          widths[width],
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <DialogPrimitive.Title className={cn('text-16 font-semibold text-content', hideTitle && 'sr-only')}>
              {title}
            </DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="mt-1 text-13 text-content-muted">
                {description}
              </DialogPrimitive.Description>
            ) : (
              <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
            )}
          </div>
          <DialogPrimitive.Close
            aria-label="关闭对话框"
            className="rounded-sm p-1 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          >
            <X size={20} strokeWidth={1.75} aria-hidden />
          </DialogPrimitive.Close>
        </div>
        <div className="max-h-[calc(100vh-220px)] overflow-y-auto px-4 py-3 sr-scroll">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
            {footer}
          </div>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});
