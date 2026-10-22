'use client';

import { useId, useState } from 'react';
import { KeyRound } from 'lucide-react';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { useChangePasswordMutation } from '@/lib/queries';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { useToast } from '@/components/ui/toast';

export interface ChangePasswordFormProps {
  /** Called after the password was changed successfully. */
  onSuccess?: () => void;
  /** Hides the cancel button (e.g. when it is shown inside a dialog). */
  submitLabel?: string;
  compact?: boolean;
}

/**
 * Self-contained "change password" form.
 *
 * It lives here (not on a page) so the `/login` first-run flow and the account
 * menu can both render it without an `/account` page existing yet.
 */
export function ChangePasswordForm({ onSuccess, submitLabel = 'Change password', compact }: ChangePasswordFormProps) {
  const toast = useToast();
  const mutation = useChangePasswordMutation();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [touched, setTouched] = useState(false);
  const ids = useId();

  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;
  const tooShort = newPassword.length > 0 && newPassword.length < 12;
  const unchanged = newPassword.length > 0 && newPassword === currentPassword;
  const formError = mismatch
    ? 'The confirmation does not match the new password.'
    : tooShort
      ? 'Use at least 12 characters.'
      : unchanged
        ? 'Choose a password different from the current one.'
        : null;
  const canSubmit = currentPassword.length > 0 && newPassword.length > 0 && confirmPassword.length > 0 && !formError;

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTouched(true);
    if (!canSubmit || mutation.isPending) return;
    try {
      await mutation.mutateAsync({ current_password: currentPassword, new_password: newPassword });
      toast.success({
        title: 'Password changed',
        description: 'Other sessions were revoked. Use the new password next time you sign in.',
      });
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      onSuccess?.();
    } catch (error) {
      toast.apiError(error, 'Could not change the password');
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      {mutation.isError ? (
        <Banner
          tone="danger"
          title="Password was not changed"
          actions={errorActions(mutation.error)}
          requestId={errorRequestId(mutation.error)}
        >
          {describeError(mutation.error)}
        </Banner>
      ) : null}

      <Field
        label="Current password"
        htmlFor={`${ids}-current`}
        required
        hint="The password you signed in with."
        error={touched && !currentPassword ? 'Enter your current password.' : null}
      >
        <Input
          id={`${ids}-current`}
          type="password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          invalid={touched && !currentPassword}
        />
      </Field>

      <Field
        label="New password"
        htmlFor={`${ids}-new`}
        required
        hint="At least 12 characters. Other sessions are revoked when this succeeds."
        error={touched ? (tooShort ? 'Use at least 12 characters.' : unchanged ? 'Pick a different password.' : null) : null}
      >
        <Input
          id={`${ids}-new`}
          type="password"
          autoComplete="new-password"
          value={newPassword}
          onChange={(event) => setNewPassword(event.target.value)}
          invalid={Boolean(tooShort || unchanged)}
        />
      </Field>

      <Field
        label="Confirm new password"
        htmlFor={`${ids}-confirm`}
        required
        error={mismatch ? 'The confirmation does not match the new password.' : null}
      >
        <Input
          id={`${ids}-confirm`}
          type="password"
          autoComplete="new-password"
          value={confirmPassword}
          onChange={(event) => setConfirmPassword(event.target.value)}
          invalid={mismatch}
        />
      </Field>

      {!compact ? (
        <p className="text-xs text-muted">
          Changing the password revokes every other session. Business endpoints stay blocked until the bootstrap
          password has been replaced.
        </p>
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted">
          {mutation.isError ? 'See the notification for the request id.' : 'Server-side policy is authoritative.'}
        </span>
        <Button type="submit" variant="primary" loading={mutation.isPending} disabled={!canSubmit && touched}>
          <KeyRound aria-hidden className="h-4 w-4" />
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
