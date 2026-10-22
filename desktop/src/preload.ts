/**
 * Preload bridge.
 *
 * The renderer is untrusted from the shell's point of view — it renders content
 * that came from providers and external repositories. It therefore gets no
 * `ipcRenderer`, no `require`, no file access: only this small, typed surface.
 *
 * `window.skillRouterDesktop.isDesktop` is what the web UI checks to decide
 * whether to show desktop-only affordances (data folder, restart API).
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface DesktopInfo {
  appVersion: string;
  backendOrigin: string;
  sidecarPort: number;
  dataDir: string;
  logsDir: string;
  platform: string;
  arch: string;
  packaged: boolean;
  enginePackage: string;
  engineVersion: string;
  adapterVersion: string;
}

export interface DesktopHealth {
  ok: boolean;
  backendReady: boolean;
  sidecarReady: boolean;
  backendPort: number;
  sidecarPort: number;
  detail: string;
}

export interface DesktopSettings {
  closeToTray: boolean;
  launchAtLogin: boolean;
  startAgentEngine: boolean;
}

const api = {
  isDesktop: true as const,
  info: (): Promise<DesktopInfo> => ipcRenderer.invoke('desktop:info'),
  health: (): Promise<DesktopHealth> => ipcRenderer.invoke('desktop:health'),
  openDataFolder: (): Promise<{ ok: boolean; path: string }> => ipcRenderer.invoke('desktop:open-data-folder'),
  openLogsFolder: (): Promise<{ ok: boolean; path: string }> => ipcRenderer.invoke('desktop:open-logs-folder'),
  restartBackend: (): Promise<DesktopHealth> => ipcRenderer.invoke('desktop:restart-backend'),
  getSettings: (): Promise<DesktopSettings> => ipcRenderer.invoke('desktop:get-settings'),
  setSettings: (patch: Partial<DesktopSettings>): Promise<DesktopSettings> =>
    ipcRenderer.invoke('desktop:set-settings', patch),
  /** Subscribe to pushed health updates; returns an unsubscribe function. */
  onStatus: (listener: (health: DesktopHealth) => void): (() => void) => {
    const handler = (_event: unknown, payload: DesktopHealth): void => listener(payload);
    ipcRenderer.on('desktop:status', handler);
    return () => ipcRenderer.removeListener('desktop:status', handler);
  },
};

contextBridge.exposeInMainWorld('skillRouterDesktop', api);
