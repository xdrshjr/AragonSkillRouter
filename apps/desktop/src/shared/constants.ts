/**
 * 桌面端共享常量（spec §2.5 / §2.3 / §2.6 / §4.5）。
 *
 * 这里是端口、超时、环境变量黑名单与错误码的**唯一**定义处：
 * 主进程、冒烟测试、单测全部从这里取值，禁止在别处再写字面量。
 */

export const PRODUCT_NAME = 'Skill Router';
export const APP_ID = 'com.skillrouter.desktop';

/** 端口：api 是构建期烧死值（spec D6），web / gateway 允许运行时上移。 */
export const DEFAULT_API_PORT = 18100;
export const DEFAULT_WEB_PORT = 18101;
export const DEFAULT_GATEWAY_PORT = 18102;
/** 顺序扫描的尝试次数（18101/18102 起各试 10 个）。 */
export const PORT_SCAN_SPAN = 10;

/** 探针与超时（**上限**语义，超时即失败；不是预期耗时）。 */
export const PROBE_INTERVAL_MS = 400;
export const API_READY_TIMEOUT_MS = 90_000;
export const GATEWAY_READY_TIMEOUT_MS = 40_000;
export const WEB_READY_TIMEOUT_MS = 120_000;
export const BOOTSTRAP_TIMEOUT_MS = 120_000;
export const SHUTDOWN_GRACE_MS = 8_000;
/** 崩溃后的退避重启间隔（1 s / 3 s / 9 s，最多 3 次）。 */
export const RESTART_BACKOFF_MS = [1_000, 3_000, 9_000] as const;

/** 日志：单文件 10 MB × 5 轮转。 */
export const LOG_MAX_BYTES = 10 * 1024 * 1024;
export const LOG_MAX_ROTATIONS = 5;

/**
 * 继承环境变量前缀黑名单（spec §2.6 第 2 条 / P1-12）。
 *
 * `composeEnv()` 的输入必须是「干净基础环境 + 本表」，凡命中以下前缀的继承变量一律 `delete`。
 * 理由：`SKILLROUTER_ENV` / `SKILLROUTER_DATA_DIR` / `SKILLROUTER_ENGINE_TOOLCHAIN=local` 一旦
 * 从父进程漏进来就会**静默改变**桌面端行为，且几乎无法复现。
 */
export const ENV_DENY_PREFIXES = [
  'SKILLROUTER_',
  'AGENT_',
  'ENGINE_',
  'PYTHON',
  'NODE_OPTIONS',
  'ELECTRON_RUN_AS_NODE',
] as const;

/** 桌面端错误码（→ docs/runbook/desktop-app.md 的处置表）。 */
export const ERROR = {
  PORT_IN_USE: 'E-D01 DESKTOP_PORT_IN_USE',
  RESOURCE_MISSING: 'E-D02 DESKTOP_RESOURCE_MISSING',
  BOOTSTRAP_FAILED: 'E-D03 DESKTOP_BOOTSTRAP_FAILED',
  API_TIMEOUT: 'E-D04 DESKTOP_API_TIMEOUT',
  WEB_TIMEOUT: 'E-D05 DESKTOP_WEB_TIMEOUT',
  PORT_CONTRACT_MISMATCH: 'E-D07 DESKTOP_PORT_CONTRACT_MISMATCH',
  GATEWAY_DOWN: 'W-D01 DESKTOP_GATEWAY_DOWN',
  STRAY_ENV_FILE: 'W-D02 DESKTOP_STRAY_ENV_FILE',
  INSTANCE_RUNNING: 'W-D06 DESKTOP_INSTANCE_RUNNING',
} as const;

export type ErrorCode = (typeof ERROR)[keyof typeof ERROR];

/** 环境：桌面端按 D7 用 development（production 会强制 Secure Cookie，明文回环下登录必失败）。 */
export const DESKTOP_ENV = 'development';

/** 首次登录凭据（仅首次初始化生效，登录后强制改密由后端既有流程驱动）。 */
export const ADMIN_USER = 'admin';
export const ADMIN_PASSWORD = 'adminjr';

/** 引擎 pin（与仓库现值一致）。 */
export const ENGINE_PIN = '0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8';
export const ENGINE_PIN_VERSION = '0.2.34';
export const ENGINE_CHANNEL = 'stable';
export const ENGINE_TOOLCHAIN = 'gateway';

/** sidecar 迁移硬校验失败时的退出码（spec §2.7 P0-3）→ 主进程映射为 E-D03。 */
export const SIDECAR_EXIT_MIGRATION_MISMATCH = 21;

/** api 的 `/healthz` 里用来辨认「占用者也是 SkillRouter」的标识。 */
export const API_SERVICE_TAG = 'skillrouter-api';

/** `desktop.json` 的 schema 版本。 */
export const DESKTOP_CONFIG_SCHEMA_VERSION = 1;
export const DESKTOP_CONFIG_FILENAME = 'desktop.json';

/** 冒烟报告里的固定文件名。 */
export const SMOKE_REPORT_FILENAME = 'smoke-report.json';

/**
 * IPC 通道名（spec §4.2）——`ipc.ts`（主进程）与 `preload.ts`（渲染进程）的**唯一**真相源。
 *
 * 两处各写一份字面量的后果是「改了主进程忘了 preload」→ invoke 永远 pending，
 * 而沙箱 preload 无法 import `ipc.ts`（那会把 `ipcMain` 拖进渲染侧），所以常量只能放这里。
 */
export const IPC_CHANNELS = {
  version: 'desktop:version',
  status: 'desktop:status',
  openDir: 'desktop:open-dir',
  restart: 'desktop:restart',
} as const;
