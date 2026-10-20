/**
 * 路径解析（spec §2.4）。
 *
 * 安装目录（`Program Files` / `%LOCALAPPDATA%\Programs`）**不可写**，所以：
 * - 只读资源从 `resources\` 读（打包后 `process.resourcesPath`）；
 * - 所有可写状态落在 `userData`（`%APPDATA%\SkillRouter`，显式重定以避开空格）与 `<userData>\data`。
 */
import { app } from 'electron';
import path from 'node:path';

export interface DesktopPaths {
  /** `<userData>` = `%APPDATA%\SkillRouter`。 */
  userData: string;
  /** `<data>` = `<userData>\data`（SKILLROUTER_DATA_DIR）。 */
  data: string;
  dbPath: string;
  /** `<userData>\logs\desktop`。 */
  logsDir: string;
  /** 只读资源根：打包后是 `resources`，开发态是 `apps/desktop/build`。 */
  resourcesRoot: string;
  /** `<resources>\web`：Next standalone 宿主（`server.js` 的 cwd，spec §2.8）。 */
  webRoot: string;
  /** `<resources>\gateway`：网关编译产物根。 */
  gatewayRoot: string;
  sidecarExe: string;
  webEntry: string;
  gatewayEntry: string;
  /** preload 与 splash（asar 内）。 */
  preloadScript: string;
  splashHtml: string;
  isPackaged: boolean;
}

export function appRoot(): string {
  // dist/main.js → apps/desktop
  return path.resolve(__dirname, '..');
}

/** `userData` 重定：Electron 默认用 productName（含空格），这里固定为 `SkillRouter`。 */
export function configureUserData(): string {
  const appData = app.getPath('appData');
  const dir = path.join(appData, 'SkillRouter');
  app.setPath('userData', dir);
  return dir;
}

export function resolvePaths(opts: { dataDirOverride?: string | null } = {}): DesktopPaths {
  const isPackaged = app.isPackaged;
  const userData = app.getPath('userData');
  const data = opts.dataDirOverride ? path.resolve(opts.dataDirOverride) : path.join(userData, 'data');
  const resourcesRoot = isPackaged ? process.resourcesPath : path.join(appRoot(), 'build');
  const webRoot = path.join(resourcesRoot, 'web');
  const gatewayRoot = path.join(resourcesRoot, 'gateway');

  return {
    userData,
    data,
    dbPath: path.join(data, 'skillrouter.db'),
    logsDir: path.join(userData, 'logs', 'desktop'),
    resourcesRoot,
    webRoot,
    gatewayRoot,
    sidecarExe: path.join(resourcesRoot, 'python', 'skillrouter-server.exe'),
    webEntry: path.join(webRoot, 'server.js'),
    gatewayEntry: path.join(gatewayRoot, 'dist', 'src', 'main.js'),
    preloadScript: path.join(appRoot(), 'dist', 'preload.js'),
    splashHtml: path.join(appRoot(), 'splash.html'),
    isPackaged,
  };
}
