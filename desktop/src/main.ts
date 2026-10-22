/**
 * Electron main process.
 *
 * Lifecycle: single instance → first-run bootstrap → start API, UI server and
 * engine → publish one loopback origin (shell proxy) → show the window only once
 * both API and UI answer → supervise, re-open, and stop everything on quit.
 *
 * The renderer is sandboxed: contextIsolation on, nodeIntegration off, and a
 * preload that exposes only shell facts and three actions. No file system access
 * and no `ipcRenderer` passthrough, because the UI displays content that came
 * from providers and third-party repositories.
 */

import {
  BrowserWindow,
  Menu,
  Tray,
  app,
  dialog,
  ipcMain,
  nativeImage,
  shell,
} from 'electron';
import { randomBytes } from 'node:crypto';
import path from 'node:path';

import log from 'electron-log/main';

import { AppServer } from './app-server';
import {
  DesktopSettings,
  getPaths,
  readRuntimeState,
  readSettings,
  writeRuntimeState,
  writeSettings,
} from './paths';
import { Supervisor, findByPreferredPort } from './supervisor';

const paths = getPaths();
log.transports.file.resolvePathFn = () => path.join(paths.logs, 'main.log');
log.transports.file.level = 'info';
log.transports.console.level = app.isPackaged ? 'warn' : 'info';

const shellToken = randomBytes(32).toString('hex');
let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let server: AppServer | null = null;
let supervisor: Supervisor | null = null;
let settings: DesktopSettings = { closeToTray: true, launchAtLogin: false, startAgentEngine: true };
let quitting = false;
let startupReport: Awaited<ReturnType<Supervisor['startAll']>> | null = null;

const APP_VERSION = app.getVersion();
const ENGINE_PACKAGE = '@aragon-agent/core';
const ENGINE_VERSION = '0.2.50';
const ADAPTER_VERSION = '1.0.0';

const sink = {
  info: (message: string, meta?: unknown) => log.info(message, meta ?? ''),
  warn: (message: string, meta?: unknown) => log.warn(message, meta ?? ''),
  error: (message: string, meta?: unknown) => log.error(message, meta ?? ''),
};

/**
 * Pass through the documented SR_* switches an operator may set.
 *
 * `SR_INITIAL_ADMIN_PASSWORD` is deliberately included: a managed install
 * provisions the first admin non-interactively (`set SR_INITIAL_ADMIN_PASSWORD=…`
 * then launch), and the backend only consumes it while the users table is empty.
 * It is never written to disk by the shell.
 */
function inheritedEnv(): Record<string, string | undefined> {
  const allowed = [
    'SR_OUTBOUND_ALLOWED_HOSTS',
    'SR_OUTBOUND_ALLOW_PRIVATE',
    // The mode is the documented knob; the boolean is kept for explicit overrides.
    'SR_OUTBOUND_FAKE_IP_MODE',
    'SR_OUTBOUND_ALLOW_FAKE_IP',
    'SR_LOG_LEVEL',
    'SR_INITIAL_ADMIN_USERNAME',
    'SR_INITIAL_ADMIN_PASSWORD',
    'SR_ARAGON_UPGRADE_CHANNEL',
    'SR_ARAGON_TRACK_BRANCH',
  ];
  const picked: Record<string, string | undefined> = {};
  for (const name of allowed) {
    if (process.env[name]) picked[name] = process.env[name];
  }
  return picked;
}

function shellInfo() {
  return {
    appVersion: APP_VERSION,
    backendOrigin: supervisor?.backendOrigin ?? '',
    sidecarPort: supervisor?.ports.sidecar ?? 0,
    dataDir: paths.data,
    logsDir: paths.logs,
    platform: process.platform,
    arch: process.arch,
    packaged: app.isPackaged,
    enginePackage: ENGINE_PACKAGE,
    engineVersion: ENGINE_VERSION,
    adapterVersion: ADAPTER_VERSION,
  };
}

async function createWindow(): Promise<void> {
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'icon.png')
    : path.join(app.getAppPath(), 'resources', 'icon.png');

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: '#f8fafc',
    title: 'Skill Router',
    autoHideMenuBar: true,
    ...(nativeImage.createFromPath(iconPath).isEmpty() ? {} : { icon: iconPath }),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow?.show());

  mainWindow.on('close', (event) => {
    if (quitting || !settings.closeToTray) return;
    event.preventDefault();
    mainWindow?.hide();
    ensureTray();
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Nothing navigates the shell away from its own origin: that would hand the
  // session cookie to another document. External links open in the real browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const own = server?.origin ?? '';
    if (own && !url.startsWith(own)) {
      event.preventDefault();
      if (/^https?:/i.test(url)) void shell.openExternal(url);
    }
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    log.error('renderer process gone', details);
    if (quitting) return;
    void dialog
      .showMessageBox({
        type: 'error',
        title: 'Skill Router window stopped',
        message: 'The interface stopped responding and needs to be reloaded.',
        detail: `Reason: ${details.reason}. If this repeats, check ${paths.logs}.`,
        buttons: ['Reload', 'Quit'],
        defaultId: 0,
        cancelId: 1,
      })
      .then((result) => {
        if (result.response === 0) mainWindow?.reload();
        else {
          quitting = true;
          app.quit();
        }
      });
  });

  const devRenderer = process.env.SR_DESKTOP_DEV_RENDERER?.trim();
  await mainWindow.loadURL(devRenderer || server?.origin || 'about:blank');
}

function ensureTray(): void {
  if (tray) return;
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'icon.png')
    : path.join(app.getAppPath(), 'resources', 'icon.png');
  const image = nativeImage.createFromPath(iconPath);
  tray = new Tray(image.isEmpty() ? nativeImage.createEmpty() : image.resize({ width: 16, height: 16 }));
  tray.setToolTip('Skill Router');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: 'Open Skill Router',
        click: () => {
          if (!mainWindow) {
            void createWindow();
            return;
          }
          mainWindow.show();
          mainWindow.focus();
        },
      },
      {
        label: 'Status…',
        click: () => {
          void (async () => {
            const report = await supervisor?.checkHealth();
            await dialog.showMessageBox({
              type: report?.ok ? 'info' : 'warning',
              title: 'Skill Router status',
              message: report?.detail ?? 'The API is not running.',
              detail:
                `API port: ${report?.backendPort || '—'}\nUI port: ${report?.webPort || '—'}\n` +
                `Engine port: ${report?.sidecarPort || '—'}\nData: ${paths.data}`,
              buttons: ['OK'],
            });
          })();
        },
      },
      { type: 'separator' },
      { label: 'Open data folder', click: () => void shell.openPath(paths.data) },
      { label: 'Open logs folder', click: () => void shell.openPath(paths.logs) },
      { type: 'separator' },
      {
        label: 'Quit Skill Router',
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('click', () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
}

async function bootstrap(): Promise<void> {
  settings = readSettings(paths);
  const runtime = readRuntimeState(paths);
  const sidecarToken = runtime.sidecarToken || randomBytes(24).toString('hex');

  // Reserve the shell port first: the backend must trust exactly this origin for
  // its cookie/CSRF checks, and it reads SR_TRUSTED_ORIGINS at startup.
  //
  // The previous ports are preferred, not merely remembered: the platform URL is
  // copied into other programs when an agent is connected
  // (`skill-router-connect install --url http://127.0.0.1:<shell>`), and a port that
  // changes on every launch would silently break that installation. A port taken by
  // something else falls back to an arbitrary free one, and the connection is then
  // refreshed with `skill-router-connect update`.
  const shellPort = await findByPreferredPort(runtime.shellPort);

  supervisor = new Supervisor({
    paths,
    sink,
    sidecarToken,
    startAgentEngine: settings.startAgentEngine,
    shellPort,
    preferredPorts: {
      backend: runtime.backendPort,
      sidecar: runtime.sidecarPort,
      web: runtime.webPort,
    },
    inheritEnv: inheritedEnv(),
  });

  await supervisor.bootstrapBackend();
  const report = await supervisor.startAll();
  startupReport = report;
  writeRuntimeState(paths, {
    backendPort: report.backendPort,
    sidecarPort: report.sidecarPort,
    webPort: report.webPort,
    shellPort: report.shellPort,
    sidecarToken,
    appVersion: APP_VERSION,
    lastStartedAt: new Date().toISOString(),
  });

  server = new AppServer({
    shellToken,
    sink,
    info: shellInfo,
    health: () => supervisor!.checkHealth(),
  });
  server.setBackendOrigin(supervisor.backendOrigin);
  server.setWebOrigin(supervisor.webOrigin);
  await server.listen(shellPort);
  log.info('desktop shell listening', { origin: server.origin, ...supervisor.ports });

  await createWindow();
  if (!mainWindow?.isVisible()) mainWindow?.show();

  if (!report.ok) {
    void dialog.showMessageBox({
      type: 'error',
      title: 'Skill Router could not start completely',
      message: report.detail,
      detail: `Logs: ${paths.logs}\nData: ${paths.data}`,
      buttons: ['Open logs', 'Continue'],
      defaultId: 1,
    });
  }

  if (settings.launchAtLogin) {
    app.setLoginItemSettings({ openAtLogin: true, args: ['--hidden'] });
  }
}

function registerIpc(): void {
  ipcMain.handle('desktop:info', () => shellInfo());
  ipcMain.handle('desktop:health', async () => (await supervisor?.checkHealth()) ?? null);
  ipcMain.handle('desktop:startup-report', () => startupReport);
  ipcMain.handle('desktop:open-data-folder', async () => {
    const error = await shell.openPath(paths.data);
    return { ok: error === '', path: paths.data, error };
  });
  ipcMain.handle('desktop:open-logs-folder', async () => {
    const error = await shell.openPath(paths.logs);
    return { ok: error === '', path: paths.logs, error };
  });
  ipcMain.handle('desktop:restart-backend', async () => {
    const report = (await supervisor?.restartBackend()) ?? null;
    if (report) {
      startupReport = report;
      server?.setBackendOrigin(supervisor!.backendOrigin);
      server?.setWebOrigin(supervisor!.webOrigin);
    }
    return report;
  });
  ipcMain.handle('desktop:get-settings', () => settings);
  ipcMain.handle('desktop:set-settings', (_event, patch: Partial<DesktopSettings>) => {
    settings = { ...settings, ...patch };
    writeSettings(paths, settings);
    app.setLoginItemSettings({ openAtLogin: settings.launchAtLogin, args: ['--hidden'] });
    if (!settings.startAgentEngine) {
      // Taking the engine down must not touch published capabilities.
      log.info('agent engine disabled by the user; the sidecar will stop on next launch');
    }
    return settings;
  });
}

async function shutdown(reason: string): Promise<void> {
  log.info('shutting down', reason);
  // Every step here can block on a socket or a child that will not die; quitting
  // must still finish. The services are all local and restartable, so a bounded
  // wait plus app.exit() is the safe trade.
  const bounded = async (): Promise<void> => {
    await server?.close();
    await supervisor?.stopAll(reason);
  };
  await Promise.race([bounded(), new Promise<void>((resolve) => setTimeout(resolve, 8_000))]);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app
    .whenReady()
    .then(async () => {
      registerIpc();
      try {
        await bootstrap();
      } catch (error) {
        log.error('startup failed', error);
        const detail = error instanceof Error ? error.message : String(error);
        const choice = await dialog.showMessageBox({
          type: 'error',
          title: 'Skill Router failed to start',
          message: 'The local services could not be started.',
          detail: `${detail}\n\nLogs: ${paths.logs}`,
          buttons: ['Open logs', 'Quit'],
          defaultId: 0,
          cancelId: 1,
        });
        if (choice.response === 0) void shell.openPath(paths.logs);
        quitting = true;
        app.quit();
      }
    })
    .catch((error: unknown) => {
      log.error('whenReady failed', error);
      quitting = true;
      app.quit();
    });

  app.on('window-all-closed', () => {
    // Closing the window hides to the tray by default; quitting is explicit.
    if (!settings.closeToTray) {
      quitting = true;
      app.quit();
    }
  });

  app.on('activate', () => {
    if (mainWindow) mainWindow.show();
    else void createWindow();
  });

  app.on('before-quit', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    void shutdown('application quit').finally(() => app.exit(0));
  });

  // A second quit request from the OS must not wait for the first one.
  app.on('will-quit', () => {
    quitting = true;
  });

  for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.on(signal, () => {
      quitting = true;
      void shutdown(`signal ${signal}`).finally(() => app.exit(0));
    });
  }
}
