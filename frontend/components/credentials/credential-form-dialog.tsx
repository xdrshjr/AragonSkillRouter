'use client';

import { useEffect, useMemo } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { useCreateCredentialMutation, useUpdateCredentialMutation } from '@/lib/queries';
import { AUTH_TYPES, SECRET_FIELDS, type Credential, type CredentialConfig } from '@/lib/types';
import { fromDateTimeLocalValue, statusLabel, toDateTimeLocalValue } from '@/lib/format';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, FormRow, FormSection } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';

const baseSchema = z.object({
  name: z.string().trim().min(1, 'Name is required.').max(120, 'Use 120 characters or fewer.'),
  auth_type: z.enum(['api_key', 'bearer', 'basic', 'custom_header']),
  status: z.enum(['active', 'disabled', 'expired']),
  header_name: z.string().trim().max(120, 'Use 120 characters or fewer.').optional(),
  prefix: z.string().trim().max(60, 'Use 60 characters or fewer.').optional(),
  value_template: z.string().trim().max(400, 'Use 400 characters or fewer.').optional(),
  test_url: z.string().trim().max(2000, 'URL is too long.').optional(),
  expires_at: z.string().optional(),
  secret_api_key: z.string().optional(),
  secret_token: z.string().optional(),
  secret_username: z.string().optional(),
  secret_password: z.string().optional(),
  secret_value: z.string().optional(),
});

type CredentialFormValues = z.infer<typeof baseSchema>;

function defaultsFor(credential?: Credential | null): CredentialFormValues {
  const config = credential?.config ?? {};
  return {
    name: credential?.name ?? '',
    auth_type: credential?.auth_type ?? 'api_key',
    status: credential?.status ?? 'active',
    header_name: config.header_name ?? '',
    prefix: config.prefix ?? '',
    value_template: config.value_template ?? '',
    test_url: config.test_url ?? '',
    expires_at: toDateTimeLocalValue(credential?.expires_at),
    secret_api_key: '',
    secret_token: '',
    secret_username: '',
    secret_password: '',
    secret_value: '',
  };
}

const SECRET_LABELS: Record<string, string> = {
  api_key: 'API key',
  token: 'Bearer token',
  username: 'Username',
  password: 'Password',
  value: 'Header value',
};

function buildSecret(values: CredentialFormValues): Record<string, string> {
  const secret: Record<string, string> = {};
  if (values.auth_type === 'api_key' && values.secret_api_key?.trim()) secret.api_key = values.secret_api_key.trim();
  if (values.auth_type === 'bearer' && values.secret_token?.trim()) secret.token = values.secret_token.trim();
  if (values.auth_type === 'basic') {
    if (values.secret_username?.trim()) secret.username = values.secret_username.trim();
    if (values.secret_password) secret.password = values.secret_password;
  }
  if (values.auth_type === 'custom_header' && values.secret_value) secret.value = values.secret_value;
  return secret;
}

function buildConfig(values: CredentialFormValues, isEdit: boolean): CredentialConfig | null {
  const config: CredentialConfig = {};
  if (values.auth_type === 'api_key') {
    if (values.header_name?.trim()) config.header_name = values.header_name.trim();
    if (values.prefix?.trim()) config.prefix = values.prefix.trim();
  }
  if (values.auth_type === 'custom_header') {
    if (values.header_name?.trim()) config.header_name = values.header_name.trim();
    if (values.value_template?.trim()) config.value_template = values.value_template.trim();
  }
  if (values.test_url?.trim()) config.test_url = values.test_url.trim();
  if (Object.keys(config).length === 0) return isEdit ? {} : null;
  return config;
}

export interface CredentialFormDialogProps {
  open: boolean;
  onClose: () => void;
  providerId: string;
  providerName?: string;
  /** Editing an existing credential; auth type is immutable server-side. */
  credential?: Credential | null;
  onSaved?: (credential: Credential) => void;
}

export function CredentialFormDialog({
  open,
  onClose,
  providerId,
  providerName,
  credential,
  onSaved,
}: CredentialFormDialogProps) {
  const toast = useToast();
  const isEdit = Boolean(credential);
  const create = useCreateCredentialMutation();
  const update = useUpdateCredentialMutation(credential?.id ?? '');
  const mutation = isEdit ? update : create;

  const schema = useMemo(
    () =>
      baseSchema.superRefine((values, ctx) => {
        const required = SECRET_FIELDS[values.auth_type];
        if (!isEdit) {
          for (const field of required) {
            const key = `secret_${field}` as keyof CredentialFormValues;
            const value = values[key];
            if (typeof value !== 'string' || !value.trim()) {
              ctx.addIssue({
                code: z.ZodIssueCode.custom,
                path: [key],
                message: `${SECRET_LABELS[field] ?? field} is required for ${statusLabel(values.auth_type)}.`,
              });
            }
          }
        }
        if (values.auth_type === 'custom_header' && !values.header_name?.trim()) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['header_name'],
            message: 'A custom header needs a header name.',
          });
        }
        if (values.test_url && !/^https?:\/\/[^\s]+$/i.test(values.test_url)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['test_url'],
            message: 'Use an http:// or https:// URL.',
          });
        }
        if (values.expires_at && Number.isNaN(new Date(`${values.expires_at}Z`).getTime())) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['expires_at'], message: 'Invalid date/time.' });
        }
      }),
    [isEdit],
  );

  const form = useForm<CredentialFormValues>({
    resolver: zodResolver(schema),
    defaultValues: defaultsFor(credential),
  });

  const authType = form.watch('auth_type');

  useEffect(() => {
    if (open) {
      form.reset(defaultsFor(credential));
    }
  }, [open, credential, form]);

  const submit = form.handleSubmit(async (values) => {
    const secret = buildSecret(values);
    const config = buildConfig(values, isEdit);
    const expires_at = fromDateTimeLocalValue(values.expires_at);
    try {
      if (isEdit && credential) {
        const payload: {
          name: string;
          config: CredentialConfig | null;
          expires_at: string | null;
          status: 'active' | 'disabled' | 'expired';
          secret?: Record<string, string>;
        } = {
          name: values.name.trim(),
          config,
          expires_at,
          status: values.status,
        };
        if (Object.keys(secret).length > 0) payload.secret = secret;
        const saved = await update.mutateAsync(payload);
        toast.success({ title: 'Credential updated', description: saved.name });
        onSaved?.(saved);
      } else {
        const saved = await create.mutateAsync({
          provider_id: providerId,
          name: values.name.trim(),
          auth_type: values.auth_type,
          secret,
          config,
          expires_at,
          status: values.status,
        });
        toast.success({
          title: 'Credential created',
          description: `${saved.name} — secret stored encrypted, only a hint is shown afterwards.`,
        });
        onSaved?.(saved);
      }
      onClose();
    } catch (error) {
      toast.apiError(error, isEdit ? 'Credential was not updated' : 'Credential was not created');
    }
  });

  const error = mutation.error;
  const secretFields = SECRET_FIELDS[authType];
  const primarySecretField = secretFields[0] ?? '';
  const primarySecretError =
    authType === 'api_key'
      ? form.formState.errors.secret_api_key?.message
      : authType === 'bearer'
        ? form.formState.errors.secret_token?.message
        : form.formState.errors.secret_value?.message;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={isEdit ? `Edit credential ${credential?.name ?? ''}` : 'New credential'}
      description={
        providerName
          ? `Attached to ${providerName}. Secrets are write-only: the API only returns a masked hint.`
          : 'Secrets are write-only: the API only returns a masked hint.'
      }
      size="lg"
      dismissible={!mutation.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={mutation.isPending}>
            {isEdit ? 'Save credential' : 'Create credential'}
          </Button>
        </>
      }
    >
      <form className="space-y-5" onSubmit={submit} noValidate>
        {error ? (
          <Banner
            tone="danger"
            title={isEdit ? 'Credential was not updated' : 'Credential was not created'}
            actions={errorActions(error)}
            requestId={errorRequestId(error)}
          >
            {describeError(error)}
          </Banner>
        ) : null}

        <FormRow columns={2}>
          <Field label="Name" htmlFor="credential-name" required error={form.formState.errors.name?.message}>
            <Input
              id="credential-name"
              placeholder="Primary production key"
              invalid={Boolean(form.formState.errors.name)}
              {...form.register('name')}
            />
          </Field>
          <Field
            label="Authentication type"
            htmlFor="credential-auth-type"
            required
            hint={isEdit ? 'Cannot change after creation — create a new credential instead.' : undefined}
          >
            <Select
              id="credential-auth-type"
              disabled={isEdit}
              options={AUTH_TYPES.map((value) => ({ value, label: statusLabel(value) }))}
              {...form.register('auth_type')}
            />
          </Field>
        </FormRow>

        <FormSection
          title="Secret"
          description={
            isEdit
              ? 'Leave the fields blank to keep the stored secret. Any value you type replaces it.'
              : 'Exactly the fields this auth type needs; the API rejects extras.'
          }
        >
          {authType === 'basic' ? (
            <FormRow columns={2}>
              <Field
                label="Username"
                htmlFor="credential-secret-username"
                required={!isEdit}
                error={form.formState.errors.secret_username?.message}
              >
                <Input
                  id="credential-secret-username"
                  autoComplete="off"
                  invalid={Boolean(form.formState.errors.secret_username)}
                  {...form.register('secret_username')}
                />
              </Field>
              <Field
                label="Password"
                htmlFor="credential-secret-password"
                required={!isEdit}
                error={form.formState.errors.secret_password?.message}
              >
                <Input
                  id="credential-secret-password"
                  type="password"
                  autoComplete="new-password"
                  invalid={Boolean(form.formState.errors.secret_password)}
                  {...form.register('secret_password')}
                />
              </Field>
            </FormRow>
          ) : (
            <Field
              label={SECRET_LABELS[primarySecretField] ?? 'Secret value'}
              htmlFor={`credential-secret-${primarySecretField}`}
              required={!isEdit}
              hint={isEdit ? 'Blank keeps the current value.' : 'Stored encrypted; never returned by the API.'}
              error={primarySecretError}
            >
              <Input
                id={`credential-secret-${primarySecretField}`}
                type={primarySecretField === 'value' ? 'text' : 'password'}
                autoComplete="new-password"
                {...(primarySecretField === 'api_key'
                  ? form.register('secret_api_key')
                  : primarySecretField === 'token'
                    ? form.register('secret_token')
                    : form.register('secret_value'))}
              />
            </Field>
          )}
        </FormSection>

        <FormSection title="Request shaping" description="How the secret is turned into wire headers.">
          <FormRow columns={2}>
            <Field
              label={authType === 'custom_header' ? 'Header name' : 'API key header name'}
              htmlFor="credential-header-name"
              error={form.formState.errors.header_name?.message}
              hint={authType === 'custom_header' ? 'e.g. x-auth' : 'Defaults to x-api-key when empty.'}
            >
              <Input id="credential-header-name" placeholder="x-api-key" {...form.register('header_name')} />
            </Field>
            <Field
              label={authType === 'custom_header' ? 'Value template' : 'Value prefix'}
              htmlFor="credential-template"
              error={
                (authType === 'custom_header'
                  ? form.formState.errors.value_template
                  : form.formState.errors.prefix
                )?.message
              }
              hint={
                authType === 'custom_header'
                  ? 'Use {value} for the secret, e.g. "Key {value}".'
                  : 'e.g. "Bearer" — prepended with a space.'
              }
            >
              <Input
                id="credential-template"
                placeholder={authType === 'custom_header' ? 'Token {value}' : 'Bearer'}
                {...(authType === 'custom_header'
                  ? form.register('value_template')
                  : form.register('prefix'))}
              />
            </Field>
          </FormRow>
          <Field
            label="Probe URL"
            htmlFor="credential-test-url"
            error={form.formState.errors.test_url?.message}
            hint="Used by “Verify”. Falls back to the provider base URL when empty."
          >
            <Input
              id="credential-test-url"
              placeholder="https://api.example.com/v1/models"
              invalid={Boolean(form.formState.errors.test_url)}
              {...form.register('test_url')}
            />
          </Field>
        </FormSection>

        <FormRow columns={2}>
          <Field
            label="Expires at (UTC)"
            htmlFor="credential-expires"
            error={form.formState.errors.expires_at?.message}
            hint="Optional. Expiring credentials are flagged on the overview."
          >
            <Input id="credential-expires" type="datetime-local" {...form.register('expires_at')} />
          </Field>
          <Field label="Status" htmlFor="credential-status" hint="Disabled credentials are skipped by routing.">
            <Select
              id="credential-status"
              options={[
                { value: 'active', label: 'Active' },
                { value: 'disabled', label: 'Disabled' },
                { value: 'expired', label: 'Expired' },
              ]}
              {...form.register('status')}
            />
          </Field>
        </FormRow>

        {isEdit && credential ? (
          <InlineAlert tone="info">
            Stored hint: <span className="font-mono">{credential.hint}</span> · fields:{' '}
            {credential.field_names.join(', ') || '—'}
          </InlineAlert>
        ) : null}
      </form>
    </Dialog>
  );
}
