'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';

interface NavigationGuard {
  /** Snapshot shown in the discard dialog, e.g. "capability v3 config". */
  what: string;
  isDirty: () => boolean;
  /** Persist the pending work; resolves true when it is safe to leave. */
  save?: () => Promise<boolean>;
}

interface NavigationGuardContextValue {
  register: (guard: NavigationGuard | null) => void;
  /** Navigate, asking first when the registered editor has unsaved changes. */
  push: (href: string) => void;
  hasGuard: boolean;
}

const NavigationGuardContext = createContext<NavigationGuardContextValue | null>(null);

/**
 * In-app unsaved-changes guard.
 *
 * An editor calls `useRegisterUnsavedChanges({ what, isDirty, save })`; every
 * shell navigation goes through `useGuardedRouter().push()` which shows a
 * discard confirmation instead of silently losing the edit. `beforeunload` is
 * covered as well.
 */
export function NavigationGuardProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const guardRef = useRef<NavigationGuard | null>(null);
  const [pendingHref, setPendingHref] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [hasGuard, setHasGuard] = useState(false);

  const register = useCallback((guard: NavigationGuard | null) => {
    guardRef.current = guard;
    setHasGuard(Boolean(guard));
  }, []);

  const push = useCallback(
    (href: string) => {
      const guard = guardRef.current;
      if (guard && guard.isDirty()) {
        setPendingHref(href);
        return;
      }
      router.push(href);
    },
    [router],
  );

  const value = useMemo<NavigationGuardContextValue>(() => ({ register, push, hasGuard }), [register, push, hasGuard]);

  const guard = guardRef.current;

  const onDiscard = () => {
    const target = pendingHref;
    setPendingHref(null);
    if (target) router.push(target);
  };

  const onSaveAndLeave = async () => {
    if (!guard?.save) return onDiscard();
    setSaving(true);
    try {
      const ok = await guard.save();
      if (ok) onDiscard();
    } finally {
      setSaving(false);
    }
  };

  return (
    <NavigationGuardContext.Provider value={value}>
      {children}
      <ConfirmDialog
        open={pendingHref !== null}
        title="Leave without saving?"
        description={`You have unsaved changes in ${guard?.what ?? 'this editor'}. Leaving now discards them.`}
        confirmLabel="Discard changes and leave"
        cancelLabel="Stay on this page"
        tone="danger"
        loading={saving}
        onConfirm={onDiscard}
        onCancel={() => setPendingHref(null)}
        secondaryAction={
          guard?.save
            ? { label: 'Save, then leave', onClick: onSaveAndLeave, loading: saving }
            : undefined
        }
      />
    </NavigationGuardContext.Provider>
  );
}

function useGuardContext(): NavigationGuardContextValue {
  const context = useContext(NavigationGuardContext);
  if (!context) throw new Error('useGuardedRouter must be used inside <NavigationGuardProvider>.');
  return context;
}

export function useGuardedRouter() {
  return useGuardContext();
}

/** Register (and clean up) the current editor's dirty state. */
export function useRegisterUnsavedChanges(guard: NavigationGuard) {
  const { register } = useGuardContext();
  const isDirtyRef = useRef(guard.isDirty);
  isDirtyRef.current = guard.isDirty;
  const saveRef = useRef(guard.save);
  saveRef.current = guard.save;

  useEffect(() => {
    register({
      what: guard.what,
      isDirty: () => isDirtyRef.current(),
      save: saveRef.current ? () => (saveRef.current as () => Promise<boolean>)() : undefined,
    });
    return () => register(null);
  }, [register, guard.what]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!isDirtyRef.current()) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, []);
}
