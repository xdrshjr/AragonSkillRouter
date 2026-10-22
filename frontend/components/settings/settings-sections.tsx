'use client';

/**
 * `/settings` sections.
 *
 * Each section owns its own local state, its own save button and its own
 * saved/failed indicator, because "saved" and "failed" must never look alike:
 * a successful PATCH renders a success banner naming the keys the server
 * confirmed (`updated[]`), a failed one renders the error envelope with its
 * request id and leaves the form values untouched.
 */

import { useEffect, useState } from 'react';
import { AlertTriangle, Info, Save } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatNumber } from '@/lib/format';
import { Badge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { Checkbox, Switch } from '@/components/ui/checkbox';
import { Field, FormRow } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import type { EffectiveSettings, SettingsPatch, SettingsPayload } from './api';
import { useUpdateSettingsMutation } from './api';

type SaveState = { state: 'idle' } | { state: 'saved'; keys: string[] } | { state: 'failed'; error: unknown };

function SaveFeedback({ result }: { result: SaveState }) {
  if (result.state === 'idle') return null;
  if (result.state === 'saved') {
    return (
      <Banner tone="success" title="Saved — the effective settings changed">
        {result.keys.length === 0 ? 'The server reported no key change.' : `Updated: ${result.keys.join(', ')}`}
      </Banner>
    );
  }
  return (
    <Banner tone="danger" title="Not saved" requestId={errorRequestId(result.error)}>
      {describeError(result.error)} Your input is unchanged in the form; nothing was persisted.
    </Banner>
  );
}

function Section({
  title,
  description,
  children,
  footer,
  badge,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  badge?: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader
        title={title}
        description={description}
        actions={badge ? <Badge tone="neutral" dot={false}>{badge}</Badge> : null}
      />
      <CardBody className="space-y-3">{children}</CardBody>
      {footer ? <CardFooter>{footer}</CardFooter> : null}
    </Card>
  );
}

function useSectionSave() {
  const toast = useToast();
  const mutation = useUpdateSettingsMutation();
  const [result, setResult] = useState<SaveState>({ state: 'idle' });

  const save = async (patch: SettingsPatch, label: string) => {
    try {
      const response = await mutation.mutateAsync(patch);
      setResult({ state: 'saved', keys: response.updated });
      toast.success({
        title: `${label} saved`,
        description: response.updated.length > 0 ? `Updated: ${response.updated.join(', ')}` : 'No key changed on the server.',
      });
    } catch (cause) {
      setResult({ state: 'failed', error: cause });
      toast.apiError(cause, `${label} was not saved`);
    }
  };

  return { save, result, pending: mutation.isPending };
}

/* ------------------------------------------------------------------- logging -- */

export function LoggingSection({ settings }: { settings: EffectiveSettings }) {
  const { save, result, pending } = useSectionSave();
  const [requestBodies, setRequestBodies] = useState(settings.logging.store_request_bodies);
  const [responseBodies, setResponseBodies] = useState(settings.logging.store_response_bodies);

  useEffect(() => {
    setRequestBodies(settings.logging.store_request_bodies);
    setResponseBodies(settings.logging.store_response_bodies);
  }, [settings.logging.store_request_bodies, settings.logging.store_response_bodies]);

  const dirty =
    requestBodies !== settings.logging.store_request_bodies || responseBodies !== settings.logging.store_response_bodies;

  return (
    <Section
      title="Logging policy"
      description="Storing bodies makes debugging possible and makes the database sensitive. Both switches default to off."
      badge={dirty ? 'unsaved' : 'saved'}
      footer={
        <>
          <SaveFeedback result={result} />
          <Button
            size="sm"
            variant="primary"
            loading={pending}
            disabled={!dirty}
            onClick={() => void save({ logging: { store_request_bodies: requestBodies, store_response_bodies: responseBodies } }, 'Logging policy')}
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
          >
            Save logging policy
          </Button>
        </>
      }
    >
      <Switch
        checked={requestBodies}
        onCheckedChange={setRequestBodies}
        label="Store request bodies"
        hint="Persists the outbound payload of every call, redacted with the configured secret values."
      />
      <Switch
        checked={responseBodies}
        onCheckedChange={setResponseBodies}
        label="Store response bodies"
        hint="Persists the provider response, redacted the same way."
      />
      <InlineAlert tone="warning">
        <span className="flex items-start gap-1">
          <AlertTriangle aria-hidden className="mt-0.5 h-3 w-3 shrink-0" />
          Turning either switch on writes user payloads to the database. They are redacted with known secret values, but
          redaction is best-effort: a secret the platform has never seen can pass through. Prefer off in production; if
          you turn it on, shorten the file retention below and review who can read the database.
        </span>
      </InlineAlert>
    </Section>
  );
}

/* --------------------------------------------------------------------- files -- */

export function FilesSection({ settings }: { settings: EffectiveSettings }) {
  const { save, result, pending } = useSectionSave();
  const [retention, setRetention] = useState(String(settings.files.retention_days));

  useEffect(() => {
    setRetention(String(settings.files.retention_days));
  }, [settings.files.retention_days]);

  const parsed = Number.parseInt(retention, 10);
  const invalid = !Number.isFinite(parsed) || parsed < 1 || parsed > 3650;
  const dirty = String(settings.files.retention_days) !== retention;

  return (
    <Section
      title="File retention"
      description="Uploaded files and task artifacts are deleted after this many days."
      badge={dirty ? 'unsaved' : 'saved'}
      footer={
        <>
          <SaveFeedback result={result} />
          <Button
            size="sm"
            variant="primary"
            loading={pending}
            disabled={!dirty || invalid}
            onClick={() => void save({ files: { retention_days: parsed } }, 'File retention')}
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
          >
            Save retention
          </Button>
        </>
      }
    >
      <Field
        label="Retention (days)"
        htmlFor="retention-days"
        error={invalid ? 'Use a whole number between 1 and 3650.' : null}
        hint="The API validates 1…3650; a rejected value is shown as an error, never silently clamped."
      >
        <Input
          id="retention-days"
          value={retention}
          inputMode="numeric"
          onChange={(event) => setRetention(event.target.value)}
          className="max-w-[160px]"
        />
      </Field>
    </Section>
  );
}

/* ------------------------------------------------------------------ security -- */

export function SecuritySection({ settings }: { settings: EffectiveSettings }) {
  const { save, result, pending } = useSectionSave();
  const [sessionTtl, setSessionTtl] = useState(String(settings.security.session_ttl_minutes));
  const [idleTimeout, setIdleTimeout] = useState(String(settings.security.idle_timeout_minutes));
  const [maxAttempts, setMaxAttempts] = useState(String(settings.security.login_max_attempts));

  useEffect(() => {
    setSessionTtl(String(settings.security.session_ttl_minutes));
    setIdleTimeout(String(settings.security.idle_timeout_minutes));
    setMaxAttempts(String(settings.security.login_max_attempts));
  }, [
    settings.security.session_ttl_minutes,
    settings.security.idle_timeout_minutes,
    settings.security.login_max_attempts,
  ]);

  const ttl = Number.parseInt(sessionTtl, 10);
  const idle = Number.parseInt(idleTimeout, 10);
  const attempts = Number.parseInt(maxAttempts, 10);
  const invalidTtl = !Number.isFinite(ttl) || ttl < 5 || ttl > 10080;
  const invalidIdle = !Number.isFinite(idle) || idle < 5 || idle > 10080;
  const invalidAttempts = !Number.isFinite(attempts) || attempts < 1 || attempts > 50;
  const dirty =
    String(settings.security.session_ttl_minutes) !== sessionTtl ||
    String(settings.security.idle_timeout_minutes) !== idleTimeout ||
    String(settings.security.login_max_attempts) !== maxAttempts;

  return (
    <Section
      title="Session and login security"
      description="These values are read by the platform on the next request, not only at restart."
      badge={dirty ? 'unsaved' : 'saved'}
      footer={
        <>
          <SaveFeedback result={result} />
          <Button
            size="sm"
            variant="primary"
            loading={pending}
            disabled={!dirty || invalidTtl || invalidIdle || invalidAttempts}
            onClick={() =>
              void save(
                {
                  security: {
                    session_ttl_minutes: ttl,
                    idle_timeout_minutes: idle,
                    login_max_attempts: attempts,
                  },
                },
                'Security values',
              )
            }
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
          >
            Save security values
          </Button>
        </>
      }
    >
      <FormRow columns={3}>
        <Field label="Session lifetime (min)" htmlFor="session-ttl" error={invalidTtl ? '5…10080' : null}>
          <Input id="session-ttl" value={sessionTtl} inputMode="numeric" onChange={(event) => setSessionTtl(event.target.value)} />
        </Field>
        <Field label="Idle timeout (min)" htmlFor="idle-timeout" error={invalidIdle ? '5…10080' : null}>
          <Input id="idle-timeout" value={idleTimeout} inputMode="numeric" onChange={(event) => setIdleTimeout(event.target.value)} />
        </Field>
        <Field label="Max login attempts" htmlFor="login-attempts" error={invalidAttempts ? '1…50' : null}>
          <Input id="login-attempts" value={maxAttempts} inputMode="numeric" onChange={(event) => setMaxAttempts(event.target.value)} />
        </Field>
      </FormRow>
      <p className="text-xs text-muted">
        Existing sessions keep their own expiry; a shorter session lifetime applies to sessions created after the change.
        Changing your password already revokes every other session.
      </p>
    </Section>
  );
}

/* -------------------------------------------------------------------- upgrade -- */

export function AgentUpgradeSection({ settings }: { settings: EffectiveSettings }) {
  const { save, result, pending } = useSectionSave();
  const [policy, setPolicy] = useState(settings.agent.upgrade_policy);
  const [channel, setChannel] = useState(settings.agent.upgrade_channel);
  const [branch, setBranch] = useState(settings.agent.track_branch);

  useEffect(() => {
    setPolicy(settings.agent.upgrade_policy);
    setChannel(settings.agent.upgrade_channel);
    setBranch(settings.agent.track_branch);
  }, [settings.agent.upgrade_policy, settings.agent.upgrade_channel, settings.agent.track_branch]);

  const dirty =
    policy !== settings.agent.upgrade_policy ||
    channel !== settings.agent.upgrade_channel ||
    branch !== settings.agent.track_branch;
  const invalidBranch = channel === 'branch' && (!branch.trim() || /\s/.test(branch));

  return (
    <Section
      title="Agent engine upgrade"
      description="How a new engine release is picked up. The engine console performs the checks; this only sets the policy."
      badge={dirty ? 'unsaved' : 'saved'}
      footer={
        <>
          <SaveFeedback result={result} />
          <Button
            size="sm"
            variant="primary"
            loading={pending}
            disabled={!dirty || invalidBranch}
            onClick={() =>
              void save(
                { agent: { upgrade_policy: policy, upgrade_channel: channel, track_branch: branch.trim() } },
                'Upgrade policy',
              )
            }
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
          >
            Save upgrade policy
          </Button>
        </>
      }
    >
      <FormRow columns={3}>
        <Field label="Policy" htmlFor="upgrade-policy" hint="manual = detect and wait for you.">
          <Select
            id="upgrade-policy"
            value={policy}
            onChange={(event) => setPolicy(event.target.value as 'manual' | 'auto')}
            options={[
              { value: 'manual', label: 'Manual approval' },
              { value: 'auto', label: 'Automatic after validation' },
            ]}
          />
        </Field>
        <Field label="Channel" htmlFor="upgrade-channel">
          <Select
            id="upgrade-channel"
            value={channel}
            onChange={(event) => setChannel(event.target.value as 'stable' | 'branch')}
            options={[
              { value: 'stable', label: 'Stable releases' },
              { value: 'branch', label: 'Follow a branch' },
            ]}
          />
        </Field>
        <Field
          label="Tracked branch"
          htmlFor="track-branch"
          error={invalidBranch ? 'A branch name without spaces is required.' : null}
        >
          <Input
            id="track-branch"
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
            disabled={channel !== 'branch'}
          />
        </Field>
      </FormRow>
      <p className="text-xs text-muted">
        <Info aria-hidden className="mr-1 inline h-3 w-3" />
        An automatic policy still requires the validation checks to pass; a failed candidate never replaces the current
        release.
      </p>
    </Section>
  );
}

/* --------------------------------------------------------------- auto-execute -- */

export function AutoExecuteSection({
  settings,
  providers,
}: {
  settings: EffectiveSettings;
  providers: Array<{ id: string; name: string; slug: string }>;
}) {
  const { save, result, pending } = useSectionSave();
  const policy = settings.agent.auto_execute;
  const [enabled, setEnabled] = useState(policy.enabled);
  const [allowed, setAllowed] = useState<string[]>(policy.allowed_providers);
  const [maxCalls, setMaxCalls] = useState(String(policy.max_calls));
  const [maxMinutes, setMaxMinutes] = useState(String(policy.max_minutes));
  const [maxCost, setMaxCost] = useState(String(policy.max_cost_usd));

  useEffect(() => {
    setEnabled(policy.enabled);
    setAllowed(policy.allowed_providers);
    setMaxCalls(String(policy.max_calls));
    setMaxMinutes(String(policy.max_minutes));
    setMaxCost(String(policy.max_cost_usd));
  }, [policy.enabled, policy.allowed_providers, policy.max_calls, policy.max_minutes, policy.max_cost_usd]);

  const calls = Number.parseInt(maxCalls, 10);
  const minutes = Number.parseInt(maxMinutes, 10);
  const cost = Number.parseFloat(maxCost);
  const invalid = !Number.isFinite(calls) || calls < 1 || !Number.isFinite(minutes) || minutes < 0 || Number.isNaN(cost) || cost < 0;
  const dirty =
    enabled !== policy.enabled ||
    JSON.stringify([...allowed].sort()) !== JSON.stringify([...policy.allowed_providers].sort()) ||
    String(policy.max_calls) !== maxCalls ||
    String(policy.max_minutes) !== maxMinutes ||
    String(policy.max_cost_usd) !== maxCost;

  const toggle = (slug: string) => {
    setAllowed((current) => (current.includes(slug) ? current.filter((item) => item !== slug) : [...current, slug]));
  };

  return (
    <Section
      title="Auto-execute budget"
      description="What the agent may spend without asking, per run. The agent still cannot exceed a token or budget policy."
      badge={dirty ? 'unsaved' : 'saved'}
      footer={
        <>
          <SaveFeedback result={result} />
          <Button
            size="sm"
            variant="primary"
            loading={pending}
            disabled={!dirty || invalid}
            onClick={() =>
              void save(
                {
                  agent: {
                    auto_execute: {
                      enabled,
                      allowed_providers: allowed,
                      max_calls: calls,
                      max_minutes: minutes,
                      max_cost_usd: cost,
                    },
                  },
                },
                'Auto-execute budget',
              )
            }
            iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
          >
            Save auto-execute budget
          </Button>
        </>
      }
    >
      <Switch
        checked={enabled}
        onCheckedChange={setEnabled}
        label="Allow the agent to execute without asking"
        hint="When off, every mutating tool call needs your explicit confirmation in the panel."
      />

      <FormRow columns={3}>
        <Field label="Max calls per run" htmlFor="auto-calls" error={invalid && (!Number.isFinite(calls) || calls < 1) ? 'At least 1.' : null}>
          <Input id="auto-calls" value={maxCalls} inputMode="numeric" onChange={(event) => setMaxCalls(event.target.value)} />
        </Field>
        <Field label="Max minutes per run" htmlFor="auto-minutes" error={invalid && !Number.isFinite(minutes) ? 'A number ≥ 0.' : null}>
          <Input id="auto-minutes" value={maxMinutes} inputMode="numeric" onChange={(event) => setMaxMinutes(event.target.value)} />
        </Field>
        <Field label="Max cost (USD)" htmlFor="auto-cost" error={invalid && Number.isNaN(cost) ? 'A number ≥ 0.' : null}>
          <Input id="auto-cost" value={maxCost} inputMode="decimal" onChange={(event) => setMaxCost(event.target.value)} />
        </Field>
      </FormRow>

      <fieldset className="rounded-card border border-border p-3">
        <legend className="px-1 text-xs font-medium text-text">Allowed providers</legend>
        <p className="mb-2 text-xs text-muted">
          Empty means no provider restriction, which is only safe with a tight call/cost budget.
        </p>
        {providers.length === 0 ? (
          <p className="text-xs text-muted">No provider exists yet.</p>
        ) : (
          <div className="grid gap-1.5 sm:grid-cols-2">
            {providers.map((provider) => (
              <Checkbox
                key={provider.id}
                checked={allowed.includes(provider.slug) || allowed.includes(provider.id)}
                onChange={() => toggle(allowed.includes(provider.slug) ? provider.slug : provider.id)}
                label={provider.name}
                hint={provider.slug}
              />
            ))}
          </div>
        )}
      </fieldset>

      <p className="text-xs text-muted">
        Currently enforced: {formatNumber(policy.max_calls)} call(s), {formatNumber(policy.max_minutes)} minute(s),{' '}
        {policy.max_cost_usd} USD{policy.enabled ? '' : ' — but auto-execute is off, so nothing runs unattended'}.
      </p>
    </Section>
  );
}

/* --------------------------------------------------------------------- model -- */

export function SettingsLoading() {
  return (
    <div className="space-y-3">
      <SkeletonBlock rows={4} />
      <SkeletonBlock rows={3} />
    </div>
  );
}

export type { SettingsPayload };
