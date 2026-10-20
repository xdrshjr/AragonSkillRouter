// 构建并组装 Node 网关（spec §2.9）。
//
// 1. agents/aragon-gateway 依赖（node_modules 已存在则跳过；FORCE_NPM_CI=1 强制）
// 2. npm run build → dist/src/main.js
// 3. 组装 build/gateway：dist + package.json + **仅生产依赖**的 node_modules
//    （在 build 目录里 npm install --omit=dev，不动仓库里的 node_modules）
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const repoRoot = path.resolve(desktopRoot, '..', '..');
const gwRoot = path.join(repoRoot, 'agents', 'aragon-gateway');
const outRoot = path.join(desktopRoot, 'build', 'gateway');
const entry = path.join(outRoot, 'dist', 'src', 'main.js');

function log(m) {
  console.log(`[build:gateway] ${m}`);
}
function fail(m) {
  console.error(`[build:gateway] FAIL ${m}`);
  process.exit(1);
}
function run(cmd, args, opts = {}) {
  log(`${cmd} ${args.join(' ')}`);
  const res = spawnSync(cmd, args, { stdio: 'inherit', shell: process.platform === 'win32', ...opts });
  if (res.error) fail(`${cmd} 无法执行：${res.error.message}`);
  if (res.status !== 0) fail(`${cmd} 退出码 ${res.status}`);
}

function main() {
  if (!fs.existsSync(path.join(gwRoot, 'package.json'))) fail(`缺 ${gwRoot}/package.json`);

  const needInstall = process.env.FORCE_NPM_CI === '1' || !fs.existsSync(path.join(gwRoot, 'node_modules'));
  if (needInstall) {
    run('npm', ['ci', '--include=dev', '--no-fund', '--no-audit'], { cwd: gwRoot });
  } else {
    log('agents/aragon-gateway/node_modules 已存在，跳过 npm ci（FORCE_NPM_CI=1 可强制）');
  }
  run('npm', ['run', 'build'], { cwd: gwRoot });

  const dist = path.join(gwRoot, 'dist');
  if (!fs.existsSync(path.join(dist, 'src', 'main.js'))) fail(`tsc 没有产出 ${dist}/src/main.js`);

  fs.rmSync(outRoot, { recursive: true, force: true });
  fs.mkdirSync(outRoot, { recursive: true });
  fs.cpSync(dist, path.join(outRoot, 'dist'), { recursive: true, force: true });
  fs.copyFileSync(path.join(gwRoot, 'package.json'), path.join(outRoot, 'package.json'));

  // 生产依赖：在组装目录里装一次，避免把 tsc/tsx/vitest 一起塞进安装包
  run('npm', ['install', '--omit=dev', '--no-fund', '--no-audit', '--ignore-scripts'], { cwd: outRoot });

  if (!fs.existsSync(entry)) fail(`组装后缺 ${entry}`);
  log(`组装完成：${outRoot}`);
}

main();
