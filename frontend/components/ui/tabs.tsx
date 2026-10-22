'use client';

import { useRef, type KeyboardEvent, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

export interface TabItem {
  id: string;
  label: string;
  icon?: ReactNode;
  /** Optional count rendered after the label (e.g. number of tests). */
  count?: number;
  disabled?: boolean;
}

export interface TabsProps {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Accessible name for the tab list. */
  label: string;
  className?: string;
  /** Panels are rendered by the caller; this id wires `aria-controls`. */
  panelIdPrefix?: string;
}

/** WAI-ARIA tabs with Arrow/Home/End keyboard support. */
export function Tabs({ tabs, value, onChange, label, className, panelIdPrefix = 'tab-panel' }: TabsProps) {
  const listRef = useRef<HTMLDivElement | null>(null);

  const focusTab = (index: number) => {
    const nodes = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    nodes?.[index]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const enabled = tabs.filter((tab) => !tab.disabled);
    const currentIndex = enabled.findIndex((tab) => tab.id === value);
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      const next = enabled[(currentIndex + 1) % enabled.length];
      if (next) onChange(next.id);
      focusTab((currentIndex + 1) % enabled.length);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      const prevIndex = (currentIndex - 1 + enabled.length) % enabled.length;
      const prev = enabled[prevIndex];
      if (prev) onChange(prev.id);
      focusTab(prevIndex);
    } else if (event.key === 'Home') {
      event.preventDefault();
      const first = enabled[0];
      if (first) onChange(first.id);
      focusTab(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      const last = enabled[enabled.length - 1];
      if (last) onChange(last.id);
      focusTab(enabled.length - 1);
    }
  };

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn('flex gap-1 overflow-x-auto border-b border-border', className)}
    >
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`${panelIdPrefix}-tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={`${panelIdPrefix}-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => onChange(tab.id)}
            className={cn(
              'inline-flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors duration-150',
              selected
                ? 'border-accent font-medium text-text'
                : 'border-transparent text-muted hover:border-border-strong hover:text-text',
              tab.disabled && 'cursor-not-allowed opacity-50 hover:border-transparent hover:text-muted',
            )}
          >
            {tab.icon}
            {tab.label}
            {typeof tab.count === 'number' ? (
              <span className="rounded-pill bg-info-soft px-1.5 text-xs text-muted">{tab.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  id,
  active,
  panelIdPrefix = 'tab-panel',
  children,
}: {
  id: string;
  active: boolean;
  panelIdPrefix?: string;
  children: ReactNode;
}) {
  if (!active) return null;
  return (
    <div
      role="tabpanel"
      id={`${panelIdPrefix}-${id}`}
      aria-labelledby={`${panelIdPrefix}-tab-${id}`}
      tabIndex={0}
      className="pt-4 outline-none"
    >
      {children}
    </div>
  );
}
