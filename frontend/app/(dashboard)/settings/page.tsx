'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { ArrowRight, Settings2 } from 'lucide-react';

import { api, describeError, errorRequestId } from '@/lib/api';
import type { ListEnvelope } from '@/lib/types';
import { Badge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { PageHeader, SectionHeading } from '@/components/ui/page-header';
import { AgentModelSection } from '@/components/settings/agent-model-section';
import { DesktopSection } from '@/components/settings/desktop-section';
import {
  AgentUpgradeSection,
  AutoExecuteSection,
  FilesSection,
  LoggingSection,
  SecuritySection,
  SettingsLoading,
} from '@/components/settings/settings-sections';
import { useSettingsQuery } from '@/components/settings/api';

export default function SettingsPage() {
  const settings = useSettingsQuery();
  const providers = useQuery<ListEnvelope<{ id: string; name: string; slug: string }>, Error>({
    queryKey: ['settings-provider-options'],
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<{ id: string; name: string; slug: string }>>('/api/admin/providers', {
        query: { limit: 200 },
        signal,
      }),
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="Settings"
        description="Effective runtime values: what the running process would use right now, not a stored draft."
        actions={
          <Link href="/settings/engine">
            <Button size="sm" variant="secondary" iconRight={<ArrowRight aria-hidden className="h-3.5 w-3.5" />}>
              Engine console
            </Button>
          </Link>
        }
      />

      <Banner tone="info" title="Each section saves independently">
        A save shows the keys the server confirmed. A failure shows the error envelope with its request id and leaves your
        input in place — nothing is reported as saved unless the API answered 2xx.
      </Banner>

      {settings.isLoading ? <SettingsLoading /> : null}

      {settings.isError ? (
        <Banner
          tone="danger"
          title="Could not load settings"
          requestId={errorRequestId(settings.error)}
          primaryAction={{ label: 'Retry', onClick: () => void settings.refetch(), loading: settings.isFetching, variant: 'primary' }}
        >
          {describeError(settings.error)}
        </Banner>
      ) : null}

      {settings.data ? (
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Now in effect"
              description="Engine state as the backend reports it."
              actions={
                <Badge tone="neutral" dot={false}>
                  adapter {settings.data.engine.adapter_version}
                </Badge>
              }
            />
            <CardBody className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
              <p>
                <span className="block uppercase tracking-wide text-muted">Sidecar</span>
                <span className="font-mono">{settings.data.engine.sidecar.url}</span>
              </p>
              <p>
                <span className="block uppercase tracking-wide text-muted">Sidecar token</span>
                {settings.data.engine.sidecar.token_configured ? 'configured' : 'missing'}
              </p>
              <p>
                <span className="block uppercase tracking-wide text-muted">Platform tools</span>
                {settings.data.engine.tools.length}
              </p>
              <p>
                <span className="block uppercase tracking-wide text-muted">Editable keys</span>
                {settings.data.editable.length}
              </p>
            </CardBody>
          </Card>

          <DesktopSection />
          <AgentModelSection settings={settings.data.settings} />
          <LoggingSection settings={settings.data.settings} />
          <FilesSection settings={settings.data.settings} />
          <SecuritySection settings={settings.data.settings} />
          <AgentUpgradeSection settings={settings.data.settings} />
          <AutoExecuteSection settings={settings.data.settings} providers={providers.data?.items ?? []} />

          <div className="pt-2">
            <SectionHeading
              title="Engine releases"
              description="Check, validate, activate and roll back the AragonAgent engine."
              actions={
                <Link href="/settings/engine">
                  <Button size="sm" variant="secondary" iconLeft={<Settings2 aria-hidden className="h-3.5 w-3.5" />}>
                    Open the engine console
                  </Button>
                </Link>
              }
            />
          </div>

          {providers.isError ? (
            <Banner tone="warning" title="Could not load providers for the auto-execute allow-list">
              {describeError(providers.error)} The provider checkboxes therefore start empty.
            </Banner>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
