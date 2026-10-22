/**
 * Render Markdown through the panel's own component and print the HTML.
 *
 * Reads the Markdown on stdin so a caller can feed it text it obtained elsewhere
 * (e.g. a real model answer) without quoting it into a shell argument.
 *
 *   echo "## Hi" | node desktop/scripts/render-markdown.mjs
 */

import { build } from 'esbuild';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, rmSync } from 'node:fs';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '..', '..', 'frontend');
const outDir = mkdtempSync(path.join(frontend, '.render-markdown-'));

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const markdown = Buffer.concat(chunks).toString('utf8');

try {
  await build({
    stdin: {
      contents: `
        import * as React from 'react';
        import { renderToStaticMarkup } from 'react-dom/server';
        import { Markdown } from '@/components/ui/markdown';
        export function render(md) { return renderToStaticMarkup(React.createElement(Markdown, null, md)); }
      `,
      resolveDir: frontend,
      loader: 'tsx',
    },
    bundle: true,
    format: 'cjs',
    platform: 'node',
    outfile: path.join(outDir, 'b.cjs'),
    jsx: 'automatic',
    alias: { '@': frontend },
    logLevel: 'warning',
    external: ['react', 'react-dom', 'react-dom/server'],
  });
  const { render } = require(path.join(outDir, 'b.cjs'));
  process.stdout.write(render(markdown));
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
