'use client';

import { useId, useMemo } from 'react';
import { AlignLeft, CheckCircle2 } from 'lucide-react';

import { formatJsonText, parseJson } from '@/lib/json';
import { cn } from '@/lib/utils';
import { InlineAlert } from './banner';

export interface JsonEditorProps {
  /** Raw text the user types; the parent owns this string. */
  value: string;
  onChange: (value: string) => void;
  label: string;
  hint?: string;
  /** Force the JSON to be an object (config / test input). */
  requireObject?: boolean;
  rows?: number;
  disabled?: boolean;
  className?: string;
  /** Error coming from the API (e.g. a validation issue for this field). */
  externalError?: string | null;
  /** Extra actions rendered beside the format button. */
  actions?: React.ReactNode;
}

/**
 * Monospace JSON editor with live validation. The parsed value is reported by
 * the parent holding the text, so a structured form and this textarea stay in
 * sync through a single source of truth.
 */
export function JsonEditor({
  value,
  onChange,
  label,
  hint,
  requireObject = false,
  rows = 10,
  disabled,
  className,
  externalError,
  actions,
}: JsonEditorProps) {
  const id = useId();
  const errorId = `${id}-error`;

  const localError = useMemo(() => {
    if (!value.trim()) return null;
    const parsed = parseJson(value);
    if (!parsed.ok) return parsed.error;
    if (requireObject && parsed.value !== undefined) {
      if (parsed.value === null || typeof parsed.value !== 'object' || Array.isArray(parsed.value)) {
        return 'Expected a JSON object ({ … }).';
      }
    }
    return null;
  }, [value, requireObject]);

  const error = localError ?? externalError ?? null;

  const format = () => {
    const parsed = parseJson(value);
    if (parsed.ok) onChange(formatJsonText(parsed.value));
  };

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-xs font-medium text-text">
          {label}
        </label>
        <div className="flex items-center gap-1">
          {actions}
          <button
            type="button"
            onClick={format}
            disabled={disabled || Boolean(localError) || !value.trim()}
            className="inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-muted transition-colors duration-150 hover:bg-info-soft hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
          >
            <AlignLeft aria-hidden className="h-3 w-3" />
            Format
          </button>
        </div>
      </div>
      <textarea
        id={id}
        rows={rows}
        value={value}
        disabled={disabled}
        spellCheck={false}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        onChange={(event) => onChange(event.target.value)}
        className={cn(
          'w-full rounded-control border border-border-strong bg-surface px-3 py-2 font-mono text-xs leading-5 text-text',
          'transition-colors duration-150 hover:border-muted/60 disabled:cursor-not-allowed disabled:bg-info-soft',
          error && 'border-danger',
        )}
      />
      {error ? (
        <InlineAlert tone="danger">
          <span id={errorId}>{error}</span>
        </InlineAlert>
      ) : hint ? (
        <p className="text-xs text-muted">{hint}</p>
      ) : null}
      {!error && value.trim() ? (
        <p className="inline-flex items-center gap-1 text-[11px] text-success">
          <CheckCircle2 aria-hidden className="h-3 w-3" />
          Valid JSON
        </p>
      ) : null}
    </div>
  );
}
