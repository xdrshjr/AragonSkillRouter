/**
 * Render-level checks for `components/ui/markdown.tsx`.
 *
 * The component is compiled with esbuild and rendered with `react-dom/server`, so
 * these assertions are about the HTML the browser will actually receive — not about
 * the source looking plausible.
 *
 * Run: node desktop/scripts/verify-markdown.mjs
 */

import { build } from 'esbuild';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';

const require = createRequire(import.meta.url);

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '..', '..', 'frontend');

// Kept inside `frontend/` so the externalised React imports resolve against
// frontend/node_modules; a temp dir elsewhere cannot see them.
const outDir = mkdtempSync(path.join(frontend, '.verify-markdown-'));
const entry = path.join(outDir, 'entry.tsx');

// A single bundle: the component plus a server renderer, so imports resolve once.
await build({
  stdin: {
    contents: `
      import * as React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { Markdown } from '@/components/ui/markdown';
      export function render(markdown: string): string {
        return renderToStaticMarkup(React.createElement(Markdown, null, markdown));
      }
    `,
    resolveDir: frontend,
    loader: 'tsx',
  },
  bundle: true,
  format: 'cjs',
  platform: 'node',
  outfile: path.join(outDir, 'bundle.cjs'),
  jsx: 'automatic',
  alias: { '@': frontend },
  logLevel: 'warning',
  external: ['react', 'react-dom', 'react-dom/server'],
});

const { render } = require(path.join(outDir, 'bundle.cjs'));

const results = [];
const record = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? `: ${detail}` : ''}`);
};

// --- 1. block markdown -------------------------------------------------------
const blocks = render(
  [
    '## Findings',
    '',
    'The endpoint returns **two** fields and _one_ nested array.',
    '',
    '1. First step',
    '2. Second step',
    '',
    '- a bullet',
    '- another bullet',
    '',
    '> A quoted note.',
  ].join('\n'),
);
record(blocks.includes('<h4') && blocks.includes('Findings'), 'heading renders as an element', 'h2 → h4');
record(blocks.includes('<strong') && blocks.includes('>two</strong>'), 'bold renders', 'strong');
record(blocks.includes('<em') && blocks.includes('>one</em>'), 'italic renders', 'em');
record(blocks.includes('<ol') && blocks.includes('<ul'), 'ordered and unordered lists render', 'ol + ul');
record(blocks.includes('<blockquote'), 'blockquote renders', 'blockquote');

// --- 2. inline + fenced code -------------------------------------------------
const code = render('Use `retry_on` in the config.\n\n```json\n{"retry_on": ["read_timeout"]}\n```');
record(code.includes('<code') && code.includes('retry_on'), 'inline code renders', 'code');
record(code.includes('read_timeout'), 'fenced block content is preserved', 'code block');
record(/language-json|max-h-64/.test(code) || code.includes('Copy'), 'fenced block uses the shared CodeBlock chrome', '');

// --- 3. table + task list (GFM) ---------------------------------------------
const gfm = render('| Provider | Status |\n| --- | --- |\n| GLM | active |\n\n- [x] configured\n- [ ] tested');
record(gfm.includes('<table') && gfm.includes('<th'), 'GFM table renders', 'table');
record(gfm.includes('type="checkbox"'), 'GFM task list renders checkboxes', 'checkbox');

// --- 4. link handling --------------------------------------------------------
const links = render('[docs](https://example.com/docs) and [local](/providers)');
record(links.includes('href="https://example.com/docs"'), 'https link kept', '');
record(links.includes('target="_blank"') && links.includes('rel="noreferrer noopener"'), 'external link opens safely', '');
record(links.includes('href="/providers"') && !/href="\/providers"[^>]*target/.test(links), 'internal link stays in-app', '');

// --- 5. hostile input --------------------------------------------------------
const hostile = render(
  [
    '[click](javascript:alert(1))',
    '',
    '![x](javascript:alert(2))',
    '',
    '<script>alert(3)</script>',
    '',
    '<img src=x onerror="alert(4)">',
    '',
    '[data](data:text/html;base64,PHNjcmlwdD4=)',
  ].join('\n'),
);
record(!/javascript:/i.test(hostile), 'javascript: URL removed from links and images', '');
record(!/data:text\/html/i.test(hostile), 'data: URL removed', '');
record(!/<script/i.test(hostile), 'raw <script> is not rendered', '');
record(!/<[a-z][^>]*\son[a-z]+\s*=/i.test(hostile), 'raw HTML is escaped as text, so no event handler becomes an attribute', '');
record(hostile.includes('&lt;script') || !hostile.includes('<script'), 'script markup arrives as text', '');

// --- 6. plain text still works (streaming partial markdown) ------------------
const partial = render('The capab');
record(partial.includes('The capab'), 'partial text renders while streaming', '');
const unterminated = render('```json\n{"a": 1');
record(unterminated.length > 0, 'unterminated fence does not throw', '');

rmSync(outDir, { recursive: true, force: true });

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
process.exit(failed.length === 0 ? 0 : 1);
