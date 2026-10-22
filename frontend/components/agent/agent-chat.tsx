'use client';

/**
 * Timeline + composer for the agent panel.
 *
 * Every entry kind comes from a real CONTRACT §5.10 event or a persisted message;
 * nothing here invents progress. A stop is only ever described with the report the
 * backend returned (`stopped` false → the panel says so).
 */

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type KeyboardEvent } from 'react';
import {
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  CircleSlash,
  FileText,
  Link2,
  ListChecks,
  Loader2,
  Paperclip,
  Play,
  RotateCcw,
  Square,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';

import { describeError } from '@/lib/api';
import { formatBytes, formatDateTimeShort, formatDuration, statusLabel } from '@/lib/format';
import { uploadFile } from '@/lib/agent';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CodeBlock } from '@/components/ui/code-block';
import { Markdown } from '@/components/ui/markdown';
import { Textarea } from '@/components/ui/input';
import { InlineAlert } from '@/components/ui/banner';
import { Tooltip } from '@/components/ui/tooltip';
import { useToast } from '@/components/ui/toast';
import { cn } from '@/lib/utils';
import { useAgent, type DraftAttachment, type PanelEntry } from './agent-context';

const IMAGE_TYPES = /^image\//;

function isImage(contentType: string | null | undefined): boolean {
  return Boolean(contentType && IMAGE_TYPES.test(contentType));
}

function AttachmentChip({ item }: { item: { id: string; filename?: string | null; content_type?: string | null; size?: number | null } }) {
  return (
    <a
      href={`/api/files/${encodeURIComponent(item.id)}`}
      target="_blank"
      rel="noreferrer noopener"
      className="inline-flex max-w-full items-center gap-1.5 rounded-control border border-border bg-surface px-2 py-1 text-[11px] text-text hover:bg-info-soft"
    >
      {isImage(item.content_type) ? (
        <span
          aria-hidden
          className="h-5 w-5 shrink-0 rounded border border-border bg-info-soft bg-cover bg-center"
          style={{ backgroundImage: `url(/api/files/${encodeURIComponent(item.id)})` }}
        />
      ) : (
        <FileText aria-hidden className="h-3.5 w-3.5 shrink-0 text-muted" />
      )}
      <span className="truncate">{item.filename ?? item.id}</span>
      {typeof item.size === 'number' ? <span className="text-muted">{formatBytes(item.size)}</span> : null}
    </a>
  );
}

function ToolCard({ entry }: { entry: PanelEntry }) {
  const tool = entry.tool;
  if (!tool) return null;
  const tone = tool.status === 'running' ? 'accent' : tool.status === 'error' ? 'danger' : 'success';
  return (
    <div className="rounded-card border border-border bg-surface p-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-text">
          <Wrench aria-hidden className="h-3.5 w-3.5 text-muted" />
          <span className="font-mono">{tool.name}</span>
        </span>
        <span className="flex items-center gap-2">
          {tool.durationMs !== null && tool.durationMs !== undefined ? (
            <span className="text-[11px] text-muted">{formatDuration(tool.durationMs)}</span>
          ) : null}
          <Badge tone={tone} dot={false}>
            {tool.status === 'running' ? 'Running' : tool.status === 'error' ? 'Tool error' : 'Done'}
          </Badge>
        </span>
      </div>
      {tool.args ? (
        <div className="mt-2">
          <CodeBlock code={tool.args} label="arguments" maxHeightClassName="max-h-32" copyable={false} />
        </div>
      ) : null}
      {tool.result ? (
        <div className="mt-1.5">
          <CodeBlock code={tool.result} label="result" maxHeightClassName="max-h-40" copyable={false} />
        </div>
      ) : null}
    </div>
  );
}

function StopReportCard({ entry, retry }: { entry: PanelEntry; retry?: () => void }) {
  const report = entry.report;
  if (!report) return null;
  const stopped = report.stopped;
  return (
    <div className="rounded-card border border-warning/30 bg-warning-soft p-2 text-xs">
      <p className="flex items-center gap-1.5 font-medium text-text">
        <CircleSlash aria-hidden className="h-3.5 w-3.5" />
        {stopped ? 'Stopped' : report.status === 'failed' ? 'Stop failed — run marked failed' : 'Nothing was stopped'}
        <span className="font-mono text-[11px] text-muted">({report.reason})</span>
      </p>
      {report.note ? <p className="mt-1 text-text/80">{report.note}</p> : null}
      <div className="mt-2">
        <p className="font-medium text-text">What stopped</p>
        {report.stopped_steps.length === 0 ? (
          <p className="text-text/80">No step was reported as stopped.</p>
        ) : (
          <ul className="list-disc pl-4 text-text/80">
            {report.stopped_steps.map((step, index) => (
              <li key={`${step.kind ?? 'step'}-${index}`}>
                {statusLabel(String(step.kind ?? 'step'))}
                {step.tool ? ` · ${step.tool}` : ''}
                {typeof step.turn === 'number' ? ` · turn ${step.turn}` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-2">
        <p className="font-medium text-text">May still complete upstream</p>
        {report.may_continue_upstream.length === 0 ? (
          <p className="text-text/80">The engine reported nothing in flight.</p>
        ) : (
          <ul className="list-disc pl-4 text-text/80">
            {report.may_continue_upstream.map((item, index) => (
              <li key={`${item.tool_call_id ?? 'call'}-${index}`}>
                {item.tool ?? 'tool call'}
                {item.tool_call_id ? ` · ${item.tool_call_id}` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>
      {retry ? (
        <div className="mt-2">
          <Button size="sm" variant="secondary" onClick={retry} iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}>
            Retry this run
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function PanelEntryView({
  entry,
  onRetry,
}: {
  entry: PanelEntry;
  onRetry?: () => void;
}) {
  if (entry.kind === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[92%] rounded-card rounded-br-sm bg-accent-soft px-3 py-2 text-sm text-text">
          <p className="whitespace-pre-wrap break-words">{entry.text}</p>
          {entry.attachments && entry.attachments.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {entry.attachments.map((item) => (
                <AttachmentChip key={item.id} item={item} />
              ))}
            </div>
          ) : null}
          <p className="mt-1 text-[10px] text-muted">{formatDateTimeShort(entry.at)}</p>
        </div>
      </div>
    );
  }

  if (entry.kind === 'assistant') {
    return (
      <div className="flex flex-col gap-1">
        <div className="max-w-full rounded-card rounded-bl-sm border border-border bg-surface px-3 py-2 text-sm text-text">
          <Markdown
            trailing={
              entry.streaming ? (
                <span
                  aria-hidden
                  className="ml-0.5 inline-block h-3.5 w-1.5 animate-shimmer bg-accent align-middle"
                />
              ) : undefined
            }
          >
            {entry.text ?? ''}
          </Markdown>
        </div>
        {entry.streaming ? (
          <span className="sr-visually-hidden" role="status">
            Assistant is writing…
          </span>
        ) : null}
      </div>
    );
  }

  if (entry.kind === 'tool') return <ToolCard entry={entry} />;

  if (entry.kind === 'todo') {
    return (
      <div className="rounded-card border border-border bg-info-soft/60 p-2 text-xs">
        <p className="flex items-center gap-1.5 font-medium text-text">
          <ListChecks aria-hidden className="h-3.5 w-3.5" />
          Todo list
        </p>
        {entry.items && entry.items.length > 0 ? (
          <ul className="mt-1 space-y-0.5">
            {entry.items.map((item, index) => (
              <li key={`${item}-${index}`} className="flex items-start gap-1.5 text-text/80">
                <CheckCircle2 aria-hidden className="mt-0.5 h-3 w-3 shrink-0 text-muted" />
                <span>{item}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-muted">The engine sent an empty todo list.</p>
        )}
      </div>
    );
  }

  if (entry.kind === 'plan') {
    return (
      <div className="rounded-card border border-accent/30 bg-accent-soft p-2 text-xs">
        <p className="flex items-center gap-1.5 font-medium text-text">
          <ArrowUpRight aria-hidden className="h-3.5 w-3.5" />
          {entry.text ?? 'Proposed plan'}
        </p>
        {entry.items && entry.items.length > 0 ? (
          <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-text/80">
            {entry.items.map((item, index) => (
              <li key={`${item}-${index}`}>{item}</li>
            ))}
          </ol>
        ) : (
          <p className="mt-1 text-muted">The proposal carried no steps.</p>
        )}
        <p className="mt-1 text-[11px] text-muted">This is a proposal. Nothing was saved or published.</p>
      </div>
    );
  }

  if (entry.kind === 'change') {
    return (
      <div className="rounded-card border border-success/30 bg-success-soft p-2 text-xs">
        <p className="flex items-center gap-1.5 font-medium text-text">
          <CheckCircle2 aria-hidden className="h-3.5 w-3.5 text-success" />
          {entry.text}
        </p>
        {entry.detail ? (
          <div className="mt-1">
            <CodeBlock code={entry.detail} label="applied change" maxHeightClassName="max-h-40" copyable={false} />
          </div>
        ) : null}
      </div>
    );
  }

  if (entry.kind === 'test') {
    const passed = (entry.text ?? '').toLowerCase().includes('passed');
    return (
      <div className="rounded-card border border-border bg-surface p-2 text-xs">
        <p className="flex items-center gap-1.5 font-medium text-text">
          <Play aria-hidden className="h-3.5 w-3.5 text-muted" />
          {entry.text}
          <Badge tone={passed ? 'success' : 'warning'} dot={false}>
            {passed ? 'Observed pass' : 'Check the detail'}
          </Badge>
        </p>
        {entry.detail ? (
          <div className="mt-1">
            <CodeBlock code={entry.detail} label="test result" maxHeightClassName="max-h-40" copyable={false} />
          </div>
        ) : null}
      </div>
    );
  }

  if (entry.kind === 'waiting') {
    return (
      <div role="status" className="rounded-card border border-warning/30 bg-warning-soft p-2 text-xs text-text">
        <p className="flex items-center gap-1.5 font-medium">
          <AlertTriangle aria-hidden className="h-3.5 w-3.5 text-warning" />
          Waiting for you
        </p>
        <p className="mt-0.5 text-text/80">{entry.text}</p>
      </div>
    );
  }

  if (entry.kind === 'stopped') return <StopReportCard entry={entry} retry={onRetry} />;

  if (entry.kind === 'failed') {
    return (
      <div role="alert" className="rounded-card border border-danger/30 bg-danger-soft p-2 text-xs">
        <p className="flex items-center gap-1.5 font-medium text-text">
          <X aria-hidden className="h-3.5 w-3.5 text-danger" />
          Run failed
        </p>
        <p className="mt-0.5 text-text/80">{entry.text}</p>
        {entry.detail ? (
          <div className="mt-1">
            <CodeBlock code={entry.detail} label="error detail" maxHeightClassName="max-h-32" copyable={false} />
          </div>
        ) : null}
        {onRetry ? (
          <div className="mt-2">
            <Button size="sm" variant="secondary" onClick={onRetry} iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}>
              Retry
            </Button>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <p className="text-[11px] text-muted">
      {entry.text}
      <span className="ml-1">{formatDateTimeShort(entry.at)}</span>
    </p>
  );
}

/* ----------------------------------------------------------------- composer -- */

const LINK_PATTERN = /https?:\/\/[^\s)]+/g;

interface PendingUpload {
  key: string;
  name: string;
  size: number;
  state: 'uploading' | 'failed';
  message?: string;
}

export function AgentComposer() {
  const toast = useToast();
  const {
    draft,
    setDraft,
    attachments,
    addAttachment,
    removeAttachment,
    send,
    sending,
    stopping,
    stop,
    run,
    queued,
    error,
    clearError,
    sessionLoading,
  } = useAgent();
  const [pending, setPending] = useState<PendingUpload[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const uploading = pending.some((item) => item.state === 'uploading');
  const running = Boolean(run.id) && (run.status === 'running' || run.status === 'queued');
  const links = Array.from(new Set(draft.match(LINK_PATTERN) ?? []));

  const uploadOne = useCallback(
    async (file: File) => {
      const key = `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      setPending((current) => [...current, { key, name: file.name, size: file.size, state: 'uploading' }]);
      try {
        const uploaded = await uploadFile(file, 'upload');
        const attachment: DraftAttachment = {
          id: uploaded.id,
          filename: uploaded.filename,
          contentType: uploaded.content_type,
          size: uploaded.size,
        };
        addAttachment(attachment);
        setPending((current) => current.filter((item) => item.key !== key));
        toast.success({
          title: 'Attached',
          description: `${uploaded.filename} (${formatBytes(uploaded.size)})${uploaded.deduplicated ? ' — already stored, reused' : ''}`,
        });
      } catch (cause) {
        setPending((current) =>
          current.map((item) =>
            item.key === key ? { ...item, state: 'failed', message: describeError(cause) } : item,
          ),
        );
        toast.apiError(cause, `Could not attach ${file.name}`);
      }
    },
    [addAttachment, toast],
  );

  const uploadFiles = useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files);
      for (const file of list) void uploadOne(file);
    },
    [uploadOne],
  );

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (event.dataTransfer?.files?.length) uploadFiles(event.dataTransfer.files);
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = event.clipboardData?.files;
    if (files && files.length > 0) {
      event.preventDefault();
      uploadFiles(files);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (!sending && !uploading) void send();
    }
  };

  const disabled = sending || uploading || sessionLoading;

  return (
    <div
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        // The composer is pinned by the drawer's footer slot, which already draws the
        // divider: no border of its own, and no extra padding doubling the footer's.
        'shrink-0 bg-surface',
        dragging && 'bg-accent-soft ring-2 ring-inset ring-accent',
      )}
    >
      {error ? (
        <div className="mb-2">
          <InlineAlert tone="danger">
            <span className="flex items-start justify-between gap-2">
              <span>{error}</span>
              <button type="button" onClick={clearError} aria-label="Dismiss error" className="shrink-0">
                <X aria-hidden className="h-3 w-3" />
              </button>
            </span>
          </InlineAlert>
        </div>
      ) : null}

      {attachments.length > 0 || pending.length > 0 ? (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {attachments.map((item) => (
            <span
              key={item.id}
              className="inline-flex max-w-full items-center gap-1.5 rounded-control border border-border bg-info-soft px-1.5 py-1 text-[11px]"
            >
              {isImage(item.contentType) ? (
                <span
                  aria-hidden
                  className="h-6 w-6 shrink-0 rounded border border-border bg-cover bg-center"
                  style={{ backgroundImage: `url(/api/files/${encodeURIComponent(item.id)})` }}
                />
              ) : (
                <FileText aria-hidden className="h-3.5 w-3.5 text-muted" />
              )}
              <span className="max-w-[140px] truncate">{item.filename}</span>
              <span className="text-muted">{formatBytes(item.size)}</span>
              <button
                type="button"
                aria-label={`Remove ${item.filename}`}
                onClick={() => removeAttachment(item.id)}
                className="rounded p-0.5 text-muted hover:bg-surface hover:text-text"
              >
                <Trash2 aria-hidden className="h-3 w-3" />
              </button>
            </span>
          ))}
          {pending.map((item) => (
            <span
              key={item.key}
              className={cn(
                'inline-flex max-w-full items-center gap-1.5 rounded-control border px-1.5 py-1 text-[11px]',
                item.state === 'failed' ? 'border-danger/40 bg-danger-soft text-danger' : 'border-border bg-info-soft',
              )}
            >
              {item.state === 'uploading' ? (
                <Loader2 aria-hidden className="h-3 w-3 animate-spin" />
              ) : (
                <AlertTriangle aria-hidden className="h-3 w-3" />
              )}
              <span className="max-w-[140px] truncate">{item.name}</span>
              <span>{item.state === 'uploading' ? 'uploading…' : 'failed'}</span>
              {item.state === 'failed' ? (
                <button
                  type="button"
                  aria-label={`Dismiss ${item.name}`}
                  onClick={() => setPending((current) => current.filter((entry) => entry.key !== item.key))}
                  className="rounded p-0.5"
                >
                  <X aria-hidden className="h-3 w-3" />
                </button>
              ) : null}
            </span>
          ))}
        </div>
      ) : null}

      {links.length > 0 ? (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {links.map((link) => (
            <span
              key={link}
              className="inline-flex max-w-full items-center gap-1 rounded-pill border border-border bg-surface px-2 py-0.5 text-[11px] text-muted"
            >
              <Link2 aria-hidden className="h-3 w-3" />
              <span className="max-w-[180px] truncate">{link}</span>
            </span>
          ))}
        </div>
      ) : null}

      {queued ? (
        <div className="mb-2">
          <InlineAlert tone="warning">
            {queued.acknowledged
              ? 'Queued — the backend stored this message for the live run; it is processed after the current turn.'
              : 'Queued — waiting to be picked up by the live run.'}
          </InlineAlert>
        </div>
      ) : null}

      <label htmlFor="agent-draft" className="sr-visually-hidden">
        Message the agent
      </label>
      <Textarea
        id="agent-draft"
        value={draft}
        rows={3}
        placeholder="Ask the agent to configure a capability, test an endpoint… (Enter sends, Shift+Enter adds a line)"
        onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        className="resize-none"
      />
      <div className="mt-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <input
            ref={inputRef}
            type="file"
            multiple
            className="sr-visually-hidden"
            aria-hidden
            tabIndex={-1}
            onChange={(event) => {
              if (event.target.files) uploadFiles(event.target.files);
              event.target.value = '';
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => inputRef.current?.click()}
            aria-label="Attach files"
            iconLeft={<Paperclip aria-hidden className="h-3.5 w-3.5" />}
          >
            Attach
          </Button>
          <span className="hidden text-[11px] text-muted sm:inline">or drop / paste a file</span>
        </div>
        <div className="flex items-center gap-1.5">
          {running ? (
            <Tooltip content="Stop the run and show which steps stopped and what may still complete upstream">
              <Button
                size="sm"
                variant="danger"
                onClick={() => void stop()}
                loading={stopping}
                iconLeft={<Square aria-hidden className="h-3.5 w-3.5" />}
              >
                Stop
              </Button>
            </Tooltip>
          ) : null}
          <Button
            size="sm"
            variant="primary"
            onClick={() => void send()}
            loading={sending}
            disabled={disabled || (draft.trim().length === 0 && attachments.length === 0)}
            iconRight={<ArrowUpRight aria-hidden className="h-3.5 w-3.5" />}
          >
            {running ? 'Queue' : 'Send'}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Scroll container that restores and reports its offset (panel state survives routes). */
export function AgentTimeline({ onRetry }: { onRetry?: () => void }) {
  const { entries, scrollTop, setScrollTop, connection } = useAgent();
  const listRef = useRef<HTMLDivElement | null>(null);
  const restored = useRef(false);

  useEffect(() => {
    if (restored.current || !listRef.current) return;
    listRef.current.scrollTop = scrollTop;
    restored.current = true;
  }, [scrollTop]);

  useEffect(() => {
    const node = listRef.current;
    if (!node) return;
    // Follow the tail while a stream is live; otherwise the reader's position wins.
    if (connection === 'open') node.scrollTop = node.scrollHeight;
  }, [entries, connection]);

  return (
    <div
      ref={listRef}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overscroll-contain p-3"
      role="log"
      aria-live="polite"
      aria-label="Agent conversation"
    >
      {entries.length === 0 ? (
        <div className="m-auto max-w-[280px] text-center">
          <p className="text-sm font-medium text-text">The agent can read and change your router</p>
          <p className="mt-1 text-xs text-muted">
            Ask it to draft a capability, validate and test it, or save a SKILL draft. Every action appears here as it
            happens — and a stop is reported exactly as the engine answered.
          </p>
        </div>
      ) : (
        entries.map((entry) => <PanelEntryView key={entry.id} entry={entry} onRetry={onRetry} />)
      )}
    </div>
  );
}
