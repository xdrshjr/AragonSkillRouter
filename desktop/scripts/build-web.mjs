/**
 * Bundle the Next.js web UI for the desktop app.
 *
 * The desktop build reuses the *same* Next.js server the Docker image runs
 * (`output: 'standalone'`), for one reason: the web deployment is the path that
 * was verified end to end (cookies, CSRF, SSE proxying, uploads). A static export
 * would have required rewriting every dynamic route and hand-rolling an SPA
 * fallback, i.e. a second, differently-behaved UI.
 *
 * Layout produced:
 *   resources/runtime/web/server.js        Next standalone entry
 *   resources/runtime/web/.next/static     client assets (not included by default)
 *   resources/runtime/web/public           static files
 *   resources/runtime/web/node_modules     the standalone server's own deps
 *
 * The server is started by the Electron supervisor as Node
 * (`ELECTRON_RUN_AS_NODE=1`) on a loopback port, with `PORT` set. Its `/api`
 * rewrite target is irrelevant in the desktop build: the shell origin proxies
 * `/api` straight to the backend before a request ever reaches Next.
 */

import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');
const frontendDir = path.join(repoRoot, 'frontend');
const runtimeDir = path.join(desktopDir, 'resources', 'runtime');
const target = path.join(runtimeDir, 'web');

const args = new Set(process.argv.slice(2));
const skipBuild = args.has('--skip-build');

if (!existsSync(path.join(frontendDir, 'app', 'layout.tsx'))) {
  console.error(`Frontend not found at ${frontendDir}`);
  process.exit(1);
}

if (!skipBuild) {
  console.log('[build-web] next build (standalone)');
  rmSync(path.join(frontendDir, '.next'), { recursive: true, force: true });
  const result = spawnSync('npx', ['next', 'build'], {
    cwd: frontendDir,
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: {
      ...process.env,
      NEXT_TELEMETRY_DISABLED: '1',
      // Baked into the standalone manifest; unused in the desktop shell, which
      // proxies /api itself, but kept consistent for a manual `node server.js`.
      SR_API_ORIGIN: process.env.SR_API_ORIGIN ?? 'http://127.0.0.1:8412',
    },
  });
  if (result.status !== 0) {
    console.error(`\n[build-web] next build failed with exit code ${result.status}`);
    process.exit(result.status ?? 1);
  }
}

const standalone = path.join(frontendDir, '.next', 'standalone');
if (!existsSync(path.join(standalone, 'server.js'))) {
  console.error(
    `\n[build-web] no standalone server at ${standalone}.\n` +
      '  Check that frontend/next.config.mjs still sets output: "standalone".',
  );
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(standalone, target, { recursive: true });
// Next does not copy these into standalone; without them the UI loads unstyled
// and every client chunk 404s.
cpSync(path.join(frontendDir, '.next', 'static'), path.join(target, '.next', 'static'), { recursive: true });
if (existsSync(path.join(frontendDir, 'public'))) {
  cpSync(path.join(frontendDir, 'public'), path.join(target, 'public'), { recursive: true });
}

const manifestPath = path.join(runtimeDir, 'runtime.json');
const previous = existsSync(manifestPath) ? JSON.parse(readFileSync(manifestPath, 'utf8')) : {};
const nextVersion = JSON.parse(readFileSync(path.join(frontendDir, 'package.json'), 'utf8')).dependencies?.next ?? 'unknown';
writeFileSync(
  manifestPath,
  JSON.stringify(
    {
      ...previous,
      builtAt: new Date().toISOString(),
      web: { entry: 'web/server.js', next: nextVersion },
    },
    null,
    2,
  ),
  'utf8',
);
console.log(`[build-web] web runtime ready at ${target} (next ${nextVersion})`);
