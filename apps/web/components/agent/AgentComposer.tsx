'use client';

import { ArrowUp, Loader2, Paperclip, Square, X } from 'lucide-react';
import * as React from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { formatBytes } from '@/lib/format';
import { useAgentSession } from './useAgentSession';

/**
 * 输入区（spec §13.3）：
 * - Enter 发送 / Shift+Enter 换行
 * - 执行中仍可继续输入，新消息标记「排队中（第 N 条）」——N 来自后端真实 queue_position
 * - 附件：拖拽/选择、单文件 ≤50MB、上传进度可取消、>8000 字符折叠为「粘贴内容（N 字）」
 */
const COLLAPSE_THRESHOLD = 8000;
const MAX_FILE_MB = 50;

export function AgentComposer() {
  const {
    draft,
    setDraft,
    send,
    phase,
    queuePosition,
    attachments,
    attachFiles,
    removeAttachment,
    uploading,
    cancelUpload,
    stop,
    sseStatus,
  } = useAgentSession();

  const [dragging, setDragging] = React.useState(false);
  const [expandedPaste, setExpandedPaste] = React.useState(false);
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const busy = phase === 'running' || phase === 'queued' || phase === 'sending' || phase === 'stopping';
  const collapsed = draft.length > COLLAPSE_THRESHOLD && !expandedPaste;

  const submit = async (): Promise<void> => {
    if (!draft.trim()) return;
    if (phase === 'stopping') return;
    await send();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  };

  const handleDrop = async (event: React.DragEvent<HTMLDivElement>): Promise<void> => {
    event.preventDefault();
    setDragging(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length > 0) await attachFiles(files);
  };

  return (
    <div className="flex flex-col gap-2 border-t border-border p-3">
      {/* 附件预览 */}
      {attachments.length > 0 || uploading ? (
        <ul className="flex flex-wrap gap-1.5" aria-label="已添加的附件">
          {attachments.map((attachment) => (
            <li
              key={attachment.file_id}
              className="flex items-center gap-1.5 rounded-sm border border-border bg-[var(--sr-surface-sunken)] px-2 py-1 text-12 text-content"
            >
              <Paperclip size={12} strokeWidth={1.75} aria-hidden />
              <span className="max-w-[140px] truncate">{attachment.name ?? attachment.file_id}</span>
              {attachment.size ? <span className="text-content-muted">{formatBytes(attachment.size)}</span> : null}
              <button
                type="button"
                onClick={() => removeAttachment(attachment.file_id)}
                aria-label={`移除附件 ${attachment.name ?? attachment.file_id}`}
                className="rounded-sm p-0.5 text-content-muted hover:text-danger focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
              >
                <X size={12} strokeWidth={1.75} aria-hidden />
              </button>
            </li>
          ))}
          {uploading ? (
            <li className="flex items-center gap-2 rounded-sm border border-border px-2 py-1 text-12 text-content-muted">
              <Loader2 size={12} strokeWidth={1.75} className="animate-spin" aria-hidden />
              <span className="max-w-[140px] truncate">{uploading.name}</span>
              <span>{uploading.percent}%</span>
              <button
                type="button"
                onClick={cancelUpload}
                className="rounded-sm border border-border px-1 text-12 hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
              >
                取消
              </button>
            </li>
          ) : null}
        </ul>
      ) : null}

      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          void handleDrop(event);
        }}
        className={cn(
          'rounded border bg-surface transition-colors duration-150 ease-sr',
          dragging ? 'border-accent bg-[var(--sr-accent-weak)]' : 'border-border',
        )}
      >
        {collapsed ? (
          <div className="flex items-center justify-between gap-2 px-3 py-2">
            <span className="text-13 text-content-muted">粘贴内容（{draft.length} 字）</span>
            <button
              type="button"
              onClick={() => setExpandedPaste(true)}
              className="rounded-sm border border-border px-1.5 py-0.5 text-12 text-content hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            >
              展开
            </button>
          </div>
        ) : (
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={3}
            aria-label="给 Agent 的消息"
            aria-describedby="agent-composer-help"
            placeholder="描述你要接入的能力、要修的配置，或粘贴接口文档链接…（Enter 发送，Shift+Enter 换行）"
            className="w-full resize-none border-0 bg-transparent px-3 py-2 text-14 text-content outline-none placeholder:text-content-muted"
          />
        )}
        <div className="flex items-center gap-1.5 border-t border-border px-2 py-1.5">
          <input
            ref={fileRef}
            type="file"
            multiple
            className="sr-only"
            aria-label="选择附件"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              if (files.length > 0) void attachFiles(files);
              event.target.value = '';
            }}
          />
          <Button
            variant="ghost"
            size="sm"
            aria-label={`添加附件（单文件 ≤ ${MAX_FILE_MB}MB）`}
            onClick={() => fileRef.current?.click()}
            icon={<Paperclip size={16} strokeWidth={1.75} />}
          >
            附件
          </Button>

          <span id="agent-composer-help" className="truncate text-12 text-content-muted">
            {phase === 'queued' && queuePosition
              ? `排队中（第 ${queuePosition} 条），等待本轮结束`
              : busy
                ? '执行中，可继续输入（Enter 发送，会排队）'
                : sseStatus === 'reconnecting'
                  ? '连接中断，正在重连…'
                  : 'Enter 发送 · Shift+Enter 换行'}
          </span>

          <div className="ml-auto flex items-center gap-1.5">
            {phase === 'running' || phase === 'queued' || phase === 'stopping' ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void stop()}
                loading={phase === 'stopping'}
                icon={<Square size={14} strokeWidth={1.75} />}
              >
                停止
              </Button>
            ) : null}
            <Button
              variant="primary"
              size="sm"
              onClick={() => void submit()}
              disabled={!draft.trim() || phase === 'stopping'}
              aria-label="发送消息"
              icon={<ArrowUp size={16} strokeWidth={1.75} />}
            >
              发送
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
