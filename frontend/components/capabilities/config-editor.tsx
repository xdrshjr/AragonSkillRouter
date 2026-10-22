'use client';

import { useEffect, useMemo, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';

import type {
  AsyncConfig,
  CapabilityConfig,
  CapabilityIssue,
  ErrorRule,
  InvocationMode,
  QueryParam,
} from '@/lib/types';
import { configArray, configBoolean, configNumber, configRecord, configString, numberOrNull, splitList, setConfigPath } from '@/lib/config';
import { formatJsonText, parseJson, parseJsonObject } from '@/lib/json';
import { Button } from '@/components/ui/button';
import { Field, FormRow, FormSection } from '@/components/ui/field';
import { InlineAlert } from '@/components/ui/banner';
import { Input, Textarea } from '@/components/ui/input';
import { JsonEditor } from '@/components/ui/json-editor';
import { Select } from '@/components/ui/select';
import { Switch } from '@/components/ui/checkbox';

export type SetPath = (path: string, value: unknown) => void;

function issuesFor(issues: CapabilityIssue[], prefix: string): CapabilityIssue[] {
  return issues.filter((issue) => issue.field === prefix || issue.field.startsWith(`${prefix}.`) || issue.field.startsWith(`${prefix}[`));
}

/** Inline validation feedback for one config subtree. */
export function IssuesFor({ issues, prefix }: { issues: CapabilityIssue[]; prefix: string }) {
  const relevant = issuesFor(issues, prefix);
  if (relevant.length === 0) return null;
  return (
    <div className="space-y-1">
      {relevant.map((issue) => (
        <InlineAlert key={`${issue.field}-${issue.message}`} tone={issue.severity === 'error' ? 'danger' : 'warning'}>
          <span className="font-mono">{issue.field}</span> — {issue.message}
        </InlineAlert>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ key / value --- */

export function KeyValueEditor({
  label,
  hint,
  entries,
  onChange,
  disabled,
  keyPlaceholder = 'name',
  valuePlaceholder = 'value',
}: {
  label: string;
  hint?: string;
  entries: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  disabled?: boolean;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  const rows = useMemo(() => Object.entries(entries), [entries]);

  const update = (index: number, key: string, value: string) => {
    const next: Array<[string, string]> = rows.map((entry) => [...entry] as [string, string]);
    const current = next[index];
    if (!current) return;
    next[index] = [key, value];
    const rebuilt: Record<string, string> = {};
    for (const [entryKey, entryValue] of next) {
      if (entryKey.trim()) rebuilt[entryKey.trim()] = entryValue;
    }
    onChange(rebuilt);
  };

  const remove = (index: number) => {
    const rebuilt: Record<string, string> = {};
    rows.forEach(([key, value], rowIndex) => {
      if (rowIndex !== index && key.trim()) rebuilt[key] = value;
    });
    onChange(rebuilt);
  };

  const add = () => onChange({ ...entries, [`field${rows.length + 1}`]: '' });

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-text">{label}</span>
        <Button size="sm" variant="ghost" onClick={add} disabled={disabled} iconLeft={<Plus aria-hidden className="h-3 w-3" />}>
          Add row
        </Button>
      </div>
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
      {rows.length === 0 ? <p className="text-xs text-muted">No entries.</p> : null}
      <div className="flex flex-col gap-1.5">
        {rows.map(([key, value], index) => (
          <div key={`${key}-${index}`} className="flex items-center gap-1.5">
            <Input
              aria-label={`${label} name ${index + 1}`}
              value={key}
              placeholder={keyPlaceholder}
              disabled={disabled}
              onChange={(event) => update(index, event.target.value, value)}
            />
            <Input
              aria-label={`${label} value ${index + 1}`}
              value={value}
              placeholder={valuePlaceholder}
              disabled={disabled}
              onChange={(event) => update(index, key, event.target.value)}
            />
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled}
              aria-label={`Remove ${label} row ${index + 1}`}
              onClick={() => remove(index)}
              iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ json field ---- */

export function JsonField({
  label,
  hint,
  value,
  onChange,
  rows = 6,
  disabled,
  requireObject = false,
  issues,
}: {
  label: string;
  hint?: string;
  value: unknown;
  onChange: (value: unknown) => void;
  rows?: number;
  disabled?: boolean;
  requireObject?: boolean;
  issues?: CapabilityIssue[];
}) {
  const external = useMemo(() => formatJsonText(value), [value]);
  const [text, setText] = useState(external);
  const [lastExternal, setLastExternal] = useState(external);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (external !== lastExternal) {
      setLastExternal(external);
      setText(external);
      setError(null);
    }
  }, [external, lastExternal]);

  const handleChange = (next: string) => {
    setText(next);
    const parsed = requireObject ? parseJsonObject(next) : parseJson(next);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setError(null);
    onChange(parsed.value);
  };

  return (
    <div className="space-y-1">
      <JsonEditor
        label={label}
        hint={hint}
        value={text}
        onChange={handleChange}
        rows={rows}
        disabled={disabled}
        requireObject={requireObject}
        externalError={error}
      />
      {issues && issues.length > 0 ? (
        <div className="space-y-1">
          {issues.map((issue) => (
            <InlineAlert key={`${issue.field}-${issue.message}`} tone={issue.severity === 'error' ? 'danger' : 'warning'}>
              <span className="font-mono">{issue.field}</span> — {issue.message}
            </InlineAlert>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ panels --- */

interface PanelProps {
  config: CapabilityConfig;
  set: SetPath;
  /** Replaces the whole config (used by the "unmodelled keys" fallback). */
  replace: (next: CapabilityConfig) => void;
  issues: CapabilityIssue[];
  disabled: boolean;
}

export function RequestPanel({ config, set, issues, disabled }: PanelProps) {
  const query = configArray<QueryParam>(config, 'request.query');
  const encoding = configString(config, 'request.encoding') || 'json';

  const updateQuery = (next: QueryParam[]) => set('request.query', next);

  return (
    <div className="space-y-5">
      <IssuesFor issues={issues} prefix="request" />
      <FormSection title="Endpoint" description="URLs and bodies support {{ input.x }} / {{ model }} templates.">
        <FormRow columns={2}>
          <Field label="HTTP method" htmlFor="request-method">
            <Select
              id="request-method"
              disabled={disabled}
              value={configString(config, 'request.method') || 'POST'}
              options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((value) => ({ value, label: value }))}
              onChange={(event) => set('request.method', event.target.value)}
            />
          </Field>
          <Field
            label="URL"
            htmlFor="request-url"
            hint="Absolute upstream URL. Header/query templates may use {model}."
            required
          >
            <Input
              id="request-url"
              disabled={disabled}
              placeholder="https://api.example.com/v1/run"
              value={configString(config, 'request.url')}
              onChange={(event) => set('request.url', event.target.value)}
            />
          </Field>
        </FormRow>
      </FormSection>

      <FormSection title="Headers" description="Never put secrets here — secrets come from the credential.">
        <KeyValueEditor
          label="Request headers"
          hint="Header names are lower-cased on the wire. Templates are allowed in values."
          entries={configRecord(config, 'request.headers')}
          onChange={(next) => set('request.headers', next)}
          disabled={disabled}
          keyPlaceholder="content-type"
          valuePlaceholder="application/json"
        />
      </FormSection>

      <FormSection title="Query parameters" description="Optional; `when_mode` limits a parameter to one invocation mode.">
        <div className="flex flex-col gap-2">
          {query.length === 0 ? <p className="text-xs text-muted">No query parameters.</p> : null}
          {query.map((entry, index) => (
            <div key={index} className="flex flex-col gap-1.5 sm:flex-row sm:items-center">
              <Input
                aria-label={`Query parameter ${index + 1} name`}
                value={entry.name ?? ''}
                placeholder="alt"
                disabled={disabled}
                onChange={(event) => {
                  const next = [...query];
                  next[index] = { ...entry, name: event.target.value };
                  updateQuery(next);
                }}
              />
              <Input
                aria-label={`Query parameter ${index + 1} value`}
                value={entry.value ?? ''}
                placeholder="sse"
                disabled={disabled}
                onChange={(event) => {
                  const next = [...query];
                  next[index] = { ...entry, value: event.target.value };
                  updateQuery(next);
                }}
              />
              <Select
                aria-label={`Query parameter ${index + 1} mode`}
                disabled={disabled}
                value={entry.when_mode ?? ''}
                placeholder="Any mode"
                options={[
                  { value: 'sync', label: 'Sync only' },
                  { value: 'async', label: 'Async only' },
                  { value: 'stream', label: 'Stream only' },
                ]}
                onChange={(event) => {
                  const next = [...query];
                  next[index] = { ...entry, when_mode: event.target.value as InvocationMode | '' };
                  updateQuery(next);
                }}
              />
              <Button
                size="sm"
                variant="ghost"
                disabled={disabled}
                aria-label={`Remove query parameter ${index + 1}`}
                onClick={() => updateQuery(query.filter((_, rowIndex) => rowIndex !== index))}
                iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
              />
            </div>
          ))}
          <div>
            <Button
              size="sm"
              variant="secondary"
              disabled={disabled}
              onClick={() => updateQuery([...query, { name: '', value: '', when_mode: '' }])}
              iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
            >
              Add parameter
            </Button>
          </div>
        </div>
      </FormSection>

      <FormSection title="Encoding and body" description="The body template is rendered per request and must parse as JSON when encoding is json.">
        <FormRow columns={2}>
          <Field label="Encoding" htmlFor="request-encoding">
            <Select
              id="request-encoding"
              disabled={disabled}
              value={encoding}
              options={[
                { value: 'json', label: 'JSON' },
                { value: 'form', label: 'Form (urlencoded)' },
                { value: 'multipart', label: 'Multipart' },
                { value: 'none', label: 'No body' },
              ]}
              onChange={(event) => set('request.encoding', event.target.value)}
            />
          </Field>
          <Field
            label="Multipart file field"
            htmlFor="request-multipart-field"
            hint="Only for encoding=multipart; names the binary part."
          >
            <Input
              id="request-multipart-field"
              disabled={disabled || encoding !== 'multipart'}
              value={configString(config, 'request.multipart_file_field')}
              placeholder="file"
              onChange={(event) => set('request.multipart_file_field', event.target.value)}
            />
          </Field>
        </FormRow>

        {encoding === 'form' ? (
          <KeyValueEditor
            label="Form fields"
            entries={configRecord(config, 'request.form_fields')}
            onChange={(next) => set('request.form_fields', next)}
            disabled={disabled}
          />
        ) : null}

        <JsonField
          label="Body template (JSON)"
          hint="Keep {{ input.prompt }} templates as strings; the renderer substitutes them before sending."
          value={config.request?.body_template}
          onChange={(value) => set('request.body_template', value)}
          rows={10}
          disabled={disabled || encoding === 'none'}
          issues={issuesFor(issues, 'request.body_template')}
        />
      </FormSection>
    </div>
  );
}

export function InputSchemaPanel({ config, set, issues, disabled }: PanelProps) {
  return (
    <div className="space-y-4">
      <IssuesFor issues={issues} prefix="input_schema" />
      <p className="text-xs text-muted">
        The input schema is what agents see when discovering this capability. Unknown inputs are rejected before any
        upstream call is made.
      </p>
      <JsonField
        label="Input schema (JSON Schema)"
        value={config.input_schema}
        onChange={(value) => set('input_schema', value)}
        rows={16}
        requireObject
        disabled={disabled}
        issues={issuesFor(issues, 'input_schema')}
      />
    </div>
  );
}

export function OutputPanel({ config, set, issues, disabled }: PanelProps) {
  const responseKind = configString(config, 'response.kind') || 'json';
  const artifactKind = configString(config, 'response.artifact.kind');

  return (
    <div className="space-y-5">
      <IssuesFor issues={issues} prefix="response" />
      <FormSection title="Response handling">
        <FormRow columns={2}>
          <Field label="Response kind" htmlFor="response-kind">
            <Select
              id="response-kind"
              disabled={disabled}
              value={responseKind}
              options={[
                { value: 'json', label: 'JSON' },
                { value: 'text', label: 'Plain text' },
              ]}
              onChange={(event) => set('response.kind', event.target.value)}
            />
          </Field>
        </FormRow>
      </FormSection>

      <FormSection title="Extraction paths" description="JSONPath ($.a[0].b). Leave a path empty to skip that value.">
        <FormRow columns={2}>
          <Field label="Result path" htmlFor="extract-result" hint="Required: what the agent receives.">
            <Input
              id="extract-result"
              disabled={disabled}
              placeholder="$.candidates[0].content.parts[0].text"
              value={configString(config, 'response.extract.result')}
              onChange={(event) => set('response.extract.result', event.target.value)}
            />
          </Field>
          <Field label="Input usage path" htmlFor="extract-usage-in">
            <Input
              id="extract-usage-in"
              disabled={disabled}
              placeholder="$.usageMetadata.promptTokenCount"
              value={configString(config, 'response.extract.usage_in')}
              onChange={(event) => set('response.extract.usage_in', event.target.value)}
            />
          </Field>
          <Field label="Output usage path" htmlFor="extract-usage-out">
            <Input
              id="extract-usage-out"
              disabled={disabled}
              placeholder="$.usageMetadata.candidatesTokenCount"
              value={configString(config, 'response.extract.usage_out')}
              onChange={(event) => set('response.extract.usage_out', event.target.value)}
            />
          </Field>
          <Field label="Total usage path" htmlFor="extract-usage-total">
            <Input
              id="extract-usage-total"
              disabled={disabled}
              placeholder="$.usage.total_tokens"
              value={configString(config, 'response.extract.usage_total')}
              onChange={(event) => set('response.extract.usage_total', event.target.value)}
            />
          </Field>
          <Field label="Cost path" htmlFor="extract-cost">
            <Input
              id="extract-cost"
              disabled={disabled}
              placeholder="$.cost"
              value={configString(config, 'response.extract.cost')}
              onChange={(event) => set('response.extract.cost', event.target.value)}
            />
          </Field>
        </FormRow>
      </FormSection>

      <FormSection title="Artifact" description="Binary outputs (image/audio/video/file) are stored and returned as a link.">
        <FormRow columns={2}>
          <Field label="Artifact kind" htmlFor="artifact-kind">
            <Select
              id="artifact-kind"
              disabled={disabled}
              value={artifactKind}
              placeholder="No artifact"
              options={[
                { value: 'image', label: 'Image' },
                { value: 'audio', label: 'Audio' },
                { value: 'video', label: 'Video' },
                { value: 'file', label: 'File' },
              ]}
              onChange={(event) => set('response.artifact.kind', event.target.value || undefined)}
            />
          </Field>
          <Field label="Artifact path" htmlFor="artifact-path">
            <Input
              id="artifact-path"
              disabled={disabled || !artifactKind}
              placeholder="$.audio.data"
              value={configString(config, 'response.artifact.path')}
              onChange={(event) => set('response.artifact.path', event.target.value)}
            />
          </Field>
          <Field label="Encoding" htmlFor="artifact-encoding">
            <Select
              id="artifact-encoding"
              disabled={disabled || !artifactKind}
              value={configString(config, 'response.artifact.encoding')}
              placeholder="Choose…"
              options={[
                { value: 'base64', label: 'Base64' },
                { value: 'url', label: 'URL' },
              ]}
              onChange={(event) => set('response.artifact.encoding', event.target.value || undefined)}
            />
          </Field>
          <Field label="MIME type" htmlFor="artifact-mime">
            <Input
              id="artifact-mime"
              disabled={disabled || !artifactKind}
              placeholder="audio/mpeg"
              value={configString(config, 'response.artifact.mime')}
              onChange={(event) => set('response.artifact.mime', event.target.value)}
            />
          </Field>
          <Field label="MIME path" htmlFor="artifact-mime-path" hint="Use when the provider reports the type in the body.">
            <Input
              id="artifact-mime-path"
              disabled={disabled || !artifactKind}
              placeholder="$.audio.mime_type"
              value={configString(config, 'response.artifact.mime_path')}
              onChange={(event) => set('response.artifact.mime_path', event.target.value)}
            />
          </Field>
          <Field label="Filename" htmlFor="artifact-filename">
            <Input
              id="artifact-filename"
              disabled={disabled || !artifactKind}
              placeholder="out.mp3"
              value={configString(config, 'response.artifact.filename')}
              onChange={(event) => set('response.artifact.filename', event.target.value)}
            />
          </Field>
        </FormRow>
      </FormSection>

      <FormSection title="Output schema" description="Describes the result for agents; not enforced on the wire.">
        <JsonField
          label="Output schema (JSON Schema)"
          value={config.output_schema}
          onChange={(value) => set('output_schema', value)}
          rows={12}
          requireObject
          disabled={disabled}
        />
      </FormSection>
    </div>
  );
}

export function ModesPanel({ config, set, issues, disabled }: PanelProps) {
  const asyncConfig: AsyncConfig = config.async ?? {};

  return (
    <div className="space-y-5">
      <IssuesFor issues={issues} prefix="modes" />
      <FormSection title="Supported modes" description="At least one mode must be enabled for routing to select a route.">
        <div className="grid gap-3 sm:grid-cols-3">
          <Switch
            label="Sync"
            hint="One request, one response."
            checked={configBoolean(config, 'modes.sync')}
            disabled={disabled}
            onCheckedChange={(checked) => set('modes.sync', checked)}
          />
          <Switch
            label="Async"
            hint="Submit, poll, then collect."
            checked={configBoolean(config, 'modes.async')}
            disabled={disabled}
            onCheckedChange={(checked) => set('modes.async', checked)}
          />
          <Switch
            label="Stream"
            hint="Incremental deltas."
            checked={configBoolean(config, 'modes.stream')}
            disabled={disabled}
            onCheckedChange={(checked) => set('modes.stream', checked)}
          />
        </div>
      </FormSection>

      <FormSection title="Async submission and polling" description="Used when mode=async: extract the upstream task id, poll it, then read the result.">
        <div className="space-y-4">
          <Switch
            label="Async enabled"
            checked={Boolean(asyncConfig.enabled)}
            disabled={disabled}
            onCheckedChange={(checked) => set('async.enabled', checked)}
          />
          <JsonField
            label="Submit extract"
            hint='JSONPath map, e.g. {"task_id": "$.id"}'
            value={config.async?.submit_extract ?? { task_id: '$.id' }}
            onChange={(value) => set('async.submit_extract', value)}
            rows={4}
            requireObject
            disabled={disabled}
            issues={issuesFor(issues, 'async.submit_extract')}
          />
          <FormRow columns={2}>
            <Field label="Status URL" htmlFor="async-status-url" hint="Absolute URL; {task_id} is substituted.">
              <Input
                id="async-status-url"
                disabled={disabled}
                placeholder="https://api.example.com/jobs/{task_id}"
                value={configString(config, 'async.status.url')}
                onChange={(event) => set('async.status.url', event.target.value)}
              />
            </Field>
            <Field label="Status method" htmlFor="async-status-method">
              <Select
                id="async-status-method"
                disabled={disabled}
                value={configString(config, 'async.status.method') || 'GET'}
                options={['GET', 'POST'].map((value) => ({ value, label: value }))}
                onChange={(event) => set('async.status.method', event.target.value)}
              />
            </Field>
          </FormRow>
          <FormRow columns={2}>
            <Field label="Status extract path" htmlFor="async-status-extract">
              <Input
                id="async-status-extract"
                disabled={disabled}
                placeholder="$.status"
                value={configString(config, 'async.status.extract.status')}
                onChange={(event) => set('async.status.extract.status', event.target.value)}
              />
            </Field>
            <Field label="Progress extract path" htmlFor="async-progress-extract">
              <Input
                id="async-progress-extract"
                disabled={disabled}
                placeholder="$.progress"
                value={configString(config, 'async.status.extract.progress')}
                onChange={(event) => set('async.status.extract.progress', event.target.value)}
              />
            </Field>
          </FormRow>
          <Field
            label="Poll interval (seconds)"
            htmlFor="async-poll-interval"
            hint="How long the worker waits between status polls."
          >
            <Input
              id="async-poll-interval"
              type="number"
              min={1}
              disabled={disabled}
              value={configNumber(config, 'async.status.poll_interval_s') ?? 5}
              onChange={(event) => set('async.status.poll_interval_s', numberOrNull(event.target.value))}
            />
          </Field>
          <JsonField
            label="Status map"
            hint='Upstream status to router status, e.g. {"queued": ["PENDING"], "succeeded": ["DONE"]}'
            value={
              config.async?.status?.map ?? {
                queued: ['PENDING'],
                running: ['RUNNING'],
                succeeded: ['DONE'],
                failed: ['FAILED'],
                cancelled: ['CANCELLED'],
              }
            }
            onChange={(value) => set('async.status.map', value)}
            rows={8}
            requireObject
            disabled={disabled}
            issues={issuesFor(issues, 'async.status.map')}
          />
          <JsonField
            label="Result extract"
            hint='JSONPath map applied to the finished status payload, e.g. {"result": "$.output"}'
            value={config.async?.status?.result_extract ?? { result: '$.output' }}
            onChange={(value) => set('async.status.result_extract', value)}
            rows={4}
            requireObject
            disabled={disabled}
            issues={issuesFor(issues, 'async.status.result_extract')}
          />
          <FormRow columns={2}>
            <Field label="Cancel URL" htmlFor="async-cancel-url">
              <Input
                id="async-cancel-url"
                disabled={disabled}
                placeholder="https://api.example.com/jobs/{task_id}/cancel"
                value={configString(config, 'async.cancel.url')}
                onChange={(event) => set('async.cancel.url', event.target.value)}
              />
            </Field>
            <Field label="Cancel method" htmlFor="async-cancel-method">
              <Select
                id="async-cancel-method"
                disabled={disabled}
                value={configString(config, 'async.cancel.method') || 'POST'}
                options={['POST', 'DELETE', 'PATCH'].map((value) => ({ value, label: value }))}
                onChange={(event) => set('async.cancel.method', event.target.value)}
              />
            </Field>
          </FormRow>
        </div>
      </FormSection>

      <FormSection title="Streaming" description="SSE or NDJSON deltas; the delta path is applied to every event.">
        <div className="space-y-4">
          <Switch
            label="Stream enabled"
            checked={configBoolean(config, 'stream.enabled')}
            disabled={disabled}
            onCheckedChange={(checked) => set('stream.enabled', checked)}
          />
          <FormRow columns={3}>
            <Field label="Protocol" htmlFor="stream-protocol">
              <Select
                id="stream-protocol"
                disabled={disabled}
                value={configString(config, 'stream.protocol') || 'sse'}
                options={[
                  { value: 'sse', label: 'SSE' },
                  { value: 'ndjson', label: 'NDJSON' },
                ]}
                onChange={(event) => set('stream.protocol', event.target.value)}
              />
            </Field>
            <Field label="Delta path" htmlFor="stream-delta-path">
              <Input
                id="stream-delta-path"
                disabled={disabled}
                placeholder="$.candidates[0].content.parts[0].text"
                value={configString(config, 'stream.delta_path')}
                onChange={(event) => set('stream.delta_path', event.target.value)}
              />
            </Field>
            <Field label="Done marker" htmlFor="stream-done-marker">
              <Input
                id="stream-done-marker"
                disabled={disabled}
                placeholder="[DONE]"
                value={configString(config, 'stream.done_marker')}
                onChange={(event) => set('stream.done_marker', event.target.value)}
              />
            </Field>
          </FormRow>
        </div>
      </FormSection>
    </div>
  );
}

export function ErrorsPanel({ config, set, issues, disabled }: PanelProps) {
  const rules = configArray<ErrorRule>(config, 'errors.rules');

  const updateRule = (index: number, patch: Partial<ErrorRule>) => {
    const next = [...rules];
    next[index] = { ...rules[index], ...patch };
    set('errors.rules', next);
  };

  const updateMatch = (index: number, patch: Record<string, unknown>) => {
    const next = [...rules];
    next[index] = { ...rules[index], match: { ...(rules[index]?.match ?? {}), ...patch } };
    set('errors.rules', next);
  };

  return (
    <div className="space-y-4">
      <IssuesFor issues={issues} prefix="errors" />
      <p className="text-xs text-muted">
        Rules map upstream failures onto router error codes. Without a match the default code is used, so an agent still
        gets a usable error.
      </p>
      <Field label="Default error code" htmlFor="errors-default-code">
        <Input
          id="errors-default-code"
          disabled={disabled}
          placeholder="upstream_error"
          value={configString(config, 'errors.default_code')}
          onChange={(event) => set('errors.default_code', event.target.value)}
        />
      </Field>

      <div className="space-y-2">
        {rules.length === 0 ? <p className="text-xs text-muted">No mapping rules.</p> : null}
        {rules.map((rule, index) => (
          <div key={index} className="rounded-control border border-border p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-text">Rule {index + 1}</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={disabled}
                aria-label={`Remove rule ${index + 1}`}
                onClick={() => set('errors.rules', rules.filter((_, rowIndex) => rowIndex !== index))}
                iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
              />
            </div>
            <FormRow columns={3}>
              <Field label="HTTP status list" htmlFor={`rule-${index}-status`} hint="Comma separated, e.g. 401, 403">
                <Input
                  id={`rule-${index}-status`}
                  disabled={disabled}
                  value={(rule.match?.http_status ?? []).join(', ')}
                  onChange={(event) =>
                    updateMatch(index, {
                      http_status: splitList(event.target.value)
                        .map((item) => Number(item))
                        .filter((item) => !Number.isNaN(item)),
                    })
                  }
                />
              </Field>
              <Field label="Body path" htmlFor={`rule-${index}-path`}>
                <Input
                  id={`rule-${index}-path`}
                  disabled={disabled}
                  placeholder="$.error.code"
                  value={rule.match?.body_path ?? ''}
                  onChange={(event) => updateMatch(index, { body_path: event.target.value })}
                />
              </Field>
              <Field label="Equals" htmlFor={`rule-${index}-equals`}>
                <Input
                  id={`rule-${index}-equals`}
                  disabled={disabled}
                  value={rule.match?.equals ?? ''}
                  onChange={(event) => updateMatch(index, { equals: event.target.value })}
                />
              </Field>
              <Field label="Contains" htmlFor={`rule-${index}-contains`}>
                <Input
                  id={`rule-${index}-contains`}
                  disabled={disabled}
                  value={rule.match?.contains ?? ''}
                  onChange={(event) => updateMatch(index, { contains: event.target.value })}
                />
              </Field>
              <Field label="Router code" htmlFor={`rule-${index}-code`} required>
                <Input
                  id={`rule-${index}-code`}
                  disabled={disabled}
                  placeholder="upstream_auth_failed"
                  value={rule.code ?? ''}
                  onChange={(event) => updateRule(index, { code: event.target.value })}
                />
              </Field>
              <Field label="Retryable" htmlFor={`rule-${index}-retryable`}>
                <Select
                  id={`rule-${index}-retryable`}
                  disabled={disabled}
                  value={rule.retryable ? 'yes' : 'no'}
                  options={[
                    { value: 'no', label: 'No' },
                    { value: 'yes', label: 'Yes' },
                  ]}
                  onChange={(event) => updateRule(index, { retryable: event.target.value === 'yes' })}
                />
              </Field>
            </FormRow>
            <FormRow columns={2} className="mt-3">
              <Field label="Message" htmlFor={`rule-${index}-message`}>
                <Textarea
                  id={`rule-${index}-message`}
                  rows={2}
                  disabled={disabled}
                  value={rule.message ?? ''}
                  onChange={(event) => updateRule(index, { message: event.target.value })}
                />
              </Field>
              <Field label="Suggested action" htmlFor={`rule-${index}-action`}>
                <Textarea
                  id={`rule-${index}-action`}
                  rows={2}
                  disabled={disabled}
                  value={rule.action ?? ''}
                  onChange={(event) => updateRule(index, { action: event.target.value })}
                />
              </Field>
            </FormRow>
          </div>
        ))}
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled}
          onClick={() => set('errors.rules', [...rules, { match: { http_status: [], body_path: '', equals: '', contains: '' }, code: '', message: '', action: '', retryable: false }])}
          iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
        >
          Add rule
        </Button>
      </div>
    </div>
  );
}

export function LimitsPanel({ config, set, issues, disabled }: PanelProps) {
  return (
    <div className="space-y-5">
      <IssuesFor issues={issues} prefix="limits" />
      <FormSection title="Limits">
        <FormRow columns={3}>
          <Field label="Timeout (s)" htmlFor="limits-timeout">
            <Input
              id="limits-timeout"
              type="number"
              min={1}
              disabled={disabled}
              value={configNumber(config, 'limits.timeout_s') ?? 60}
              onChange={(event) => set('limits.timeout_s', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Max output bytes" htmlFor="limits-max-output">
            <Input
              id="limits-max-output"
              type="number"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'limits.max_output_bytes') ?? 8388608}
              onChange={(event) => set('limits.max_output_bytes', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Max input bytes" htmlFor="limits-max-input">
            <Input
              id="limits-max-input"
              type="number"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'limits.max_input_bytes') ?? 262144}
              onChange={(event) => set('limits.max_input_bytes', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Max duration (s)" htmlFor="limits-max-duration" hint="Media duration cap; empty means unlimited.">
            <Input
              id="limits-max-duration"
              type="number"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'limits.max_duration_s') ?? ''}
              onChange={(event) => set('limits.max_duration_s', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Max file size (MB)" htmlFor="limits-max-file">
            <Input
              id="limits-max-file"
              type="number"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'limits.max_file_mb') ?? 10}
              onChange={(event) => set('limits.max_file_mb', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Allowed MIME types" htmlFor="limits-allowed-mime" hint="Comma separated.">
            <Input
              id="limits-allowed-mime"
              disabled={disabled}
              value={(configArray<string>(config, 'limits.allowed_mime') ?? []).join(', ')}
              onChange={(event) => set('limits.allowed_mime', splitList(event.target.value))}
            />
          </Field>
        </FormRow>
        <Switch
          label="Accept file input"
          hint="Requires multipart encoding and a file field."
          checked={configBoolean(config, 'limits.file_input')}
          disabled={disabled}
          onCheckedChange={(checked) => set('limits.file_input', checked)}
        />
      </FormSection>

      <FormSection title="Retry policy">
        <FormRow columns={3}>
          <Field label="Max attempts" htmlFor="retry-max-attempts">
            <Input
              id="retry-max-attempts"
              type="number"
              min={1}
              disabled={disabled}
              value={configNumber(config, 'retry.max_attempts') ?? 2}
              onChange={(event) => set('retry.max_attempts', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Backoff (ms)" htmlFor="retry-backoff" hint="Comma separated per attempt.">
            <Input
              id="retry-backoff"
              disabled={disabled}
              value={(configArray<number>(config, 'retry.backoff_ms') ?? []).join(', ')}
              onChange={(event) =>
                set(
                  'retry.backoff_ms',
                  splitList(event.target.value)
                    .map((item) => Number(item))
                    .filter((item) => !Number.isNaN(item)),
                )
              }
            />
          </Field>
          <Field label="Retry on" htmlFor="retry-on" hint="connect_timeout, read_timeout, upstream_5xx, rate_limited">
            <Input
              id="retry-on"
              disabled={disabled}
              value={(configArray<string>(config, 'retry.retry_on') ?? []).join(', ')}
              onChange={(event) => set('retry.retry_on', splitList(event.target.value))}
            />
          </Field>
        </FormRow>
      </FormSection>

      <FormSection title="Pricing" description="Used for cost estimates; actual provider cost always wins when reported.">
        <FormRow columns={3}>
          <Field label="Per call" htmlFor="pricing-per-call">
            <Input
              id="pricing-per-call"
              type="number"
              step="0.000001"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'pricing.per_call') ?? 0}
              onChange={(event) => set('pricing.per_call', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Input per 1k tokens" htmlFor="pricing-input">
            <Input
              id="pricing-input"
              type="number"
              step="0.000001"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'pricing.input_per_1k') ?? 0}
              onChange={(event) => set('pricing.input_per_1k', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Output per 1k tokens" htmlFor="pricing-output">
            <Input
              id="pricing-output"
              type="number"
              step="0.000001"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'pricing.output_per_1k') ?? 0}
              onChange={(event) => set('pricing.output_per_1k', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Per second" htmlFor="pricing-per-second">
            <Input
              id="pricing-per-second"
              type="number"
              step="0.000001"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'pricing.per_second') ?? 0}
              onChange={(event) => set('pricing.per_second', numberOrNull(event.target.value))}
            />
          </Field>
          <Field label="Currency" htmlFor="pricing-currency">
            <Input
              id="pricing-currency"
              disabled={disabled}
              value={configString(config, 'pricing.currency') || 'USD'}
              onChange={(event) => set('pricing.currency', event.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Dedupe window (s)" htmlFor="dedupe-window" hint="0 disables idempotent dedupe.">
            <Input
              id="dedupe-window"
              type="number"
              min={0}
              disabled={disabled}
              value={configNumber(config, 'dedupe_window_s') ?? 0}
              onChange={(event) => set('dedupe_window_s', numberOrNull(event.target.value))}
            />
          </Field>
        </FormRow>
      </FormSection>
    </div>
  );
}

export function ExamplesPanel({ config, set, issues, disabled }: PanelProps) {
  const examples = configArray<{ input?: Record<string, unknown>; note?: string }>(config, 'examples');

  return (
    <div className="space-y-4">
      <IssuesFor issues={issues} prefix="examples" />
      <p className="text-xs text-muted">
        Examples are shown to agents in the descriptor and can be loaded into the tester with one click.
      </p>
      {examples.length === 0 ? <p className="text-xs text-muted">No examples yet.</p> : null}
      {examples.map((example, index) => (
        <div key={index} className="rounded-control border border-border p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-medium text-text">Example {index + 1}</span>
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled}
              aria-label={`Remove example ${index + 1}`}
              onClick={() => set('examples', examples.filter((_, rowIndex) => rowIndex !== index))}
              iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />}
            />
          </div>
          <div className="space-y-2">
            <JsonField
              label="Input"
              value={example.input ?? {}}
              onChange={(value) => {
                const next = [...examples];
                next[index] = { ...example, input: (value as Record<string, unknown>) ?? {} };
                set('examples', next);
              }}
              rows={6}
              requireObject
              disabled={disabled}
            />
            <Field label="Note" htmlFor={`example-${index}-note`}>
              <Input
                id={`example-${index}-note`}
                disabled={disabled}
                value={example.note ?? ''}
                onChange={(event) => {
                  const next = [...examples];
                  next[index] = { ...example, note: event.target.value };
                  set('examples', next);
                }}
              />
            </Field>
          </div>
        </div>
      ))}
      <Button
        size="sm"
        variant="secondary"
        disabled={disabled}
        onClick={() => set('examples', [...examples, { input: {}, note: '' }])}
        iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
      >
        Add example
      </Button>
    </div>
  );
}

/** Config keys that the structured panels do not cover yet. */
export function AdvancedPanel({ config, set, replace, disabled }: PanelProps) {
  const leftover = useMemo(() => {
    const known = new Set([
      'model',
      'auth',
      'provider_params',
      'request',
      'input_schema',
      'output_schema',
      'response',
      'async',
      'stream',
      'errors',
      'limits',
      'modes',
      'retry',
      'pricing',
      'dedupe_window_s',
      'examples',
    ]);
    return Object.fromEntries(Object.entries(config).filter(([key]) => !known.has(key)));
  }, [config]);

  return (
    <div className="space-y-5">
      <FormSection title="Model and provider parameters">
        <FormRow columns={2}>
          <Field label="Model" htmlFor="config-model" hint="Template value available as {model} in the URL and body.">
            <Input
              id="config-model"
              disabled={disabled}
              placeholder="gemini-2.5-flash"
              value={configString(config, 'model')}
              onChange={(event) => set('model', event.target.value || undefined)}
            />
          </Field>
          <Field label="Authentication mode" htmlFor="config-auth-mode">
            <Select
              id="config-auth-mode"
              disabled={disabled}
              value={configString(config, 'auth.mode') || 'credential'}
              options={[
                { value: 'credential', label: 'Use a credential' },
                { value: 'none', label: 'No authentication' },
              ]}
              onChange={(event) => set('auth.mode', event.target.value)}
            />
          </Field>
        </FormRow>
        <Field label="Provider parameters" htmlFor="config-provider-params" hint="Comma separated allow-list, e.g. temperature, top_p">
          <Input
            id="config-provider-params"
            disabled={disabled}
            value={(configArray<string>(config, 'provider_params') ?? []).join(', ')}
            onChange={(event) => set('provider_params', splitList(event.target.value))}
          />
        </Field>
      </FormSection>

      <FormSection
        title="Unmodelled keys"
        description="Keys without a structured field yet (kept verbatim so nothing is lost on save)."
      >
        <JsonField
          label="Remaining config keys"
          value={leftover}
          onChange={(value) => {
            const parsed = parseJsonObject(formatJsonText(value));
            if (!parsed.ok) return;
            // Re-apply every leftover key on top of the known config.
            let next = config;
            for (const [key, entry] of Object.entries(parsed.value)) {
              next = setConfigPath(next, key, entry);
            }
            replace(next);
          }}
          rows={8}
          requireObject
          disabled={disabled}
        />
      </FormSection>
    </div>
  );
}
