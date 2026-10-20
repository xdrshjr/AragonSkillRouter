// esbuild 打包：主进程（dist/main.js）与 preload（dist/preload.js）。
// 只 external electron —— 主进程不使用任何第三方运行时依赖（spec §2.13）。
import { build, context } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const watch = process.argv.includes('--watch');

/** @type {import('esbuild').BuildOptions} */
const common = {
  bundle: true,
  platform: 'node',
  target: 'node20',
  format: 'cjs',
  external: ['electron'],
  sourcemap: false,
  logLevel: 'info',
};

const targets = [
  { entryPoints: [path.join(here, 'src/main/main.ts')], outfile: path.join(here, 'dist/main.js') },
  {
    entryPoints: [path.join(here, 'src/preload/preload.ts')],
    outfile: path.join(here, 'dist/preload.js'),
  },
];

if (watch) {
  for (const t of targets) {
    const ctx = await context({ ...common, ...t });
    await ctx.watch();
  }
  console.log('[build:main] watching…');
} else {
  for (const t of targets) {
    await build({ ...common, ...t });
  }
  console.log('[build:main] dist/main.js + dist/preload.js');
}
