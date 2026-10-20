// 构建 Python 边车（spec §2.7 / §8 步骤 3）。
//
// 顺序固定，任一步失败即非 0 退出：
//   1. 解释器来源校验（**禁止 conda**，P1-16）
//   2. pip install -r apps/desktop/python/requirements-build.txt
//   3. PyInstaller onedir
//   4. **双检**：--role bootstrap + --role api 起服务探活（P0-2）；两条都在净化环境下跑（P1-6）
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const repoRoot = path.resolve(desktopRoot, '..', '..');
const buildDir = path.join(desktopRoot, 'build');
const venvPython = path.join(buildDir, '.venv', 'Scripts', 'python.exe');
const distDir = path.join(buildDir, 'python');
const workDir = path.join(buildDir, '.pyi');
const smokeData = path.join(buildDir, '.smoke-data');
const specFile = path.join(desktopRoot, 'python', 'skillrouter-sidecar.spec');
const serverExe = path.join(distDir, 'skillrouter-server', 'skillrouter-server.exe');

const DENY_PREFIXES = ['SKILLROUTER_', 'AGENT_', 'ENGINE_', 'PYTHONHOME', 'PYTHONPATH'];

function log(msg) {
  console.log(`[build:sidecar] ${msg}`);
}

function ok(msg) {
  console.log(`[build:sidecar] OK  ${msg}`);
}

function fail(msg) {
  console.error(`[build:sidecar] FAIL ${msg}`);
  process.exit(1);
}

/** 净化环境：剔除会静默改变行为或指向错误解释器的继承变量（P1-6）。 */
function cleanEnv(extra = {}) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue;
    if (DENY_PREFIXES.some((p) => k.startsWith(p))) continue;
    env[k] = v;
  }
  return { ...env, ...extra };
}

function run(cmd, args, opts = {}) {
  log(`${path.basename(cmd)} ${args.join(' ')}`);
  const res = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (res.error) fail(`${cmd} 无法执行：${res.error.message}`);
  if (res.status !== 0) fail(`${cmd} 退出码 ${res.status}`);
}

function pythonBin() {
  if (process.env.PYTHON_BIN) return process.env.PYTHON_BIN;
  if (fs.existsSync(venvPython)) return venvPython;
  return 'python';
}

function resolveInterpreter() {
  const bin = pythonBin();
  const probe = spawnSync(bin, ['-c', 'import sys;print(sys.base_prefix);print(sys.version.split()[0])'], {
    encoding: 'utf8',
    env: cleanEnv(),
    windowsHide: true,
  });
  if (probe.status !== 0) {
    fail(
      `无法用 ${bin} 取解释器信息。先建一个纯净 CPython 3.11 venv：\n` +
        `  uv venv --seed --python 3.11 apps/desktop/build/.venv\n` +
        `  set PYTHON_BIN=apps\\desktop\\build\\.venv\\Scripts\\python.exe`,
    );
  }
  const [basePrefix = '', version = ''] = probe.stdout.trim().split(/\r?\n/);
  log(`interpreter=${bin} base_prefix=${basePrefix} version=${version}`);

  const banned = /conda|anaconda|miniconda|windowsapps/i.test(`${basePrefix} ${bin}`);
  if (banned) {
    fail(
      `构建解释器来自 conda / Microsoft Store（${basePrefix}）。这类解释器会把 conda DLL 烘进产物，` +
        `在没装 Python 的干净机上表现为「启动即缺 DLL」（P1-16 / R13）。\n` +
        `请改用纯净 CPython 3.11 venv：uv venv --seed --python 3.11 apps/desktop/build/.venv ` +
        `然后 set PYTHON_BIN=apps\\desktop\\build\\.venv\\Scripts\\python.exe`,
    );
  }
  if (!version.startsWith('3.11')) {
    fail(`需要 CPython 3.11，当前是 ${version}`);
  }
  return bin;
}

/** 从混合输出里挖出第一个完整的 JSON 对象（alembic 的 INFO 行也会打到 stdout）。 */
function extractJson(text) {
  const start = text.indexOf('{');
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}

/** 双检 1：bootstrap。期望 stdout 里有 JSON 摘要，退出码 0。 */
function checkBootstrap() {
  const env = cleanEnv({
    SKILLROUTER_DATA_DIR: smokeData,
    SKILLROUTER_DB_PATH: path.join(smokeData, 'skillrouter.db'),
  });
  const res = spawnSync(serverExe, ['--role', 'bootstrap', '--data-dir', smokeData, '--json'], {
    encoding: 'utf8',
    cwd: buildDir, // P1-6：cwd 不是仓库根，避免读到仓库根的 .env
    env,
    windowsHide: true,
    timeout: 180_000,
  });
  const out = `${res.stdout ?? ''}${res.stderr ?? ''}`;
  if (res.status !== 0) fail(`bootstrap 自检失败（退出码 ${res.status}）：\n${out.slice(-4000)}`);
  const raw = extractJson(out);
  if (raw === null) fail(`bootstrap 自检没有输出 JSON：\n${out.slice(-2000)}`);
  let summary;
  try {
    summary = JSON.parse(raw);
  } catch (err) {
    fail(`bootstrap 摘要不是合法 JSON：${String(err)}\n${raw.slice(0, 2000)}`);
  }
  if (!summary.migration || typeof summary.migration.mode !== 'string') {
    fail(`bootstrap 摘要缺 migration.mode：${raw.slice(0, 400)}`);
  }
  ok(`bootstrap 自检通过（migration=${summary.migration.mode}, admin.created=${summary.admin?.created}）`);
  return summary;
}

/** 双检 2：真起一次 api 并探活（P0-2：v1.0 只跑 bootstrap 会绕开这条必崩路径）。 */
async function checkApi() {
  const port = 18099;
  const env = cleanEnv({
    SKILLROUTER_DATA_DIR: smokeData,
    SKILLROUTER_DB_PATH: path.join(smokeData, 'skillrouter.db'),
  });
  const proc = spawn(serverExe, ['--role', 'api', '--port', String(port), '--data-dir', smokeData], {
    cwd: buildDir,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let out = '';
  proc.stdout.on('data', (b) => {
    out += b.toString();
  });
  proc.stderr.on('data', (b) => {
    out += b.toString();
  });

  const deadline = Date.now() + 60_000;
  let body = null;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      const res = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (res.ok) {
        const parsed = await res.json();
        if (parsed.ok === true && parsed.initialized === true) {
          body = parsed;
          break;
        }
      }
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 400));
  }

  try {
    proc.kill();
  } catch {
    /* ignore */
  }

  if (body === null) {
    fail(`api 自检失败：18099 在 60 s 内没有就绪（P0-2 / R3 的典型症状）。stdout/stderr 尾部：\n${out.slice(-4000)}`);
  }
  ok(`api 自检通过（/healthz ok=${body.ok} initialized=${body.initialized} schema=${body.db?.schema_version}）`);
}

async function main() {
  if (!fs.existsSync(specFile)) fail(`缺 spec：${specFile}`);
  fs.mkdirSync(buildDir, { recursive: true });
  fs.mkdirSync(distDir, { recursive: true });
  fs.rmSync(smokeData, { recursive: true, force: true });

  const py = resolveInterpreter();
  ok(`解释器来源校验通过：${py}`);

  run(py, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'apps/desktop/python/requirements-build.txt'], {
    cwd: repoRoot,
    env: cleanEnv(),
  });
  ok('构建依赖就绪');

  run(
    py,
    [
      '-m',
      'PyInstaller',
      '--noconfirm',
      '--clean',
      '--distpath',
      path.relative(repoRoot, distDir),
      '--workpath',
      path.relative(repoRoot, workDir),
      path.relative(repoRoot, specFile),
    ],
    { cwd: repoRoot, env: cleanEnv() },
  );
  if (!fs.existsSync(serverExe)) fail(`PyInstaller 没有产出 ${serverExe}`);
  ok(`sidecar 产出：${serverExe}`);

  // P1-5：--version 必须可执行（verify-artifact 会再断言一次）
  const ver = spawnSync(serverExe, ['--version'], { encoding: 'utf8', cwd: buildDir, env: cleanEnv(), windowsHide: true });
  if (ver.status !== 0) fail(`skillrouter-server.exe --version 退出码 ${ver.status}：${ver.stderr}`);
  ok(`--version → ${(ver.stdout ?? '').trim()}`);

  checkBootstrap();
  await checkApi();
  log('完成');
}

main().catch((err) => fail(String(err?.stack ?? err)));
