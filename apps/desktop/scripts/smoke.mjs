// 冒烟外壳（spec §4.4 / §6.2 `npm run smoke`）。
//
// 职责：
//   1. 用**净化 PATH**（去掉 Python / Node）启动 win-unpacked\SkillRouter.exe --smoke-test，
//      以证明「目标机不需要装 Python / Node」（INV-3 的前置条件）；
//   2. 在被测进程**退出之后**执行 `shellChecks`：
//      - `clean_shutdown`：tasklist 里没有 skillrouter-server.exe / SkillRouter.exe 残留（P1-10）
//      - `artifact_hash`：被测二进制的哈希（便于和 SKILLROUTER-SHA256SUMS.txt 对照）
//   3. 把 shellChecks 回填进报告并据其决定退出码。
import { execFileSync, spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const unpacked = path.join(desktopRoot, 'release', 'win-unpacked');
const exe = path.join(unpacked, 'SkillRouter.exe');
const resources = path.join(unpacked, 'resources');
const reportPath = path.join(desktopRoot, 'release', 'smoke-report.json');
const SANITIZED_PATH = 'C:\\Windows\\system32;C:\\Windows';

function log(m) {
  console.log(`[smoke] ${m}`);
}
function fail(m) {
  console.error(`[smoke] FAIL ${m}`);
  process.exit(1);
}

function which(cmd) {
  try {
    const out = execFileSync('where', [cmd], { encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    return out.trim().split(/\r?\n/)[0] ?? null;
  } catch {
    return null;
  }
}

function tasklistCount(image) {
  try {
    const out = execFileSync('tasklist', ['/FI', `IMAGENAME eq ${image}`, '/FO', 'CSV', '/NH'], {
      encoding: 'utf8',
      windowsHide: true,
    });
    return out.split(/\r?\n/).filter((l) => l.toLowerCase().includes(image.toLowerCase())).length;
  } catch {
    return 0;
  }
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

/** 前置检查：单实例锁与冒烟互斥（P2-8），已有实例在跑就直接提示，不去抢锁。 */
function preflight() {
  if (!fs.existsSync(exe)) fail(`缺 ${exe}（先跑 npm run dist:win）`);
  const running = tasklistCount('SkillRouter.exe');
  if (running > 0) {
    fail(`检测到 ${running} 个 SkillRouter.exe 正在运行。冒烟需要独占单实例锁，请先退出应用（P2-8）。`);
  }
}

async function main() {
  preflight();

  const pythonOnPath = which('python') !== null;
  const nodeOnPath = which('node') !== null;
  log(`纯净前探测：pythonOnPath=${pythonOnPath} nodeOnPath=${nodeOnPath}`);
  log(`被测进程 PATH=${SANITIZED_PATH}`);

  // 带空格的临时数据目录（R11：路径含空格 / 中文时参数不能被拆断）
  const dataDir = path.join(os.tmpdir(), `Skill Router Data ${Date.now()}`);
  fs.mkdirSync(dataDir, { recursive: true });
  fs.rmSync(reportPath, { force: true });

  const env = {
    ...process.env,
    PATH: SANITIZED_PATH,
    SR_SMOKE_PYTHON_ON_PATH: pythonOnPath ? '1' : '0',
    SR_SMOKE_NODE_ON_PATH: nodeOnPath ? '1' : '0',
  };
  // 继承变量必须清干净：本机 shell 里就有 ELECTRON_RUN_AS_NODE=1，
  // 漏进去会让 SkillRouter.exe 以 node 身份启动（根本不会进主进程）。
  for (const key of Object.keys(env)) {
    if (/^(SKILLROUTER_|AGENT_|ENGINE_)/.test(key)) delete env[key];
  }
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  delete env.NODE_OPTIONS;
  delete env.ELECTRON_RUN_AS_NODE;

  log(`启动 ${exe} --smoke-test --data-dir "${dataDir}"`);
  const started = Date.now();
  const code = await new Promise((resolve) => {
    const child = spawn(exe, ['--smoke-test', '--smoke-out', reportPath, '--data-dir', dataDir], {
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    child.stdout?.on('data', (b) => process.stdout.write(b));
    child.stderr?.on('data', (b) => process.stderr.write(b));
    child.once('exit', (c) => resolve(c ?? 1));
  });
  log(`被测进程退出码 ${code}（${((Date.now() - started) / 1000).toFixed(1)} s）`);

  if (!fs.existsSync(reportPath)) fail(`没有生成报告 ${reportPath}`);
  const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));

  // ---- shellChecks：只有在被测进程死掉之后才能观察 ----
  const leftoverSidecar = tasklistCount('skillrouter-server.exe');
  const leftoverApp = tasklistCount('SkillRouter.exe');
  const shellChecks = {
    note: '由 scripts/smoke.mjs 在被测进程退出后执行并回填（P1-10）',
    clean_shutdown: {
      ok: leftoverSidecar === 0 && leftoverApp === 0,
      detail: `tasklist: skillrouter-server.exe=${leftoverSidecar} SkillRouter.exe=${leftoverApp}`,
    },
    artifact_hash: {
      ok: true,
      exe: sha256(exe),
      sidecar: sha256(path.join(resources, 'python', 'skillrouter-server.exe')),
      webServer: sha256(path.join(resources, 'web', 'server.js')),
    },
  };
  report.shellChecks = shellChecks;
  const shellOk = Object.values(shellChecks)
    .filter((v) => v !== null && typeof v === 'object' && 'ok' in v)
    .every((v) => v.ok === true);
  report.ok = report.ok === true && shellOk;
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');

  console.log(JSON.stringify(report, null, 2));
  log(`报告：${reportPath}`);
  log(`checks：${report.checks.map((c) => `${c.name}=${c.ok ? 'ok' : 'FAIL'}`).join(' ')}`);
  log(`shellChecks：clean_shutdown=${shellChecks.clean_shutdown.ok ? 'ok' : 'FAIL'}`);
  if (!report.ok || code !== 0) {
    process.exit(1);
  }
  log('冒烟通过');
}

await main();
