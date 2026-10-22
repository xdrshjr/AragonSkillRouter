'use client';

import { useEffect, useMemo } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { useCapabilitiesQuery, useCredentialsQuery, useCreateCapabilityMutation, useProvidersQuery } from '@/lib/queries';
import { CAPABILITY_TYPES, type CapabilityDetail, type CapabilityType } from '@/lib/types';
import { statusLabel } from '@/lib/format';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog } from '@/components/ui/dialog';
import { Field, FormRow } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';

const schema = z.object({
  name: z.string().trim().min(1, 'Name is required.').max(160, 'Use 160 characters or fewer.'),
  slug: z
    .string()
    .trim()
    .max(80, 'Use 80 characters or fewer.')
    .refine((value) => value === '' || /^[a-z0-9._-]+$/.test(value), {
      message: 'Use lowercase letters, digits, ".", "_" or "-".',
    }),
  type: z.enum(['text', 'image', 'video', 'speech', 'transcription', 'generic']),
  description: z.string().max(4000, 'Use 4000 characters or fewer.'),
  provider_id: z.string(),
  credential_id: z.string(),
  needs_live_agent: z.boolean(),
});

type FormValues = z.infer<typeof schema>;

export interface CapabilityFormDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (capability: CapabilityDetail) => void;
  defaultProviderId?: string;
}

/** Creates the capability and its draft v1 (POST /api/admin/capabilities). */
export function CapabilityFormDialog({ open, onClose, onCreated, defaultProviderId }: CapabilityFormDialogProps) {
  const toast = useToast();
  const create = useCreateCapabilityMutation();
  const providers = useProvidersQuery({ limit: 200 });
  const existing = useCapabilitiesQuery({ limit: 200 });

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      slug: '',
      type: 'generic',
      description: '',
      provider_id: defaultProviderId ?? '',
      credential_id: '',
      needs_live_agent: false,
    },
  });

  const providerId = form.watch('provider_id');
  const credentials = useCredentialsQuery({ provider_id: providerId || undefined, limit: 200 }, Boolean(providerId));

  useEffect(() => {
    if (open) {
      form.reset({
        name: '',
        slug: '',
        type: 'generic',
        description: '',
        provider_id: defaultProviderId ?? '',
        credential_id: '',
        needs_live_agent: false,
      });
    }
  }, [open, defaultProviderId, form]);

  const providerOptions = useMemo(
    () => (providers.data?.items ?? []).map((provider) => ({ value: provider.id, label: provider.name })),
    [providers.data?.items],
  );

  const credentialOptions = useMemo(
    () =>
      (credentials.data?.items ?? []).map((credential) => ({
        value: credential.id,
        label: `${credential.name} (${statusLabel(credential.auth_type)}, ${statusLabel(credential.status)})`,
      })),
    [credentials.data?.items],
  );

  const submit = form.handleSubmit(async (values) => {
    const trimmedSlug = values.slug.trim();
    try {
      const created = await create.mutateAsync({
        name: values.name.trim(),
        ...(trimmedSlug ? { slug: trimmedSlug } : {}),
        type: values.type as CapabilityType,
        description: values.description.trim(),
        provider_id: values.provider_id || null,
        credential_id: values.credential_id || null,
      });
      toast.success({
        title: 'Capability created',
        description: `${created.slug} — draft v1 is open for configuration.`,
      });
      onCreated?.(created);
      onClose();
    } catch (error) {
      toast.apiError(error, 'Capability was not created');
    }
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New capability"
      description="Creates the capability and a draft version 1. Publishing needs a real passing test first."
      size="lg"
      dismissible={!create.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={create.isPending}>
            Create capability
          </Button>
        </>
      }
    >
      <form className="space-y-4" onSubmit={submit} noValidate>
        {create.isError ? (
          <Banner
            tone="danger"
            title="Capability was not created"
            actions={errorActions(create.error)}
            requestId={errorRequestId(create.error)}
          >
            {describeError(create.error)}
          </Banner>
        ) : null}

        <FormRow columns={2}>
          <Field label="Name" htmlFor="capability-name" required error={form.formState.errors.name?.message}>
            <Input
              id="capability-name"
              placeholder="Summarize text"
              invalid={Boolean(form.formState.errors.name)}
              {...form.register('name')}
            />
          </Field>
          <Field
            label="Slug"
            htmlFor="capability-slug"
            hint="Optional. Generated from the name when empty; agents call the slug."
            error={form.formState.errors.slug?.message}
          >
            <Input
              id="capability-slug"
              placeholder="text.summarize"
              invalid={Boolean(form.formState.errors.slug)}
              {...form.register('slug')}
            />
          </Field>
        </FormRow>

        <FormRow columns={2}>
          <Field label="Type" htmlFor="capability-type" required hint="Drives default config and validation.">
            <Select
              id="capability-type"
              options={CAPABILITY_TYPES.map((value) => ({ value, label: statusLabel(value) }))}
              {...form.register('type')}
            />
          </Field>
          <Field
            label="Provider"
            htmlFor="capability-provider"
            hint="Optional at creation; routing needs one later."
            error={form.formState.errors.provider_id?.message}
          >
            <Select
              id="capability-provider"
              placeholder="No provider"
              options={providerOptions}
              {...form.register('provider_id')}
            />
          </Field>
        </FormRow>

        <Field
          label="Credential"
          htmlFor="capability-credential"
          hint={
            providerId
              ? 'Only credentials of the selected provider are listed.'
              : 'Pick a provider first to list its credentials.'
          }
          error={form.formState.errors.credential_id?.message}
        >
          <Select
            id="capability-credential"
            placeholder="No credential"
            disabled={!providerId || credentials.isLoading}
            options={credentialOptions}
            {...form.register('credential_id')}
          />
        </Field>

        <Field
          label="Description"
          htmlFor="capability-description"
          hint="Shown to agents in the discovered capability descriptor."
          error={form.formState.errors.description?.message}
        >
          <Textarea id="capability-description" rows={3} {...form.register('description')} />
        </Field>

        <Checkbox
          label="Needs live agent reasoning"
          hint="Only set this when the capability itself depends on the agent engine."
          {...form.register('needs_live_agent')}
        />

        <Field
          label="Existing slugs"
          htmlFor="capability-existing"
          hint="Slugs are unique platform-wide; the API rejects duplicates."
        >
          <p id="capability-existing" className="text-xs text-muted">
            {(existing.data?.items ?? [])
              .slice(0, 8)
              .map((item) => item.slug)
              .join(', ') || 'none yet'}
          </p>
        </Field>
      </form>
    </Dialog>
  );
}
