'use client';

import { useEffect, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';

import { useDebouncedValue } from '@/lib/hooks/use-debounced-value';
import { cn } from '@/lib/utils';

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  /** Accessible name, e.g. "Search providers". */
  label: string;
  placeholder?: string;
  delayMs?: number;
  className?: string;
  disabled?: boolean;
}

/** Debounced search field; the parent only sees settled values. */
export function SearchInput({
  value,
  onChange,
  label,
  placeholder = 'Search…',
  delayMs = 300,
  className,
  disabled,
}: SearchInputProps) {
  const [text, setText] = useState(value);
  const debounced = useDebouncedValue(text, delayMs);
  const lastEmitted = useRef(value);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (debounced !== lastEmitted.current) {
      lastEmitted.current = debounced;
      onChangeRef.current(debounced);
    }
  }, [debounced]);

  useEffect(() => {
    if (value !== lastEmitted.current) {
      lastEmitted.current = value;
      setText(value);
    }
  }, [value]);

  return (
    <div className={cn('relative', className)}>
      <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
      <input
        type="search"
        value={text}
        aria-label={label}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        className={cn(
          'h-10 w-full rounded-control border border-border-strong bg-surface pl-9 pr-9 text-sm text-text',
          'placeholder:text-muted transition-colors duration-150 hover:border-muted/60',
          'disabled:cursor-not-allowed disabled:bg-info-soft disabled:text-muted',
        )}
      />
      {text ? (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => {
            setText('');
            lastEmitted.current = '';
            onChangeRef.current('');
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded-control p-1 text-muted transition-colors duration-150 hover:bg-info-soft hover:text-text"
        >
          <X aria-hidden className="h-3.5 w-3.5" />
        </button>
      ) : null}
    </div>
  );
}
