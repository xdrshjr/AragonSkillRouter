/**
 * 四个服务的描述（spec §2.1 / §2.9）：可执行文件、参数、就绪探针、超时、致命性。
 *
 * 顺序不能变：bootstrap → api → worker → gateway → web
 * （网关读 `agent-policy.json`，它由 bootstrap 写出，§2.9 第 4 条）。
 */
import {
  API_READY_TIMEOUT_MS,
  GATEWAY_READY_TIMEOUT_MS,
  WEB_READY_TIMEOUT_MS,
} from '../shared/constants';
import type { ServiceName } from '../shared/types';
import type { SpawnSpec } from './supervisor';
import type { DesktopPaths } from './paths';
import { composeEnv } from './env';
import type { ComposeEnvOptions } from './env';

export interface ServicePorts {
  api: number;
  web: number;
  gateway: number;
}

export interface ServiceSpec extends SpawnSpec {
  /** 就绪探针；`worker` 没有探针（只记录启动）。 */
  probeUrl: string | null;
  timeoutMs: number;
  /** 致命性：`api` / `web` 挂掉要弹窗，`gateway` / `worker` 只降级。 */
  fatal: boolean;
}

export interface ServiceContext {
  paths: DesktopPaths;
  ports: ServicePorts;
  gatewaySecret: string;
  electronExecPath: string;
}

function envOptions(ctx: ServiceContext): ComposeEnvOptions {
  return {
    dataDir: ctx.paths.data,
    dbPath: ctx.paths.dbPath,
    apiPort: ctx.ports.api,
    webPort: ctx.ports.web,
    gatewayPort: ctx.ports.gateway,
    gatewaySecret: ctx.gatewaySecret,
  };
}

export function buildServiceSpec(name: ServiceName, ctx: ServiceContext): ServiceSpec {
  const envOpts = envOptions(ctx);
  const cwd = ctx.paths.data;

  switch (name) {
    case 'api':
      return {
        name,
        exe: ctx.paths.sidecarExe,
        args: ['--role', 'api', '--port', String(ctx.ports.api), '--data-dir', ctx.paths.data],
        cwd,
        env: composeEnv('api', envOpts),
        probeUrl: `http://127.0.0.1:${ctx.ports.api}/healthz`,
        timeoutMs: API_READY_TIMEOUT_MS,
        fatal: true,
      };
    case 'worker':
      return {
        name,
        exe: ctx.paths.sidecarExe,
        args: ['--role', 'worker', '--data-dir', ctx.paths.data],
        cwd,
        env: composeEnv('worker', envOpts),
        probeUrl: null,
        timeoutMs: 0,
        fatal: false,
      };
    case 'gateway':
      return {
        name,
        exe: ctx.electronExecPath,
        args: [ctx.paths.gatewayEntry],
        cwd: ctx.paths.gatewayRoot,
        env: composeEnv('gateway', envOpts),
        probeUrl: `http://127.0.0.1:${ctx.ports.gateway}/healthz`,
        timeoutMs: GATEWAY_READY_TIMEOUT_MS,
        fatal: false,
      };
    case 'web':
      return {
        name,
        exe: ctx.electronExecPath,
        args: [ctx.paths.webEntry],
        cwd: ctx.paths.webRoot,
        env: composeEnv('web', envOpts),
        probeUrl: `http://127.0.0.1:${ctx.ports.web}/login`,
        timeoutMs: WEB_READY_TIMEOUT_MS,
        fatal: true,
      };
  }
}

/** bootstrap 的一次性 spawn 描述（阻塞等退出码，不探活）。 */
export function buildBootstrapSpec(ctx: ServiceContext): SpawnSpec {
  return {
    name: 'api',
    exe: ctx.paths.sidecarExe,
    args: ['--role', 'bootstrap', '--data-dir', ctx.paths.data, '--json'],
    cwd: ctx.paths.data,
    env: composeEnv('bootstrap', envOptions(ctx)),
  };
}

/** 启动顺序（也是关停的逆序）。 */
export const BOOT_ORDER: ServiceName[] = ['api', 'worker', 'gateway', 'web'];
export const SHUTDOWN_ORDER: ServiceName[] = ['web', 'gateway', 'worker', 'api'];
