'use strict';

/**
 * EAI Studio · 运行环境检测 / 安装（2026-10-24）
 *
 * 设计口径（用户要求）：
 *   · 一个 bat 同时负责「开 EAI」和「装环境」：真正判断由这里做，界面只是呈现；
 *   · 需要装的东西只有三件：Python 运行时（.venv）→ PyTorch（XPU 构建）→ 基础依赖；
 *   · 一律装进「应用目录/.venv」——环境跟着应用走，整个文件夹复制到别的机器也能用；
 *   · 只用官方/镜像源，不静默换版本；装完用新进程复验（当前进程里 torch 已加载看不出来）。
 *
 * Electron 本身不在安装清单里：没有它连这个界面都起不来，由 启动.bat 在控制台处理。
 */

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const PY_VERSION = '3.13';                                  // 与开发机 .venv 对齐（cp313 的 +xpu 轮子齐全）
const TORCH_VERSION = '2.14.1';
const TORCH_SPEC = TORCH_VERSION + '+xpu';
const TORCH_INDEX = 'https://download.pytorch.org/whl/xpu';
const PYPI_MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple';
const BASE_DEPS = ['numpy', 'psutil'];

const IS_WIN = process.platform === 'win32';

function venvPython(venvDir) {
  return IS_WIN ? path.join(venvDir, 'Scripts', 'python.exe') : path.join(venvDir, 'bin', 'python');
}

/** 运行环境位置：应用目录内优先，其次上级目录（兼容老的开发布局），最后 PATH 里的 python。 */
function resolvePython(appDir) {
  const local = venvPython(path.join(appDir, '.venv'));
  if (fs.existsSync(local)) return { py: local, venv: path.join(appDir, '.venv'), kind: 'local' };
  const parentVenv = path.join(path.dirname(appDir), '.venv');
  const parent = venvPython(parentVenv);
  if (fs.existsSync(parent)) return { py: parent, venv: parentVenv, kind: 'parent' };
  return { py: IS_WIN ? 'python' : 'python3', venv: null, kind: 'path' };
}

/** 跑一条命令，收齐输出（供探测用，不打印）。 */
function run(cmd, args, opts) {
  return new Promise((resolve) => {
    let out = '';
    let err = '';
    let child;
    try {
      child = spawn(cmd, args, {
        cwd: (opts && opts.cwd) || undefined,
        windowsHide: true,
        env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }, (opts && opts.env) || {}),
      });
    } catch (e) {
      return resolve({ code: -1, out: '', err: String((e && e.message) || e) });
    }
    const timer = setTimeout(() => { try { child.kill(); } catch (e) { /* ignore */ } }, (opts && opts.timeout) || 60000);
    child.stdout.on('data', (b) => { out += b.toString('utf8'); });
    child.stderr.on('data', (b) => { err += b.toString('utf8'); });
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out: out, err: err + String((e && e.message) || e) }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code: code == null ? -1 : code, out: out, err: err }); });
  });
}

/** 流式跑一条命令：每一行输出交给 onLine，最后给 onDone。 */
function runStream(cmd, args, opts, onLine, onDone) {
  let child;
  try {
    child = spawn(cmd, args, {
      cwd: (opts && opts.cwd) || undefined,
      windowsHide: true,
      env: Object.assign({}, process.env, {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
        // uv：给足超时/并发，国内直连 pytorch 源也能跑满带宽
        UV_HTTP_TIMEOUT: '300',
        UV_CONCURRENT_DOWNLOADS: '8',
        PIP_DISABLE_PIP_VERSION_CHECK: '1',
        PIP_NO_INPUT: '1',
      }, (opts && opts.env) || {}),
    });
  } catch (e) {
    onDone && onDone(-1, String((e && e.message) || e));
    return null;
  }
  let tail = '';
  const feed = (buf) => {
    const text = buf.toString('utf8');
    tail = (tail + text).slice(-4000);
    text.split(/\r?\n|\r/).forEach((line) => {
      const s = line.trim();
      if (s) onLine && onLine(s);
    });
  };
  child.stdout.on('data', feed);
  child.stderr.on('data', feed);
  child.on('error', (e) => onDone && onDone(-1, String((e && e.message) || e) + '\n' + tail));
  child.on('close', (code) => onDone && onDone(code == null ? -1 : code, tail));
  return child;
}

let uvCache = null;
async function hasUv() {
  if (uvCache !== null) return uvCache;
  const r = await run('uv', ['--version'], { timeout: 15000 });
  uvCache = r.code === 0 && /\buv\s+\d/.test(r.out);
  return uvCache;
}

/** 探测：{ ok, version } */
async function probePy(py, code) {
  const r = await run(py, ['-c', code], { timeout: 30000 });
  if (r.code !== 0) return { ok: false, out: '', err: (r.err || r.out || '').trim() };
  return { ok: true, out: r.out.trim(), err: '' };
}

/**
 * 环境扫描：返回给安装界面直接渲染的结构。
 * 顺序 = 安装顺序，界面上也按这个顺序排。
 */
async function scanEnv(appDir) {
  const resolved = resolvePython(appDir);
  const items = [];

  // ① Electron（只报告，不在这里装：没 Electron 就没有这个界面）
  const electronExe = path.join(appDir, 'node_modules', 'electron', 'dist',
    IS_WIN ? 'electron.exe' : 'electron');
  items.push({
    id: 'electron',
    installable: false,
    ok: fs.existsSync(electronExe),
    detail: fs.existsSync(electronExe) ? 'node_modules/electron' : '',
  });

  // ②③④ 一次进程问完：Python 版本 / torch / 基础依赖（2026-10-24：本来三次 spawn，
  //        fresh 环境里 import torch 要好几秒，串起来界面会「正在检测」停太久）
  const probeCode = [
    'import sys',
    'print("py=" + "%d.%d.%d" % sys.version_info[:3])',
    'try:',
    '    import torch; print("torch=" + torch.__version__)',
    'except Exception:',
    '    print("torch=")',
    'try:',
    '    import numpy, psutil; print("deps=" + numpy.__version__ + " / psutil " + psutil.__version__)',
    'except Exception:',
    '    print("deps=")',
  ].join('\n');

  const pyOk = resolved.kind !== 'path' && fs.existsSync(resolved.py);
  let pyVersion = '';
  let torchVersion = '';
  let depsDetail = '';
  if (pyOk) {
    const r = await run(resolved.py, ['-c', probeCode], { timeout: 90000 });
    (r.out || '').split(/\r?\n/).forEach((line) => {
      const t = line.trim();
      if (t.startsWith('py=')) pyVersion = t.slice(3).trim();
      else if (t.startsWith('torch=')) torchVersion = t.slice(6).trim();
      else if (t.startsWith('deps=')) depsDetail = t.slice(5).trim();
    });
  }

  items.push({
    id: 'python',
    installable: true,
    ok: pyOk,
    detail: pyOk ? (pyVersion || resolved.py) : '',
  });
  items.push({
    id: 'torch',
    installable: true,
    ok: Boolean(torchVersion),
    detail: torchVersion,
    warn: Boolean(torchVersion) && !/\+xpu/.test(torchVersion) ? torchVersion : '',
  });
  items.push({ id: 'deps', installable: true, ok: Boolean(depsDetail), detail: depsDetail });

  return {
    ok: items.every((it) => it.ok),
    python: resolved,
    venvDir: resolved.venv || path.join(appDir, '.venv'),
    items: items,
  };
}

/**
 * 依次安装，onProgress({ id, pct, text, state })：
 *   state: 'run' | 'ok' | 'error'
 * 返回 { ok, error }
 */
async function runInstall(appDir, onProgress) {
  const venvDir = path.join(appDir, '.venv');
  const py = venvPython(venvDir);
  const uv = await hasUv();

  // 进度事件只带「文案代码 + 参数」，具体语言由渲染层决定（中英切换时能重刷），
  // 不要把中文写进事件里 —— 否则切到英文全是中文提示（2026-10-24 实测）。
  const emit = (id, pct, code, arg, state) => {
    try {
      onProgress({
        id: id,
        pct: Math.max(0, Math.min(100, Math.round(pct))),
        code: code || '',
        arg: arg == null ? '' : String(arg).slice(0, 120),
        state: state || 'run',
      });
    } catch (e) { /* 渲染层挂了不该拖垮安装 */ }
  };

  /** 长下载阶段：里程碑之间匀速爬，避免看起来卡死。 */
  function creep(id, from, to, ms, code, arg) {
    let pct = from;
    let last = Date.now();
    const rate = (to - from) / ms;
    emit(id, pct, code, arg);
    const timer = setInterval(() => {
      const now = Date.now();
      pct = Math.min(to, pct + rate * (now - last));
      last = now;
      emit(id, pct, code, arg);
    }, 700);
    return {
      set(next, code2, arg2) {
        if (next > pct) {
          pct = Math.min(to, next);
          emit(id, pct, code2 || code, arg2 == null ? arg : arg2);
          last = Date.now();
        }
      },
      stop() { clearInterval(timer); },
    };
  }

  // ---------- ① Python 运行时 ----------
  if (fs.existsSync(py)) emit('python', 45, 'python.exists');
  else {
    const bar = creep('python', 5, 80, 90000, uv ? 'python.prepare' : 'python.prepareVenv', PY_VERSION);
    let code;
    if (uv) {
      code = await new Promise((resolve) => {
        runStream('uv', ['venv', '--python', PY_VERSION, venvDir], { cwd: appDir }, (line) => {
          bar.set(60, 'uv.line', line);
        }, (c) => resolve(c));
      });
      if (code !== 0) { bar.stop(); emit('python', 0, 'python.uvFail', '', 'error'); return { ok: false, error: 'uv venv failed' }; }
    } else {
      const sysPy = IS_WIN ? 'python' : 'python3';
      const r = await run(sysPy, ['-m', 'venv', venvDir], { cwd: appDir, timeout: 180000 });
      if (r.code !== 0) {
        bar.stop();
        emit('python', 0, 'python.noPython', '', 'error');
        return { ok: false, error: 'no python host' };
      }
      bar.set(70, 'python.created');
    }
    bar.stop();
    if (!fs.existsSync(py)) { emit('python', 0, 'python.noVenvPy', '', 'error'); return { ok: false, error: 'venv python missing' }; }
  }
  const v = await probePy(py, 'import sys;print("%d.%d.%d" % sys.version_info[:3])');
  emit('python', 100, v.ok ? 'python.version' : 'python.ready', v.ok ? v.out : '', 'ok');

  // ---------- ② PyTorch（XPU） ----------
  const have = await probePy(py, 'import torch;print(torch.__version__)');
  if (have.ok) {
    emit('torch', 100, 'torch.version', have.out, 'ok');
  } else {
    const bar = creep('torch', 3, 88, 8 * 60 * 1000, 'torch.start', TORCH_SPEC);
    const argsUv = ['pip', 'install', '--python', py,
      '--index-url', TORCH_INDEX, '--extra-index-url', PYPI_MIRROR,
      '--index-strategy', 'unsafe-best-match',
      'torch==' + TORCH_SPEC];
    const argsPip = ['-m', 'pip', 'install', '--no-input',
      '--index-url', TORCH_INDEX, '--extra-index-url', PYPI_MIRROR,
      'torch==' + TORCH_SPEC];
    const code = await new Promise((resolve) => {
      runStream(uv ? 'uv' : py, uv ? argsUv : argsPip, { cwd: appDir }, (line) => {
        if (/Resolved \d+ package/i.test(line)) bar.set(12, 'torch.resolved');
        else if (/Prepared \d+ package/i.test(line)) bar.set(45, 'torch.prepared');
        else if (/Downloading/i.test(line)) bar.set(20, 'torch.download', line.replace(/^.*Downloading\s*/i, ''));
        else if (/Installing|Installed \d+ package/i.test(line)) bar.set(80, 'torch.install');
        // 只认真正的错误行；uv 的 hardlink 回退是警告，不算失败
        else if (/^\s*(error|ERROR)[:\s]|No matching distribution/i.test(line)) bar.set(88, 'uv.line', line);
      }, (c) => resolve(c));
    });
    bar.stop();
    const after = await probePy(py, 'import torch;print(torch.__version__)');
    if (!after.ok) {
      emit('torch', 0, 'torch.fail', code === 0 ? 'import' : 'exit ' + code, 'error');
      return { ok: false, error: 'torch install failed' };
    }
    emit('torch', 100, 'torch.version', after.out, 'ok');
  }

  // ---------- ③ 基础依赖 ----------
  const depsHave = await probePy(py, 'import numpy,psutil');
  if (depsHave.ok) {
    emit('deps', 100, 'deps.ready', '', 'ok');
  } else {
    emit('deps', 10, 'deps.start');
    const code = await new Promise((resolve) => {
      const args = uv
        ? ['pip', 'install', '--python', py, '--index-url', PYPI_MIRROR].concat(BASE_DEPS)
        : ['-m', 'pip', 'install', '--no-input', '--index-url', PYPI_MIRROR].concat(BASE_DEPS);
      runStream(uv ? 'uv' : py, args, { cwd: appDir }, (line) => {
        if (/Installed|Installing|Prepared/i.test(line)) emit('deps', 80, 'deps.install');
      }, (c) => resolve(c));
    });
    const after = await probePy(py, 'import numpy,psutil;print("ok")');
    if (!after.ok) { emit('deps', 0, 'deps.fail', String(code), 'error'); return { ok: false, error: 'deps install failed' }; }
    emit('deps', 100, 'deps.ready', '', 'ok');
  }

  return { ok: true };
}

module.exports = { scanEnv, runInstall, resolvePython, venvPython, hasUv, TORCH_SPEC, PY_VERSION };
