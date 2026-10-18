'use client';

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import * as React from 'react';
import { cn } from './cn';

export const Drawer = DialogPrimitive.Root;
export const DrawerTrigger = DialogPrimitive.Trigger;
export const DrawerClose = DialogPrimitive.Close;

export interface DrawerContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  title: string;
  description?: string;
  side?: 'right' | 'left';
  width?: number;
  footer?: React.ReactNode;
}

/** 右侧抽屉：宽屏固定宽度，窄屏全宽。Esc 关闭 + 焦点陷阱由 Radix 提供。 */
export const DrawerContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  DrawerContentProps
>(function DrawerContent(
  { className, children, title, description, side = 'right', width = 560, footer, ...props },
  ref,
) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-overlay bg-[rgba(28,26,23,0.36)]" />
      <DialogPrimitive.Content
        ref={ref}
        style={{ width: `min(${width}px, 100vw)` }}
        className={cn(
          'fixed bottom-0 top-0 z-overlay flex flex-col border-border bg-surface focus:outline-none',
          side === 'right' ? 'right-0 border-l' : 'left-0 border-r',
          'sr-enter',
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <DialogPrimitive.Title className="text-16 font-semibold text-content">
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
            aria-label="关闭抽屉"
            className="rounded-sm p-1 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
          >
            <X size={20} strokeWidth={1.75} aria-hidden />
          </DialogPrimitive.Close>
        </div>
        <div className="flex-1 overflow-y-auto px-4 py-3 sr-scroll">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
            {footer}
          </div>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});
