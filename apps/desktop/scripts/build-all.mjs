// 一次完整出包的构建编排（spec §2.13）：gateway → web → sidecar → main → 汇总资源。
// 任一步失败即停（打印耗时，便于判断卡在哪一步）。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const buildDir = path.join(desktopRoot, 'build');
const resDir = path.join(desktopRoot, 'resources');

function log(m) {
  console.log(`[build:all] ${m}`);
}
function fail(m) {
  console.error(`[build:all] FAIL ${m}`);
  process.exit(1);
}

function step(label, script) {
  const t0 = Date.now();
  log(`▶ ${label}`);
  const res = spawnSync(process.execPath, [path.join(here, script)], { stdio: 'inherit', cwd: desktopRoot });
  if (res.status !== 0) fail(`${label} 失败（退出码 ${res.status}）`);
  log(`✔ ${label}（${((Date.now() - t0) / 1000).toFixed(1)} s）`);
}

function copyDir(from, to) {
  if (!fs.existsSync(from)) fail(`缺构建产物：${from}`);
  fs.rmSync(to, { recursive: true, force: true });
  fs.cpSync(from, to, { recursive: true, dereference: true, force: true });
}

function main() {
  // 1) 网关最便宜，先失败先止损
  step('gateway', 'build-gateway.mjs');
  // 2) 控制台
  step('web', 'build-web.mjs');
  // 3) Python 边车（含双检）
  step('sidecar', 'build-sidecar.mjs');
  // 4) 主进程（esbuild）
  step('main', '../esbuild.config.mjs');

  // 5) 汇总到 resources/（electron-builder 的 extraResources 源）
  log('汇总 resources/ …');
  copyDir(path.join(buildDir, 'gateway'), path.join(resDir, 'gateway'));
  copyDir(path.join(buildDir, 'web'), path.join(resDir, 'web'));
  copyDir(path.join(buildDir, 'python', 'skillrouter-server'), path.join(resDir, 'python'));

  const required = [
    path.join(resDir, 'python', 'skillrouter-server.exe'),
    path.join(resDir, 'web', 'server.js'),
    path.join(resDir, 'gateway', 'dist', 'src', 'main.js'),
    path.join(desktopRoot, 'dist', 'main.js'),
    path.join(desktopRoot, 'dist', 'preload.js'),
  ];
  const missing = required.filter((p) => !fs.existsSync(p));
  if (missing.length > 0) fail(`资源汇总后仍缺件：\n${missing.join('\n')}`);
  log('资源汇总完成');
}

main();
