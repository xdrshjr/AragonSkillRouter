/**
 * `desktop.json` 的读取 / 创建 / 版本迁移 / 原子写（spec §5.1）。
 *
 * 规则：
 * - 写入必须原子：先写 `desktop.json.tmp` 再 `renameSync`；
 * - 损坏的 JSON → 备份为 `desktop.json.corrupt-<ts>` 后用默认值重建，**不阻断启动**；
 * - `apiPort` 是构建期烧死值，只允许被端口契约校正逻辑改写（见 ports.ts 的 `repair` 分支）。
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import {
  DEFAULT_API_PORT,
  DEFAULT_GATEWAY_PORT,
  DEFAULT_WEB_PORT,
  DESKTOP_CONFIG_FILENAME,
  DESKTOP_CONFIG_SCHEMA_VERSION,
  DESKTOP_ENV,
  ENGINE_PIN,
} from '../shared/constants';
import type { DesktopConfig } from '../shared/types';

export type LogFn = (message: string) => void;

/** 48 字符 base64url 随机串（36 字节 → 48 base64 字符，不含 `=`）。 */
export function generateGatewaySecret(): string {
  return crypto.randomBytes(36).toString('base64url');
}

export function desktopConfigPath(userDataDir: string): string {
  return path.join(userDataDir, DESKTOP_CONFIG_FILENAME);
}

export function defaultDesktopConfig(now: Date = new Date()): DesktopConfig {
  return {
    schemaVersion: DESKTOP_CONFIG_SCHEMA_VERSION,
    apiPort: DEFAULT_API_PORT,
    gatewayPort: DEFAULT_GATEWAY_PORT,
    webPort: DEFAULT_WEB_PORT,
    gatewaySecret: generateGatewaySecret(),
    env: DESKTOP_ENV,
    payloadPolicy: 'redacted_preview',
    enginePolicy: 'notify',
    enginePin: ENGINE_PIN,
    adminInitialized: false,
    firstRunAt: now.toISOString(),
    lastVersion: '0.1.0',
  };
}

/** 原子写：tmp → rename。Windows 上 rename 覆盖同名文件是允许的。 */
export function writeJsonAtomic(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, file);
}

/** 把未知 JSON 归一成合法的 `DesktopConfig`（缺字段补默认值，类型错回落默认值）。 */
export function normalizeDesktopConfig(raw: unknown, fallback: DesktopConfig): DesktopConfig {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return fallback;
  const r = raw as Record<string, unknown>;
  const num = (key: keyof DesktopConfig, def: number): number => {
    const v = r[key];
    return typeof v === 'number' && Number.isInteger(v) && v > 0 && v < 65536 ? v : def;
  };
  const str = (key: keyof DesktopConfig, def: string): string => {
    const v = r[key];
    return typeof v === 'string' && v.length > 0 ? v : def;
  };
  const bool = (key: keyof DesktopConfig, def: boolean): boolean => {
    const v = r[key];
    return typeof v === 'boolean' ? v : def;
  };

  const secret = str('gatewaySecret', fallback.gatewaySecret);
  return {
    schemaVersion: typeof r.schemaVersion === 'number' ? r.schemaVersion : fallback.schemaVersion,
    apiPort: num('apiPort', fallback.apiPort),
    gatewayPort: num('gatewayPort', fallback.gatewayPort),
    webPort: num('webPort', fallback.webPort),
    // 太短的密钥视为损坏（spec §6.1：首次创建必须 ≥32 字符）。
    gatewaySecret: secret.length >= 32 ? secret : generateGatewaySecret(),
    env: str('env', fallback.env),
    payloadPolicy: str('payloadPolicy', fallback.payloadPolicy),
    enginePolicy: str('enginePolicy', fallback.enginePolicy),
    enginePin: str('enginePin', fallback.enginePin),
    adminInitialized: bool('adminInitialized', fallback.adminInitialized),
    firstRunAt: str('firstRunAt', fallback.firstRunAt),
    lastVersion: str('lastVersion', fallback.lastVersion),
  };
}

/** schema 迁移（1 → 2 的占位已留）。就地猜测字段是不允许的。 */
export function migrateDesktopConfig(cfg: DesktopConfig): DesktopConfig {
  let next = cfg;
  if (next.schemaVersion < DESKTOP_CONFIG_SCHEMA_VERSION) {
    next = { ...next, schemaVersion: DESKTOP_CONFIG_SCHEMA_VERSION };
  }
  return next;
}

/**
 * 读/建 `desktop.json`。永远返回可用配置（损坏时备份 + 重建），不抛异常。
 */
export function loadDesktopConfig(userDataDir: string, log: LogFn = () => {}): DesktopConfig {
  const file = desktopConfigPath(userDataDir);
  const fallback = defaultDesktopConfig();

  if (!fs.existsSync(file)) {
    writeJsonAtomic(file, fallback);
    log(`event=config_created path=${file}`);
    return fallback;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    const backup = `${file}.corrupt-${Date.now()}`;
    try {
      fs.renameSync(file, backup);
    } catch {
      /* 备份失败也要继续启动 */
    }
    writeJsonAtomic(file, fallback);
    log(`event=config_corrupt_backup path=${file} backup=${backup} err=${String(err)}`);
    return fallback;
  }

  const normalized = migrateDesktopConfig(normalizeDesktopConfig(parsed, fallback));
  if (JSON.stringify(normalized) !== JSON.stringify(parsed)) {
    writeJsonAtomic(file, normalized);
    log(`event=config_repaired path=${file}`);
  }
  return normalized;
}

export function saveDesktopConfig(userDataDir: string, cfg: DesktopConfig): DesktopConfig {
  const file = desktopConfigPath(userDataDir);
  writeJsonAtomic(file, cfg);
  return cfg;
}

/** 端口被运行时上移后写回（apiPort 不在此列：它只由端口契约校正改写）。 */
export function recordPorts(
  userDataDir: string,
  cfg: DesktopConfig,
  ports: { gatewayPort?: number; webPort?: number },
): DesktopConfig {
  const next: DesktopConfig = {
    ...cfg,
    gatewayPort: ports.gatewayPort ?? cfg.gatewayPort,
    webPort: ports.webPort ?? cfg.webPort,
  };
  return saveDesktopConfig(userDataDir, next);
}

/** 端口契约校正（spec §2.3.1 第 3 条）：以构建期产物为准改写 `desktop.json.apiPort`。 */
export function repairApiPort(
  userDataDir: string,
  cfg: DesktopConfig,
  manifestPort: number,
  log: LogFn = () => {},
): DesktopConfig {
  const next: DesktopConfig = { ...cfg, apiPort: manifestPort };
  saveDesktopConfig(userDataDir, next);
  log(`event=port_contract_repair from=${cfg.apiPort} to=${manifestPort}`);
  return next;
}
