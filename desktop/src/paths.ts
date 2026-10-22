/**
 * Where the desktop app keeps its files.
 *
 * The packaged app must never write inside its own installation directory: an
 * installer upgrade replaces those files and Program Files is usually read-only
 * for the user. All mutable state therefore lives under Electron's per-user
 * `appData` directory:
 *
 *   %APPDATA%\Skill Router\
 *     data\            database, keyring, uploads, artifacts, backups
 *     logs\            main + backend + sidecar + web logs
 *     runtime.json     ports, sidecar token, build marker
 *     desktop-settings.json
 *
 * `SR_DESKTOP_DATA_DIR` overrides the root so an automated test cannot touch the
 * real user's database.
 */

import { app } from 'electron';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export interface DesktopPaths {
  root: string;
  data: string;
  logs: string;
  runtimeFile: string;
  runtimeDir: string;
  backendDir: string;
  backendExe: string;
  sidecarDir: string;
  sidecarEntry: string;
  webDir: string;
  webEntry: string;
}

function ensureDir(target: string): string {
  if (!existsSync(target)) mkdirSync(target, { recursive: true });
  return target;
}

export function resolveRoot(): string {
  const override = process.env.SR_DESKTOP_DATA_DIR?.trim();
  if (override) return ensureDir(path.resolve(override));
  return ensureDir(path.join(app.getPath('appData'), 'Skill Router'));
}

/**
 * Where the bundled runtimes live.
 *
 * electron-builder copies `resources/runtime/**` next to the asar
 * (`process.resourcesPath`). A development run reads them from
 * `desktop/resources/runtime`, so `npm start` in `desktop/` exercises the same
 * layout the installer produces.
 */
export function resolveRuntimeDir(): string {
  if (app.isPackaged) return path.join(process.resourcesPath, 'runtime');
  return path.join(app.getAppPath(), 'resources', 'runtime');
}

export function getPaths(): DesktopPaths {
  const root = resolveRoot();
  const data = ensureDir(path.join(root, 'data'));
  const logs = ensureDir(path.join(root, 'logs'));
  const runtimeDir = resolveRuntimeDir();
  const backendDir = path.join(runtimeDir, 'backend');
  const sidecarDir = path.join(runtimeDir, 'sidecar');
  const webDir = path.join(runtimeDir, 'web');
  const exe = process.platform === 'win32' ? 'skillrouter-backend.exe' : 'skillrouter-backend';
  return {
    root,
    data,
    logs,
    runtimeFile: path.join(root, 'runtime.json'),
    runtimeDir,
    backendDir,
    backendExe: path.join(backendDir, exe),
    sidecarDir,
    sidecarEntry: path.join(sidecarDir, 'main.js'),
    webDir,
    webEntry: path.join(webDir, 'server.js'),
  };
}

export interface RuntimeState {
  backendPort: number;
  sidecarPort: number;
  webPort: number;
  shellPort: number;
  sidecarToken: string;
  appVersion?: string;
  lastStartedAt?: string;
}

const EMPTY_STATE: RuntimeState = { backendPort: 0, sidecarPort: 0, webPort: 0, shellPort: 0, sidecarToken: '' };

export function readRuntimeState(paths: DesktopPaths): RuntimeState {
  try {
    const parsed = JSON.parse(readFileSync(paths.runtimeFile, 'utf8')) as Partial<RuntimeState>;
    return {
      ...EMPTY_STATE,
      ...parsed,
      sidecarToken: typeof parsed.sidecarToken === 'string' ? parsed.sidecarToken : '',
    };
  } catch {
    return { ...EMPTY_STATE };
  }
}

export function writeRuntimeState(paths: DesktopPaths, state: RuntimeState): void {
  writeFileSync(paths.runtimeFile, JSON.stringify(state, null, 2), { encoding: 'utf8', mode: 0o600 });
}

/**
 * Per-machine preferences. Deliberately small: anything that belongs to the
 * product itself lives in the app's Settings page, behind its own audit trail.
 */
export interface DesktopSettings {
  closeToTray: boolean;
  launchAtLogin: boolean;
  startAgentEngine: boolean;
}

const DEFAULT_SETTINGS: DesktopSettings = {
  closeToTray: true,
  launchAtLogin: false,
  startAgentEngine: true,
};

export function readSettings(paths: DesktopPaths): DesktopSettings {
  try {
    const parsed = JSON.parse(readFileSync(path.join(paths.root, 'desktop-settings.json'), 'utf8'));
    return {
      closeToTray: typeof parsed.closeToTray === 'boolean' ? parsed.closeToTray : DEFAULT_SETTINGS.closeToTray,
      launchAtLogin:
        typeof parsed.launchAtLogin === 'boolean' ? parsed.launchAtLogin : DEFAULT_SETTINGS.launchAtLogin,
      startAgentEngine:
        typeof parsed.startAgentEngine === 'boolean' ? parsed.startAgentEngine : DEFAULT_SETTINGS.startAgentEngine,
    };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function writeSettings(paths: DesktopPaths, settings: DesktopSettings): void {
  writeFileSync(path.join(paths.root, 'desktop-settings.json'), JSON.stringify(settings, null, 2), 'utf8');
}
