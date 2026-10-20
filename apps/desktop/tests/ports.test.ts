/**
 * spec §6.1 `ports.test.ts`：占用检测、顺序扫描、归属查询与**端口契约**判定。
 * 占用用真实 `net.createServer`，不走 mock —— 只有真实占用才能证明探测有效。
 */
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  checkPortContract,
  classifyApiPort,
  describePortConflict,
  findOwner,
  isPortFree,
  readManifestApiPort,
  scanPorts,
} from '../src/main/ports';
import { API_SERVICE_TAG, DEFAULT_API_PORT, ERROR } from '../src/shared/constants';

const servers: net.Server[] = [];

function occupy(port: number): Promise<net.Server> {
  return new Promise((resolve, reject) => {
    const server = net.createServer(() => {});
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      servers.push(server);
      resolve(server);
    });
  });
}

afterEach(async () => {
  await Promise.all(
    servers.splice(0).map((s) => new Promise<void>((resolve) => s.close(() => resolve()))),
  );
});

describe('isPortFree', () => {
  it('真实占用后返回 false，释放后返回 true', async () => {
    const server = await occupy(0);
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('no port');
    expect(await isPortFree(addr.port)).toBe(false);
    await new Promise<void>((resolve) => server.close(() => resolve()));
    servers.splice(servers.indexOf(server), 1);
    expect(await isPortFree(addr.port)).toBe(true);
  });
});

describe('scanPorts', () => {
  it('返回起点之后（含起点）的第一个可用端口', async () => {
    const base = 19100;
    await occupy(base);
    const found = await scanPorts(base, 5);
    expect(found).toBe(base + 1);
  });
});

describe('findOwner', () => {
  it('能给出占用端口的 PID（Windows）', async () => {
    const server = await occupy(0);
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('no port');
    const owner = findOwner(addr.port);
    expect(owner).not.toBeNull();
    expect(owner?.pid).toBeGreaterThan(0);
  });
});

describe('classifyApiPort', () => {
  it('端口被外部进程占用 → E-D01', async () => {
    const server = await occupy(0);
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('no port');
    const probe = async (): Promise<{ status: number; body: string }> => ({ status: 200, body: '{"hello":"world"}' });
    const verdict = await classifyApiPort(addr.port, probe);
    expect(verdict.kind).toBe('foreign');
    expect(describePortConflict(addr.port, verdict).code).toBe(ERROR.PORT_IN_USE);
  });

  it('占用者是 SkillRouter（/healthz service=skillrouter-api）→ W-D06 而不是 E-D01', async () => {
    const server = await occupy(0);
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('no port');
    const probe = async (): Promise<{ status: number; body: string }> => ({
      status: 200,
      body: JSON.stringify({ service: API_SERVICE_TAG, ok: true }),
    });
    const verdict = await classifyApiPort(addr.port, probe);
    expect(verdict.kind).toBe('skillrouter');
    expect(describePortConflict(addr.port, verdict).code).toBe(ERROR.INSTANCE_RUNNING);
  });

  it('端口空闲 → free', async () => {
    const probe = async (): Promise<{ status: number; body: string }> => ({ status: 0, body: '' });
    expect((await classifyApiPort(18999, probe)).kind).toBe('free');
  });
});

describe('checkPortContract', () => {
  it('三方一致 → none', () => {
    const verdict = checkPortContract({ configPort: DEFAULT_API_PORT, manifestPort: DEFAULT_API_PORT, sidecarPort: DEFAULT_API_PORT });
    expect(verdict).toEqual({ ok: true, action: 'none', port: DEFAULT_API_PORT });
  });

  it('desktop.json 与构建期产物不一致 → repair（以构建期为准）', () => {
    const verdict = checkPortContract({ configPort: 18200, manifestPort: DEFAULT_API_PORT, sidecarPort: DEFAULT_API_PORT });
    expect(verdict.ok).toBe(true);
    expect(verdict.action).toBe('repair');
    if (verdict.action === 'repair') {
      expect(verdict.port).toBe(DEFAULT_API_PORT);
      expect(verdict.configPort).toBe(18200);
    }
  });

  it('sidecar --port 与构建期产物不一致 → E-D07（不允许带着不一致启动）', () => {
    const verdict = checkPortContract({ configPort: DEFAULT_API_PORT, manifestPort: DEFAULT_API_PORT, sidecarPort: 18200 });
    expect(verdict.ok).toBe(false);
    expect(verdict.action).toBe('error');
    if (verdict.action === 'error') expect(verdict.code).toBe(ERROR.PORT_CONTRACT_MISMATCH);
  });

  it('读不到 manifest 且端口不一致 → E-D07', () => {
    const verdict = checkPortContract({ configPort: 18200, manifestPort: null, sidecarPort: DEFAULT_API_PORT });
    expect(verdict.action).toBe('error');
  });
});

describe('readManifestApiPort', () => {
  it('从 routes-manifest.json 的 /api/:path* 目标里取端口', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sr-desktop-manifest-'));
    fs.mkdirSync(path.join(dir, '.next'), { recursive: true });
    fs.writeFileSync(
      path.join(dir, '.next', 'routes-manifest.json'),
      JSON.stringify({
        rewrites: [
          [
            { source: '/healthz', destination: 'http://127.0.0.1:18100/healthz' },
            { source: '/api/:path*', destination: 'http://127.0.0.1:18100/api/:path*' },
          ],
        ],
      }),
      'utf8',
    );
    expect(readManifestApiPort(dir)).toBe(18100);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('Next 15 的对象形态 rewrites（{beforeFiles, afterFiles, fallback}）也能取到端口（I-4）', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sr-desktop-manifest-obj-'));
    fs.mkdirSync(path.join(dir, '.next'), { recursive: true });
    fs.writeFileSync(
      path.join(dir, '.next', 'routes-manifest.json'),
      JSON.stringify({
        version: 4,
        rewrites: {
          beforeFiles: [],
          afterFiles: [
            { source: '/healthz', destination: 'http://127.0.0.1:18100/healthz' },
            { source: '/api/:path*', destination: 'http://127.0.0.1:18100/api/:path*' },
          ],
          fallback: [],
        },
      }),
      'utf8',
    );
    expect(readManifestApiPort(dir)).toBe(18100);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('manifest 缺失时返回 null', () => {
    expect(readManifestApiPort(path.join(os.tmpdir(), 'sr-nope'))).toBeNull();
  });
});
