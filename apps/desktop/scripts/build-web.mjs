// 构建并组装 Next.js standalone 控制台宿主（spec §2.8）。
//
// 顺序固定：
//   1. apps/web 依赖（node_modules 已存在则跳过；FORCE_NPM_CI=1 强制 npm ci）
//   2. 用**构建期** api 地址 next build（SKILLROUTER_API_URL=http://127.0.0.1:18100）
//   3. 组装 build/web：.next/standalone/* + .next/static + public
//   4. 断言 build/web/server.js 存在
//   5. 端口契约断言：routes-manifest.json 的 /api/:path* 必须指向 18100（P1-17，构建期防线）
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const repoRoot = path.resolve(desktopRoot, '..', '..');
const webRoot = path.join(repoRoot, 'apps', 'web');
const outRoot = path.join(desktopRoot, 'build', 'web');
const API_PORT = 18100;
const API_URL = `http://127.0.0.1:${API_PORT}`;

function log(m) {
  console.log(`[build:web] ${m}`);
}
function fail(m) {
  console.error(`[build:web] FAIL ${m}`);
  process.exit(1);
}

function run(cmd, args, opts = {}) {
  log(`${cmd} ${args.join(' ')}`);
  const res = spawnSync(cmd, args, { stdio: 'inherit', shell: process.platform === 'win32', ...opts });
  if (res.error) fail(`${cmd} 无法执行：${res.error.message}`);
  if (res.status !== 0) fail(`${cmd} 退出码 ${res.status}`);
}

function copyTree(from, to) {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.cpSync(from, to, { recursive: true, dereference: true, force: true });
}

function assertDir(p, hint) {
  if (!fs.existsSync(p) || !fs.statSync(p).isDirectory()) fail(`缺目录 ${p}：${hint}`);
}

function checkPortContract() {
  const manifestPath = path.join(webRoot, '.next', 'routes-manifest.json');
  if (!fs.existsSync(manifestPath)) fail(`缺 ${manifestPath}（next build 没跑成功？）`);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  // Next 15 的 routes-manifest.json 把 rewrites 存成 {beforeFiles, afterFiles, fallback}，
  // 早期版本是数组 —— 两种形态都要支持（实施期实测：只认数组会得到 null，把绿构建判成红）。
  const rw = manifest.rewrites;
  const groups = Array.isArray(rw)
    ? rw
    : [rw?.beforeFiles, rw?.afterFiles, rw?.fallback].flatMap((g) => (Array.isArray(g) ? [g] : []));
  let apiDest = null;
  let healthDest = null;
  for (const group of groups) {
    if (!Array.isArray(group)) continue;
    for (const rule of group) {
      if (rule?.source === '/api/:path*') apiDest = rule.destination;
      if (rule?.source === '/healthz') healthDest = rule.destination;
    }
  }
  const expected = `${API_URL}/api/:path*`;
  if (apiDest !== expected) {
    fail(
      `端口契约不成立：routes-manifest.json 的 /api/:path* 目标是 ${JSON.stringify(apiDest)}，期望 ${expected}。\n` +
        `这意味着构建时没有注入 SKILLROUTER_API_URL，运行期会出现「页面能开、/api 全 502」（R4）。`,
    );
  }
  if (typeof healthDest !== 'string' || !healthDest.startsWith(API_URL)) {
    fail(`端口契约不成立：/healthz 目标是 ${JSON.stringify(healthDest)}，期望以 ${API_URL} 开头`);
  }
  log(`端口契约断言通过：/api/:path* → ${apiDest}`);
}

function main() {
  assertDir(webRoot, 'apps/web 不存在');
  const needInstall = process.env.FORCE_NPM_CI === '1' || !fs.existsSync(path.join(webRoot, 'node_modules'));
  if (needInstall) {
    run('npm', ['ci', '--include=dev', '--no-fund', '--no-audit'], { cwd: webRoot });
  } else {
    log('apps/web/node_modules 已存在，跳过 npm ci（FORCE_NPM_CI=1 可强制）');
  }

  // 以 Node 注入 env，避开 Windows shell 的变量语法差异
  run('npm', ['run', 'build'], {
    cwd: webRoot,
    env: { ...process.env, SKILLROUTER_API_URL: API_URL, NEXT_TELEMETRY_DISABLED: '1' },
  });
  log('next build 完成');

  checkPortContract();

  const standalone = path.join(webRoot, '.next', 'standalone');
  assertDir(standalone, "output: 'standalone' 没生效，或 tracing root 变了（spec §2.8 末段）");
  const serverJs = path.join(standalone, 'server.js');
  if (!fs.existsSync(serverJs)) {
    fail(`缺 ${serverJs}：standalone 根目录结构与 spec 假设不一致`);
  }

  fs.rmSync(outRoot, { recursive: true, force: true });
  fs.mkdirSync(outRoot, { recursive: true });
  copyTree(standalone, outRoot);
  copyTree(path.join(webRoot, '.next', 'static'), path.join(outRoot, '.next', 'static'));
  const publicDir = path.join(webRoot, 'public');
  if (fs.existsSync(publicDir)) copyTree(publicDir, path.join(outRoot, 'public'));

  if (!fs.existsSync(path.join(outRoot, 'server.js'))) fail(`组装后缺 ${path.join(outRoot, 'server.js')}`);
  log(`组装完成：${outRoot}`);
}

main();
