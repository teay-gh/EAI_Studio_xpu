'use strict';

/**
 * EAI Studio · 安装页逻辑（2026-10-24）
 *
 * 用户口径（2026-10-24 第二轮）：
 *   · 页面上不要任何小字 —— 副标题、每行说明、底部提示、标题栏后缀都去掉，
 *     只保留「正在下载 / 正在安装 / 失败原因」这类实时状态；
 *   · 语言段选放到「开始安装」同一行的左侧，样式与设置页「语言」卡片一致（.seg + 蓝色滑块）；
 *   · 进度与日志文案全双语，切语言时连正在显示的那行和整段日志一起翻；
 *   · 输出框不显示滚动条。
 */

(function () {
  var MSG = {
    zh: {
      'python.prepare': '正在准备 Python {0}',
      'python.prepareVenv': '正在创建虚拟环境',
      'python.exists': '虚拟环境已存在，检查版本',
      'python.version': 'Python {0}',
      'python.created': '虚拟环境已创建',
      'python.ready': 'Python 就绪',
      'python.uvFail': 'uv 创建虚拟环境失败',
      'python.noPython': '没有可用的 Python，请先安装 Python 3.13 或 uv',
      'python.noVenvPy': '虚拟环境里没有 python',
      'torch.start': '正在下载 PyTorch {0}（约 2 GB）',
      'torch.resolved': '已解析依赖',
      'torch.prepared': '已下载核心包',
      'torch.download': '正在下载：{0}',
      'torch.install': '正在安装',
      'torch.version': 'PyTorch {0}',
      'torch.fail': 'PyTorch 安装失败（{0}）',
      'deps.start': '正在安装 numpy / psutil',
      'deps.install': '正在安装',
      'deps.ready': '已就绪',
      'deps.fail': '依赖安装失败（退出码 {0}）',
      'uv.line': '{0}',
    },
    en: {
      'python.prepare': 'Preparing Python {0}',
      'python.prepareVenv': 'Creating the virtual environment',
      'python.exists': 'Virtual environment found — checking version',
      'python.version': 'Python {0}',
      'python.created': 'Virtual environment created',
      'python.ready': 'Python ready',
      'python.uvFail': 'uv failed to create the virtual environment',
      'python.noPython': 'No usable Python found — install Python 3.13 or uv first',
      'python.noVenvPy': 'No python inside the virtual environment',
      'torch.start': 'Downloading PyTorch {0} (~2 GB)',
      'torch.resolved': 'Dependencies resolved',
      'torch.prepared': 'Core wheel downloaded',
      'torch.download': 'Downloading: {0}',
      'torch.install': 'Installing',
      'torch.version': 'PyTorch {0}',
      'torch.fail': 'PyTorch install failed ({0})',
      'deps.start': 'Installing numpy / psutil',
      'deps.install': 'Installing',
      'deps.ready': 'Ready',
      'deps.fail': 'Package install failed (exit {0})',
      'uv.line': '{0}',
    },
  };

  var T = {
    zh: {
      title: '安装运行环境',
      scanning: '正在检测环境 …',
      install: '开始安装',
      installing: '安装中…',
      enter: '进入 EAI',
      retry: '重试',
      stateIdle: '待安装',
      stateOk: '已完成',
      stateErr: '失败',
      stateSkip: '已就绪',
      warnNoXpu: '不是 XPU 构建，训练会退回 CPU',
      rows: {
        electron: { name: 'Electron', req: '' },
        python: { name: 'Python', req: '3.13' },
        torch: { name: 'PyTorch (Intel XPU)', req: '2.14.1+xpu' },
        deps: { name: '基础依赖', req: 'numpy / psutil' },
      },
    },
    en: {
      title: 'Set up the runtime environment',
      scanning: 'Scanning environment …',
      install: 'Install',
      installing: 'Installing…',
      enter: 'Open EAI',
      retry: 'Retry',
      stateIdle: 'Pending',
      stateOk: 'Done',
      stateErr: 'Failed',
      stateSkip: 'Ready',
      warnNoXpu: 'not an XPU build — training falls back to CPU',
      rows: {
        electron: { name: 'Electron', req: '' },
        python: { name: 'Python', req: '3.13' },
        torch: { name: 'PyTorch (Intel XPU)', req: '2.14.1+xpu' },
        deps: { name: 'Base packages', req: 'numpy / psutil' },
      },
    },
  };

  var ORDER = ['electron', 'python', 'torch', 'deps'];
  var lang = document.documentElement.getAttribute('data-lang') === 'en' ? 'en' : 'zh';
  var scan = null;
  var busy = false;
  var done = false;
  var rows = {};                    // id → { row, name, req, desc, fill, pct, state, num, skip, code, arg, warn }
  var logItems = [];                // [{ time, id, code, arg }]

  var $ = function (id) { return document.getElementById(id); };
  var t = function () { return T[lang]; };

  /** 文案代码 → 当前语言文本；{0} 用参数替换。 */
  function txt(code, arg) {
    var m = MSG[lang][code];
    if (!m) return arg ? String(arg).slice(0, 120) : '';
    return String(m).replace('{0}', arg == null ? '' : arg);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ---------- 语言段选：把蓝色滑块挪到选中项下面（与设置页同一套观感） ---------- */
  function placeThumb(animate) {
    var seg = $('langSeg');
    if (!seg) return;
    var thumb = seg.querySelector('.seg-thumb');
    var on = seg.querySelector('.seg-opt.on');
    if (!thumb || !on) return;
    if (!animate) thumb.classList.add('seg-thumb-init');
    thumb.style.width = on.offsetWidth + 'px';
    thumb.style.transform = 'translateX(' + on.offsetLeft + 'px)';
    seg.classList.add('seg-has-thumb');
    // styles.css 里滑块默认 opacity:0，只有量准宽度、带上 data-seg-fit 才显示
    seg.setAttribute('data-seg-fit', lang);
    if (!animate) {
      requestAnimationFrame(function () { thumb.classList.remove('seg-thumb-init'); });
    }
  }

  /* ---------- 渲染 ---------- */
  function buildRows() {
    var list = $('suList');
    list.innerHTML = '';
    rows = {};
    ORDER.forEach(function (id) {
      var row = el('div', 'su-row idle');
      row.setAttribute('data-env', id);

      var main = el('div', 'su-row-main');
      var nameLine = el('div', 'su-name');
      nameLine.appendChild(el('span', 'su-name-text', ''));
      nameLine.appendChild(el('span', 'su-req', ''));
      var desc = el('div', 'su-desc', '');
      main.appendChild(nameLine);
      main.appendChild(desc);

      var prog = el('div', 'su-prog');
      var bar = el('div', 'su-bar');
      var fill = document.createElement('i');
      bar.appendChild(fill);
      var pct = el('span', 'su-pct', '');
      prog.appendChild(bar);
      prog.appendChild(pct);

      row.appendChild(main);
      row.appendChild(prog);
      list.appendChild(row);

      rows[id] = {
        row: row,
        name: nameLine.querySelector('.su-name-text'),
        req: nameLine.querySelector('.su-req'),
        desc: desc,
        fill: fill,
        pct: pct,
        state: 'idle',
        num: 0,
        skip: false,
        code: '',
        arg: '',
        warn: '',
      };
    });
  }

  function pctText(r) {
    if (r.state === 'ok') return r.skip ? t().stateSkip : t().stateOk;
    if (r.state === 'error') return t().stateErr;
    if (r.state === 'run') return r.num + '%';
    return t().stateIdle;
  }

  function setRow(id, state, pct, code, arg) {
    var r = rows[id]; if (!r) return;
    r.state = state;
    r.row.classList.remove('idle', 'run', 'ok', 'error');
    r.row.classList.add(state === 'error' ? 'error' : state);
    var p = typeof pct === 'number' ? pct : (state === 'ok' ? 100 : 0);
    r.num = p;
    r.fill.style.width = p + '%';
    r.pct.textContent = pctText(r);
    if (code) { r.code = code; r.arg = arg; r.warn = ''; }
    // 装完就把那行「临时状态」收掉：页面上只留进行中的状态，不留静态小字
    if (state === 'ok') { r.code = ''; r.desc.textContent = r.warn || ''; return; }
    r.desc.textContent = r.warn ? r.warn : (r.code ? txt(r.code, r.arg) : '');
  }

  function updateFoot() {
    var btn = $('suGo');
    if (busy) {
      btn.disabled = true;
      btn.textContent = t().installing;
      return;
    }
    if (done) {
      btn.disabled = false;
      btn.textContent = t().enter;
      return;
    }
    var missing = scan ? scan.items.filter(function (i) { return !i.ok && i.installable; }).length : 0;
    var failed = ORDER.some(function (id) { return rows[id] && rows[id].state === 'error'; });
    if (failed) {
      btn.disabled = false;
      btn.textContent = t().retry;
      return;
    }
    btn.disabled = missing === 0 && !scan;
    btn.textContent = t().install;
  }

  function paintLabels() {
    $('suTitle').textContent = t().title;
    Array.prototype.forEach.call(document.querySelectorAll('#langSeg .seg-opt'), function (b) {
      var on = b.getAttribute('data-lang') === lang;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    document.documentElement.setAttribute('data-lang', lang);
    document.documentElement.setAttribute('lang', lang === 'en' ? 'en-US' : 'zh-CN');

    ORDER.forEach(function (id) {
      var r = rows[id]; if (!r) return;
      var meta = t().rows[id];
      r.name.textContent = meta.name;
      r.req.textContent = meta.req;
      r.req.hidden = !meta.req;                 // 没有版本要求就不显示药丸（2026-10-24）
      r.desc.textContent = r.warn ? r.warn : (r.code ? txt(r.code, r.arg) : '');
      r.pct.textContent = pctText(r);
    });
    rebuildLog();
    updateFoot();
    placeThumb(false);
  }

  /* ---------- 日志（输出框） ---------- */
  function logPush(id, code, arg) {
    var last = logItems[logItems.length - 1];
    // 进度爬升每 0.7s 推一次同一行，别把日志刷满
    if (last && last.id === id && last.code === code && last.arg === arg) return;
    var d = new Date();
    var hh = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
    logItems.push({ time: hh, id: id, code: code, arg: arg });
    while (logItems.length > 400) logItems.shift();
    rebuildLog();
  }

  function rebuildLog() {
    var box = $('suDetailBody');
    if (!box || box.hidden) return;
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    box.textContent = '';
    logItems.forEach(function (it) {
      var line = document.createElement('div');
      var name = (t().rows[it.id] || {}).name || it.id;
      line.textContent = '[' + it.time + '] ' + name + ' · ' + txt(it.code, it.arg);
      box.appendChild(line);
    });
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  /* ---------- 扫描 / 安装 ---------- */
  async function doScan() {
    $('suList').innerHTML = '<div class="su-loading" id="suLoading">' + t().scanning + '</div>';
    try {
      scan = await window.eai.setup.scan();
    } catch (e) {
      scan = null;
      $('suList').innerHTML = '<div class="su-loading">' + String((e && e.message) || e) + '</div>';
      return;
    }
    buildRows();
    scan.items.forEach(function (it) {
      if (!rows[it.id]) return;
      var r = rows[it.id];
      r.skip = it.installable === false;
      setRow(it.id, it.ok ? 'ok' : 'idle', it.ok ? 100 : 0);
      r.code = '';
      r.desc.textContent = '';
      if (it.ok && it.warn) { r.warn = it.warn + '（' + t().warnNoXpu + '）'; r.desc.textContent = r.warn; }
    });
    paintLabels();
    if (scan.ok) { done = true; updateFoot(); }
  }

  async function doInstall() {
    busy = true; done = false;
    var box = $('suDetailBody');
    box.hidden = false;
    ORDER.forEach(function (id) {
      if (rows[id] && rows[id].state !== 'ok') setRow(id, 'idle', 0);
    });
    updateFoot();
    var off = window.eai.setup.onProgress(function (p) {
      if (!p || !rows[p.id]) return;
      setRow(p.id, p.state === 'ok' ? 'ok' : p.state === 'error' ? 'error' : 'run', p.pct, p.code, p.arg);
      if (p.code) logPush(p.id, p.code, p.arg);
    });
    var res;
    try {
      res = await window.eai.setup.start();
    } catch (e) {
      res = { ok: false, error: String((e && e.message) || e) };
    }
    if (off) off();
    busy = false;
    if (res && res.ok) {
      done = true;
      updateFoot();
      setTimeout(function () { window.eai.setup.enter(); }, 900);
    } else {
      logPush('torch', 'torch.fail', (res && res.error) || 'unknown');
      updateFoot();
    }
  }

  /* ---------- 事件 ---------- */
  window.addEventListener('DOMContentLoaded', function () {
    buildRows();
    paintLabels();
    Array.prototype.forEach.call(document.querySelectorAll('#langSeg .seg-opt'), function (b) {
      b.addEventListener('click', function () {
        var next = b.getAttribute('data-lang');
        if (next === lang) return;
        lang = next;
        try { localStorage.setItem('eaiLang', lang); } catch (e) { /* ignore */ }
        paintLabels();
      });
    });
    $('suGo').addEventListener('click', function () {
      if (busy) return;
      if (done) { window.eai.setup.enter(); return; }
      doInstall();
    });
    $('winMin').addEventListener('click', function () { window.eai.win.minimize(); });
    $('winMax').addEventListener('click', function () { window.eai.win.toggleMax(); });
    $('winClose').addEventListener('click', function () { window.eai.win.close(); });
    window.eai.onMaxChange(function (s) {
      var b = $('winMax');
      if (!b) return;
      b.classList.toggle('restore', Boolean(s && s.maximized));
    });
    window.addEventListener('resize', function () { placeThumb(true); });
    doScan();
  });
})();
