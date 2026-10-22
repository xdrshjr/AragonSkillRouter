'use client';

/**
 * Agent model configuration.
 *
 * This used to be a read-only card that told the user to call the API with curl —
 * exactly the dead end someone reaches when the agent panel says "No model is
 * configured for the agent". Naming a model is first-run configuration, so it has to
 * be doable here:
 *
 *   pick the credential  →  pick the model  →  save  →  see the resolved engine view
 *
 * The model list comes from the provider itself
 * (`POST /api/admin/providers/{id}/models`), so the id a user picks is one the
 * provider actually serves. Typing an id stays possible for providers that do not
 * expose a list.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Check, Download, RefreshCw, Save } from 'lucide-react';

import { api, describeError, errorRequestId } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { Field, FormRow } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import type { ListEnvelope } from '@/lib/types';
import { useUpdateSettingsMutation, type EffectiveSettings } from './api';

interface CredentialRow {
  id: string;
  name: string;
  provider_id: string;
  provider_name?: string | null;
  auth_type: string;
  status: string;
  hint: string;
}

interface ProviderRow {
  id: string;
  name: string;
  category: string;
  base_url?: string | null;
}

interface ModelListResponse {
  items: string[];
  source_url: string;
  count: number;
}

const ADAPTER_OPTIONS = [
  { value: '', label: 'Detect from the base URL (recommended)' },
  { value: 'anthropic', label: 'anthropic — /v1/messages protocol' },
  { value: 'openai', label: 'openai — /chat/completions protocol (gateways)' },
  { value: 'google', label: 'google — Gemini generateContent' },
];

export function AgentModelSection({ settings }: { settings: EffectiveSettings }) {
  const resolved = settings.agent.model_resolved;
  const configured = resolved?.configured === true;
  const toast = useToast();
  const mutation = useUpdateSettingsMutation();

  const credentialsQuery = useQuery({
    queryKey: ['settings', 'llm-credentials'],
    queryFn: () =>
      api.get<ListEnvelope<CredentialRow>>('/api/admin/credentials', {
        query: { status: 'active', limit: 200 },
      }),
    staleTime: 30_000,
  });
  const providersQuery = useQuery({
    queryKey: ['settings', 'llm-providers'],
    queryFn: () =>
      api.get<ListEnvelope<ProviderRow>>('/api/admin/providers', {
        query: { category: 'llm', limit: 200 },
      }),
    staleTime: 60_000,
  });

  const providerById = useMemo(() => {
    const map = new Map<string, ProviderRow>();
    for (const provider of providersQuery.data?.items ?? []) map.set(provider.id, provider);
    return map;
  }, [providersQuery.data]);

  const candidates = useMemo(
    () =>
      (credentialsQuery.data?.items ?? []).filter((credential) => providerById.has(credential.provider_id)),
    [credentialsQuery.data, providerById],
  );

  const [credentialId, setCredentialId] = useState(settings.agent.model.credential_id ?? '');
  const [modelId, setModelId] = useState(settings.agent.model.model_id ?? '');
  const [adapter, setAdapter] = useState(settings.agent.model.provider_id ?? '');
  const [baseUrl, setBaseUrl] = useState(settings.agent.model.base_url ?? '');
  const [models, setModels] = useState<string[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Adopt the persisted values when they arrive, or change underneath us.
  useEffect(() => {
    setCredentialId(settings.agent.model.credential_id ?? '');
    setModelId(settings.agent.model.model_id ?? '');
    setAdapter(settings.agent.model.provider_id ?? '');
    setBaseUrl(settings.agent.model.base_url ?? '');
  }, [
    settings.agent.model.credential_id,
    settings.agent.model.model_id,
    settings.agent.model.provider_id,
    settings.agent.model.base_url,
  ]);

  // With exactly one LLM credential there is nothing to choose: preselect it, because
  // an empty select gives the user nothing to act on.
  useEffect(() => {
    if (!credentialId && candidates.length === 1) setCredentialId(candidates[0].id);
  }, [candidates, credentialId]);

  const selectedCredential = candidates.find((credential) => credential.id === credentialId) ?? null;
  const selectedProvider = selectedCredential ? providerById.get(selectedCredential.provider_id) ?? null : null;

  const dirty =
    credentialId !== (settings.agent.model.credential_id ?? '') ||
    modelId !== (settings.agent.model.model_id ?? '') ||
    adapter !== (settings.agent.model.provider_id ?? '') ||
    baseUrl !== (settings.agent.model.base_url ?? '');

  const discover = useMutation({
    mutationFn: (providerId: string) =>
      api.post<ModelListResponse>(`/api/admin/providers/${providerId}/models`, {}),
    onSuccess: (data) => {
      setModels(data.items);
      setModelsError(null);
      toast.success({ title: `${data.count} model(s) available`, description: `Read from ${data.source_url}` });
      if (!modelId && data.items.length > 0) setModelId(data.items.find((id) => id.includes('flash')) ?? data.items[0]);
    },
    onError: (error) => {
      setModels([]);
      setModelsError(describeError(error));
    },
  });

  const fetchModels = useCallback(() => {
    if (!selectedCredential) return;
    discover.mutate(selectedCredential.provider_id);
  }, [discover, selectedCredential]);

  const save = async () => {
    try {
      const response = await mutation.mutateAsync({
        agent: {
          model: {
            credential_id: credentialId,
            model_id: modelId.trim(),
            // "" means "let the platform infer it from the base URL".
            provider_id: adapter,
            base_url: baseUrl.trim(),
          },
        },
      });
      const next = response.settings.agent.model_resolved;
      toast.success({
        title: 'Agent model saved',
        description: next?.configured
          ? `The agent will run on ${next.provider_id} / ${next.model_id}.`
          : 'Saved, but the platform still cannot resolve a model — see the message above.',
      });
    } catch (error) {
      toast.apiError(error, 'Agent model was not saved');
    }
  };

  const credentialOptions = [
    { value: '', label: '— choose a credential —' },
    ...candidates.map((credential) => ({
      value: credential.id,
      label: `${credential.provider_name ?? 'provider'} · ${credential.name} (${credential.auth_type})`,
    })),
  ];

  return (
    <Card>
      <CardHeader
        title="Agent model"
        description="Which model the built-in agent runs on. A model is referenced by provider and credential; no key is ever rendered here."
        actions={<Badge tone={configured ? 'success' : 'warning'}>{configured ? 'configured' : 'not configured'}</Badge>}
      />
      <CardBody className="space-y-5">
        {configured ? (
          <div className="rounded-control border border-border bg-surface-muted px-3 py-2">
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone="success" dot={false}>
                <Check aria-hidden className="h-3 w-3" /> resolving
              </Badge>
              <span className="font-mono text-xs">
                {resolved?.provider_id} / {resolved?.model_id}
              </span>
            </p>
            <p className="mt-1 text-xs text-muted">
              credential {resolved?.credential_name ?? resolved?.credential_id ?? '—'}
              {resolved?.base_url ? ` · ${resolved.base_url}` : ''}
            </p>
          </div>
        ) : (
          <Banner tone="warning" title="No model resolves, so agent runs cannot start">
            <span className="block">{resolved?.error?.message ?? 'Choose a model below and save.'}</span>
            {resolved?.error?.actions?.length ? (
              <ul className="mt-1 list-disc pl-4 text-xs">
                {resolved.error.actions.map((action) => (
                  <li key={action}>{action}</li>
                ))}
              </ul>
            ) : null}
          </Banner>
        )}

        {credentialsQuery.isLoading || providersQuery.isLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : candidates.length === 0 ? (
          <InlineAlert tone="warning">
            No active credential exists on an active provider with category <code>llm</code>. Create one under
            Providers &amp; credentials first; it will appear here automatically.
          </InlineAlert>
        ) : (
          <div className="space-y-4">
            <FormRow columns={2}>
              <Field
                label="Credential"
                hint="Only active credentials on an llm-category provider can run the agent."
                required
                htmlFor="agent-credential"
              >
                <Select
                  id="agent-credential"
                  options={credentialOptions}
                  value={credentialId}
                  onChange={(event) => {
                    setCredentialId(event.target.value);
                    setModels([]);
                    setModelsError(null);
                  }}
                />
              </Field>
              <Field
                label="Model id"
                hint={
                  selectedProvider
                    ? `An id ${selectedProvider.name} serves — from its documentation or the button below.`
                    : 'The provider’s model id, e.g. glm-5.3-flash or gpt-4o-mini.'
                }
                required
                htmlFor="agent-model-id"
              >
                <div className="flex gap-2">
                  <Input
                    id="agent-model-id"
                    value={modelId}
                    onChange={(event) => setModelId(event.target.value)}
                    placeholder="glm-5.3-flash"
                    list="agent-model-options"
                    spellCheck={false}
                    className="flex-1"
                  />
                  <datalist id="agent-model-options">
                    {models.map((model) => (
                      <option key={model} value={model} />
                    ))}
                  </datalist>
                  <Button
                    size="sm"
                    variant="secondary"
                    iconLeft={<Download aria-hidden className="h-3.5 w-3.5" />}
                    loading={discover.isPending}
                    disabled={!selectedCredential}
                    onClick={fetchModels}
                  >
                    Fetch models
                  </Button>
                </div>
              </Field>
            </FormRow>

            {models.length > 0 ? (
              <p className="text-xs text-muted">
                {models.length} model(s) reported by the provider:{' '}
                <span className="font-mono">{models.slice(0, 6).join(', ')}</span>
                {models.length > 6 ? ` … +${models.length - 6}` : ''}
              </p>
            ) : null}
            {modelsError ? (
              <InlineAlert tone="warning">
                Could not read the provider’s model list: {modelsError}. Type the id manually — the provider’s
                documentation is authoritative.
              </InlineAlert>
            ) : null}

            <FormRow columns={2}>
              <Field
                label="Protocol adapter"
                hint="Leave on detection unless the provider’s base URL does not reveal its protocol."
                htmlFor="agent-adapter"
              >
                <Select
                  id="agent-adapter"
                  options={ADAPTER_OPTIONS}
                  value={adapter}
                  onChange={(event) => setAdapter(event.target.value)}
                />
              </Field>
              <Field
                label="Base URL override"
                hint={selectedProvider?.base_url ? `Empty uses ${selectedProvider.base_url}` : 'Empty uses the provider’s address.'}
                htmlFor="agent-base-url"
              >
                <Input
                  id="agent-base-url"
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder={selectedProvider?.base_url ?? 'https://…'}
                  spellCheck={false}
                />
              </Field>
            </FormRow>
          </div>
        )}

        <p className="text-xs text-muted">
          The protocol comes from this setting, then from the provider’s base URL. A wrong adapter fails as a 404 that
          looks like a bad key, so detection is the safe default.
        </p>
      </CardBody>
      <CardFooter className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted">{dirty ? 'Unsaved changes.' : 'Matches the saved setting.'}</span>
        <span className="flex gap-2">
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
            onClick={() => {
              setCredentialId(settings.agent.model.credential_id ?? '');
              setModelId(settings.agent.model.model_id ?? '');
              setAdapter(settings.agent.model.provider_id ?? '');
              setBaseUrl(settings.agent.model.base_url ?? '');
            }}
            disabled={!dirty}
          >
            Reset
          </Button>
          <Button
            size="sm"
            variant="primary"
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
            loading={mutation.isPending}
            disabled={!dirty || !credentialId || !modelId.trim()}
            onClick={() => void save()}
          >
            Save agent model
          </Button>
        </span>
      </CardFooter>
      {mutation.isError ? (
        <CardBody className="pt-0">
          <Banner tone="danger" title="Not saved" requestId={errorRequestId(mutation.error)}>
            {describeError(mutation.error)} Nothing was persisted.
          </Banner>
        </CardBody>
      ) : null}
    </Card>
  );
}
