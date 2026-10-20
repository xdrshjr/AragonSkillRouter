// 出包校验（spec §6.2 `npm run verify`）。
//
// 逐条断言：
//   1. win-unpacked\resources\{python,web,gateway} 三个目录、app.asar、resources\web\server.js
//   2. skillrouter-server.exe --version 与 SkillRouter.exe --version 都以 0 退出（P1-5）
//   3. resources\web\.next\routes-manifest.json 的 /api/:path* 指向 127.0.0.1:18100（R4 第三层）
//   4. 写 release/SKILLROUTER-SHA256SUMS.txt（安装器 + SkillRouter.exe + 三个资源目录）
//
// 任何一条不满足 → 非 0 退出。**不允许**「打印后继续」。
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const releaseDir = path.join(desktopRoot, 'release');
const unpacked = path.join(releaseDir, 'win-unpacked');
const resources = path.join(unpacked, 'resources');
// 版本只从 package.json 读：写死 "0.1.0" 会在升版后让这条断言指向一个不存在的文件，
// 而真正的安装器就在旁边（表现是「产物明明在，verify 说缺」）。
const { version } = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
const installer = path.join(releaseDir, `SkillRouter-Setup-${version}-win-x64.exe`);
const sumsFile = path.join(releaseDir, 'SKILLROUTER-SHA256SUMS.txt');

/**
 * 净化环境：`ELECTRON_RUN_AS_NODE` 之类变量一旦从父进程漏进来，`SkillRouter.exe --version`
 * 会被 Electron 当成 node 跑（实测本机 shell 里就有 `ELECTRON_RUN_AS_NODE=1`，
 * 打印出 `v20.18.3` 而不是应用版本）。校验必须在一个干净环境里做。
 */
function cleanEnv() {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/^(SKILLROUTER_|AGENT_|ENGINE_)/.test(key)) delete env[key];
  }
  for (const key of ['ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'PYTHONHOME', 'PYTHONPATH']) delete env[key];
  return env;
}

let failures = 0;
function check(label, fn) {
  try {
    const detail = fn();
    console.log(`  ok   ${label}${detail ? ` — ${detail}` : ''}`);
  } catch (err) {
    failures += 1;
    console.error(`  FAIL ${label} — ${String(err instanceof Error ? err.message : err)}`);
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

/** 目录哈希：按相对路径排序后，把「路径 + 文件哈希」再哈希一次（可复现）。 */
function sha256Dir(dir) {
  const files = [];
  const walk = (base) => {
    for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
      const full = path.join(base, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile()) files.push(full);
    }
  };
  walk(dir);
  files.sort();
  const h = crypto.createHash('sha256');
  for (const f of files) h.update(path.relative(dir, f).split(path.sep).join('/')).update('=').update(sha256File(f)).update('\n');
  return { hash: h.digest('hex'), count: files.length };
}

function sizeMb(file) {
  return `${(fs.statSync(file).size / 1024 / 1024).toFixed(1)} MB`;
}

console.log('[verify] 出包产物校验');

console.log('\n1) 产物与资源完整性');
check('NSIS 安装器存在', () => {
  assert(fs.existsSync(installer), `缺 ${installer}（先跑 npm run dist:win）`);
  return sizeMb(installer);
});
check('win-unpacked/SkillRouter.exe 存在', () => {
  const exe = path.join(unpacked, 'SkillRouter.exe');
  assert(fs.existsSync(exe), `缺 ${exe}`);
  return sizeMb(exe);
});
for (const name of ['python', 'web', 'gateway']) {
  check(`resources/${name} 存在`, () => {
    const dir = path.join(resources, name);
    assert(fs.existsSync(dir) && fs.statSync(dir).isDirectory(), `缺 ${dir}`);
    const { count } = sha256Dir(dir);
    return `${count} files`;
  });
}
check('resources/app.asar 存在', () => {
  const asar = path.join(resources, 'app.asar');
  assert(fs.existsSync(asar), `缺 ${asar}`);
  return sizeMb(asar);
});
check('resources/web/server.js 存在', () => {
  const server = path.join(resources, 'web', 'server.js');
  assert(fs.existsSync(server), `缺 ${server}`);
  return `${(fs.statSync(server).size / 1024).toFixed(0)} KB`;
});
check('PyInstaller 双布局迁移目录存在', () => {
  const a = path.join(resources, 'python', '_internal', 'migrations', 'alembic.ini');
  const b = path.join(resources, 'python', '_internal', 'apps', 'api', 'migrations', 'alembic.ini');
  assert(fs.existsSync(a), `缺 ${a}（bootstrap.MIGRATIONS_DIR 推导路径）`);
  assert(fs.existsSync(b), `缺 ${b}（adopt_alembic.MIGRATIONS_DIR 推导路径）`);
  return '2 布局';
});

console.log('\n2) 两个 exe 的 --version（P1-5）');
check('skillrouter-server.exe --version', () => {
  const exe = path.join(resources, 'python', 'skillrouter-server.exe');
  const res = spawnSync(exe, ['--version'], { encoding: 'utf8', windowsHide: true, env: cleanEnv() });
  assert(res.status === 0, `退出码 ${res.status}: ${res.stderr}`);
  return (res.stdout ?? '').trim();
});
check('SkillRouter.exe --version', () => {
  const res = spawnSync(path.join(unpacked, 'SkillRouter.exe'), ['--version'], {
    encoding: 'utf8',
    windowsHide: true,
    env: cleanEnv(),
  });
  assert(res.status === 0, `退出码 ${res.status}: ${res.stderr}`);
  const out = (res.stdout ?? '').trim();
  assert(out.includes('Skill Router'), `输出不是应用版本：${out}（多半是继承到的 ELECTRON_RUN_AS_NODE）`);
  return out;
});

console.log('\n3) 端口契约（构建期烧死的 /api 目标）');
check('routes-manifest.json /api/:path* → 127.0.0.1:18100', () => {
  const manifestPath = path.join(resources, 'web', '.next', 'routes-manifest.json');
  assert(fs.existsSync(manifestPath), `缺 ${manifestPath}`);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const rw = manifest.rewrites;
  const groups = Array.isArray(rw)
    ? rw
    : [rw?.beforeFiles, rw?.afterFiles, rw?.fallback].flatMap((g) => (Array.isArray(g) ? [g] : []));
  let dest = null;
  for (const group of groups) {
    for (const rule of group) if (rule?.source === '/api/:path*') dest = rule.destination;
  }
  assert(dest === 'http://127.0.0.1:18100/api/:path*', `实际是 ${JSON.stringify(dest)}`);
  return dest;
});

console.log('\n4) 哈希清单');
check('写出 SKILLROUTER-SHA256SUMS.txt', () => {
  const lines = [];
  const exe = path.join(unpacked, 'SkillRouter.exe');
  lines.push(`${sha256File(installer)}  ${path.basename(installer)}`);
  lines.push(`${sha256File(exe)}  win-unpacked/SkillRouter.exe`);
  for (const name of ['python', 'web', 'gateway']) {
    const { hash, count } = sha256Dir(path.join(resources, name));
    lines.push(`${hash}  resources/${name}/ (${count} files)`);
  }
  lines.push(`${sha256File(path.join(resources, 'python', 'skillrouter-server.exe'))}  resources/python/skillrouter-server.exe`);
  fs.writeFileSync(sumsFile, `${lines.join('\n')}\n`, 'utf8');
  return sumsFile;
});

if (failures > 0) {
  console.error(`\n[verify] FAIL ${failures} 项不通过`);
  process.exit(1);
}
console.log('\n[verify] 全部通过');
if (fs.existsSync(sumsFile)) {
  console.log('\n--- SKILLROUTER-SHA256SUMS.txt ---');
  console.log(fs.readFileSync(sumsFile, 'utf8'));
}
