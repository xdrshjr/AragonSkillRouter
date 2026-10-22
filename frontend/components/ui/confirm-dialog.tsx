'use client';

import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';

import { Dialog } from './dialog';
import { Button } from './button';

export interface ConfirmSecondaryAction {
  label: string;
  onClick: () => void;
  loading?: boolean;
}

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'danger' | 'primary';
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /**
   * What the action will affect. Rendered as a bullet list so a destructive
   * confirm never hides its blast radius (CONTRACT §9 "delete with impact").
   */
  impact?: ReactNode;
  secondaryAction?: ConfirmSecondaryAction;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'danger',
  loading = false,
  onConfirm,
  onCancel,
  impact,
  secondaryAction,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      dismissible={!loading}
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={loading}>
            {cancelLabel}
          </Button>
          {secondaryAction ? (
            <Button variant="secondary" onClick={secondaryAction.onClick} loading={secondaryAction.loading}>
              {secondaryAction.label}
            </Button>
          ) : null}
          <Button variant={tone === 'danger' ? 'danger' : 'primary'} onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="flex gap-3">
        {tone === 'danger' ? (
          <span
            aria-hidden
            className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-pill bg-danger-soft text-danger"
          >
            <AlertTriangle className="h-4 w-4" />
          </span>
        ) : null}
        <div className="min-w-0 flex-1 space-y-3">
          <div className="text-sm text-text">{description}</div>
          {impact ? (
            <div className="rounded-control border border-border bg-info-soft p-3 text-xs text-text">{impact}</div>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
