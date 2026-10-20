/**
 * IPC（spec §4.2）：只注册 4 个处理器 —— 版本 / 状态 / 打开目录 / 重启服务。
 *
 * 控制台前端**不消费**这些方法（INV-1）；它们由菜单与错误窗口使用。
 */
import { app, ipcMain, shell } from 'electron';

import { IPC_CHANNELS } from '../shared/constants';
import type { ServiceStatus } from '../shared/types';

export interface IpcContext {
  getStatus: () => ServiceStatus;
  dataDir: string;
  logsDir: string;
  restartServices: () => void;
}

/** 重启服务的最小间隔（§4.2：最多 1 次 / 30 s）。 */
const RESTART_MIN_INTERVAL_MS = 30_000;

export function registerIpc(ctx: IpcContext): void {
  let lastRestart = 0;

  // 幂等：`activate` / 重启服务等路径可能再次进入这里，`ipcMain.handle` 对同一通道
  // 重复注册会直接抛 "Attempted to register a second handler"。
  unregisterIpc();

  ipcMain.handle(IPC_CHANNELS.version, () => app.getVersion());
  ipcMain.handle(IPC_CHANNELS.status, () => ctx.getStatus());
  ipcMain.handle(IPC_CHANNELS.openDir, async (_event, which: unknown) => {
    const target = which === 'logs' ? ctx.logsDir : ctx.dataDir;
    await shell.openPath(target);
  });
  ipcMain.handle(IPC_CHANNELS.restart, () => {
    const now = Date.now();
    if (now - lastRestart < RESTART_MIN_INTERVAL_MS) return { ok: false, reason: 'throttled' };
    lastRestart = now;
    ctx.restartServices();
    return { ok: true };
  });
}

export function unregisterIpc(): void {
  for (const channel of Object.values(IPC_CHANNELS)) ipcMain.removeHandler(channel);
}
