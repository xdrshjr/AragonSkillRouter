/**
 * spec §6.1 `config.test.ts`：`desktop.json` 创建 / 损坏恢复 / 版本迁移 / 原子写。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  defaultDesktopConfig,
  desktopConfigPath,
  loadDesktopConfig,
  recordPorts,
  repairApiPort,
  saveDesktopConfig,
  writeJsonAtomic,
} from '../src/main/config';
import { DEFAULT_API_PORT, DESKTOP_CONFIG_SCHEMA_VERSION } from '../src/shared/constants';

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sr-desktop-cfg-'));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

describe('desktop.json', () => {
  it('首次调用创建配置并生成 ≥32 字符的 gatewaySecret', () => {
    const cfg = loadDesktopConfig(dir);
    expect(fs.existsSync(desktopConfigPath(dir))).toBe(true);
    expect(cfg.gatewaySecret.length).toBeGreaterThanOrEqual(32);
    expect(cfg.apiPort).toBe(DEFAULT_API_PORT);
    expect(cfg.schemaVersion).toBe(DESKTOP_CONFIG_SCHEMA_VERSION);
    // 二次调用读到同一份，不会重新生成密钥
    expect(loadDesktopConfig(dir).gatewaySecret).toBe(cfg.gatewaySecret);
  });

  it('损坏的 JSON 被备份并用默认值重建，不抛异常', () => {
    const file = desktopConfigPath(dir);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(file, '{ not json', 'utf8');

    const cfg = loadDesktopConfig(dir);
    expect(cfg.apiPort).toBe(DEFAULT_API_PORT);
    const backups = fs.readdirSync(dir).filter((f) => f.includes('corrupt-'));
    expect(backups).toHaveLength(1);
    // 重建后的文件是可解析的 JSON
    expect(() => JSON.parse(fs.readFileSync(file, 'utf8'))).not.toThrow();
  });

  it('原子写不留下 .tmp 半文件', () => {
    const file = path.join(dir, 'sample.json');
    writeJsonAtomic(file, { a: 1 });
    expect(fs.existsSync(file)).toBe(true);
    expect(fs.existsSync(`${file}.tmp`)).toBe(false);
    expect(JSON.parse(fs.readFileSync(file, 'utf8'))).toEqual({ a: 1 });
  });

  it('apiPort 只能由端口契约校正改写，普通保存不会被外部篡改', () => {
    const cfg = loadDesktopConfig(dir);
    // 运行时上移只写回 web/gateway 端口，apiPort 保持不变
    const afterPorts = recordPorts(dir, cfg, { webPort: 18111, gatewayPort: 18112 });
    expect(afterPorts.apiPort).toBe(DEFAULT_API_PORT);
    expect(loadDesktopConfig(dir).apiPort).toBe(DEFAULT_API_PORT);

    const logs: string[] = [];
    const repaired = repairApiPort(dir, afterPorts, 18100, (m) => logs.push(m));
    expect(repaired.apiPort).toBe(18100);
    expect(logs.join('\n')).toContain('event=port_contract_repair');
  });

  it('schemaVersion 迁移幂等', () => {
    const cfg = defaultDesktopConfig();
    const old = { ...cfg, schemaVersion: 0 };
    const logs: string[] = [];
    saveDesktopConfig(dir, old);
    const migrated = loadDesktopConfig(dir, (m) => logs.push(m));
    expect(migrated.schemaVersion).toBe(DESKTOP_CONFIG_SCHEMA_VERSION);
    const again = loadDesktopConfig(dir, (m) => logs.push(m));
    expect(again.schemaVersion).toBe(DESKTOP_CONFIG_SCHEMA_VERSION);
    // 第二次读不再触发写回
    expect(logs.filter((l) => l.includes('config_repaired'))).toHaveLength(1);
  });
});
