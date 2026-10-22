'use client';

/**
 * Markdown rendering for agent output.
 *
 * The assistant answers in Markdown — headings, lists, tables, fenced code — and
 * showing it as raw text was the reason the panel read like a log file instead of a
 * conversation.
 *
 * Safety rules, because this renders model output that may quote untrusted content
 * from a web page, a repository or an imported SKILL:
 *
 *  * `react-markdown` never injects HTML — raw HTML in the source is dropped, not
 *    interpreted. `rehype-raw` is deliberately NOT installed, and this component
 *    must not add it: rendering model-produced HTML would put an injection sink in
 *    the middle of the product.
 *  * `urlTransform` is applied to every link and image so only http(s), mailto and
 *    site-relative targets survive; `javascript:`, `data:` and `file:` are dropped.
 *  * links open in a new tab with `rel="noreferrer noopener"`. In the desktop app the
 *    shell's `setWindowOpenHandler` forwards these to the real browser instead.
 *  * fenced code is rendered through the existing `CodeBlock` so it gets the copy
 *    button and the same chrome as everywhere else.
 */

import { memo, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { cn } from '@/lib/utils';
import { CodeBlock } from './code-block';

/** Only protocols that cannot execute script. Everything else is blanked. */
function safeUrlTransform(url: string): string {
  const trimmed = url.trim();
  if (!trimmed) return '';
  if (trimmed.startsWith('/') || trimmed.startsWith('#')) return trimmed;
  const lowered = trimmed.toLowerCase();
  if (
    lowered.startsWith('http://') ||
    lowered.startsWith('https://') ||
    lowered.startsWith('mailto:') ||
    lowered.startsWith('tel:')
  ) {
    return trimmed;
  }
  return '';
}

function isExternal(href: string | undefined): boolean {
  if (!href) return false;
  return /^(https?:)?\/\//i.test(href) || href.startsWith('mailto:') || href.startsWith('tel:');
}

const COMPONENTS: Components = {
  // Headings inside a chat bubble must not out-shout the page chrome.
  h1: ({ children }) => <h3 className="mb-1.5 mt-3 text-base font-semibold first:mt-0">{children}</h3>,
  h2: ({ children }) => <h4 className="mb-1.5 mt-3 text-sm font-semibold first:mt-0">{children}</h4>,
  h3: ({ children }) => <h5 className="mb-1 mt-2.5 text-sm font-semibold first:mt-0">{children}</h5>,
  h4: ({ children }) => <h6 className="mb-1 mt-2 text-[13px] font-semibold first:mt-0">{children}</h6>,
  h5: ({ children }) => <p className="mb-1 mt-2 text-[13px] font-semibold first:mt-0">{children}</p>,
  h6: ({ children }) => <p className="mb-1 mt-2 text-[13px] font-semibold first:mt-0">{children}</p>,
  p: ({ children }) => <p className="my-1.5 leading-relaxed first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-1.5 list-disc space-y-0.5 pl-5 first:mt-0 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 list-decimal space-y-0.5 pl-5 first:mt-0 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed [&>p]:my-0">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-border-strong pl-3 text-text/80">{children}</blockquote>
  ),
  hr: () => <hr className="my-3 border-border" />,
  strong: ({ children }) => <strong className="font-semibold text-text">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="text-muted line-through">{children}</del>,
  a: ({ href, children }) => {
    const safe = href ? safeUrlTransform(href) : '';
    if (!safe) return <span>{children}</span>;
    return (
      <a
        href={safe}
        className="text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
        {...(isExternal(safe) ? { target: '_blank', rel: 'noreferrer noopener' } : {})}
      >
        {children}
      </a>
    );
  },
  // Block code arrives as `pre > code`; render the whole block through CodeBlock.
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children, ...rest }) => {
    const text = String(children ?? '').replace(/\n$/, '');
    const language = /language-([\w+#-]+)/.exec(className ?? '')?.[1];
    const isBlock = Boolean(language) || text.includes('\n');
    if (!isBlock) {
      return (
        <code
          className="rounded-[4px] border border-border bg-surface-muted px-1 py-0.5 font-mono text-[0.85em] text-text"
          {...rest}
        >
          {children}
        </code>
      );
    }
    return (
      <div className="my-2 first:mt-0 last:mb-0">
        <CodeBlock code={text} language={language} maxHeightClassName="max-h-64" />
      </div>
    );
  },
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto rounded-control border border-border">
      <table className="w-full border-collapse text-[12px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-surface-muted text-left">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => <tr className="border-b border-border last:border-0">{children}</tr>,
  th: ({ children }) => <th className="whitespace-nowrap px-2 py-1 font-medium text-text">{children}</th>,
  td: ({ children }) => <td className="px-2 py-1 align-top text-text/90">{children}</td>,
  img: ({ src, alt }) => {
    const safe = typeof src === 'string' ? safeUrlTransform(src) : '';
    if (!safe) return null;
    return (
      // eslint-disable-next-line @next/next/no-img-element -- markdown images are arbitrary URLs
      <img src={safe} alt={alt ?? ''} className="my-2 max-h-64 rounded-control border border-border" loading="lazy" />
    );
  },
  // GFM task lists
  input: ({ checked, ...rest }) => (
    <input type="checkbox" checked={Boolean(checked)} readOnly disabled className="mr-1.5 align-middle" {...rest} />
  ),
};

export interface MarkdownProps {
  children: string;
  className?: string;
  /** Rendered after the content (e.g. the streaming caret). */
  trailing?: ReactNode;
}

/**
 * Memoised on the text: a streamed answer re-renders on every delta, and the panel
 * keeps the previous tree when the text has not changed (e.g. a tool card update).
 */
export const Markdown = memo(function Markdown({ children, className, trailing }: MarkdownProps) {
  return (
    <div className={cn('sr-markdown min-w-0 break-words text-sm text-text', className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrlTransform} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
      {trailing}
    </div>
  );
});
