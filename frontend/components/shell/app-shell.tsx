'use client';

import { useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import { Menu, ShieldAlert } from 'lucide-react';

import { useSession } from '@/lib/auth';
import { useGuardedRouter } from '@/lib/navigation-guard';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Drawer } from '@/components/ui/drawer';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { ChangePasswordForm } from '@/components/account/change-password-form';
import { AccountMenu } from './account-menu';
import { NavList, navSectionFor } from './nav-list';

/**
 * Dashboard shell: fixed left navigation on ≥1024px, a top bar with a menu
 * button below that. Content is capped at 1180px.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? '/';
  const router = useGuardedRouter();
  const { status, mustChangePassword } = useSession();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);

  useEffect(() => {
    if (status === 'anonymous') {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [status]);

  const section = navSectionFor(pathname);

  if (status === 'loading' || status === 'anonymous') {
    return (
      <div className="mx-auto max-w-content px-4 py-10">
        <SkeletonBlock rows={2} className="max-w-sm" />
        <p className="mt-4 text-xs text-muted">
          {status === 'anonymous' ? 'Redirecting to sign in…' : 'Checking your session…'}
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-screen lg:flex">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 shrink-0 border-r border-border bg-surface lg:flex lg:flex-col">
        <BrandMark />
        <NavList onNavigate={() => undefined} />
        <div className="mt-auto border-t border-border p-3 text-[11px] text-muted">
          Skill Router admin · cookies + CSRF same-origin
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col lg:pl-60">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-2 border-b border-border bg-surface px-4">
          <div className="flex min-w-0 items-center gap-2">
            <span className="lg:hidden">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setMobileNavOpen(true)}
                aria-label="Open navigation"
                aria-haspopup="dialog"
              >
                <Menu aria-hidden className="h-4 w-4" />
              </Button>
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-text">{section.title}</p>
              <p className="hidden truncate text-[11px] text-muted sm:block">{section.description}</p>
            </div>
          </div>
          <AccountMenu />
        </header>

        <main className="mx-auto w-full max-w-content flex-1 px-4 py-6">
          {mustChangePassword ? (
            <Banner
              tone="warning"
              className="mb-4"
              title="Change the bootstrap admin password"
              primaryAction={{ label: 'Change password', onClick: () => setPasswordOpen(true), variant: 'primary' }}
            >
              Business endpoints stay blocked with <code>password_change_required</code> until the initial deployment
              password has been replaced.
            </Banner>
          ) : null}
          {children}
        </main>
      </div>

      <Drawer open={mobileNavOpen} onClose={() => setMobileNavOpen(false)} title="Navigation" widthClassName="w-64">
        <NavList
          onNavigate={(href) => {
            setMobileNavOpen(false);
            router.push(href);
          }}
        />
      </Drawer>

      <Dialog
        open={passwordOpen}
        onClose={() => setPasswordOpen(false)}
        title="Change password"
        description="Required before the router accepts business requests."
        size="md"
      >
        <div className="space-y-4">
          <div className="flex items-start gap-2 rounded-control border border-warning/30 bg-warning-soft p-2 text-xs text-text">
            <ShieldAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <span>Other sessions are revoked. This browser stays signed in with a fresh CSRF token.</span>
          </div>
          <ChangePasswordForm onSuccess={() => setPasswordOpen(false)} />
        </div>
      </Dialog>
    </div>
  );
}

function BrandMark() {
  return (
    <div className="flex h-14 items-center gap-2 border-b border-border px-4">
      <span aria-hidden className="flex h-7 w-7 items-center justify-center rounded-control bg-accent text-xs font-semibold text-white">
        SR
      </span>
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold text-text">Skill Router</p>
        <p className="truncate text-[11px] text-muted">Admin console</p>
      </div>
    </div>
  );
}
