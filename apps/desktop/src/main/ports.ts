/**
 * 端口占用探测、归属查询、顺序扫描，以及**端口契约校验**（spec §2.3 / §2.3.1 / §2.3.2）。
 *
 * 为什么这么啰嗦：Next 的 `rewrites()` 在 `next build` 时把 `/api` 目标烧进
 * `routes-manifest.json`，运行时改不了。api 端口一旦漂移，表现是「页面能开、`/api` 全 502」
 * —— 典型假成功。因此 api 端口在构建期、运行期、出包校验三处都要对得上。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

import { API_SERVICE_TAG, ERROR, PORT_SCAN_SPAN } from '../shared/constants';
import type { DesktopError } from '../shared/types';

/** 探针结果（只依赖最小字段，便于单测注入）。 */
export interface ProbeResult {
  status: number;
  body: string;
}

export type Probe = (url: string) => Promise<ProbeResult>;

export interface PortOwner {
  pid: number;
  image: string | null;
}

export type ApiPortVerdict =
  | { kind: 'free' }
  | { kind: 'skillrouter'; service: string; owner: PortOwner | null }
  | { kind: 'foreign'; owner: PortOwner | null };

/** 端口契约三方比对结果。 */
export type PortContractVerdict =
  | { ok: true; action: 'none'; port: number }
  | { ok: true; action: 'repair'; port: number; configPort: number; manifestPort: number }
  | { ok: false; action: 'error'; code: string; detail: string };

export function isPortFree(port: number, host = '127.0.0.1'): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once('error', () => resolve(false));
    server.once('listening', () => {
      server.close(() => resolve(true));
    });
    server.listen(port, host);
  });
}

/** 从 `netstat -ano` 里取占用该端口的 PID，再用 `tasklist` 换成镜像名（Windows）。 */
export function findOwner(port: number): PortOwner | null {
  let pid: number | null = null;
  try {
    const out = execFileSync('netstat', ['-ano', '-p', 'TCP'], { encoding: 'utf8', windowsHide: true });
    for (const line of out.split(/\r?\n/)) {
      const cols = line.trim().split(/\s+/);
      // Proto  Local  Foreign  State  PID
      if (cols.length < 5) continue;
      const local = cols[1] ?? '';
      const state = cols[3] ?? '';
      if (state.toUpperCase() !== 'LISTENING') continue;
      if (!local.endsWith(`:${port}`)) continue;
      const candidate = Number.parseInt(cols[4] ?? '', 10);
      if (Number.isInteger(candidate) && candidate > 0) {
        pid = candidate;
        break;
      }
    }
  } catch {
    return null;
  }
  if (pid === null) return null;

  let image: string | null = null;
  try {
    const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], {
      encoding: 'utf8',
      windowsHide: true,
    });
    const first = out.split(/\r?\n/).find((l) => l.trim().length > 0) ?? '';
    const match = /^"([^"]+)"/.exec(first.trim());
    if (match) image = match[1] ?? null;
  } catch {
    image = null;
  }
  return { pid, image };
}

/**
 * 判断 api 端口的状态（spec §2.3.2 第 2/3 条）：
 * - 空闲 → `free`；
 * - 占用者 `/healthz` 返回 `service === 'skillrouter-api'` → `skillrouter`（→ `W-D06`）；
 * - 其它 → `foreign`（→ `E-D01`）。
 */
export async function classifyApiPort(port: number, probe: Probe, host = '127.0.0.1'): Promise<ApiPortVerdict> {
  if (await isPortFree(port, host)) return { kind: 'free' };

  const owner = findOwnerSafely(port);
  try {
    const res = await probe(`http://${host}:${port}/healthz`);
    if (res.status === 200) {
      try {
        const body = JSON.parse(res.body) as Record<string, unknown>;
        if (body.service === API_SERVICE_TAG) {
          return { kind: 'skillrouter', service: String(body.service), owner };
        }
      } catch {
        /* 非 JSON：按外部进程处理 */
      }
    }
  } catch {
    /* 连不上但端口被占（半开连接 / 非 HTTP）：按外部进程处理 */
  }
  return { kind: 'foreign', owner };
}

function findOwnerSafely(port: number): PortOwner | null {
  try {
    return findOwner(port);
  } catch {
    return null;
  }
}

/** 从 `start` 起顺序试 `span` 个端口，返回第一个可用的。 */
export async function scanPorts(start: number, span = PORT_SCAN_SPAN, host = '127.0.0.1'): Promise<number> {
  for (let port = start; port < start + span; port += 1) {
    if (await isPortFree(port, host)) return port;
  }
  throw new Error(`在 ${start}~${start + span - 1} 之间找不到可用端口`);
}

/** 把 `routes-manifest.json` 的 rewrites 归一成规则组列表（Next 15 是对象，早期是数组）。 */
function rewriteGroups(webRoot: string): unknown[][] {
  const manifest = path.join(webRoot, '.next', 'routes-manifest.json');
  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  } catch {
    return [];
  }
  const rewrites = (parsed as { rewrites?: unknown }).rewrites;
  if (Array.isArray(rewrites)) return rewrites.filter((g): g is unknown[] => Array.isArray(g));
  const r = rewrites as { beforeFiles?: unknown; afterFiles?: unknown; fallback?: unknown } | undefined;
  const groups: unknown[][] = [];
  for (const candidate of [r?.beforeFiles, r?.afterFiles, r?.fallback]) {
    if (Array.isArray(candidate)) groups.push(candidate);
  }
  return groups;
}

/** 从 `routes-manifest.json` 的 `/api/:path*` rewrite 目标里取端口；取不到返回 null。 */
export function readManifestApiPort(webRoot: string): number | null {
  for (const group of rewriteGroups(webRoot)) {
    for (const rule of group) {
      if (rule === null || typeof rule !== 'object') continue;
      const r = rule as { source?: unknown; destination?: unknown };
      if (r.source !== '/api/:path*') continue;
      if (typeof r.destination !== 'string') continue;
      const match = /^https?:\/\/[^/:]+:(\d+)\//.exec(r.destination);
      if (match?.[1]) return Number.parseInt(match[1], 10);
    }
  }
  return null;
}

/** 从任意 rewrite 目标里取端口（供 `/healthz` / 诊断用）。 */
export function readManifestPortFor(webRoot: string, source: string): number | null {
  for (const group of rewriteGroups(webRoot)) {
    for (const rule of group) {
      if (rule === null || typeof rule !== 'object') continue;
      const r = rule as { source?: unknown; destination?: unknown };
      if (r.source !== source || typeof r.destination !== 'string') continue;
      const match = /^https?:\/\/[^/:]+:(\d+)\//.exec(r.destination);
      if (match?.[1]) return Number.parseInt(match[1], 10);
    }
  }
  return null;
}

/**
 * 端口契约三方比对（spec §2.3.1）：`desktop.json.apiPort`、manifest 烧死端口、`--port` 实际值。
 *
 * 语义：
 * - 三者全等 → `none`；
 * - manifest 端口存在且与 configPort 不同 → `repair`（以构建期产物为准，见 §2.3.1 第 3 条）；
 * - manifest 读不到 → 只有当 configPort 与 sidecarPort 一致时才 `none`（`--dev` 无产物）；
 *   否则 `error`（`E-D07`）。
 */
export function checkPortContract(input: {
  configPort: number;
  manifestPort: number | null;
  sidecarPort: number;
}): PortContractVerdict {
  const { configPort, manifestPort, sidecarPort } = input;

  if (manifestPort === null) {
    if (configPort === sidecarPort) return { ok: true, action: 'none', port: configPort };
    return {
      ok: false,
      action: 'error',
      code: ERROR.PORT_CONTRACT_MISMATCH,
      detail: `读不到 routes-manifest.json 的 /api 目标，且 desktop.json.apiPort=${configPort} ≠ --port=${sidecarPort}`,
    };
  }

  if (manifestPort !== sidecarPort) {
    return {
      ok: false,
      action: 'error',
      code: ERROR.PORT_CONTRACT_MISMATCH,
      detail: `routes-manifest.json /api 目标=${manifestPort}，但 sidecar --port=${sidecarPort}`,
    };
  }

  if (manifestPort !== configPort) {
    return {
      ok: true,
      action: 'repair',
      port: manifestPort,
      configPort,
      manifestPort,
    };
  }

  return { ok: true, action: 'none', port: manifestPort };
}

/** 把 `E-D01` / `W-D06` 的处置文案拼出来（错误窗口 + 日志共用）。 */
export function describePortConflict(port: number, verdict: ApiPortVerdict): { code: string; detail: string } {
  if (verdict.kind === 'skillrouter') {
    return {
      code: ERROR.INSTANCE_RUNNING,
      detail: `检测到另一个 SkillRouter 正在使用端口 ${port}（PID ${verdict.owner?.pid ?? 'unknown'}，镜像 ${verdict.owner?.image ?? 'unknown'}）`,
    };
  }
  const owner = verdict.kind === 'foreign' ? verdict.owner : null;
  return {
    code: ERROR.PORT_IN_USE,
    detail: `端口 ${port} 被 PID ${owner?.pid ?? 'unknown'}（${owner?.image ?? 'unknown'}）占用`,
  };
}

/** 供错误窗口使用的类型别名（避免调用方再 import 一次 types）。 */
export type DesktopErrorLike = DesktopError;
