/**
 * 窗口层（spec §2.10 / §2.12）：splash、主窗口、错误窗口与对话框。
 *
 * 安全约束（§2.12）：`contextIsolation: true` + `sandbox: true` + `nodeIntegration: false`；
 * 导航只允许 `http://127.0.0.1:<webPort>` 前缀，其余交给 `shell.openExternal`（仅 http/https）。
 */
import { BrowserWindow, dialog, shell } from 'electron';
import fs from 'node:fs';
import path from 'node:path';

import { PRODUCT_NAME } from '../shared/constants';
import type { StartupState } from '../shared/types';

export interface WindowContext {
  preloadScript: string;
  splashHtml: string;
  logsDir: string;
  iconPath?: string;
}

export type ErrorAction = 'retry' | 'logs' | 'quit';

/** 打开日志目录（错误窗口的「打开日志」、`W-D06` 对话框、菜单共用同一条实现）。 */
export async function openLogsDir(logsDir: string): Promise<void> {
  await shell.openPath(logsDir);
}

export function createSplash(ctx: WindowContext, state: StartupState): BrowserWindow {
  const win = new BrowserWindow({
    width: 460,
    height: 300,
    resizable: false,
    frame: false,
    show: true,
    center: true,
    alwaysOnTop: true,
    skipTaskbar: false,
    backgroundColor: '#0b1020',
    title: PRODUCT_NAME,
    icon: ctx.iconPath,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  void win.loadFile(ctx.splashHtml, { query: { state } });
  return win;
}

export function updateSplash(
  ctx: WindowContext,
  win: BrowserWindow | null,
  state: StartupState,
  adminInitialized?: boolean,
): void {
  if (win === null || win.isDestroyed()) return;
  const query: Record<string, string> = { state };
  if (adminInitialized !== undefined) query.adminInitialized = adminInitialized ? '1' : '0';
  void win.loadFile(ctx.splashHtml, { query }).catch(() => {});
}

export interface MainWindowHandle {
  win: BrowserWindow;
  /** 只允许这个前缀留在应用内。 */
  allowedOrigin: string;
}

export function createMainWindow(ctx: WindowContext, url: string): MainWindowHandle {
  const allowed = new URL(url);
  const allowedOrigin = `${allowed.protocol}//${allowed.host}`;

  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    backgroundColor: '#0b1020',
    title: PRODUCT_NAME,
    icon: ctx.iconPath,
    autoHideMenuBar: false,
    webPreferences: {
      preload: ctx.preloadScript,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });

  // 导航拦截：只放行本机控制台，其余外链交给系统浏览器（仅 http/https）
  win.webContents.on('will-navigate', (event, target) => {
    if (target.startsWith(allowedOrigin)) return;
    event.preventDefault();
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
  });
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (target.startsWith(allowedOrigin)) return { action: 'allow' };
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
    return { action: 'deny' };
  });

  win.once('ready-to-show', () => win.show());
  void win.loadURL(url);
  return { win, allowedOrigin };
}

/**
 * 错误窗口：本地 HTML（错误码 + 尾部日志）+ 一个三按钮的原生对话框。
 *
 * 为什么不把三个按钮做进 HTML：沙箱渲染进程没有 IPC 面（§4.2 只允许四个控制台方法），
 * 为错误页再开一套通道会扩大暴露面。原生对话框承担动作，HTML 窗口承担可复制的细节。
 */
export async function showErrorWindow(
  ctx: WindowContext,
  info: { code: string; detail: string; tail: string },
  onAction: (action: ErrorAction) => void,
): Promise<void> {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const file = path.join(ctx.logsDir, `error-${stamp}.html`);
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>${PRODUCT_NAME} 启动失败</title>
<style>
body{font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;background:#0b1020;color:#e6e9f2;margin:0;padding:24px}
h1{font-size:18px;margin:0 0 4px}
code{background:rgba(255,255,255,.08);border-radius:4px;padding:1px 5px;font-family:Consolas,monospace}
pre{white-space:pre-wrap;word-break:break-all;background:#060a16;border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:12px;font-size:12px;line-height:1.5;max-height:52vh;overflow:auto}
.detail{color:#9aa6c4;font-size:13px;margin:8px 0 16px}
</style></head><body>
<h1>${PRODUCT_NAME} 启动失败</h1>
<div class="detail">错误码 <code>${info.code}</code><br>${info.detail}</div>
<pre>${info.tail.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' })[c] ?? c)}</pre>
</body></html>`;
  try {
    fs.mkdirSync(ctx.logsDir, { recursive: true });
    fs.writeFileSync(file, html, 'utf8');
  } catch {
    /* 写失败也要把对话框弹出来 */
  }

  const win = new BrowserWindow({
    width: 780,
    height: 560,
    title: `${PRODUCT_NAME} 启动失败`,
    backgroundColor: '#0b1020',
    icon: ctx.iconPath,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  try {
    await win.loadFile(file);
  } catch {
    win.destroy();
  }

  const buttons = ['重试', '打开日志', '退出'];
  const dialogOptions = {
    type: 'error' as const,
    title: `${PRODUCT_NAME} 启动失败`,
    message: info.code,
    detail: `${info.detail}\n\n日志：${ctx.logsDir}`,
    buttons,
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  };
  const choice = win.isDestroyed()
    ? await dialog.showMessageBox(dialogOptions)
    : await dialog.showMessageBox(win, dialogOptions);
  if (!win.isDestroyed()) win.destroy();
  onAction(choice.response === 0 ? 'retry' : choice.response === 1 ? 'logs' : 'quit');
}

/** `W-D06` / 已有实例：只提供「退出 / 打开日志」（重试不会变好，§2.3.2 第 2 条）。 */
export async function showInstanceRunningDialog(ctx: WindowContext, detail: string): Promise<void> {
  const choice = await dialog.showMessageBox({
    type: 'warning',
    title: PRODUCT_NAME,
    message: '检测到另一个 Skill Router 正在运行',
    detail,
    buttons: ['退出', '打开日志'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  if (choice.response === 1) await openLogsDir(ctx.logsDir);
}
