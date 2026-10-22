'use client';

import { useEffect, useRef, useState } from 'react';
import { LogOut, Settings, ShieldCheck, UserRound } from 'lucide-react';

import { useSession } from '@/lib/auth';
import { useLogoutMutation } from '@/lib/queries';
import { useToast } from '@/components/ui/toast';
import { useGuardedRouter } from '@/lib/navigation-guard';
import { cn } from '@/lib/utils';

/** Account menu in the top bar. Sign-out failures stay visible, never silent. */
export function AccountMenu() {
  const { user } = useSession();
  const router = useGuardedRouter();
  const toast = useToast();
  const logout = useLogoutMutation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const initials = (user?.username ?? '?').slice(0, 2).toUpperCase();

  const signOut = async () => {
    try {
      await logout.mutateAsync();
      router.push('/login');
    } catch (error) {
      toast.apiError(error, 'Sign out failed');
      setOpen(false);
    }
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(
          'flex items-center gap-2 rounded-control border border-border px-2 py-1 text-xs transition-colors duration-150',
          'hover:bg-info-soft',
        )}
      >
        <span
          aria-hidden
          className="flex h-6 w-6 items-center justify-center rounded-pill bg-accent-soft text-[11px] font-semibold text-accent-strong"
        >
          {initials}
        </span>
        <span className="hidden max-w-[120px] truncate text-text sm:block">{user?.username ?? 'Account'}</span>
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Account"
          className="absolute right-0 z-30 mt-1 w-56 rounded-card border border-border bg-surface p-1 shadow-overlay animate-slide-in"
        >
          <div className="px-2 py-1.5">
            <p className="flex items-center gap-1.5 text-xs font-medium text-text">
              <UserRound aria-hidden className="h-3.5 w-3.5 text-muted" />
              {user?.username ?? 'Unknown user'}
            </p>
            <p className="mt-0.5 flex items-center gap-1 text-[11px] text-muted">
              <ShieldCheck aria-hidden className="h-3 w-3" />
              role: {user?.role ?? '—'}
            </p>
          </div>
          <div className="my-1 border-t border-border" />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              router.push('/account');
            }}
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-xs text-text transition-colors duration-150 hover:bg-info-soft"
          >
            <Settings aria-hidden className="h-3.5 w-3.5 text-muted" />
            Account settings
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={signOut}
            disabled={logout.isPending}
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-xs text-text transition-colors duration-150 hover:bg-info-soft disabled:cursor-not-allowed disabled:opacity-60"
          >
            <LogOut aria-hidden className="h-3.5 w-3.5 text-muted" />
            {logout.isPending ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      ) : null}
    </div>
  );
}
