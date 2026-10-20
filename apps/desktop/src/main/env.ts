/**
 * 子进程环境变量合成（spec §2.6）——**唯一真相源**。
 *
 * 两条硬规则：
 * 1. 输入必须是「干净基础环境 + 本文件的表」，先按 `ENV_DENY_PREFIXES` 剔除继承变量；
 * 2. `SKILLROUTER_INTERNAL_URL` 必须同时给 **api 与 gateway**（P0-1：只给 api 会让网关的
 *    平台回调打向开发端口 8000，探针全绿但 Agent 全废）。
 *
 * 本文件是纯函数，不 import electron，可被 vitest 直接覆盖。
 */
import path from 'node:path';

import {
  ADMIN_PASSWORD,
  ADMIN_USER,
  API_SERVICE_TAG,
  DESKTOP_ENV,
  ENGINE_CHANNEL,
  ENGINE_PIN,
  ENGINE_PIN_VERSION,
  ENGINE_TOOLCHAIN,
  ENV_DENY_PREFIXES,
} from '../shared/constants';
import type { EnvRole } from '../shared/types';

export interface ComposeEnvOptions {
  dataDir: string;
  dbPath: string;
  apiPort: number;
  webPort: number;
  gatewayPort: number;
  gatewaySecret: string;
  /** 继承环境（默认 `process.env`）；命中黑名单前缀的键会被剔除。 */
  base?: NodeJS.ProcessEnv;
}

export type ComposedEnv = Record<string, string>;

/** 去掉命中黑名单前缀的继承变量（`PYTHON*` 用前缀匹配，其余同样前缀匹配）。 */
export function stripInherited(base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const clean: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(base)) {
    if (value === undefined) continue;
    if (ENV_DENY_PREFIXES.some((prefix) => key.startsWith(prefix))) continue;
    clean[key] = value;
  }
  return clean;
}

function internalUrl(apiPort: number): string {
  return `http://127.0.0.1:${apiPort}`;
}

/** api 与 worker 共用的 Python 侧环境（api 额外多出 admin / 公开地址 / 网关相关键）。 */
function pythonBase(o: ComposeEnvOptions): ComposedEnv {
  const engineHome = path.join(o.dataDir, 'aragon-home', 'engines');
  return {
    SKILLROUTER_ENV: DESKTOP_ENV,
    SKILLROUTER_DATA_DIR: o.dataDir,
    SKILLROUTER_DB_PATH: o.dbPath,
    // 注意：**不设置** SKILLROUTER_MASTER_KEY —— 缺失时 settings.generate_missing_secrets()
    // 生成 <data>/credentials-master.key，密钥永不进环境变量（spec §2.6 表）。
    SKILLROUTER_MASTER_KEY_ID: 'mk1',
    SKILLROUTER_ENGINE_POLICY: 'notify',
    SKILLROUTER_ENGINE_CHANNEL: ENGINE_CHANNEL,
    SKILLROUTER_ENGINE_PIN: ENGINE_PIN,
    SKILLROUTER_AGENT_ENGINE_HOME_ROOT: engineHome,
    SKILLROUTER_EGRESS_POLICY: 'deny',
    SKILLROUTER_PAYLOAD_POLICY: 'redacted_preview',
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  };
}

function apiEnv(o: ComposeEnvOptions): ComposedEnv {
  return {
    ...pythonBase(o),
    SKILLROUTER_PUBLIC_BASE_URL: `http://127.0.0.1:${o.webPort}`,
    SKILLROUTER_ADMIN_USER: ADMIN_USER,
    SKILLROUTER_ADMIN_PASSWORD: ADMIN_PASSWORD,
    SKILLROUTER_SESSION_TTL_MINUTES: '480',
    // P0-1：网关读同一个键；api 侧同样注入，保证两侧口径一致。
    SKILLROUTER_INTERNAL_URL: internalUrl(o.apiPort),
    SKILLROUTER_AGENT_GATEWAY_URL: `http://127.0.0.1:${o.gatewayPort}`,
    SKILLROUTER_AGENT_GATEWAY_SECRET: o.gatewaySecret,
    SKILLROUTER_AGENT_GATEWAY_BIND: '127.0.0.1',
    // P1-12：必须显式钉死 gateway —— 继承的 `local` 在 development 下会被接受，
    // 而安装包里没有 npm / git，引擎构建必然失败（且只在冷路径上炸）。
    SKILLROUTER_ENGINE_TOOLCHAIN: ENGINE_TOOLCHAIN,
  };
}

function workerEnv(o: ComposeEnvOptions): ComposedEnv {
  return pythonBase(o);
}

function gatewayEnv(o: ComposeEnvOptions): ComposedEnv {
  return {
    AGENT_GATEWAY_SHARED_SECRET: o.gatewaySecret,
    AGENT_GATEWAY_BIND: '127.0.0.1',
    AGENT_GATEWAY_PORT: String(o.gatewayPort),
    AGENT_GATEWAY_ENGINE_MODE: 'auto',
    AGENT_GATEWAY_LOG_LEVEL: 'info',
    // P0-1：网关的平台回调目标。少了这一行，`sr_*` 工具 / SSE 回传 / 制品校验会打向 8000。
    SKILLROUTER_INTERNAL_URL: internalUrl(o.apiPort),
    SKILLROUTER_AGENT_GATEWAY_SECRET: o.gatewaySecret,
    SKILLROUTER_ENGINE_PIN: ENGINE_PIN,
    AGENT_ENGINE_HOME_ROOT: path.join(o.dataDir, 'aragon-home', 'engines'),
    AGENT_ENGINE_RUNTIME_DIR: path.join(o.dataDir, 'engine-runtime'),
    AGENT_ENGINE_BUILDS_DIR: path.join(o.dataDir, 'engine-builds'),
    AGENT_POLICY_PATH: path.join(o.dataDir, 'agent-policy.json'),
    AGENT_SESSION_DIR: path.join(o.dataDir, 'agent-sessions'),
    ENGINE_PIN: ENGINE_PIN_VERSION,
    NODE_ENV: 'production',
    ELECTRON_RUN_AS_NODE: '1',
  };
}

function webEnv(o: ComposeEnvOptions): ComposedEnv {
  return {
    PORT: String(o.webPort),
    HOSTNAME: '127.0.0.1',
    NEXT_TELEMETRY_DISABLED: '1',
    NODE_ENV: 'production',
    ELECTRON_RUN_AS_NODE: '1',
    // 构建期同名变量烧进 rewrite；运行时值只供日志与诊断（spec §2.6）。
    SKILLROUTER_API_URL: internalUrl(o.apiPort),
  };
}

/**
 * 合成某个角色的完整子进程环境。
 *
 * 返回的是**完整** env（含 `PATH` 等继承项），可以直接传给 `spawn`；黑名单前缀已被剔除。
 * `cwd` 由调用方统一设为 `<dataDir>`（spec §2.6 末段）。
 */
export function composeEnv(role: EnvRole, o: ComposeEnvOptions): ComposedEnv {
  const clean = stripInherited(o.base ?? process.env);
  const layer = role === 'api' || role === 'bootstrap'
    ? apiEnv(o)
    : role === 'worker'
      ? workerEnv(o)
      : role === 'gateway'
        ? gatewayEnv(o)
        : webEnv(o);

  const merged: ComposedEnv = {};
  for (const [key, value] of Object.entries(clean)) {
    if (typeof value === 'string') merged[key] = value;
  }
  return { ...merged, ...layer };
}

/** `/healthz` 里判「占用者也是 SkillRouter」用的标识，供 ports.ts 使用。 */
export const HEALTH_SERVICE_TAG = API_SERVICE_TAG;
