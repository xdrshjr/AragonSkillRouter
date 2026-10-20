/**
 * 应用入口（spec §2.5 / §2.10 / §4.1）。
 *
 * 职责：单实例锁 → userData 重定 → 状态机启动 → 窗口 → 关停编排。
 * 所有「怎么 spawn」的细节都在 supervisor.ts，所有「环境怎么来」的细节都在 env.ts。
 */
import { app, Menu } from 'electron';
import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import {
  API_SERVICE_TAG,
  APP_ID,
  BOOTSTRAP_TIMEOUT_MS,
  DEFAULT_WEB_PORT,
  DEFAULT_GATEWAY_PORT,
  ERROR,
  PROBE_INTERVAL_MS,
  PRODUCT_NAME,
} from '../shared/constants';
import { DesktopError } from '../shared/types';
import type { CliOptions, ServiceName, ServiceState, ServiceStatus, StartupState } from '../shared/types';
import { composeEnv } from './env';
import { loadDesktopConfig, recordPorts, repairApiPort } from './config';
import { Logger } from './logger';
import { configureUserData, resolvePaths } from './paths';
import type { DesktopPaths } from './paths';
import {
  checkPortContract,
  classifyApiPort,
  describePortConflict,
  readManifestApiPort,
  scanPorts,
} from './ports';
import { SHUTDOWN_ORDER, buildBootstrapSpec, buildServiceSpec } from './services';
import { httpProbe, pollUntil, Supervisor } from './supervisor';
import { restartDelay, runStartup } from './startup';
import type { StartupPorts } from './startup';
import { registerIpc } from './ipc';
import { buildMenu } from './menu';
import {
  createMainWindow,
  createSplash,
  openLogsDir,
  showErrorWindow,
  showInstanceRunningDialog,
  updateSplash,
} from './windows';
import { defaultSmokeOutPath, runSmoke } from './smoke';

/** 进程启动时刻：冒烟报告的 `startedAt` / `durationMs` 以它为起点（含自举耗时）。 */
const PROCESS_STARTED_AT = new Date().toISOString();

function parseArgs(argv: string[]): CliOptions {
  const opts: CliOptions = { smokeTest: false, smokeOut: null, dataDir: null, dev: false, verbose: false, version: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--smoke-test') opts.smokeTest = true;
    else if (arg === '--smoke-out') opts.smokeOut = argv[(i += 1)] ?? null;
    else if (arg === '--data-dir') opts.dataDir = argv[(i += 1)] ?? null;
    else if (arg === '--dev') opts.dev = true;
    else if (arg === '--verbose') opts.verbose = true;
    else if (arg === '--version' || arg === '-v') opts.version = true;
  }
  return opts;
}

const opts = parseArgs(process.argv.slice(1));

// §4.1：--version 不建窗口、不 spawn、不申请锁，恒 0 退出
if (opts.version) {
  process.stdout.write(`${PRODUCT_NAME} ${app.getVersion()} (electron ${process.versions.electron})\n`);
  app.exit(0);
}

app.setAppUserModelId(APP_ID); // P2-3：任务栏分组 / 通知归属与 appId 一致
configureUserData();

const paths: DesktopPaths = resolvePaths({ dataDirOverride: opts.dataDir });
const logger = new Logger({ dir: paths.logsDir, verbose: opts.verbose });

if (opts.smokeTest) logger.main(`event=smoke_mode out=${opts.smokeOut ?? defaultSmokeOutPath(paths.logsDir)}`);

// §2.3.2 第 1 条：单实例锁是**唯一**第一判据。拿不到锁不探端口、不 spawn、不写错误码。
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  logger.main('event=second_instance');
  app.exit(0);
}

const supervisor = new Supervisor(logger);
const config = loadDesktopConfig(paths.userData, (line) => logger.main(line));

let currentState: StartupState = 'idle';
let degraded = false;
let errorCode: string | null = null;
let ports: StartupPorts = {
  api: config.apiPort,
  web: config.webPort,
  gateway: config.gatewayPort,
};
let mainWindow: ReturnType<typeof createMainWindow> | null = null;
let splash: Electron.BrowserWindow | null = null;
let restartAttempts = new Map<ServiceName, number>();
let shuttingDown = false;

const windowCtx = {
  preloadScript: paths.preloadScript,
  splashHtml: paths.splashHtml,
  logsDir: paths.logsDir,
  iconPath: fs.existsSync(path.join(__dirname, 'icon.png')) ? path.join(__dirname, 'icon.png') : undefined,
};

function status(): ServiceStatus {
  const up = currentState === 'ready';
  const down = currentState === 'failed';
  const mark = (): ServiceState => (up ? 'up' : down ? 'down' : 'starting');
  return {
    state: currentState,
    api: mark(),
    gateway: degraded ? 'down' : mark(),
    worker: mark(),
    web: mark(),
    degraded,
    errorCode,
    ports: { api: ports.api, gateway: ports.gateway, web: ports.web },
  };
}

/** 诊断信息（菜单「复制诊断信息」）。 */
function diagnostics(): string {
  return [
    `${PRODUCT_NAME} ${app.getVersion()} · electron ${process.versions.electron}`,
    `state=${currentState} degraded=${degraded} error=${errorCode ?? 'none'}`,
    `ports=${JSON.stringify(ports)}`,
    `userData=${paths.userData}`,
    `data=${paths.data}`,
    `logs=${paths.logsDir}`,
    `resources=${paths.resourcesRoot}`,
    `api_healthz=${`http://127.0.0.1:${ports.api}/healthz`}`,
    `internal_url(SKILLROUTER_INTERNAL_URL)=${composeEnv('gateway', envOptions()).SKILLROUTER_INTERNAL_URL}`,
  ].join('\n');
}

function envOptions() {
  return {
    dataDir: paths.data,
    dbPath: paths.dbPath,
    apiPort: ports.api,
    webPort: ports.web,
    gatewayPort: ports.gateway,
    gatewaySecret: config.gatewaySecret,
  };
}

function ensureDirTree(): void {
  for (const dir of [paths.userData, paths.data, paths.logsDir]) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

/** `preparing` 的产物校验（`--dev` 跳过，P1-15）。 */
function assertResources(): void {
  const required: Array<[string, string]> = [
    [paths.sidecarExe, 'Python 边车（skillrouter-server.exe）'],
    [paths.webEntry, 'Next standalone（web\\server.js）'],
    [paths.gatewayEntry, '网关产物（gateway\\dist\\src\\main.js）'],
  ];
  const missing = required.filter(([p]) => !fs.existsSync(p)).map(([p, label]) => `${label}: ${p}`);
  if (missing.length > 0) {
    throw new DesktopError(ERROR.RESOURCE_MISSING, `缺少运行资源：\n${missing.join('\n')}`);
  }
}

/** 端口契约校验（§2.3.1 运行期那一半）：能自动校正就校正，否则 E-D07。 */
function enforcePortContract(): void {
  const manifestPort = readManifestApiPort(paths.webRoot);
  const verdict = checkPortContract({ configPort: config.apiPort, manifestPort, sidecarPort: ports.api });
  if (verdict.action === 'error') {
    throw new DesktopError(ERROR.PORT_CONTRACT_MISMATCH, verdict.detail);
  }
  if (verdict.action === 'repair') {
    config.apiPort = verdict.port;
    repairApiPort(paths.userData, config, verdict.port, (line) => logger.main(line));
    ports = { ...ports, api: verdict.port };
  }
  logger.main(`event=port_contract_ok api=${ports.api} manifest=${manifestPort ?? 'n/a'}`);
}

async function prepare(): Promise<void> {
  ensureDirTree();

  const stray = path.join(paths.data, '.env');
  if (fs.existsSync(stray)) {
    logger.main(`error=${ERROR.STRAY_ENV_FILE} path=${stray}（Settings 会按 cwd 读它，只警告不删除）`);
  }

  if (opts.dev) {
    logger.main('event=dev_mode（跳过产物校验与端口契约）');
    return;
  }
  assertResources();
  enforcePortContract();
}

async function resolvePorts(): Promise<StartupPorts> {
  if (opts.dev) {
    return { api: 8000, web: 3000, gateway: 8500 };
  }

  const verdict = await classifyApiPort(ports.api, httpProbe);
  if (verdict.kind !== 'free') {
    const { code, detail } = describePortConflict(ports.api, verdict);
    throw new DesktopError(code, detail);
  }

  const web = await scanPorts(Math.max(DEFAULT_WEB_PORT, ports.api + 1));
  const gateway = await scanPorts(Math.max(DEFAULT_GATEWAY_PORT, web + 1));
  const resolved: StartupPorts = { api: ports.api, web, gateway };
  ports = resolved;
  config.webPort = web;
  config.gatewayPort = gateway;
  recordPorts(paths.userData, config, { webPort: web, gatewayPort: gateway });
  return resolved;
}

function serviceCtx() {
  return {
    paths,
    ports,
    gatewaySecret: config.gatewaySecret,
    electronExecPath: process.execPath,
  };
}

/**
 * 正在跑的 bootstrap 子进程（`{ role: bootstrap }`）。
 *
 * 它不归 supervisor 管（要同步等退出码、要接 stdout 摘要），但**必须**在这里也留一份引用：
 * 否则用户在「初始化数据库」那 120 s 里关窗口时，`shutdown()` 只关得掉 supervisor 的四个服务，
 * 这个 Python 进程会变成孤儿继续写库（下次启动就 `DB_BUSY`，R9）。
 */
let bootstrapChild: ChildProcess | null = null;

/** 跑一次 `--role bootstrap`，返回退出码与输出（上限 120 s）。 */
function runBootstrap(): Promise<{ code: number; output: string }> {
  const spec = buildBootstrapSpec(serviceCtx());
  return new Promise((resolve) => {
    const child = spawn(spec.exe, spec.args, {
      cwd: spec.cwd,
      env: spec.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: false,
    });
    bootstrapChild = child;
    let output = '';
    child.stdout?.on('data', (b: Buffer) => {
      output += b.toString('utf8');
    });
    child.stderr?.on('data', (b: Buffer) => {
      output += b.toString('utf8');
    });

    const timer = setTimeout(() => {
      logger.main(`error=${ERROR.BOOTSTRAP_FAILED} detail=timeout pid=${child.pid ?? 0}`);
      child.kill();
    }, BOOTSTRAP_TIMEOUT_MS);

    child.once('error', (err) => {
      clearTimeout(timer);
      bootstrapChild = null;
      resolve({ code: 1, output: `${output}\n${String(err)}` });
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      bootstrapChild = null;
      for (const line of output.split(/\r?\n/).filter((l) => l.length > 0)) {
        logger.child('api', 'stdout', line);
      }
      resolve({ code: code ?? 1, output });
    });
  });
}

function waitReady(name: ServiceName, timeoutMs: number): Promise<{ ok: boolean; detail: string }> {
  const spec = buildServiceSpec(name, serviceCtx());
  if (spec.probeUrl === null) return Promise.resolve({ ok: true, detail: 'no probe' });

  return pollUntil(
    async () => {
      // 子进程已经退出就没必要等满超时：直接报「启动即崩」，日志里已有 Traceback
      if (!supervisor.has(name)) {
        return { ok: false, fatal: true, detail: `${name} 进程已退出（见 logs\\desktop\\${name}.log）` };
      }
      const res = await httpProbe(spec.probeUrl as string);
      if (name === 'api') {
        try {
          const body = JSON.parse(res.body) as { ok?: boolean; initialized?: boolean; service?: string };
          const ok = res.status === 200 && body.ok === true && body.initialized === true;
          return { ok, detail: `status=${res.status} service=${body.service ?? '?'} initialized=${String(body.initialized)}` };
        } catch {
          return { ok: false, detail: `status=${res.status} body-not-json` };
        }
      }
      // web：200 或 302/307 都算就绪
      const ok = res.status === 200 || res.status === 302 || res.status === 307;
      return { ok, detail: `status=${res.status}` };
    },
    timeoutMs,
    PROBE_INTERVAL_MS,
  );
}

function writeFirstRunSummary(migrationMode: string, adminCreated: boolean, output: string): void {
  let summary: Record<string, unknown> = {};
  const start = output.indexOf('{');
  if (start >= 0) {
    try {
      summary = JSON.parse(output.slice(start, output.lastIndexOf('}') + 1)) as Record<string, unknown>;
    } catch {
      summary = {};
    }
  }
  const payload = {
    capturedAt: new Date().toISOString(),
    migration: { mode: migrationMode },
    admin: { created: adminCreated, username: 'admin' },
    master_key_fingerprint: summary.master_key_fingerprint ?? null,
    env: summary.env ?? 'development',
    public_base_url: summary.public_base_url ?? `http://127.0.0.1:${ports.web}`,
  };
  fs.writeFileSync(path.join(paths.data, 'desktop-first-run.json'), `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  logger.main(`event=first_run admin_created=${adminCreated}`);
}

/**
 * 从 `--role bootstrap --json` 的混合输出里挖出摘要。
 *
 * 为什么不能直接 `JSON.parse(stdout)`：alembic 会把 `INFO [alembic.runtime.migration] …`
 * 一行行打到 **stdout**，所以 stdout 不是纯 JSON（实施期记录 I-9）。这里取第一个 `{` 到
 * 最后一个 `}` 之间的片段，解析失败就退回「未知」，绝不影响启动。
 */
function parseBootstrapSummary(output: string): { adminCreated: boolean; migrationMode: string } {
  const start = output.indexOf('{');
  const end = output.lastIndexOf('}');
  if (start < 0 || end <= start) return { adminCreated: false, migrationMode: 'unknown' };
  try {
    const parsed = JSON.parse(output.slice(start, end + 1)) as {
      admin?: { created?: boolean };
      migration?: { mode?: string };
    };
    return {
      adminCreated: parsed.admin?.created === true,
      migrationMode: parsed.migration?.mode ?? 'unknown',
    };
  } catch {
    return { adminCreated: false, migrationMode: 'unknown' };
  }
}

interface StartServicesResult {
  ok: boolean;
  errorCode: string | null;
  detail: string;
  /** `--role bootstrap` 的真实退出码（冒烟报告要它，spec §4.4 的 `sidecar_bootstrap.code`）。 */
  bootstrapCode: number | null;
  /** bootstrap 摘要里的 `migration.mode`（`unknown` = 没跑到 / 解析失败）。 */
  migrationMode: string;
}

async function startServices(): Promise<StartServicesResult> {
  let bootstrapCode: number | null = null;
  let bootstrapOutput = '';

  const result = await runStartup({
    dev: opts.dev,
    log: (line) => logger.main(line),
    onState: (state) => {
      currentState = state;
      updateSplash(windowCtx, splash, state, config.adminInitialized);
    },
    prepare,
    resolvePorts,
    configure: (p) => {
      ports = p;
    },
    bootstrap: async () => {
      if (opts.dev) return { code: 0, output: '{"migration":{"mode":"dev_skipped"}}' };
      const res = await runBootstrap();
      bootstrapCode = res.code;
      bootstrapOutput = res.output;
      return res;
    },
    start: async (name) => {
      if (opts.dev) return;
      supervisor.spawn(buildServiceSpec(name, serviceCtx()));
    },
    waitReady: async (name, timeout) => {
      if (opts.dev) return { ok: true, detail: 'dev' };
      return waitReady(name, timeout);
    },
    onFirstRun: (output) => {
      if (opts.dev) return;
      const parsed = parseBootstrapSummary(output);
      config.adminInitialized = parsed.adminCreated;
      recordPorts(paths.userData, config, {});
      writeFirstRunSummary(parsed.migrationMode, parsed.adminCreated, output);
    },
    onDegraded: (code, detail) => {
      degraded = true;
      errorCode = code;
      logger.main(`error=${code} detail=${detail}`);
    },
  });

  if (!result.ok) errorCode = result.errorCode;
  return {
    ok: result.ok,
    errorCode: result.errorCode,
    detail: result.detail,
    bootstrapCode,
    migrationMode: parseBootstrapSummary(bootstrapOutput).migrationMode,
  };
}


/** 崩溃监控（§2.10）：ready 之后的意外退出按重要性重启（1 s / 3 s / 9 s）。 */
supervisor.onExit((name, code, signal) => {
  if (shuttingDown || currentState !== 'ready') return;
  const attempt = restartAttempts.get(name) ?? 0;
  const delay = restartDelay(name, attempt);
  logger.main(`event=child_crash name=${name} exit=${String(code)} signal=${String(signal)} attempt=${attempt}`);
  if (delay === null) {
    degraded = true;
    errorCode = ERROR.GATEWAY_DOWN;
    logger.main(`error=${ERROR.GATEWAY_DOWN} detail=${name} 重启次数用尽`);
    return;
  }
  restartAttempts.set(name, attempt + 1);
  logger.main(`restart=${name} delay=${delay}`);
  setTimeout(() => {
    if (shuttingDown) return;
    try {
      supervisor.spawn(buildServiceSpec(name, serviceCtx()));
    } catch (err) {
      logger.main(`error=${ERROR.RESOURCE_MISSING} detail=${String(err)}`);
    }
  }, delay);
});

async function shutdown(): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  supervisor.markShuttingDown();
  logger.main('event=shutdown_begin');

  // bootstrap 不归 supervisor 管，但它也是本应用起的子进程，同样不能留成孤儿。
  if (bootstrapChild !== null && bootstrapChild.exitCode === null) {
    const pid = bootstrapChild.pid ?? 0;
    try {
      bootstrapChild.kill();
      logger.main(`event=stopped name=bootstrap pid=${pid} graceful=true`);
    } catch {
      /* 已经退出了 */
    }
    bootstrapChild = null;
  }

  await supervisor.stopAll(SHUTDOWN_ORDER);
  logger.main('event=shutdown_done');
}

/**
 * 「重启服务」（菜单 / IPC 共用一份实现）。
 *
 * 关键是**复位 `supervisor` 的关停标志**：只把本文件的 `shuttingDown` 置回 false 是不够的，
 * 子进程退出回调会继续被 supervisor 拦掉，之后任何崩溃都不再触发重启（静默失去监控）。
 */
let restarting = false;
async function restartServices(): Promise<void> {
  if (restarting) return;
  restarting = true;
  logger.main('event=restart_services_begin');
  try {
    await shutdown();
    shuttingDown = false;
    supervisor.resetShuttingDown();
    restartAttempts = new Map();
    degraded = false;
    errorCode = null;
    const result = await startServices();
    logger.main(`event=restart_services_done ok=${result.ok} error=${result.errorCode ?? 'none'}`);
    if (mainWindow !== null && !mainWindow.win.isDestroyed()) {
      void mainWindow.win.loadURL(`http://127.0.0.1:${ports.web}/`);
    }
  } finally {
    restarting = false;
  }
}

app.on('second-instance', () => {
  logger.main('event=second_instance_focus');
  if (mainWindow !== null && !mainWindow.win.isDestroyed()) {
    if (mainWindow.win.isMinimized()) mainWindow.win.restore();
    mainWindow.win.focus();
  }
});

app.on('window-all-closed', () => {
  // Windows 下默认不退出：不退出的话 uvicorn 还在跑，下次双击会遇到「库被占用」（§2.10）
  app.quit();
});

app.on('before-quit', (event) => {
  if (shuttingDown) return;
  event.preventDefault();
  void shutdown().finally(() => app.exit(0));
});

/** 冒烟：拉起第二个实例，断言它立刻以 0 退出、不启动任何服务。 */
function checkSingleInstance(): Promise<{ ok: boolean; detail: string }> {
  return new Promise((resolve) => {
    const args = [...(app.isPackaged ? [] : [app.getAppPath()]), '--data-dir', paths.data];
    const child = spawn(process.execPath, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    let out = '';
    child.stdout?.on('data', (b: Buffer) => {
      out += b.toString('utf8');
    });
    child.stderr?.on('data', (b: Buffer) => {
      out += b.toString('utf8');
    });
    const timer = setTimeout(() => {
      child.kill();
      resolve({ ok: false, detail: 'second instance did not exit within 20 s' });
    }, 20_000);
    child.once('exit', (code) => {
      clearTimeout(timer);
      const spawnCount = (out.match(/event=spawn/g) ?? []).length;
      resolve({
        ok: code === 0 && spawnCount === 0,
        detail: `second launch exited with code ${String(code)} and spawned ${spawnCount} services`,
      });
    });
  });
}

async function runSmokeMode(): Promise<void> {
  const started = await startServices();
  const outPath = opts.smokeOut ?? defaultSmokeOutPath(paths.logsDir);

  let apiSchemaVersion: number | null = null;
  if (started.ok) {
    try {
      const health = await httpProbe(`http://127.0.0.1:${ports.api}/healthz`);
      const body = JSON.parse(health.body) as { db?: { schema_version?: number } };
      apiSchemaVersion = body.db?.schema_version ?? null;
    } catch {
      apiSchemaVersion = null;
    }
  }

  const { code } = await runSmoke(
    {
      appVersion: app.getVersion(),
      electronVersion: process.versions.electron,
      startedAt: PROCESS_STARTED_AT,
      ports: { api: ports.api, gateway: ports.gateway, web: ports.web },
      manifestPort: opts.dev ? null : readManifestApiPort(paths.webRoot),
      dataDir: paths.data,
      resources: { sidecar: paths.sidecarExe, web: paths.webEntry, gateway: paths.gatewayEntry },
      gatewayInternalUrl: composeEnv('gateway', envOptions()).SKILLROUTER_INTERNAL_URL ?? '',
      probe: async (url, init) => {
        const res = await fetch(url, { ...init, redirect: 'manual' });
        const body = await res.text();
        return { status: res.status, body, headers: res.headers };
      },
      checkSingleInstance,
      bootstrapExitCode: started.bootstrapCode,
      // 摘要来自本次 bootstrap 的 stdout（不是磁盘上上一次的 desktop-first-run.json）
      migrationMode: started.migrationMode,
      adminCreated: config.adminInitialized,
      apiSchemaVersion,
    },
    outPath,
  );

  await shutdown();
  app.exit(code);
}

async function runInteractive(): Promise<void> {
  splash = createSplash(windowCtx, 'preparing');
  const started = await startServices();

  if (!started.ok) {
    if (splash !== null && !splash.isDestroyed()) splash.destroy();
    splash = null;
    const code = started.errorCode ?? ERROR.RESOURCE_MISSING;
    if (code === ERROR.INSTANCE_RUNNING) {
      await showInstanceRunningDialog(windowCtx, started.detail);
      app.exit(0);
      return;
    }
    await showErrorWindow(windowCtx, { code, detail: started.detail, tail: logger.tail('main', 40) }, (action) => {
      if (action === 'retry') {
        app.relaunch();
        app.exit(0);
        return;
      }
      if (action === 'logs') void openLogsDir(paths.logsDir);
      app.exit(0);
    });
    return;
  }

  mainWindow = createMainWindow(windowCtx, `http://127.0.0.1:${ports.web}/`);
  if (splash !== null && !splash.isDestroyed()) splash.destroy();
  splash = null;

  registerIpc({
    getStatus: status,
    dataDir: paths.data,
    logsDir: paths.logsDir,
    restartServices: () => restartServices(),
  });

  Menu.setApplicationMenu(
    buildMenu({
      dataDir: paths.data,
      logsDir: paths.logsDir,
      getStatus: status,
      diagnostics,
      reload: () => mainWindow?.win.webContents.reload(),
      openDevTools: () => mainWindow?.win.webContents.openDevTools({ mode: 'detach' }),
      restartServices: () => restartServices(),
    }),
  );

  logger.main(
    `event=ready_open url=http://127.0.0.1:${ports.web}/ gateway.internal_url=${composeEnv('gateway', envOptions()).SKILLROUTER_INTERNAL_URL}`,
  );
}

app.whenReady().then(async () => {
  logger.main(`event=app_ready version=${app.getVersion()} electron=${process.versions.electron}`);
  logger.main(`event=port_contract ports=${JSON.stringify(ports)}`);
  logger.main(
    `event=env api_toolchain=${composeEnv('api', envOptions()).SKILLROUTER_ENGINE_TOOLCHAIN} ` +
      `gateway_internal_url=${composeEnv('gateway', envOptions()).SKILLROUTER_INTERNAL_URL}`,
  );

  if (opts.smokeTest) {
    await runSmokeMode();
    return;
  }
  await runInteractive();

  app.on('activate', () => {
    // 只在窗口被真正销毁后重建（避免重复 registerIpc / 重复状态机）
    if (mainWindow !== null && mainWindow.win.isDestroyed()) {
      mainWindow = null;
      void runInteractive();
    }
  });

  // api 的 `/healthz` 用于事后核对「网关回调打的是不是 18100」（R14 的事后证据）
  try {
    const res = await httpProbe(`http://127.0.0.1:${ports.api}/healthz`);
    const body = JSON.parse(res.body) as { service?: string };
    logger.main(`event=api_healthz service=${body.service ?? '?'} matches=${body.service === API_SERVICE_TAG}`);
  } catch {
    /* 忽略 */
  }
});
