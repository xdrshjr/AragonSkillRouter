'use client';

import { useState } from 'react';
import Link from 'next/link';
import { LogOut, ShieldCheck } from 'lucide-react';

import { useSession } from '@/lib/auth';
import { formatDateTime } from '@/lib/format';
import { ChangePasswordForm } from '@/components/account/change-password-form';
import { Badge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { Field, FormRow } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { PageHeader } from '@/components/ui/page-header';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { useLogoutMutation } from '@/lib/queries';

export default function AccountPage() {
  const toast = useToast();
  const { user, session, mustChangePassword } = useSession();
  const logout = useLogoutMutation();
  const [changing, setChanging] = useState(false);

  const signOut = async () => {
    try {
      await logout.mutateAsync();
      toast.success({ title: 'Signed out', description: 'The session cookie was cleared on the server.' });
      window.location.assign('/login');
    } catch (cause) {
      toast.apiError(cause, 'Sign out failed');
    }
  };

  if (!user) {
    return (
      <div className="space-y-4">
        <PageHeader title="Account" />
        <Banner tone="warning" title="No session">
          The session probe returned no user; reload the page or sign in again.
        </Banner>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Account"
        description="Your operator profile, password and session. Credentials for providers live under Providers; agent tokens live under Access."
        meta={
          <>
            <Badge tone="info" dot={false}>
              role {user.role}
            </Badge>
            {mustChangePassword ? (
              <Badge tone="warning" dot={false}>
                password change required
              </Badge>
            ) : null}
          </>
        }
        actions={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => void signOut()}
            loading={logout.isPending}
            iconLeft={<LogOut aria-hidden className="h-3.5 w-3.5" />}
          >
            Sign out
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Profile" description="The single administrator account this deployment created." />
          <CardBody className="space-y-2 text-sm">
            <FormRow columns={2}>
              <Field label="Username" htmlFor="account-username">
                <Input id="account-username" value={user.username} readOnly disabled />
              </Field>
              <Field label="User id" htmlFor="account-id">
                <Input id="account-id" value={user.id} readOnly disabled className="font-mono" />
              </Field>
            </FormRow>
            <p className="text-xs text-muted">
              created {formatDateTime(user.created_at)} · password last changed{' '}
              {user.password_changed_at ? <TimeAgo value={user.password_changed_at} /> : 'never'}
            </p>
            <p className="text-xs text-muted">
              The profile has no editable fields in this build: there is no endpoint to rename the administrator, and the
              UI does not pretend otherwise.
            </p>
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Session"
            description="Cookie sessions are same-origin; the CSRF token is echoed from the sr_csrf cookie on every mutation."
            actions={session?.csrf_token ? <Badge tone="success" dot={false}>CSRF present</Badge> : null}
          />
          <CardBody className="space-y-2 text-xs">
            <p>
              <span className="text-muted">Signed in as </span>
              {user.username} ({user.role})
            </p>
            <p>
              <span className="text-muted">Bootstrap completed: </span>
              {session?.initialized ? 'yes' : 'no'}
            </p>
            <p>
              <span className="text-muted">Must change password: </span>
              {mustChangePassword ? 'yes' : 'no'}
            </p>
            <p className="text-muted">
              Session lifetime and idle timeout are configured in{' '}
              <Link href="/settings" className="hover:underline">
                Settings → Session and login security
              </Link>
              .
            </p>
          </CardBody>
          <CardFooter>
            <span className="inline-flex items-center gap-1 text-[11px] text-muted">
              <ShieldCheck aria-hidden className="h-3 w-3" />
              Signing out clears the server session; closing the tab does not.
            </span>
          </CardFooter>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Change password"
          description="Changing the password revokes every other session. This browser stays signed in with a fresh CSRF token."
          actions={
            <Button size="sm" variant="secondary" onClick={() => setChanging((value) => !value)}>
              {changing ? 'Hide the form' : 'Change password'}
            </Button>
          }
        />
        <CardBody>
          {changing ? (
            <ChangePasswordForm onSuccess={() => setChanging(false)} />
          ) : (
            <p className="text-xs text-muted">
              The form is the same component the first-run flow and the shell banner use, so there is exactly one
              password policy in the app.
            </p>
          )}
        </CardBody>
        <CardFooter>
          <span className="text-[11px] text-muted">
            There is no &quot;sign out everywhere&quot; button: the platform revokes other sessions when the password
            changes, which is the only operation that must invalidate them all.
          </span>
        </CardFooter>
      </Card>
    </div>
  );
}
