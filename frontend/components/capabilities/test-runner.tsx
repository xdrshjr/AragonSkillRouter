'use client';

import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, FlaskConical, Play, XCircle } from 'lucide-react';

import { describeError, errorActions, errorRequestId } from '@/lib/api';
import { useTestVersionMutation } from '@/lib/queries';
import type { CapabilityExample, InvocationMode, TestVersionResponse } from '@/lib/types';
import { formatDuration, statusLabel } from '@/lib/format';
import { formatJsonText, parseJsonObject } from '@/lib/json';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CodeBlock, prettyJson } from '@/components/ui/code-block';
import { Dialog } from '@/components/ui/dialog';
import { Field } from '@/components/ui/field';
import { JsonEditor } from '@/components/ui/json-editor';
import { Select } from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';

function TestResultView({ result }: { result: TestVersionResponse }) {
  const payload = result.result ?? {};
  const passed = result.test_passed;
  const request = payload.request;
  const response = payload.response;
  const extracted = payload.result;
  const error = payload.error;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded-control border border-border p-3">
        <Badge tone={passed ? 'success' : 'danger'} dot={false}>
          {passed ? (
            <>
              <CheckCircle2 aria-hidden className="h-3.5 w-3.5" /> Test passed
            </>
          ) : (
            <>
              <XCircle aria-hidden className="h-3.5 w-3.5" /> Test failed
            </>
          )}
        </Badge>
        <StatusBadge status={result.status} />
        <span className="text-xs text-muted">
          HTTP {payload.http_status ?? '—'} · {formatDuration(payload.latency_ms ?? null)} · evidence{' '}
          <span className="font-mono">{result.evidence.id}</span>
        </span>
        <span className="ml-auto text-xs text-muted">
          saved: {result.saved ? 'yes' : 'no'} · callable: {result.callable ? 'yes' : 'no'} · published v
          {result.published_version ?? '—'}
        </span>
      </div>

      {!passed ? (
        <Banner tone="danger" title="This version cannot be published yet" actions={result.actions}>
          Publishing requires a passing test for this exact version. Fix the configuration and run the test again.
        </Banner>
      ) : (
        <Banner tone="success" title="Real call succeeded — evidence stored">
          Publishing is now allowed for version {result.version}. A new version needs its own passing test.
        </Banner>
      )}

      {error ? (
        <InlineAlert tone="danger">
          <span className="font-mono">{error.code ?? 'error'}</span> — {error.message ?? 'no message'}
        </InlineAlert>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-2">
        <CodeBlock label="Request sent (redacted)" code={prettyJson(request)} maxHeightClassName="max-h-56" emptyLabel="No request captured." />
        <CodeBlock label="Extracted result" code={prettyJson(extracted)} maxHeightClassName="max-h-56" emptyLabel="No result extracted." />
      </div>
      <CodeBlock
        label="Raw upstream response (redacted)"
        code={prettyJson(response)}
        maxHeightClassName="max-h-64"
        emptyLabel="No response captured."
      />
      <CodeBlock
        label="Full test evidence"
        code={prettyJson(result.evidence)}
        maxHeightClassName="max-h-48"
        emptyLabel="No evidence."
      />
    </div>
  );
}

export interface TestRunnerProps {
  open: boolean;
  onClose: () => void;
  capabilityId: string;
  versionId: string;
  versionNumber: number;
  examples?: CapabilityExample[];
  /** Input from the last test, if any. */
  initialInput?: string;
  modes?: Array<'sync' | 'async' | 'stream'>;
}

/** Runs a real call (POST .../test). Nothing here fakes a result. */
export function TestRunner({
  open,
  onClose,
  capabilityId,
  versionId,
  versionNumber,
  examples = [],
  initialInput,
  modes = ['sync'],
}: TestRunnerProps) {
  const toast = useToast();
  const test = useTestVersionMutation(capabilityId, versionId);
  const [inputText, setInputText] = useState(initialInput ?? '{}');
  const [mode, setMode] = useState<InvocationMode>(modes[0] ?? 'sync');

  useEffect(() => {
    if (open) {
      setInputText(initialInput ?? '{}');
      setMode(modes[0] ?? 'sync');
    }
    // Re-seed only when the dialog is opened, not on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, versionId]);

  const inputIssues = useMemo(() => {
    const parsed = parseJsonObject(inputText);
    return parsed.ok ? null : parsed.error;
  }, [inputText]);

  const run = async () => {
    const parsed = parseJsonObject(inputText);
    if (!parsed.ok) {
      toast.error({ title: 'Input is not valid JSON', description: parsed.error });
      return;
    }
    try {
      const result = await test.mutateAsync({ input: parsed.value, mode });
      if (result.test_passed) {
        toast.success({ title: `Test passed on v${result.version}`, description: 'Publishing is now allowed.' });
      } else {
        toast.error({
          title: `Test ${statusLabel(result.status)}`,
          description: 'No passing evidence was stored, so publishing stays blocked.',
        });
      }
    } catch (error) {
      toast.apiError(error, 'The test request failed');
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Test version ${versionNumber}`}
      description="Makes a real upstream call and records the evidence. Request and response are redacted before they are stored."
      size="xl"
      dismissible={!test.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={test.isPending}>
            Close
          </Button>
          <Button
            variant="primary"
            onClick={() => void run()}
            loading={test.isPending}
            disabled={Boolean(inputIssues)}
            iconLeft={<Play aria-hidden className="h-4 w-4" />}
          >
            Run real test
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Mode" htmlFor="test-mode" hint="The config must enable this mode.">
            <Select
              id="test-mode"
              value={mode}
              onChange={(event) => setMode(event.target.value as InvocationMode)}
              options={modes.map((value) => ({ value, label: statusLabel(value) }))}
            />
          </Field>
          {examples.length > 0 ? (
            <Field label="Load an example" htmlFor="test-example" hint="Replaces the input below.">
              <Select
                id="test-example"
                value=""
                placeholder="Choose an example…"
                options={examples.map((example, index) => ({
                  value: String(index),
                  label: example.note?.trim() || `Example ${index + 1}`,
                }))}
                onChange={(event) => {
                  const index = Number(event.target.value);
                  const example = examples[index];
                  if (example) setInputText(formatJsonText(example.input ?? {}));
                }}
              />
            </Field>
          ) : null}
        </div>

        <JsonEditor
          label="Test input (JSON object)"
          hint="Sent as the input payload. Unknown fields are rejected before the call is made."
          value={inputText}
          onChange={setInputText}
          requireObject
          rows={8}
          disabled={test.isPending}
        />

        {test.isError ? (
          <Banner
            tone="danger"
            title="The test request was rejected"
            actions={errorActions(test.error)}
            requestId={errorRequestId(test.error)}
          >
            {describeError(test.error)}
          </Banner>
        ) : null}

        {test.data ? <TestResultView result={test.data} /> : null}

        {!test.data && !test.isError ? (
          <p className="flex items-center gap-1.5 text-xs text-muted">
            <FlaskConical aria-hidden className="h-3.5 w-3.5" />
            Running this makes a real, possibly billable, upstream request.
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}
