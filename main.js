'use strict';

/**
 * EAI Studio · Chromium 外壳主进程
 *
 * 职责（外壳层，不碰渲染层文件）：
 *   1. 无边框窗口（frame:false, 1380x860, min 900x600），底色跟主题走。
 *   2. state.json 持久化主题 + 核显加速开关，损坏自动重建。
 *   3. GPU 开关：gpu=false 时在 app ready 之前调 app.disableHardwareAcceleration()，
 *      gpu=true 时什么都不做（走默认 D3D11/ANGLE 硬件渲染）。切换需重启。
 *   4. 拉起 Python 数据桥 bridge.py，读 stdout 首行 "PORT <n>" 握手；退出时 kill。
 *   5. IPC：把窗口控制 / 主题 / GPU / 概览数据转发给渲染层的 window.eai。
 *   6. 首次运行的环境检查（2026-10-24）：Python/PyTorch/依赖缺失时先加载 setup.html，
 *      装完再切回 index.html —— 与启动 EAI 共用同一个 bat、同一个进程。
 */

/* 日志管道断了不该把主进程炸掉：写 stdout/stderr 失败（EPIPE）只丢弃这条日志。 */
for (const stream of [process.stdout, process.stderr]) {
  if (stream && typeof stream.on === 'function') {
    stream.on('error', (err) => {
      if (err && err.code === 'EPIPE') return;
    });
  }
}

const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const path = require('path');
const ENV = require('./installer/env');                 // 环境检测 / 安装（setup 页用）
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

// ---------- 路径 ----------
const APP_DIR = __dirname;                              // <应用目录>
const EAI_ROOT = path.dirname(APP_DIR);                 // <项目目录>
const STATE_FILE = path.join(APP_DIR, 'state.json');    // <应用目录>\state.json
const BRIDGE_SCRIPT = path.join(APP_DIR, 'bridge.py');
// 运行环境两种布局都认（2026-10-24）：装到应用目录里（.venv 跟应用走，可整目录搬机器），
// 或老布局放在上级目录；都没有就退回 PATH 里的 python（界面能开，训练会缺 torch）。
const PY_RESOLVED = ENV.resolvePython(APP_DIR);
let pythonPath = PY_RESOLVED.py;
const RENDERER_DIR = path.join(APP_DIR, 'renderer');
const INDEX_HTML = path.join(RENDERER_DIR, 'index.html');
const SETUP_HTML = path.join(RENDERER_DIR, 'setup.html');
const PLACEHOLDER_HTML = path.join(RENDERER_DIR, '_placeholder.html');

// ---------- 常量 ----------
// 与 ENGINE_DIR/core/theme.py 的 BG 对齐：dark #0F1117 / light #F5F7FC
const THEME_BG = { light: '#F5F7FC', dark: '#0F1117' };
const DEFAULT_STATE = { theme: 'light', gpu: false };
const BRIDGE_HANDSHAKE_TIMEOUT_MS = 15000;
const SMOKE = process.argv.includes('--smoke');

// ---------- 状态文件（不存在 / 损坏都能自愈） ----------
function normalizeState(obj) {
  const o = obj && typeof obj === 'object' ? obj : {};
  return {
    theme: o.theme === 'dark' ? 'dark' : 'light',
    gpu: o.gpu === true,
  };
}

function writeState(s) {
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2) + '\n', 'utf8');
    return true;
  } catch (err) {
    console.error('[main] state.json 写入失败:', (err && err.message) || err);
    return false;
  }
}

function readState() {
  let raw;
  try {
    raw = fs.readFileSync(STATE_FILE, 'utf8');
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      console.error('[main] state.json 读取失败，重建默认值:', err.message);
    }
    const fresh = { ...DEFAULT_STATE };
    writeState(fresh);
    return fresh;
  }
  try {
    const obj = JSON.parse(raw);
    const s = normalizeState(obj);
    // 顺手把规范化后的结构写回（缺字段 / 多余字段都不留隐患）
    writeState(s);
    return s;
  } catch (err) {
    console.error('[main] state.json 损坏，重建默认值:', err.message);
    const fresh = { ...DEFAULT_STATE };
    writeState(fresh);
    return fresh;
  }
}

let state = readState();
let mainWindow = null;
let bridgeProc = null;
let bridgePort = null;
let bridgeToken = null;      // 2026-10-23：bridge.py 启动时随机生成的鉴权 token（随握手下发）
let bridgeStderr = '';
let gpuDisabledByUs = false;
/* 进程实际在跑的渲染态：启动那一刻定死，只有重启才变。
 * 重启提示 = （期望态 state.gpu !== 运行态 gpuApplied）——点回原状态提示即消失（用户 2026-10-15）。 */
let gpuApplied = state.gpu;

/* 核显加速开关 —— 必须在 app ready 之前决定，进程起来后无法再改。
 * gpu=false → 关掉硬件加速，纯 CPU 软件渲染；
 * gpu=true  → 不调用，Electron 默认走 D3D11 / ANGLE 硬件渲染。 */
if (!state.gpu) {
  try {
    app.disableHardwareAcceleration();
    gpuDisabledByUs = true;
    console.log('[main] 已关闭硬件加速（state.gpu=false，软件渲染）');
  } catch (err) {
    console.error('[main] disableHardwareAcceleration 失败:', (err && err.message) || err);
  }
} else {
  console.log('[main] 硬件加速开启（state.gpu=true，默认 D3D11/ANGLE）');
}

// ---------- 窗口 ----------
function createWindow(pageWanted) {
  mainWindow = new BrowserWindow({
    icon: path.join(__dirname, 'assets', 'eai.ico'),
    width: 1380,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: THEME_BG[state.theme] || THEME_BG.light,
    title: 'EAI Studio',
    frame: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(APP_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  // --smoke：只加载占位页，绝不覆盖别人的 renderer/index.html
  let page = SMOKE ? PLACEHOLDER_HTML : (pageWanted || INDEX_HTML);
  if (!fs.existsSync(page)) {
    console.error('[main] 页面不存在:', page, SMOKE ? '' : '→ 回退到 _placeholder.html');
    if (!SMOKE) page = PLACEHOLDER_HTML;
  }
  // --smoke --probe：占位页额外跑一遍 setGpu/setTheme 写盘实测（默认不跑，免得改磁盘状态）
  const smokeHash = SMOKE && process.argv.includes('--probe') ? 'probe' : undefined;
  mainWindow.loadFile(page, smokeHash ? { hash: smokeHash } : undefined);

  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.on('maximize', () => send('eai:max-changed', { maximized: true }));
  mainWindow.on('unmaximize', () => send('eai:max-changed', { maximized: false }));
}

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

// ---------- Python 数据桥 ----------
/** 拉起 bridge.py，读 stdout 首行 "PORT <n>" 完成握手；超时/失败 → bridgePort=null。 */
function startBridge() {
  return new Promise((resolve) => {
    if (!fs.existsSync(BRIDGE_SCRIPT)) {
      console.error('[main] bridge.py 不存在:', BRIDGE_SCRIPT);
      return resolve(null);
    }
    let py = pythonPath;
    if (py && !fs.existsSync(py) && /[\\/]/.test(py)) {
      console.error('[main] venv python 不存在，回退到 PATH 里的 python:', py);
      py = process.platform === 'win32' ? 'python' : 'python3';
    }

    let settled = false;
    let outBuf = '';
    let child;
    try {
      child = spawn(py, [BRIDGE_SCRIPT], {
        cwd: APP_DIR,
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      });
    } catch (err) {
      console.error('[main] 拉起 bridge.py 失败:', (err && err.message) || err);
      return resolve(null);
    }
    bridgeProc = child;

    const done = (port) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (port === null) bridgePort = null;
      resolve(port);
    };

    const timer = setTimeout(() => {
      console.error(`[main] 等待 bridge 握手超时（${BRIDGE_HANDSHAKE_TIMEOUT_MS / 1000}s）→ bridgePort=null`);
      done(null);
    }, BRIDGE_HANDSHAKE_TIMEOUT_MS);

    const handleLine = (line) => {
      const m = /^PORT\s+(\d+)(?:\s+(\S+))?\s*$/.exec(String(line).trim());
      if (m) {
        bridgePort = Number(m[1]);
        bridgeToken = m[2] || null;
        console.log(`[main] Python 桥已就绪 pid=${child.pid} port=${bridgePort} auth=${bridgeToken ? 'on' : 'off'}`);
        done(bridgePort);
      } else if (String(line).trim()) {
        console.log('[bridge]', String(line).trim());
      }
    };

    if (child.stdout) {
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        outBuf += chunk;
        let idx;
        while ((idx = outBuf.indexOf('\n')) >= 0) {
          handleLine(outBuf.slice(0, idx));
          outBuf = outBuf.slice(idx + 1);
        }
      });
    }
    if (child.stderr) {
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => {
        bridgeStderr += chunk;
        if (bridgeStderr.length > 8000) bridgeStderr = bridgeStderr.slice(-8000);
      });
    }
    child.on('error', (err) => {
      console.error('[main] bridge 进程错误:', (err && err.message) || err);
      bridgeProc = null;
      done(null);
    });
    child.on('exit', (code, signal) => {
      console.log(`[main] bridge 退出 code=${code} signal=${signal}`);
      if (bridgeStderr.trim()) console.error('[bridge stderr]\n' + bridgeStderr.trim());
      if (bridgeProc === child) bridgeProc = null;
      bridgePort = null;
      done(null);
    });
  });
}

function stopBridge() {
  const child = bridgeProc;
  bridgeProc = null;
  bridgePort = null;
  bridgeToken = null;
  if (child && !child.killed) {
    try {
      child.kill();
      console.log('[main] 已终止 bridge 子进程 pid=' + child.pid);
    } catch (err) {
      console.error('[main] 终止 bridge 失败:', (err && err.message) || err);
    }
  }
}

// ---------- 主进程代取桥数据（渲染层是 file:// 页面，直连会被 CORS/Origin=null 挡住） ----------
function httpGetJson(port, apiPath, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const req = http.get(
      {
        host: '127.0.0.1', port, path: apiPath, timeout: timeoutMs,
        headers: bridgeToken ? { 'X-EAI-Token': bridgeToken } : {},
      },
      (res) => {
        let data = '';
        res.setEncoding('utf8');
        res.on('data', (c) => { data += c; });
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch (err) {
            reject(new Error('bridge 返回非 JSON: ' + data.slice(0, 200)));
          }
        });
      }
    );
    req.on('timeout', () => req.destroy(new Error('bridge 请求超时')));
    req.on('error', reject);
  });
}

// ---------- IPC ----------
ipcMain.handle('eai:state', () => ({
  theme: state.theme,
  gpu: state.gpu,
  gpuApplied,           // 本次进程启动时实际生效的渲染态（重启提示的比较基准）
  bridgePort,           // 未就绪时是 null
  bridgeToken,          // 桥鉴权 token（渲染层每个请求带 X-EAI-Token）
  version: app.getVersion(),
}));

ipcMain.handle('eai:set-theme', (_e, t) => {
  const theme = t === 'dark' ? 'dark' : 'light';
  state = { ...state, theme };
  writeState(state);
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.setBackgroundColor(THEME_BG[theme]); } catch { /* ignore */ }
  }
  // 渲染层据此设置 <html data-theme>（本进程只持久化 + 广播）
  send('eai:theme-changed', { theme });
  return { ok: true, theme };
});

ipcMain.handle('eai:set-gpu', (_e, on) => {
  const gpu = !!on;
  state = { ...state, gpu };
  writeState(state);
  // 硬件加速只能在进程启动前决定，所以这里只落盘；needRestart = 期望态与运行态不同
  // （点回原状态 → false → 重启提示消失，用户 2026-10-15）
  return { ok: true, gpu, needRestart: gpu !== gpuApplied };
});

ipcMain.handle('eai:restart', () => {
  console.log('[main] 重启进程（app.relaunch + app.exit）');
  app.relaunch();
  app.exit(0);
});

ipcMain.handle('eai:gpu-status', async () => {
  // ⚠ app.getGPUInfo('complete') 会卡 GPU 进程 ~0.65-0.85s（实测：渲染层掉帧 650/783/817/850ms），
  //   不要在任何 UI 交互路径上调它；当前渲染层已无调用方（2026-10-14 排查开关卡顿后移除）。
  let feature = null;
  try { feature = app.getGPUFeatureStatus(); } catch { feature = null; }
  let gpuInfo = null;
  try { gpuInfo = await app.getGPUInfo('complete'); } catch { gpuInfo = null; }
  return {
    enabled: !!state.gpu,
    disableRequested: gpuDisabledByUs,
    feature,
    gpuInfo,
  };
});

ipcMain.handle('eai:overview', async () => {
  if (!bridgePort) return { error: 'bridge offline' };
  try {
    return await httpGetJson(bridgePort, '/api/overview');
  } catch (err) {
    return { error: String((err && err.message) || err) };
  }
});

ipcMain.handle('eai:win-minimize', () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.handle('eai:win-toggle-max', () => {
  if (!mainWindow) return false;
  if (mainWindow.isMaximized()) { mainWindow.unmaximize(); return false; }
  mainWindow.maximize();
  return true;
});
ipcMain.handle('eai:win-close', () => { if (mainWindow) mainWindow.close(); });
ipcMain.handle('eai:win-is-max', () => Boolean(mainWindow && mainWindow.isMaximized()));

// 原生文件夹选择（侧栏「切换工程 / 新建工程」用；等价于 Tk 版 filedialog.askdirectory）
ipcMain.handle('eai:pick-folder', async (_e, opts) => {
  const r = await dialog.showOpenDialog(mainWindow, {
    title: (opts && opts.title) || '选择文件夹',
    buttonLabel: (opts && opts.buttonLabel) || '选择',
    properties: ['openDirectory', 'createDirectory'],
  });
  return (r.canceled || !r.filePaths.length) ? null : r.filePaths[0];
});

// 原生「选择文件」对话框（仓库页「+」添加数据集/权重用）：返回路径数组，取消返回 null
ipcMain.handle('eai:pick-file', async (_e, opts) => {
  const o = opts || {};
  const r = await dialog.showOpenDialog(mainWindow, {
    title: o.title || '选择文件',
    buttonLabel: o.buttonLabel || '选择',
    filters: (Array.isArray(o.filters) && o.filters.length) ? o.filters : undefined,
    /* 只能纯文件模式：Windows 上混入 openDirectory 会变「选文件夹」对话框，
       还会被文件名过滤器把文件夹全筛没（「没有与搜索条件匹配的」）——2026-10-16 实测踩过。
       选文件夹走 eai:pick-folder。 */
    properties: o.multi === false ? ['openFile'] : ['openFile', 'multiSelections'],
  });
  return (r.canceled || !r.filePaths.length) ? null : r.filePaths;
});

// 原生「另存为」对话框（数据管理「导出数据集…」用；等价于 Tk 版 filedialog.asksaveasfilename）
ipcMain.handle('eai:save-file', async (_e, opts) => {
  const o = opts || {};
  const filters = (Array.isArray(o.filters) && o.filters.length)
    ? o.filters : [{ name: 'JSONL 数据集', extensions: ['jsonl'] }];
  const r = await dialog.showSaveDialog(mainWindow, {
    title: o.title || '导出数据集',
    defaultPath: o.defaultPath || undefined,
    buttonLabel: o.buttonLabel || '保存',
    filters,
    properties: ['createDirectory', 'showOverwriteConfirmation'],
  });
  return (r.canceled || !r.filePath) ? { canceled: true, path: null }
                                     : { canceled: false, path: r.filePath };
});

// 原生确认框（会改状态的动作先问一句）。按钮顺序 = [取消, 确定]，默认停在「取消」，防手滑。
ipcMain.handle('eai:confirm', async (event, opts) => {
  const o = opts || {};
  const win = BrowserWindow.fromWebContents(event.sender) || null;
  const buttons = (Array.isArray(o.buttons) && o.buttons.length) ? o.buttons : ['取消', '确定'];
  const r = await dialog.showMessageBox(win, {
    type: o.type || 'warning',
    title: o.title || '请确认',
    message: o.message || '',
    detail: o.detail || '',
    buttons,
    defaultId: typeof o.defaultId === 'number' ? o.defaultId : 0,
    cancelId: typeof o.cancelId === 'number' ? o.cancelId : 0,
    noLink: true,
  });
  return { ok: r.response === 1, index: r.response };
});

ipcMain.handle('eai:open-external', async (_e, url) => {
  const u = String(url || '').trim();
  if (!/^https?:\/\//i.test(u)) return { ok: false, error: '只允许 http/https 链接' };
  await shell.openExternal(u);
  return { ok: true, url: u };
});

// ---------- 冒烟辅助：把窗口尺寸 + 桥状态 + GPU feature 打到 stdout ----------
function logSmoke() {
  try {
    const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
    let feature = null;
    try { feature = app.getGPUFeatureStatus(); } catch { feature = null; }
    const payload = {
      bounds: win ? win.getBounds() : null,
      contentBounds: win ? win.getContentBounds() : null,
      bridgePort,
      gpuDisabledByUs,
      state,
      gpuFeature: feature,
    };
    console.log('[smoke] ' + JSON.stringify(payload, null, 2));
  } catch (err) {
    console.error('[smoke] 失败:', (err && err.message) || err);
  }
}

// ---------- 环境检查 / 安装（setup.html，2026-10-24） ----------
/** 上次扫描结果：安装页打开时会调一次，安装完再调一次核对。 */
let setupScanCache = null;
let setupRunning = false;

ipcMain.handle('eai:setup-scan', async () => {
  setupScanCache = await ENV.scanEnv(APP_DIR);
  console.log('[setup] 扫描:', setupScanCache.items.map((i) => i.id + '=' + (i.ok ? 'ok' : 'missing')).join(' '));
  return setupScanCache;
});

ipcMain.handle('eai:setup-start', async () => {
  if (setupRunning) return { ok: false, error: 'setup already running' };
  setupRunning = true;
  try {
    const res = await ENV.runInstall(APP_DIR, (p) => send('eai:setup-progress', p));
    // 装完再扫一遍：不信任安装脚本的自述，以实际能 import 为准
    const after = await ENV.scanEnv(APP_DIR);
    setupScanCache = after;
    if (res && res.ok && !after.ok) {
      const bad = after.items.filter((i) => !i.ok).map((i) => i.id);
      return { ok: false, error: 'verify failed: ' + bad.join(',') };
    }
    if (after.ok) pythonPath = after.python.py;   // 新装的 .venv 立刻生效，不用重启
    return res;
  } catch (err) {
    console.error('[setup] 安装异常:', (err && err.message) || err);
    return { ok: false, error: String((err && err.message) || err) };
  } finally {
    setupRunning = false;
  }
});

ipcMain.handle('eai:setup-enter', async () => {
  pythonPath = ENV.resolvePython(APP_DIR).py;
  if (bridgePort === null) {
    await startBridge();
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadFile(INDEX_HTML).catch((err) => {
      console.error('[setup] 加载主界面失败:', (err && err.message) || err);
    });
  }
  return { ok: true, bridgePort };
});

// ---------- app 生命周期 ----------
// Windows 任务栏/通知用它做身份标识：与 EProject（同一份 electron.exe）分开分组，
// 否则两个窗口会共用 Electron 默认图标、看起来一模一样。
app.setAppUserModelId('EEEPro.EAIStudio');

app.whenReady().then(async () => {
  // 首次运行先扫环境（用户 2026-10-24：一个 bat 兼管「装环境」和「开 EAI」）：
  //   缺 Python/PyTorch/依赖 → 进安装页；齐全 → 直接主界面，安装页根本不闪一下。
  let needSetup = process.argv.includes('--setup');
  if (!SMOKE) {
    try {
      const scan = await ENV.scanEnv(APP_DIR);
      console.log('[main] 环境扫描:',
        scan.items.map((i) => i.id + '=' + (i.ok ? 'ok' : 'missing')).join(' '),
        '→', (needSetup || !scan.ok) ? '进安装页' : '直接启动 EAI');
      if (!scan.ok) needSetup = true;
    } catch (err) {
      console.error('[main] 环境扫描失败，按缺失处理:', (err && err.message) || err);
      needSetup = true;
    }
  }
  createWindow(needSetup ? SETUP_HTML : INDEX_HTML);
  if (!needSetup) {
    await startBridge();
    if (bridgePort === null) {
      console.error('[main] bridge 未就绪：overview() 将返回 {error:"bridge offline"}，窗口不受影响');
    }
  }
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
  if (SMOKE) setTimeout(logSmoke, 3000);
}).catch((err) => {
  console.error('[main] app.whenReady 失败:', err);
});

app.on('window-all-closed', () => {
  stopBridge();
  if (process.platform !== 'darwin') app.quit();
});

app.on('will-quit', () => {
  stopBridge();
});
