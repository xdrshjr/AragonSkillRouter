'use client';

import { useEffect } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { useCreateProviderMutation, useUpdateProviderMutation } from '@/lib/queries';
import { PROVIDER_CATEGORIES, PROVIDER_ENVIRONMENTS, type Provider, type ProviderInput } from '@/lib/types';
import { statusLabel } from '@/lib/format';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, FormRow } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';

const httpUrl = z
  .string()
  .trim()
  .max(2000, 'URL is too long.')
  .refine((value) => value === '' || /^https?:\/\/[^\s]+$/i.test(value), {
    message: 'Use an http:// or https:// URL, or leave it empty.',
  });

const providerSchema = z.object({
  name: z.string().trim().min(1, 'Name is required.').max(120, 'Use 120 characters or fewer.'),
  category: z.enum(['llm', 'image', 'video', 'speech', 'transcription', 'generic']),
  environment: z.enum(['production', 'staging', 'development']),
  status: z.enum(['active', 'disabled']),
  base_url: httpUrl,
  docs_url: httpUrl,
  tags: z.string().max(400, 'Keep tags short — 20 tags of 40 characters each.').optional(),
  notes: z.string().max(4000, 'Use 4000 characters or fewer.').optional(),
});

type ProviderFormValues = z.infer<typeof providerSchema>;

function toFormValues(provider?: Provider | null): ProviderFormValues {
  return {
    name: provider?.name ?? '',
    category: provider?.category ?? 'llm',
    environment: provider?.environment ?? 'production',
    status: provider?.status ?? 'active',
    base_url: provider?.base_url ?? '',
    docs_url: provider?.docs_url ?? '',
    tags: (provider?.tags ?? []).join(', '),
    notes: provider?.notes ?? '',
  };
}

function splitTags(value: string | undefined): string[] {
  if (!value) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of value.split(',')) {
    const tag = raw.trim();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag.slice(0, 40));
  }
  return out.slice(0, 20);
}

export interface ProviderFormDialogProps {
  open: boolean;
  onClose: () => void;
  /** `null`/omitted creates a new provider; otherwise the dialog edits it. */
  provider?: Provider | null;
  onSaved?: (provider: Provider) => void;
}

export function ProviderFormDialog({ open, onClose, provider, onSaved }: ProviderFormDialogProps) {
  const toast = useToast();
  const create = useCreateProviderMutation();
  const update = useUpdateProviderMutation(provider?.id ?? '');
  const isEdit = Boolean(provider);
  const mutation = isEdit ? update : create;

  const form = useForm<ProviderFormValues>({
    resolver: zodResolver(providerSchema),
    defaultValues: toFormValues(provider),
  });

  useEffect(() => {
    if (open) form.reset(toFormValues(provider));
  }, [open, provider, form]);

  const submit = form.handleSubmit(async (values) => {
    const payload: ProviderInput = {
      name: values.name.trim(),
      category: values.category,
      environment: values.environment,
      status: values.status,
      base_url: values.base_url.trim() || null,
      docs_url: values.docs_url.trim() || null,
      tags: splitTags(values.tags),
      notes: values.notes?.trim() || null,
    };
    try {
      const saved = isEdit
        ? await update.mutateAsync(payload)
        : await create.mutateAsync(payload);
      toast.success({
        title: isEdit ? 'Provider updated' : 'Provider created',
        description: saved.name,
      });
      onSaved?.(saved);
      onClose();
    } catch (error) {
      toast.apiError(error, isEdit ? 'Provider was not updated' : 'Provider was not created');
    }
  });

  const error = mutation.error;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={isEdit ? `Edit ${provider?.name ?? 'provider'}` : 'New provider'}
      description="A provider is the upstream service a capability calls. Credentials attach to it."
      size="lg"
      dismissible={!mutation.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            loading={mutation.isPending}
            disabled={!form.formState.isDirty && isEdit}
          >
            {isEdit ? 'Save changes' : 'Create provider'}
          </Button>
        </>
      }
    >
      <form className="space-y-4" onSubmit={submit} noValidate>
        {error ? (
          <Banner
            tone="danger"
            title={isEdit ? 'Provider was not updated' : 'Provider was not created'}
            actions={errorActions(error)}
            requestId={errorRequestId(error)}
          >
            {describeError(error)}
          </Banner>
        ) : null}

        <FormRow columns={2}>
          <Field
            label="Name"
            htmlFor="provider-name"
            required
            error={form.formState.errors.name?.message}
            hint="Shown in pickers and tables."
          >
            <Input id="provider-name" invalid={Boolean(form.formState.errors.name)} {...form.register('name')} />
          </Field>
          <Field label="Category" htmlFor="provider-category" required error={form.formState.errors.category?.message}>
            <Select
              id="provider-category"
              options={PROVIDER_CATEGORIES.map((value) => ({ value, label: statusLabel(value) }))}
              {...form.register('category')}
            />
          </Field>
        </FormRow>

        <FormRow columns={2}>
          <Field label="Environment" htmlFor="provider-environment" required>
            <Select
              id="provider-environment"
              options={PROVIDER_ENVIRONMENTS.map((value) => ({ value, label: statusLabel(value) }))}
              {...form.register('environment')}
            />
          </Field>
          <Field label="Status" htmlFor="provider-status" required hint="Disabled providers stay configured but are skipped.">
            <Select
              id="provider-status"
              options={[
                { value: 'active', label: 'Active' },
                { value: 'disabled', label: 'Disabled' },
              ]}
              {...form.register('status')}
            />
          </Field>
        </FormRow>

        <FormRow columns={2}>
          <Field
            label="Base URL"
            htmlFor="provider-base-url"
            error={form.formState.errors.base_url?.message}
            hint="API root, e.g. https://api.example.com/v1"
          >
            <Input
              id="provider-base-url"
              placeholder="https://api.example.com/v1"
              invalid={Boolean(form.formState.errors.base_url)}
              {...form.register('base_url')}
            />
          </Field>
          <Field
            label="Docs URL"
            htmlFor="provider-docs-url"
            error={form.formState.errors.docs_url?.message}
            hint="Link the team will need while editing configs."
          >
            <Input
              id="provider-docs-url"
              placeholder="https://docs.example.com"
              invalid={Boolean(form.formState.errors.docs_url)}
              {...form.register('docs_url')}
            />
          </Field>
        </FormRow>

        <Field
          label="Tags"
          htmlFor="provider-tags"
          hint="Comma separated, max 20 tags of 40 characters."
          error={form.formState.errors.tags?.message}
        >
          <Input id="provider-tags" placeholder="llm, cheap, eu" {...form.register('tags')} />
        </Field>

        <Field label="Notes" htmlFor="provider-notes" error={form.formState.errors.notes?.message}>
          <Textarea id="provider-notes" rows={3} placeholder="Rate limits, quota owners, quirks…" {...form.register('notes')} />
        </Field>
      </form>
    </Dialog>
  );
}
