/**
 * 应用菜单（spec §2.10 / §3#16）：重载、开发者工具、打开数据目录、打开日志目录、复制诊断信息、关于。
 *
 * 桌面状态只在**桌面层**表达（菜单里的状态项 + main.log），不向渲染进程注入任何 UI（INV-1）。
 */
import { app, clipboard, dialog, Menu, shell } from 'electron';
import type { MenuItemConstructorOptions } from 'electron';

import { PRODUCT_NAME } from '../shared/constants';
import type { ServiceStatus } from '../shared/types';

export interface MenuContext {
  dataDir: string;
  logsDir: string;
  getStatus: () => ServiceStatus;
  diagnostics: () => string;
  reload: () => void;
  openDevTools: () => void;
  restartServices: () => void;
}

export function buildMenu(ctx: MenuContext): Menu {
  const status = ctx.getStatus();
  const statusLine = `状态：${status.state}${status.degraded ? '（网关降级）' : ''}${
    status.errorCode ? ` · ${status.errorCode}` : ''
  }`;

  const template: MenuItemConstructorOptions[] = [
    {
      label: PRODUCT_NAME,
      submenu: [
        {
          label: `关于 ${PRODUCT_NAME}`,
          click: () => {
            void dialog.showMessageBox({
              type: 'info',
              title: `关于 ${PRODUCT_NAME}`,
              message: `${PRODUCT_NAME} ${app.getVersion()}`,
              detail: [
                `Electron ${process.versions.electron} · Chromium ${process.versions.chrome}`,
                `数据目录：${ctx.dataDir}`,
                `日志目录：${ctx.logsDir}`,
                '桌面端 ≠ 生产部署；生产请使用 deploy/docker-compose.yml。',
              ].join('\n'),
              buttons: ['好'],
              noLink: true,
            });
          },
        },
        { type: 'separator' },
        {
          label: '打开数据目录',
          click: () => {
            void shell.openPath(ctx.dataDir);
          },
        },
        {
          label: '打开日志目录',
          click: () => {
            void shell.openPath(ctx.logsDir);
          },
        },
        {
          label: '复制诊断信息',
          click: () => {
            clipboard.writeText(ctx.diagnostics());
          },
        },
        { type: 'separator' },
        {
          label: '重启服务',
          click: () => ctx.restartServices(),
        },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { label: '重载控制台', accelerator: 'F5', click: () => ctx.reload() },
        { label: '开发者工具', accelerator: 'F12', click: () => ctx.openDevTools() },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        { label: statusLine, enabled: false },
        {
          label: '排错手册（本地）',
          click: () => {
            void dialog.showMessageBox({
              type: 'info',
              title: '排错',
              message: '日志与错误码',
              detail: `日志目录：${ctx.logsDir}\n错误码处置见仓库 docs/runbook/desktop-app.md`,
              buttons: ['好'],
              noLink: true,
            });
          },
        },
      ],
    },
  ];

  return Menu.buildFromTemplate(template);
}
