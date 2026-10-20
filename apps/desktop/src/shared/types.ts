/** 桌面端共享类型（spec §4.2 / §4.4 / §5.1）。 */

export type ServiceName = 'api' | 'worker' | 'gateway' | 'web';

/** 子进程环境角色；`bootstrap` 与 `api` 共用同一份环境（bootstrap 只用它跑初始化）。 */
export type EnvRole = 'api' | 'bootstrap' | 'worker' | 'gateway' | 'web';

/** spec §2.5 的状态机状态（与表格逐项一致）。 */
export type StartupState =
  | 'idle'
  | 'preparing'
  | 'porting'
  | 'bootstrapping'
  | 'starting_api'
  | 'starting_worker'
  | 'starting_gateway'
  | 'starting_web'
  | 'ready'
  | 'failed';

export interface DesktopConfig {
  schemaVersion: number;
  /** 构建期烧死：必须与 web 的 routes-manifest.json 一致，UI 不可改。 */
  apiPort: number;
  /** 运行时可能上移，写回实际值。 */
  gatewayPort: number;
  /** 运行时可能上移，写回实际值。 */
  webPort: number;
  /** 48 字符 base64url；api 与网关共用（X-Internal-Token）。 */
  gatewaySecret: string;
  env: string;
  payloadPolicy: string;
  enginePolicy: string;
  enginePin: string;
  adminInitialized: boolean;
  firstRunAt: string;
  lastVersion: string;
}

export type ServiceState = 'down' | 'starting' | 'up' | 'degraded';

export interface ServiceStatus {
  state: StartupState;
  api: ServiceState;
  gateway: ServiceState;
  worker: ServiceState;
  web: ServiceState;
  degraded: boolean;
  errorCode: string | null;
  ports: { api: number | null; gateway: number | null; web: number | null };
}

export interface SmokeCheck {
  name: string;
  ok: boolean;
  [key: string]: unknown;
}

export interface SmokeReport {
  ok: boolean;
  appVersion: string;
  electron: string;
  startedAt: string;
  durationMs: number;
  ports: { api: number | null; gateway: number | null; web: number | null };
  dataDir: string;
  resources: { sidecar: string; web: string; gateway: string };
  externalRuntime: {
    pythonOnPath: boolean;
    nodeOnPath: boolean;
    pathUsed: string;
  };
  checks: SmokeCheck[];
  shellChecks?: Record<string, unknown>;
}

/** 冒烟模式下的运行参数（`--smoke-test` / `--smoke-out`）。 */
export interface SmokeOptions {
  enabled: boolean;
  outPath: string | null;
}

/** 启动参数（spec §4.1）。 */
export interface CliOptions {
  smokeTest: boolean;
  smokeOut: string | null;
  dataDir: string | null;
  dev: boolean;
  verbose: boolean;
  version: boolean;
}

/** 抛给状态机的桌面端错误：`code` 即错误码字符串，`detail` 进日志/错误窗口。 */
export class DesktopError extends Error {
  public readonly code: string;
  public readonly detail: string;

  constructor(code: string, detail = '') {
    super(detail ? `${code}: ${detail}` : code);
    this.name = 'DesktopError';
    this.code = code;
    this.detail = detail;
  }
}
