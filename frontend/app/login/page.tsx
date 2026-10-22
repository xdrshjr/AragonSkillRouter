'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, KeyRound, Lock } from 'lucide-react';

import { api, describeError, endpoints, errorActions, errorRequestId, isApiError } from '@/lib/api';
import { useSession } from '@/lib/auth';
import { useLoginMutation } from '@/lib/queries';
import type { BootstrapStatus } from '@/lib/types';
import { ChangePasswordForm } from '@/components/account/change-password-form';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { InlineAlert } from '@/components/ui/banner';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';

function LoginScreen() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { status, mustChangePassword, refresh } = useSession();
  const toast = useToast();
  const login = useLoginMutation();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [touched, setTouched] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const [lockoutSeconds, setLockoutSeconds] = useState<number | null>(null);

  const nextPath = useMemo(() => {
    const candidate = searchParams.get('next');
    if (candidate && candidate.startsWith('/') && !candidate.startsWith('//')) return candidate;
    return '/';
  }, [searchParams]);

  const bootstrap = useQuery<BootstrapStatus, Error>({
    queryKey: ['bootstrap-status'],
    queryFn: ({ signal }) => api.get<BootstrapStatus>(endpoints.bootstrapStatus, { signal, skipAuthRedirect: true }),
    retry: false,
  });

  useEffect(() => {
    if (status === 'authenticated' && !mustChangePassword && !changingPassword) {
      router.replace(nextPath);
    }
  }, [status, mustChangePassword, changingPassword, router, nextPath]);

  useEffect(() => {
    if (lockoutSeconds === null) return;
    if (lockoutSeconds <= 0) return;
    const timer = window.setInterval(() => setLockoutSeconds((value) => (value === null ? null : value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [lockoutSeconds]);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTouched(true);
    setLockoutSeconds(null);
    if (!username.trim() || !password) return;
    try {
      const result = await login.mutateAsync({ username: username.trim(), password });
      if (result.must_change_password) {
        setChangingPassword(true);
        toast.warning({ title: 'Set a new password', description: 'The bootstrap password must be replaced first.' });
        return;
      }
      toast.success({ title: 'Signed in', description: result.user.username });
      refresh();
      router.replace(nextPath);
    } catch (error) {
      if (isApiError(error) && error.code === 'too_many_attempts') {
        const details = error.details as { retry_after_seconds?: number } | null;
        setLockoutSeconds(details?.retry_after_seconds ?? 0);
      }
      toast.apiError(error, 'Sign in failed');
    }
  };

  const expiredPasswordFlow = changingPassword || (status === 'authenticated' && mustChangePassword);
  const showLoginError = login.isError && !expiredPasswordFlow;

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="grid w-full max-w-4xl gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="rounded-card border border-border bg-surface p-6">
          <div className="mb-6 flex items-center gap-2">
            <span
              aria-hidden
              className="flex h-8 w-8 items-center justify-center rounded-control bg-accent text-xs font-semibold text-white"
            >
              SR
            </span>
            <div>
              <h1 className="text-lg font-semibold text-text">Skill Router</h1>
              <p className="text-xs text-muted">Admin console</p>
            </div>
          </div>

          {expiredPasswordFlow ? (
            <div className="space-y-4">
              <Banner tone="warning" title="Set a new admin password">
                The deployment bootstrap password is still active. Business endpoints stay blocked until it is replaced.
              </Banner>
              <ChangePasswordForm
                submitLabel="Save new password"
                onSuccess={() => {
                  setChangingPassword(false);
                  refresh();
                  router.replace(nextPath);
                }}
              />
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4" noValidate>
              <div>
                <h2 className="text-base font-semibold text-text">Sign in</h2>
                <p className="mt-0.5 text-xs text-muted">
                  Session cookies and the CSRF token are issued by the API on this origin.
                </p>
              </div>

              {showLoginError ? (
                <Banner
                  tone="danger"
                  title="Sign in failed"
                  actions={errorActions(login.error)}
                  requestId={errorRequestId(login.error)}
                >
                  {describeError(login.error)}
                  {lockoutSeconds !== null && lockoutSeconds > 0 ? (
                    <p className="mt-1 font-medium">
                      Locked for {lockoutSeconds}s. Too many attempts from this IP or username.
                    </p>
                  ) : null}
                </Banner>
              ) : null}

              <Field
                label="Username"
                htmlFor="login-username"
                required
                error={touched && !username.trim() ? 'Enter your username.' : null}
              >
                <Input
                  id="login-username"
                  name="username"
                  autoComplete="username"
                  autoFocus
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  invalid={touched && !username.trim()}
                />
              </Field>

              <Field
                label="Password"
                htmlFor="login-password"
                required
                error={touched && !password ? 'Enter your password.' : null}
              >
                <Input
                  id="login-password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  invalid={touched && !password}
                />
              </Field>

              <Button
                type="submit"
                variant="primary"
                fullWidth
                loading={login.isPending}
                disabled={lockoutSeconds !== null && lockoutSeconds > 0}
                iconLeft={<Lock aria-hidden className="h-4 w-4" />}
              >
                Sign in
              </Button>

              <InlineAlert tone="info">
                Repeated failures are throttled per username and IP; the wait time comes from the API response.
              </InlineAlert>
            </form>
          )}
        </section>

        <aside className="rounded-card border border-border bg-surface p-4">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold text-text">
            <KeyRound aria-hidden className="h-4 w-4 text-muted" />
            First run
          </h2>
          <p className="mt-1 text-xs text-muted">
            The router is set up with a bootstrap admin account from deployment configuration.
          </p>
          {bootstrap.isLoading ? (
            <SkeletonBlock rows={3} className="mt-3" />
          ) : bootstrap.isError ? (
            <div className="mt-3">
              <Banner tone="warning" title="Bootstrap status unavailable" requestId={errorRequestId(bootstrap.error)}>
                {describeError(bootstrap.error)} The sign-in form still works; this panel only explains the first-run
                state.
              </Banner>
            </div>
          ) : bootstrap.data ? (
            <dl className="mt-3 space-y-2 text-xs">
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Initialized</dt>
                <dd className="font-medium text-text">{bootstrap.data.initialized ? 'Yes' : 'No'}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Admin username</dt>
                <dd className="font-medium text-text">{bootstrap.data.admin_username ?? '—'}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Password changed</dt>
                <dd className="font-medium text-text">{bootstrap.data.must_change_password ? 'Not yet' : 'Yes'}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Published capabilities</dt>
                <dd className="font-medium text-text">{bootstrap.data.published_capabilities}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Active agent tokens</dt>
                <dd className="font-medium text-text">{bootstrap.data.active_agent_tokens}</dd>
              </div>
            </dl>
          ) : null}
          <a
            href="https://github.com/xdrshjr/AragonSkillRouter"
            className="mt-4 inline-flex items-center gap-1 text-xs text-accent hover:underline"
            rel="noreferrer noopener"
            target="_blank"
          >
            Deployment notes
            <ArrowRight aria-hidden className="h-3 w-3" />
          </a>
        </aside>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-4xl px-4 py-10">
          <SkeletonBlock rows={4} />
        </div>
      }
    >
      <LoginScreen />
    </Suspense>
  );
}
