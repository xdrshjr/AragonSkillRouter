'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';

import { cn } from '@/lib/utils';

export interface CodeBlockProps {
  /** Already-safe text: this component never receives credential material. */
  code: string;
  language?: string;
  label?: string;
  maxHeightClassName?: string;
  wrap?: boolean;
  copyable?: boolean;
  /** Extra note rendered above the block, e.g. "secrets are masked". */
  notice?: ReactNode;
  className?: string;
  emptyLabel?: string;
}

/**
 * Monospace block with a copy button. Copy failures are reported inline instead
 * of pretending the copy worked. Secrets are never passed here: credentials are
 * only ever rendered from the API's masked `hint`.
 */
export function CodeBlock({
  code,
  language,
  label,
  maxHeightClassName = 'max-h-72',
  wrap = false,
  copyable = true,
  notice,
  className,
  emptyLabel = 'Nothing to show.',
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  const onCopy = useCallback(async () => {
    setCopyError(null);
    try {
      if (!navigator.clipboard) throw new Error('Clipboard is not available in this browser context.');
      await navigator.clipboard.writeText(code);
      setCopied(true);
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch (error) {
      setCopyError(error instanceof Error ? error.message : 'Copy failed.');
    }
  }, [code]);

  return (
    <div className={cn('rounded-control border border-border bg-info-soft/60', className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border px-2 py-1">
        <span className="truncate font-mono text-[11px] uppercase tracking-wide text-muted">
          {label ?? language ?? 'output'}
        </span>
        {copyable && code ? (
          <button
            type="button"
            onClick={onCopy}
            className="inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-muted transition-colors duration-150 hover:bg-surface hover:text-text"
          >
            {copied ? <Check aria-hidden className="h-3 w-3 text-success" /> : <Copy aria-hidden className="h-3 w-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        ) : null}
      </div>
      {notice ? <p className="border-b border-border px-2 py-1 text-[11px] text-muted">{notice}</p> : null}
      {copyError ? (
        <p role="alert" className="border-b border-border px-2 py-1 text-[11px] text-danger">
          {copyError}
        </p>
      ) : null}
      {code ? (
        <pre
          className={cn(
            'sr-scroll-x overflow-y-auto px-2 py-2 text-xs leading-5 text-text',
            wrap && 'whitespace-pre-wrap break-words',
            maxHeightClassName,
          )}
        >
          <code>{code}</code>
        </pre>
      ) : (
        <p className="px-2 py-2 text-xs text-muted">{emptyLabel}</p>
      )}
    </div>
  );
}

/** JSON pretty printer used by CodeBlock call sites. */
export function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
