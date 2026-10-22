'use client';

/**
 * Session tabs for the agent panel.
 *
 * Cursor-style: the open sessions are tabs across the top of the panel, `+` opens a
 * new one, the `×` on a tab archives it, and the rest of the history is one click
 * away in a list. The previous design hid session switching behind a "Sessions"
 * button and a full-height drawer, so changing conversation took two clicks and lost
 * sight of where you were.
 *
 * Behaviour that matters:
 *  * Tabs scroll horizontally instead of wrapping, so a narrow drawer keeps a single
 *    row and the composer keeps its height.
 *  * The active tab is a real `role="tab"` inside a `tablist`; Arrow/Home/End move
 *    between tabs, which is the keyboard contract screen readers announce.
 *  * `×` is a sibling button inside the tab, not nested inside it (nested interactive
 *    elements are invalid and swallow the click in some browsers).
 *  * The `×` is revealed on hover/focus but always present in the accessibility tree;
 *    archiving asks first when the session has content, because it is a write.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { ChevronDown, Loader2, Plus, X } from 'lucide-react';

import { formatDateTimeShort } from '@/lib/format';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Tooltip } from '@/components/ui/tooltip';
import { TimeAgo } from '@/components/ui/time-ago';
import { cn } from '@/lib/utils';
import { useAgent } from './agent-context';

function shortTitle(title: string | undefined | null): string {
  const text = (title ?? '').trim() || 'New session';
  return text.length > 26 ? `${text.slice(0, 25)}…` : text;
}

export function SessionTabs() {
  const {
    sessions,
    sessionId,
    session,
    sessionLoading,
    selectSession,
    startNewSession,
    archiveSession,
    archivingSessionId,
  } = useAgent();

  const stripRef = useRef<HTMLDivElement | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [pendingArchive, setPendingArchive] = useState<{ id: string; title: string; runs: number } | null>(null);

  // The active session may not be in the list yet (freshly created). Show it anyway so
  // the strip never looks like it lost the conversation you are reading.
  const tabs = useMemo(() => {
    const list = [...sessions];
    if (sessionId && !list.some((item) => item.id === sessionId)) {
      list.unshift({
        id: sessionId,
        title: session?.title ?? 'New session',
        status: session?.status ?? 'idle',
        engine_release: session?.engine_release ?? null,
        created_at: session?.created_at ?? new Date().toISOString(),
        updated_at: session?.updated_at ?? new Date().toISOString(),
        run_count: session?.runs.length ?? 0,
      });
    }
    return list;
  }, [session, sessionId, sessions]);

  const activeIndex = tabs.findIndex((item) => item.id === sessionId);

  // Keep the active tab visible when it changes (e.g. after archiving a neighbour).
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const active = strip.querySelector<HTMLElement>('[aria-selected="true"]');
    if (active) {
      const left = active.offsetLeft;
      const right = left + active.offsetWidth;
      if (left < strip.scrollLeft) strip.scrollTo({ left: left - 8, behavior: 'smooth' });
      else if (right > strip.scrollLeft + strip.clientWidth) {
        strip.scrollTo({ left: right - strip.clientWidth + 8, behavior: 'smooth' });
      }
    }
  }, [activeIndex, tabs.length]);

  const onKeyDown = useCallback(
    async (event: KeyboardEvent<HTMLDivElement>) => {
      if (tabs.length === 0) return;
      const current = activeIndex < 0 ? 0 : activeIndex;
      let next = current;
      if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      const target = tabs[next];
      if (target && target.id !== sessionId) await selectSession(target.id);
      const strip = stripRef.current;
      strip?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
    },
    [activeIndex, selectSession, sessionId, tabs],
  );

  const requestArchive = (item: { id: string; title: string; run_count?: number }) => {
    const runs = item.run_count ?? 0;
    if (runs === 0) {
      // Nothing has happened in it yet: no confirmation needed for an empty session.
      void archiveSession(item.id);
      return;
    }
    setPendingArchive({ id: item.id, title: item.title, runs });
  };

  return (
    <>
      <div className="border-b border-border bg-surface-muted/60">
        <div className="flex items-stretch gap-0.5 px-1 pt-1">
          <div
            ref={stripRef}
            role="tablist"
            aria-label="Agent sessions"
            aria-orientation="horizontal"
            onKeyDown={(event) => void onKeyDown(event)}
            className="flex min-w-0 flex-1 items-stretch gap-0.5 overflow-x-auto scroll-smooth [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {tabs.length === 0 ? (
              <span className="flex items-center px-2 py-1.5 text-[11px] text-muted">
                {sessionLoading ? 'Loading…' : 'No sessions yet'}
              </span>
            ) : null}
            {tabs.map((item) => {
              const active = item.id === sessionId;
              const busy = archivingSessionId === item.id;
              return (
                <div
                  key={item.id}
                  className={cn(
                    'group relative flex shrink-0 items-center rounded-t-control border border-b-0 transition-colors duration-150',
                    active
                      ? 'border-border bg-surface text-text'
                      : 'border-transparent text-muted hover:border-border hover:bg-surface/70 hover:text-text',
                  )}
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={active}
                    aria-controls="agent-tabpanel"
                    id={`agent-tab-${item.id}`}
                    tabIndex={active || (activeIndex < 0 && tabs[0]?.id === item.id) ? 0 : -1}
                    onClick={() => {
                      if (!active) void selectSession(item.id);
                    }}
                    title={`${item.title}\n${item.run_count ?? 0} run(s) · last activity ${formatDateTimeShort(item.updated_at)}`}
                    className={cn(
                      'max-w-[168px] truncate py-1.5 pl-2.5 text-[12px] outline-none',
                      'focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
                      active ? 'pr-1' : 'pr-2.5',
                    )}
                  >
                    {shortTitle(item.title)}
                  </button>
                  {active ? <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-pill bg-accent" /> : null}
                  <Tooltip content={`Archive "${item.title}"`}>
                    <button
                      type="button"
                      aria-label={`Archive session ${item.title}`}
                      disabled={busy}
                      onClick={(event) => {
                        event.stopPropagation();
                        requestArchive(item);
                      }}
                      className={cn(
                        'mr-1 ml-1 flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] text-muted',
                        'opacity-0 transition-opacity duration-150 hover:bg-info-soft hover:text-text',
                        'group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100',
                        busy && 'opacity-100',
                      )}
                    >
                      {busy ? <Loader2 aria-hidden className="h-3 w-3 animate-spin" /> : <X aria-hidden className="h-3 w-3" />}
                    </button>
                  </Tooltip>
                </div>
              );
            })}
          </div>

          <div className="flex shrink-0 items-center gap-0.5 pb-1">
            <Tooltip content="New session (⌘/Ctrl + Alt + N)">
              <button
                type="button"
                aria-label="New session"
                onClick={() => void startNewSession()}
                disabled={sessionLoading}
                className="flex h-6 w-6 items-center justify-center rounded-control text-muted hover:bg-info-soft hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
              >
                <Plus aria-hidden className="h-3.5 w-3.5" />
              </button>
            </Tooltip>
            <Tooltip content="All sessions">
              <button
                type="button"
                aria-label="Show all sessions"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((value) => !value)}
                className={cn(
                  'flex h-6 w-6 items-center justify-center rounded-control text-muted hover:bg-info-soft hover:text-text',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  menuOpen && 'bg-info-soft text-text',
                )}
              >
                <ChevronDown aria-hidden className="h-3.5 w-3.5" />
              </button>
            </Tooltip>
          </div>
        </div>

        {menuOpen ? (
          <div className="max-h-56 overflow-y-auto border-t border-border bg-surface">
            {tabs.length === 0 ? (
              <p className="px-3 py-2 text-[11px] text-muted">No sessions yet — send a message to start one.</p>
            ) : (
              <ul className="py-1">
                {tabs.map((item) => (
                  <li key={item.id}>
                    <div
                      className={cn(
                        'flex items-center gap-2 px-2 py-1.5 text-[12px]',
                        item.id === sessionId ? 'bg-accent-soft' : 'hover:bg-info-soft',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          setMenuOpen(false);
                          if (item.id !== sessionId) void selectSession(item.id);
                        }}
                        className="min-w-0 flex-1 text-left"
                      >
                        <span className="block truncate text-text">{item.title}</span>
                        <span className="block text-[11px] text-muted">
                          {item.run_count ?? 0} run(s) · <TimeAgo value={item.updated_at} />
                        </span>
                      </button>
                      {item.id === sessionId ? (
                        <Badge tone="accent" dot={false}>
                          open
                        </Badge>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Archive session ${item.title}`}
                        loading={archivingSessionId === item.id}
                        onClick={() => requestArchive(item)}
                        className="shrink-0"
                      >
                        Archive
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </div>

      <ConfirmDialog
        open={pendingArchive !== null}
        title="Archive this session?"
        description={
          pendingArchive
            ? `"${pendingArchive.title}" has ${pendingArchive.runs} recorded run(s). Its transcript and run history are kept in the database, but the session stops appearing in the tabs.`
            : ''
        }
        impact={
          pendingArchive ? (
            <ul className="list-disc pl-4">
              <li>The session disappears from the tab strip immediately.</li>
              <li>Its messages, runs and events stay in the database (archived, not deleted).</li>
              <li>Any run still executing is not cancelled.</li>
            </ul>
          ) : null
        }
        confirmLabel="Archive session"
        tone="danger"
        loading={pendingArchive !== null && archivingSessionId === pendingArchive.id}
        onConfirm={() => {
          const target = pendingArchive;
          setPendingArchive(null);
          if (target) void archiveSession(target.id);
        }}
        onCancel={() => setPendingArchive(null)}
      />
    </>
  );
}
