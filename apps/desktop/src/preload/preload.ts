/**
 * preload（spec §4.2）：唯一暴露面，只有 4 个只读/受控方法。
 *
 * 窗口是 `sandbox: true` + `contextIsolation: true`，因此这里**不得**使用 `node:` 内置模块或
 * 第三方包；`version` / `platform` 通过 `ipcRenderer.invoke` 从主进程取，不依赖 `process.versions`。
 * 通道名来自 `shared/constants.ts`（与主进程同一份，不在这里再写一遍字面量）。
 */
import { contextBridge, ipcRenderer } from 'electron';

import { IPC_CHANNELS } from '../shared/constants';

const api = {
  version: '0.0.0',
  platform: 'win32',
  getStatus: async (): Promise<unknown> => ipcRenderer.invoke(IPC_CHANNELS.status),
  openDataDir: async (): Promise<void> => {
    await ipcRenderer.invoke(IPC_CHANNELS.openDir, 'data');
  },
  openLogDir: async (): Promise<void> => {
    await ipcRenderer.invoke(IPC_CHANNELS.openDir, 'logs');
  },
  restartServices: async (): Promise<void> => {
    await ipcRenderer.invoke(IPC_CHANNELS.restart);
  },
};

// version 通过 invoke 取（沙箱 preload 里 process.versions 只是受限子集）
void ipcRenderer
  .invoke(IPC_CHANNELS.version)
  .then((v: unknown) => {
    if (typeof v === 'string') api.version = v;
  })
  .catch(() => {
    /* 取不到就保持 0.0.0 */
  });

contextBridge.exposeInMainWorld('skillrouterDesktop', api);
