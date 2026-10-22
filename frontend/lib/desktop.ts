/**
 * Desktop shell bridge.
 *
 * The packaged app runs the API locally, so a few facts that are irrelevant on the
 * web (where the services live, how to reach the data folder, whether the local
 * engine is up) become user-facing. They arrive through the preload bridge
 * (`window.skillRouterDesktop`) rather than HTTP, because the shell — not the API
 * — owns those processes.
 *
 * Everything here degrades to "not a desktop build" when the bridge is absent, so
 * the same bundle still runs in a browser untouched.
 */

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
  webReady: boolean;
  sidecarReady: boolean;
  backendPort: number;
  webPort: number;
  sidecarPort: number;
  shellPort: number;
  detail: string;
}

export interface DesktopPreferences {
  closeToTray: boolean;
  launchAtLogin: boolean;
  startAgentEngine: boolean;
}

interface DesktopBridge {
  isDesktop: true;
  info(): Promise<DesktopInfo>;
  health(): Promise<DesktopHealth>;
  openDataFolder(): Promise<{ ok: boolean; path: string; error?: string }>;
  openLogsFolder(): Promise<{ ok: boolean; path: string; error?: string }>;
  restartBackend(): Promise<DesktopHealth | null>;
  getSettings(): Promise<DesktopPreferences>;
  setSettings(patch: Partial<DesktopPreferences>): Promise<DesktopPreferences>;
  onStatus(listener: (health: DesktopHealth) => void): () => void;
}

declare global {
  interface Window {
    skillRouterDesktop?: DesktopBridge;
  }
}

export function desktopBridge(): DesktopBridge | null {
  if (typeof window === 'undefined') return null;
  return window.skillRouterDesktop ?? null;
}

export function isDesktop(): boolean {
  return desktopBridge() !== null;
}

/** Human-readable summary of which local services are up. */
export function describeHealth(health: DesktopHealth | null): string {
  if (!health) return 'Status unavailable.';
  const parts = [
    `API ${health.backendReady ? 'running' : 'down'}`,
    `Interface ${health.webReady ? 'running' : 'down'}`,
    `Agent engine ${health.sidecarReady ? 'running' : 'not running'}`,
  ];
  return parts.join(' · ');
}
