'use client';

/**
 * The seven SKILL workbench tabs.
 *
 * The page owns a single editor state (`EditorState`) so the tabs never disagree
 * about what is being edited. `manifestFromEditor` is the only place text becomes
 * JSON, which means an invalid inputs/outputs/blocks editor blocks the save with a
 * field-level message instead of silently sending something else.
 *
 * Honesty rules kept here:
 *  - `kind: "agent"` steps are visibly marked "needs live agent reasoning";
 *  - a publish button stays clickable so a real 409/422 from the API surfaces;
 *  - the export tab states that credentials are excluded (the bundle says
 *    `includes_credentials: false`).
 */

import { useMemo, useState } from 'react';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Boxes,
  Bot,
  Download,
  Play,
  Plus,
  Trash2,
  Workflow,
} from 'lucide-react';

import { formatDateTimeShort, formatDuration, statusLabel } from '@/lib/format';
import { formatJsonText, parseJsonObject } from '@/lib/json';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button, IconButton } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { CodeBlock, prettyJson } from '@/components/ui/code-block';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, FormRow } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { JsonEditor } from '@/components/ui/json-editor';
import { Select } from '@/components/ui/select';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import type {
  DependencyImpact,
  PlanStepKind,
  Skill,
  SkillDependency,
  SkillIssue,
  SkillManifest,
  SkillPlanStepRaw,
  SkillTestResult,
  SkillVersion,
} from './api';
import { useCapabilityOptionsQuery } from './api';

/* ------------------------------------------------------------------- editor -- */

export interface EditableStep {
  key: string;
  id: string;
  kind: PlanStepKind;
  capability: string;
  version: string;
  instruction: string;
  inputText: string;
  mapText: string;
  saveAs: string;
  onError: 'stop' | 'continue';
  note: string;
}

export interface EditorState {
  name: string;
  description: string;
  whenToUse: string;
  instructions: string;
  inputsText: string;
  outputsText: string;
  steps: EditableStep[];
  dependencies: SkillDependency[];
  permissionsText: string;
}

let stepKey = 0;
function newStepKey(): string {
  stepKey += 1;
  return `step-${Date.now()}-${stepKey}`;
}

export function stepsFromManifest(manifest: SkillManifest): EditableStep[] {
  return (manifest.plan ?? []).map((step, index) => ({
    key: newStepKey(),
    id: typeof step.id === 'string' && step.id ? step.id : `step${index + 1}`,
    kind: (step.kind === 'agent' || step.kind === 'transform' ? step.kind : 'capability') as PlanStepKind,
    capability: typeof step.capability === 'string' ? step.capability : '',
    version: step.version === null || step.version === undefined ? '' : String(step.version),
    instruction: typeof step.instruction === 'string' ? step.instruction : '',
    inputText: typeof step.input === 'string' ? step.input : formatJsonText(step.input ?? {}),
    mapText: formatJsonText(step.map ?? step.set ?? {}),
    saveAs: step.save_as ?? '',
    onError: step.on_error === 'continue' ? 'continue' : 'stop',
    note: step.note ?? '',
  }));
}

export function editorFromVersion(skill: Skill, version: SkillVersion | null): EditorState {
  const manifest: SkillManifest = version?.manifest ?? {
    name: skill.name,
    description: skill.description,
    when_to_use: '',
    inputs: { type: 'object', properties: {} },
    outputs: { type: 'object', properties: {} },
    dependencies: [],
    permissions: {},
    instructions: '',
    plan: [],
    examples: [],
    sources: [],
  };
  return {
    name: manifest.name ?? skill.name,
    description: manifest.description ?? skill.description,
    whenToUse: manifest.when_to_use ?? '',
    instructions: manifest.instructions ?? '',
    inputsText: formatJsonText(manifest.inputs ?? { type: 'object', properties: {} }),
    outputsText: formatJsonText(manifest.outputs ?? { type: 'object', properties: {} }),
    steps: stepsFromManifest(manifest),
    dependencies: manifest.dependencies ?? [],
    permissionsText: formatJsonText(manifest.permissions ?? {}),
  };
}

export type ManifestBuild = { ok: true; manifest: SkillManifest } | { ok: false; error: string; field: string };

export function manifestFromEditor(base: SkillManifest, state: EditorState): ManifestBuild {
  const inputs = parseJsonObject(state.inputsText);
  if (!inputs.ok) return { ok: false, error: `Inputs schema: ${inputs.error}`, field: 'inputs' };
  const outputs = parseJsonObject(state.outputsText);
  if (!outputs.ok) return { ok: false, error: `Outputs schema: ${outputs.error}`, field: 'outputs' };
  const permissions = parseJsonObject(state.permissionsText);
  if (!permissions.ok) return { ok: false, error: `Permissions: ${permissions.error}`, field: 'permissions' };

  const plan: SkillPlanStepRaw[] = [];
  for (const [index, step] of state.steps.entries()) {
    const id = step.id.trim() || `step${index + 1}`;
    if (step.kind === 'capability') {
      if (!step.capability.trim()) {
        return { ok: false, error: `Step ${id}: a capability step needs a capability slug.`, field: `plan[${index}].capability` };
      }
      const input = parseJsonObject(step.inputText);
      if (!input.ok) return { ok: false, error: `Step ${id} input: ${input.error}`, field: `plan[${index}].input` };
      plan.push({
        id,
        kind: 'capability',
        capability: step.capability.trim(),
        version: step.version.trim() ? step.version.trim() : null,
        input: input.value,
        save_as: step.saveAs.trim() || null,
        on_error: step.onError,
        note: step.note.trim() || null,
      });
    } else if (step.kind === 'agent') {
      if (!step.instruction.trim()) {
        return { ok: false, error: `Step ${id}: an agent step needs an instruction.`, field: `plan[${index}].instruction` };
      }
      plan.push({
        id,
        kind: 'agent',
        instruction: step.instruction,
        save_as: step.saveAs.trim() || null,
        on_error: step.onError,
        note: step.note.trim() || null,
      });
    } else {
      const map = parseJsonObject(step.mapText);
      if (!map.ok) return { ok: false, error: `Step ${id} map: ${map.error}`, field: `plan[${index}].map` };
      if (Object.keys(map.value).length === 0) {
        return { ok: false, error: `Step ${id}: a transform step needs at least one key in its map.`, field: `plan[${index}].map` };
      }
      plan.push({
        id,
        kind: 'transform',
        map: map.value as Record<string, string>,
        save_as: step.saveAs.trim() || null,
        on_error: step.onError,
        note: step.note.trim() || null,
      });
    }
  }

  return {
    ok: true,
    manifest: {
      ...base,
      name: state.name.trim() || base.name,
      description: state.description,
      when_to_use: state.whenToUse,
      instructions: state.instructions,
      inputs: inputs.value,
      outputs: outputs.value,
      dependencies: state.dependencies,
      permissions: permissions.value,
      plan,
    },
  };
}

/* ---------------------------------------------------------------- manifest tab -- */

export function ManifestTab({
  state,
  onChange,
  disabled,
  issues,
  readOnlyReason,
}: {
  state: EditorState;
  onChange: (patch: Partial<EditorState>) => void;
  disabled: boolean;
  issues: SkillIssue[];
  readOnlyReason?: string | null;
}) {
  const issuesFor = (prefix: string) => issues.filter((issue) => issue.field.startsWith(prefix));
  return (
    <div className="space-y-4">
      {readOnlyReason ? <Banner tone="info" title="Read-only version">{readOnlyReason}</Banner> : null}

      <Card>
        <CardHeader title="Catalogue text" description="What the agent reads before choosing this SKILL." />
        <CardBody className="space-y-3">
          <FormRow columns={2}>
            <Field label="Name" htmlFor="manifest-name" error={issuesFor('name')[0]?.message ?? null}>
              <Input
                id="manifest-name"
                value={state.name}
                disabled={disabled}
                onChange={(event) => onChange({ name: event.target.value })}
              />
            </Field>
            <Field label="Description" htmlFor="manifest-description" error={issuesFor('description')[0]?.message ?? null}>
              <Input
                id="manifest-description"
                value={state.description}
                disabled={disabled}
                onChange={(event) => onChange({ description: event.target.value })}
              />
            </Field>
          </FormRow>
          <Field
            label="When to use"
            htmlFor="manifest-when"
            hint="A warning is raised while this is empty — the agent has no trigger."
            error={issuesFor('when_to_use')[0]?.message ?? null}
          >
            <Textarea
              id="manifest-when"
              value={state.whenToUse}
              rows={2}
              disabled={disabled}
              onChange={(event) => onChange({ whenToUse: event.target.value })}
            />
          </Field>
          <Field
            label="Instructions (markdown)"
            htmlFor="manifest-instructions"
            hint="Shipped verbatim inside the exported SKILL bundle."
            error={issuesFor('instructions')[0]?.message ?? null}
          >
            <Textarea
              id="manifest-instructions"
              value={state.instructions}
              rows={10}
              monospace
              disabled={disabled}
              onChange={(event) => onChange({ instructions: event.target.value })}
            />
          </Field>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Inputs and outputs"
          description="JSON Schema. The exported bundle carries both so an agent can validate before calling."
        />
        <CardBody className="grid gap-3 lg:grid-cols-2">
          <JsonEditor
            label="Inputs schema"
            value={state.inputsText}
            requireObject
            rows={12}
            disabled={disabled}
            onChange={(value) => onChange({ inputsText: value })}
            externalError={issuesFor('inputs')[0]?.message ?? null}
          />
          <JsonEditor
            label="Outputs schema"
            value={state.outputsText}
            requireObject
            rows={12}
            disabled={disabled}
            onChange={(value) => onChange({ outputsText: value })}
            externalError={issuesFor('outputs')[0]?.message ?? null}
          />
        </CardBody>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------- plan tab -- */

const KIND_OPTIONS = [
  { value: 'capability', label: 'Capability call' },
  { value: 'agent', label: 'Agent step (needs live reasoning)' },
  { value: 'transform', label: 'Transform' },
];

export function PlanTab({
  state,
  onChange,
  disabled,
  issues,
}: {
  state: EditorState;
  onChange: (patch: Partial<EditorState>) => void;
  disabled: boolean;
  issues: SkillIssue[];
}) {
  const capabilities = useCapabilityOptionsQuery();
  const options = capabilities.data ?? [];

  const update = (key: string, patch: Partial<EditableStep>) => {
    onChange({ steps: state.steps.map((step) => (step.key === key ? { ...step, ...patch } : step)) });
  };

  const move = (index: number, direction: -1 | 1) => {
    const next = [...state.steps];
    const target = index + direction;
    if (target < 0 || target >= next.length) return;
    const current = next[index];
    next[index] = next[target];
    next[target] = current;
    onChange({ steps: next });
  };

  const add = (kind: PlanStepKind) => {
    const index = state.steps.length + 1;
    onChange({
      steps: [
        ...state.steps,
        {
          key: newStepKey(),
          id: `step${index}`,
          kind,
          capability: '',
          version: '',
          instruction: '',
          inputText: '{}',
          mapText: '{\n  "value": "{{ input.value }}"\n}',
          saveAs: '',
          onError: 'stop',
          note: '',
        },
      ],
    });
  };

  return (
    <div className="space-y-3">
      <Banner tone="info" title="Templates">
        Step inputs are template maps: <code>{'{{ input.x }}'}</code> reads the SKILL input and{' '}
        <code>{'{{ steps.id.x }}'}</code> reads a previous step result. Agent steps are the only ones that need live
        reasoning and cannot execute inside Skill Router.
      </Banner>

      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant="secondary" disabled={disabled} onClick={() => add('capability')} iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}>
          Capability step
        </Button>
        <Button size="sm" variant="secondary" disabled={disabled} onClick={() => add('agent')} iconLeft={<Bot aria-hidden className="h-3.5 w-3.5" />}>
          Agent step
        </Button>
        <Button size="sm" variant="secondary" disabled={disabled} onClick={() => add('transform')} iconLeft={<Workflow aria-hidden className="h-3.5 w-3.5" />}>
          Transform step
        </Button>
        <span className="text-xs text-muted">{state.steps.length} step(s)</span>
      </div>

      {state.steps.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Workflow aria-hidden className="h-5 w-5" />}
            title="The plan is empty"
            description="An empty plan cannot run or publish. Add at least one capability step."
          />
        </Card>
      ) : null}

      <ol className="space-y-3">
        {state.steps.map((step, index) => {
          const stepIssues = issues.filter((issue) => issue.field.startsWith(`plan[${index}]`));
          return (
            <li key={step.key}>
              <Card>
                <CardHeader
                  title={
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-muted">#{index + 1}</span>
                      <span>{step.id || 'unnamed step'}</span>
                      <Badge tone={step.kind === 'agent' ? 'warning' : step.kind === 'transform' ? 'neutral' : 'accent'} dot={false}>
                        {statusLabel(step.kind)}
                      </Badge>
                      {step.kind === 'agent' ? <Badge tone="warning">Needs live agent reasoning</Badge> : null}
                    </span>
                  }
                  actions={
                    <>
                      <IconButton
                        label={`Move step ${index + 1} up`}
                        size="sm"
                        disabled={disabled || index === 0}
                        onClick={() => move(index, -1)}
                      >
                        <ArrowUp aria-hidden className="h-3.5 w-3.5" />
                      </IconButton>
                      <IconButton
                        label={`Move step ${index + 1} down`}
                        size="sm"
                        disabled={disabled || index === state.steps.length - 1}
                        onClick={() => move(index, 1)}
                      >
                        <ArrowDown aria-hidden className="h-3.5 w-3.5" />
                      </IconButton>
                      <IconButton
                        label={`Remove step ${index + 1}`}
                        size="sm"
                        disabled={disabled}
                        onClick={() => onChange({ steps: state.steps.filter((item) => item.key !== step.key) })}
                      >
                        <Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />
                      </IconButton>
                    </>
                  }
                />
                <CardBody className="space-y-3">
                  {stepIssues.length > 0 ? (
                    <div className="space-y-1">
                      {stepIssues.map((issue) => (
                        <InlineAlert key={`${issue.field}-${issue.message}`} tone={issue.severity === 'error' ? 'danger' : 'warning'}>
                          <span className="font-mono">{issue.field}</span> — {issue.message}
                        </InlineAlert>
                      ))}
                    </div>
                  ) : null}

                  <FormRow columns={3}>
                    <Field label="Step id" htmlFor={`step-id-${step.key}`} hint="Referenced by later steps as steps.<id>.x">
                      <Input
                        id={`step-id-${step.key}`}
                        value={step.id}
                        disabled={disabled}
                        onChange={(event) => update(step.key, { id: event.target.value })}
                      />
                    </Field>
                    <Field label="Kind" htmlFor={`step-kind-${step.key}`}>
                      <Select
                        id={`step-kind-${step.key}`}
                        value={step.kind}
                        disabled={disabled}
                        options={KIND_OPTIONS}
                        onChange={(event) => update(step.key, { kind: event.target.value as PlanStepKind })}
                      />
                    </Field>
                    <Field label="Result name (save_as)" htmlFor={`step-save-${step.key}`} hint="Defaults to the step id.">
                      <Input
                        id={`step-save-${step.key}`}
                        value={step.saveAs}
                        disabled={disabled}
                        placeholder={step.id}
                        onChange={(event) => update(step.key, { saveAs: event.target.value })}
                      />
                    </Field>
                  </FormRow>

                  {step.kind === 'capability' ? (
                    <>
                      <FormRow columns={2}>
                        <Field
                          label="Capability slug"
                          htmlFor={`step-cap-${step.key}`}
                          required
                          hint={
                            options.length > 0
                              ? 'Pick a capability; the slug must resolve to a published version.'
                              : 'No capabilities loaded — type the slug manually.'
                          }
                        >
                          <Input
                            id={`step-cap-${step.key}`}
                            list={`capability-options-${step.key}`}
                            value={step.capability}
                            disabled={disabled}
                            placeholder="text.summarize"
                            onChange={(event) => update(step.key, { capability: event.target.value })}
                          />
                          <datalist id={`capability-options-${step.key}`}>
                            {options.map((option) => (
                              <option key={option.id} value={option.slug}>
                                {option.name} ({option.status})
                              </option>
                            ))}
                          </datalist>
                        </Field>
                        <Field
                          label="Version"
                          htmlFor={`step-version-${step.key}`}
                          hint="Blank uses the published version chosen at run time."
                        >
                          <Input
                            id={`step-version-${step.key}`}
                            value={step.version}
                            disabled={disabled}
                            placeholder="2"
                            onChange={(event) => update(step.key, { version: event.target.value })}
                          />
                        </Field>
                      </FormRow>
                      <JsonEditor
                        label="Input template"
                        value={step.inputText}
                        requireObject
                        rows={6}
                        disabled={disabled}
                        onChange={(value) => update(step.key, { inputText: value })}
                        hint={'Example: { "prompt": "{{ input.topic }}", "context": "{{ steps.step1.result }}" }'}
                      />
                    </>
                  ) : null}

                  {step.kind === 'agent' ? (
                    <>
                      <InlineAlert tone="warning">
                        This step needs the AragonAgent engine. Running the SKILL inside Skill Router stops here with{' '}
                        <code>engine_required</code>; the exported bundle tells the calling agent what to do.
                      </InlineAlert>
                      <Field label="Instruction for the live agent" htmlFor={`step-instruction-${step.key}`} required>
                        <Textarea
                          id={`step-instruction-${step.key}`}
                          value={step.instruction}
                          rows={4}
                          disabled={disabled}
                          onChange={(event) => update(step.key, { instruction: event.target.value })}
                        />
                      </Field>
                    </>
                  ) : null}

                  {step.kind === 'transform' ? (
                    <JsonEditor
                      label="Map (templates only)"
                      value={step.mapText}
                      requireObject
                      rows={6}
                      disabled={disabled}
                      onChange={(value) => update(step.key, { mapText: value })}
                      hint="Each value is a template; no code is ever run."
                    />
                  ) : null}

                  <FormRow columns={2}>
                    <Field label="On error" htmlFor={`step-onerror-${step.key}`}>
                      <Select
                        id={`step-onerror-${step.key}`}
                        value={step.onError}
                        disabled={disabled}
                        options={[
                          { value: 'stop', label: 'Stop the plan' },
                          { value: 'continue', label: 'Continue with the next step' },
                        ]}
                        onChange={(event) => update(step.key, { onError: event.target.value as 'stop' | 'continue' })}
                      />
                    </Field>
                    <Field label="Note" htmlFor={`step-note-${step.key}`}>
                      <Input
                        id={`step-note-${step.key}`}
                        value={step.note}
                        disabled={disabled}
                        onChange={(event) => update(step.key, { note: event.target.value })}
                      />
                    </Field>
                  </FormRow>
                </CardBody>
              </Card>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/* -------------------------------------------------------------- dependencies -- */

export function DependenciesTab({
  state,
  onChange,
  disabled,
  impact,
  impactLoading,
  impactError,
  onReloadImpact,
}: {
  state: EditorState;
  onChange: (patch: Partial<EditorState>) => void;
  disabled: boolean;
  impact: DependencyImpact | undefined;
  impactLoading: boolean;
  impactError: Error | null;
  onReloadImpact: () => void;
}) {
  const capabilities = useCapabilityOptionsQuery();

  const update = (index: number, patch: Partial<SkillDependency>) => {
    onChange({ dependencies: state.dependencies.map((item, i) => (i === index ? { ...item, ...patch } : item)) });
  };

  return (
    <div className="space-y-3">
      {impactError ? (
        <Banner
          tone="danger"
          title="Could not read the dependency impact"
          primaryAction={{ label: 'Retry', onClick: onReloadImpact, loading: impactLoading }}
        >
          {impactError.message}
        </Banner>
      ) : impact ? (
        <Banner
          tone={impact.blocking ? 'warning' : 'success'}
          title={impact.blocking ? 'A dependency does not resolve to a published version' : 'All dependencies resolve'}
        >
          {impact.summary}
          {impact.blocking
            ? ' Publishing will be refused until each capability has a published version (or the requested version is published).'
            : ''}
        </Banner>
      ) : null}

      <Card>
        <CardHeader
          title="Declared dependencies"
          description="Each dependency must resolve to a published capability version before the SKILL can publish."
          actions={
            <Button
              size="sm"
              variant="secondary"
              disabled={disabled}
              onClick={() => onChange({ dependencies: [...state.dependencies, { capability: '', required: true }] })}
              iconLeft={<Plus aria-hidden className="h-3.5 w-3.5" />}
            >
              Add dependency
            </Button>
          }
        />
        <CardBody className="space-y-2">
          {state.dependencies.length === 0 ? (
            <p className="text-xs text-muted">No dependencies declared.</p>
          ) : (
            state.dependencies.map((dependency, index) => (
              <div key={`${dependency.capability}-${index}`} className="flex flex-wrap items-end gap-2">
                <div className="min-w-[200px] flex-1">
                  <Field label={`Capability ${index + 1}`} htmlFor={`dep-cap-${index}`}>
                    <Input
                      id={`dep-cap-${index}`}
                      list="dependency-capabilities"
                      value={dependency.capability}
                      disabled={disabled}
                      onChange={(event) => update(index, { capability: event.target.value })}
                    />
                  </Field>
                </div>
                <div className="w-28">
                  <Field label="Version" htmlFor={`dep-version-${index}`}>
                    <Input
                      id={`dep-version-${index}`}
                      value={dependency.version === null || dependency.version === undefined ? '' : String(dependency.version)}
                      disabled={disabled}
                      placeholder="any"
                      onChange={(event) => update(index, { version: event.target.value || null })}
                    />
                  </Field>
                </div>
                <div className="pb-2">
                  <IconButton
                    label={`Remove dependency ${index + 1}`}
                    size="sm"
                    disabled={disabled}
                    onClick={() => onChange({ dependencies: state.dependencies.filter((_, i) => i !== index) })}
                  >
                    <Trash2 aria-hidden className="h-3.5 w-3.5 text-danger" />
                  </IconButton>
                </div>
              </div>
            ))
          )}
          <datalist id="dependency-capabilities">
            {(capabilities.data ?? []).map((option) => (
              <option key={option.id} value={option.slug}>
                {option.name} ({option.status})
              </option>
            ))}
          </datalist>
        </CardBody>
      </Card>

      {impact && impact.impact.length > 0 ? (
        <Card>
          <CardHeader
            title="Impact"
            description="What would break if a capability version were depublished or deleted."
            actions={
              <Button size="sm" variant="ghost" onClick={onReloadImpact} loading={impactLoading}>
                Re-check
              </Button>
            }
          />
          <CardBody className="p-0">
            <Table caption="Dependency impact">
              <THead sticky={false}>
                <TR>
                  <TH>Capability</TH>
                  <TH>Requested</TH>
                  <TH>Published</TH>
                  <TH>Used in steps</TH>
                  <TH>State</TH>
                </TR>
              </THead>
              <TBody>
                {impact.impact.map((entry) => (
                  <TR key={entry.capability}>
                    <TD>
                      <span className="font-mono text-xs">{entry.capability}</span>
                      <span className="block text-[11px] text-muted">{entry.capability_name ?? 'unknown capability'}</span>
                    </TD>
                    <TD className="text-xs">{entry.requested_versions.join(', ') || 'any'}</TD>
                    <TD className="text-xs">
                      {entry.published_versions.length === 0 ? (
                        <span className="text-danger">none</span>
                      ) : (
                        entry.published_versions.map((item) => `v${item.version}`).join(', ')
                      )}
                    </TD>
                    <TD className="text-xs">{entry.step_ids.join(', ') || '—'}</TD>
                    <TD>
                      <Badge tone={entry.blocking ? 'danger' : entry.exists ? 'success' : 'danger'} dot={false}>
                        {entry.blocking ? 'blocking' : entry.exists ? 'resolvable' : 'missing'}
                      </Badge>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------- permissions -- */

export function PermissionsTab({
  state,
  onChange,
  disabled,
  issues,
}: {
  state: EditorState;
  onChange: (patch: Partial<EditorState>) => void;
  disabled: boolean;
  issues: SkillIssue[];
}) {
  const permissionIssues = issues.filter((issue) => issue.field.startsWith('permissions'));
  return (
    <div className="space-y-3">
      <Banner tone="info" title="Permissions are a declaration, not a grant">
        The platform enforces credentials and scopes at call time. This block tells the calling agent and the
        administrator what the SKILL expects; the exported bundle lists it verbatim and contains no secret material.
      </Banner>
      {permissionIssues.map((issue) => (
        <InlineAlert key={`${issue.field}-${issue.message}`} tone={issue.severity === 'error' ? 'danger' : 'warning'}>
          <span className="font-mono">{issue.field}</span> — {issue.message}
        </InlineAlert>
      ))}
      <JsonEditor
        label="Permissions"
        value={state.permissionsText}
        requireObject
        rows={12}
        disabled={disabled}
        onChange={(value) => onChange({ permissionsText: value })}
        hint={'{ "network": ["api.example.com"], "files": ["read"], "credentials": ["cred_…"] }'}
      />
    </div>
  );
}

/* ------------------------------------------------------------------- versions -- */

export interface ManifestDiffEntry {
  field: string;
  before: string;
  after: string;
}

export function diffManifests(before: SkillManifest | undefined, after: SkillManifest | undefined): ManifestDiffEntry[] {
  if (!before || !after) return [];
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  const changes: ManifestDiffEntry[] = [];
  for (const key of keys) {
    const leftRecord = before as unknown as Record<string, unknown>;
    const rightRecord = after as unknown as Record<string, unknown>;
    const left = JSON.stringify(leftRecord[key] ?? null);
    const right = JSON.stringify(rightRecord[key] ?? null);
    if (left !== right) {
      changes.push({ field: key, before: left, after: right });
    }
  }
  return changes;
}

export function VersionsTab({
  skill,
  onPublish,
  publishingId,
  onValidate,
  validatingId,
  validation,
  validationError,
}: {
  skill: Skill;
  onPublish: (versionId: string) => void;
  publishingId: string | null;
  onValidate: (versionId: string) => void;
  validatingId: string | null;
  validation: { versionId: string; issues: SkillIssue[]; blocking: boolean } | null;
  validationError: string | null;
}) {
  const versions = skill.versions ?? [];
  const [compareFrom, setCompareFrom] = useState(versions[1]?.id ?? '');
  const [compareTo, setCompareTo] = useState(versions[0]?.id ?? '');

  const from = versions.find((item) => item.id === compareFrom);
  const to = versions.find((item) => item.id === compareTo);
  const diff = useMemo(() => diffManifests(from?.manifest, to?.manifest), [from?.manifest, to?.manifest]);

  if (versions.length === 0) {
    return (
      <Card>
        <EmptyState title="No versions" description="Save the editor once to create version 1." />
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {validationError ? <Banner tone="danger" title="Validation failed">{validationError}</Banner> : null}
      {validation ? (
        <Banner
          tone={validation.blocking ? 'danger' : 'success'}
          title={validation.blocking ? 'This version cannot publish yet' : 'Validation passed for this version'}
        >
          {validation.issues.length === 0 ? (
            'No structural issues were reported.'
          ) : (
            <ul className="list-disc pl-4">
              {validation.issues.map((issue) => (
                <li key={`${issue.field}-${issue.message}`}>
                  <span className="font-mono">{issue.field}</span> — {issue.message}
                </li>
              ))}
            </ul>
          )}
        </Banner>
      ) : null}

      <Card>
        <CardHeader title="Versions" description="Publishing freezes a version; edit a draft instead of a published row." />
        <CardBody className="p-0">
          <Table caption="SKILL versions">
            <THead sticky={false}>
              <TR>
                <TH>Version</TH>
                <TH>Status</TH>
                <TH>Steps</TH>
                <TH>Checksum</TH>
                <TH>Created</TH>
                <TH>Published</TH>
                <TH align="right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {versions.map((version) => (
                <TR key={version.id}>
                  <TD className="font-mono text-xs">v{version.version}</TD>
                  <TD>
                    <StatusBadge status={version.status} />
                    {version.requires_live_agent ? (
                      <span className="ml-1">
                        <Badge tone="warning" dot={false}>
                          agent step
                        </Badge>
                      </span>
                    ) : null}
                  </TD>
                  <TD className="text-xs tabular-nums">{version.step_count}</TD>
                  <TD className="font-mono text-[11px] text-muted">{version.checksum.slice(0, 12)}…</TD>
                  <TD className="text-xs text-muted">{formatDateTimeShort(version.created_at)}</TD>
                  <TD className="text-xs text-muted">
                    {version.published_at ? <TimeAgo value={version.published_at} /> : '—'}
                  </TD>
                  <TD align="right">
                    <span className="inline-flex flex-wrap items-center justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onValidate(version.id)}
                        loading={validatingId === version.id}
                      >
                        Validate
                      </Button>
                      <Button
                        size="sm"
                        variant={version.status === 'published' ? 'secondary' : 'primary'}
                        onClick={() => onPublish(version.id)}
                        loading={publishingId === version.id}
                        disabled={version.status === 'published'}
                      >
                        {version.status === 'published' ? 'Published' : 'Publish'}
                      </Button>
                    </span>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardBody>
        <CardFooter>
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-36">
              <Field label="Compare from" htmlFor="diff-from">
                <Select
                  id="diff-from"
                  value={compareFrom}
                  options={versions.map((version) => ({ value: version.id, label: `v${version.version}` }))}
                  onChange={(event) => setCompareFrom(event.target.value)}
                />
              </Field>
            </div>
            <div className="w-36">
              <Field label="Compare to" htmlFor="diff-to">
                <Select
                  id="diff-to"
                  value={compareTo}
                  options={versions.map((version) => ({ value: version.id, label: `v${version.version}` }))}
                  onChange={(event) => setCompareTo(event.target.value)}
                />
              </Field>
            </div>
            <span className="pb-2 text-xs text-muted">
              {compareFrom === compareTo
                ? 'Pick two different versions.'
                : diff.length === 0
                  ? 'No manifest difference between these versions.'
                  : `${diff.length} manifest field(s) differ.`}
            </span>
          </div>
        </CardFooter>
      </Card>

      {compareFrom !== compareTo && diff.length > 0 ? (
        <Card>
          <CardHeader title="Manifest diff" description="Field-level comparison of the stored manifests." />
          <CardBody className="space-y-2">
            {diff.map((entry) => (
              <div key={entry.field} className="rounded-control border border-border p-2">
                <p className="font-mono text-xs text-text">{entry.field}</p>
                <div className="mt-1 grid gap-1.5 md:grid-cols-2">
                  <CodeBlock code={entry.before} label="before" maxHeightClassName="max-h-32" copyable={false} />
                  <CodeBlock code={entry.after} label="after" maxHeightClassName="max-h-32" copyable={false} />
                </div>
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}

/* ----------------------------------------------------------------------- test -- */

export function TestTab({
  versions,
  selectedVersionId,
  onSelectVersion,
  inputText,
  onInputChange,
  onRun,
  running,
  result,
  error,
  errorRequestId,
}: {
  versions: SkillVersion[];
  selectedVersionId: string;
  onSelectVersion: (versionId: string) => void;
  inputText: string;
  onInputChange: (value: string) => void;
  onRun: () => void;
  running: boolean;
  result: SkillTestResult | null;
  error: string | null;
  errorRequestId: string | null;
}) {
  return (
    <div className="space-y-3">
      <Banner tone="info" title="A test is a real call">
        Running the plan executes each capability step for real through the router, so it produces invocations, usage and
        cost. A step of kind <code>agent</code> stops the run with <code>engine_required</code> — the platform does not
        simulate reasoning.
      </Banner>

      <Card>
        <CardHeader
          title="Run the plan"
          description="Input JSON is validated against the manifest input schema by the API."
          actions={
            <div className="flex items-end gap-2">
              <div className="w-32">
                <Field label="Version" htmlFor="test-version">
                  <Select
                    id="test-version"
                    value={selectedVersionId}
                    options={versions.map((version) => ({
                      value: version.id,
                      label: `v${version.version} (${version.status})`,
                    }))}
                    onChange={(event) => onSelectVersion(event.target.value)}
                  />
                </Field>
              </div>
              <Button variant="primary" size="sm" onClick={onRun} loading={running} iconLeft={<Play aria-hidden className="h-3.5 w-3.5" />}>
                Run
              </Button>
            </div>
          }
        />
        <CardBody className="space-y-3">
          <JsonEditor label="Input JSON" value={inputText} requireObject rows={8} onChange={onInputChange} />
          {error ? (
            <Banner tone="danger" title="The run failed" requestId={errorRequestId}>
              {error}
            </Banner>
          ) : null}
        </CardBody>
      </Card>

      {result ? (
        <Card>
          <CardHeader
            title="Step results"
            description={`Version ${result.version.version} · ${formatDuration(result.duration_ms)} · surface ${result.surface}`}
            actions={<StatusBadge status={result.status} />}
          />
          <CardBody className="p-0">
            <Table caption="Step results">
              <THead sticky={false}>
                <TR>
                  <TH>Step</TH>
                  <TH>Kind</TH>
                  <TH>Status</TH>
                  <TH>Capability</TH>
                  <TH>Latency</TH>
                  <TH>Result</TH>
                </TR>
              </THead>
              <TBody>
                {result.steps.map((step) => (
                  <TR key={`${step.id}-${step.status}`}>
                    <TD className="font-mono text-xs">{step.id}</TD>
                    <TD>
                      <Badge tone={step.kind === 'agent' ? 'warning' : 'neutral'} dot={false}>
                        {statusLabel(step.kind)}
                      </Badge>
                    </TD>
                    <TD>
                      <StatusBadge status={step.status} />
                    </TD>
                    <TD className="font-mono text-[11px]">{step.capability ?? '—'}</TD>
                    <TD className="text-xs tabular-nums">{formatDuration(step.duration_ms)}</TD>
                    <TD>
                      {step.error ? (
                        <span className="text-xs text-danger">
                          {step.error.code}: {step.error.message}
                        </span>
                      ) : (
                        <span className="line-clamp-2 block max-w-[320px] break-words text-[11px] text-muted">
                          {step.result_preview ?? '—'}
                        </span>
                      )}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardBody>
          <CardFooter>
            <span className="text-xs text-muted">
              usage {prettyJson(result.usage) || '—'} · output {result.output === null || result.output === undefined ? '—' : 'shown below'}
            </span>
          </CardFooter>
        </Card>
      ) : null}

      {result ? (
        <Card>
          <CardHeader title="Final output" description="Step results keyed by save_as." />
          <CardBody className="grid gap-2 lg:grid-cols-2">
            <CodeBlock code={prettyJson(result.output)} label="output" maxHeightClassName="max-h-64" />
            <CodeBlock code={prettyJson(result.outputs)} label="all step results" maxHeightClassName="max-h-64" />
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------- export -- */

export function ExportTab({
  skill,
  onDownload,
  downloading,
  error,
}: {
  skill: Skill;
  onDownload: (format: 'skill' | 'json') => void;
  downloading: 'skill' | 'json' | null;
  error: string | null;
}) {
  const requiresAgent = skill.current_version?.requires_live_agent ?? skill.requires_live_agent;
  return (
    <div className="space-y-3">
      <Banner tone="info" title="Credentials are excluded from every export">
        The bundle carries instructions, the plan, the input/output schemas and capability descriptors only. The API
        reports <code>includes_credentials: false</code>; request bodies, header values and credential material are
        never part of it.
      </Banner>

      {requiresAgent ? (
        <Banner tone="warning" title="This SKILL contains agent steps">
          The exported bundle marks those steps <code>requires_live_agent</code>: the calling agent must perform them,
          Skill Router cannot.
        </Banner>
      ) : null}

      {error ? <Banner tone="danger" title="The export failed">{error}</Banner> : null}

      <Card>
        <CardHeader title="Download" description="Both formats describe the current version." />
        <CardBody className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            onClick={() => onDownload('skill')}
            loading={downloading === 'skill'}
            iconLeft={<Download aria-hidden className="h-4 w-4" />}
          >
            Download SKILL (markdown)
          </Button>
          <Button
            variant="secondary"
            onClick={() => onDownload('json')}
            loading={downloading === 'json'}
            iconLeft={<Download aria-hidden className="h-4 w-4" />}
          >
            Download JSON bundle
          </Button>
        </CardBody>
        <CardFooter>
          <span className="text-xs text-muted">
            <Boxes aria-hidden className="mr-1 inline h-3 w-3" />
            Current version: {skill.current_version ? `v${skill.current_version.version} (${skill.current_version.status})` : 'none'}
          </span>
          <span className="inline-flex items-center gap-1 text-xs text-warning">
            <AlertTriangle aria-hidden className="h-3 w-3" /> No secrets, ever
          </span>
        </CardFooter>
      </Card>

      {skill.current_version?.manifest ? (
        <CodeBlock
          code={prettyJson(skill.current_version.manifest)}
          label="stored manifest (as saved)"
          maxHeightClassName="max-h-80"
          notice="Read-only view of what is stored for the current version."
        />
      ) : null}
    </div>
  );
}
