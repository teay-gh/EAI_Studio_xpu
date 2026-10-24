'use strict';

/**
 * EAI Studio · Chromium 外壳 preload
 *
 * 渲染层只通过 window.eai 访问主进程。contextIsolation: true，
 * 这里只暴露任务约定的这一套 API（形状固定，渲染层依赖它）。
 */

const { contextBridge, ipcRenderer } = require('electron');

/** 订阅主进程推送，返回取消订阅函数。 */
function on(channel, handler) {
  const listener = (_event, payload) => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('eai', {
  // 全局状态：主题、核显加速开关、Python 桥端口、应用版本
  state: () => ipcRenderer.invoke('eai:state'),

  // 主题：主进程负责落盘 + 换窗口底色，并由渲染层设置 <html data-theme>
  setTheme: (t) => ipcRenderer.invoke('eai:set-theme', t),

  // 核显加速开关：只写 state.json，返回 needRestart，由渲染层点「立即重启」调 restart()
  setGpu: (on) => ipcRenderer.invoke('eai:set-gpu', on),

  // 重启进程（app.relaunch + app.exit）
  restart: () => ipcRenderer.invoke('eai:restart'),

  // GPU 状态：{ enabled, disableRequested, feature, gpuInfo }
  gpuStatus: () => ipcRenderer.invoke('eai:gpu-status'),

  // 概览数据：主进程代取 http://127.0.0.1:<bridgePort>/api/overview
  overview: () => ipcRenderer.invoke('eai:overview'),

  // 无边框窗口的标题栏按钮
  win: {
    minimize: () => ipcRenderer.invoke('eai:win-minimize'),
    toggleMax: () => ipcRenderer.invoke('eai:win-toggle-max'),
    close: () => ipcRenderer.invoke('eai:win-close'),
    isMax: () => ipcRenderer.invoke('eai:win-is-max'),
  },

  // 最大化/还原时主进程推送 { maximized: boolean }
  onMaxChange: (cb) => on('eai:max-changed', cb),

  // 只用系统浏览器打开 http/https
  openExternal: (url) => ipcRenderer.invoke('eai:open-external', url),

  // 原生「选择文件夹」对话框（工程切换/新建用）：返回路径字符串，取消返回 null
  pickFolder: (opts) => ipcRenderer.invoke('eai:pick-folder', opts),

  // 原生「选择文件」对话框（仓库页添加数据集/权重用）：返回路径数组，取消返回 null
  pickFile: (opts) => ipcRenderer.invoke('eai:pick-file', opts),

  // 原生「另存为」对话框（数据管理导出数据集用）：返回 { canceled, path }
  saveFile: (opts) => ipcRenderer.invoke('eai:save-file', opts),

  // 原生确认框：{title, message, detail, buttons:[取消, 确定], defaultId} → {ok, index}
  confirm: (opts) => ipcRenderer.invoke('eai:confirm', opts),

  // 环境安装页（setup.html）：扫描 → 依次安装（进度走 onProgress 推送）→ 进入 EAI
  setup: {
    scan: () => ipcRenderer.invoke('eai:setup-scan'),
    start: () => ipcRenderer.invoke('eai:setup-start'),
    enter: () => ipcRenderer.invoke('eai:setup-enter'),
    // 进度事件：{ id, pct, text, state } state = run | ok | error
    onProgress: (cb) => on('eai:setup-progress', cb),
  },
});
