/* ============================================================
   格式转换（导航键 formatconv，数据中心）
   ------------------------------------------------------------
   用户 2026-10-17：界面照「响应蒸馏 / 网页整理」重制 —— 三个页签：
     模型配置   只有一个模型（转换用的 config.article.writer），框大小与其它页一致，右侧留空
     采集中心   源文件 / 转换设置（左）＋ 运行状态 / 运行日志（右）＋ 按钮行
     数据管理   转换记录表 + 行内预览
   行为与后端接口没变（bridge_api/formatconv.py）：
     GET  /api/formatconv/config         参数 + 模型概要 + 输出目录
     POST /api/formatconv/config/save    保存参数
     POST /api/formatconv/probe          识别源文件
     POST /api/formatconv/start / stop   转换
     GET  /api/formatconv/status?since=  增量事件
     GET  /api/formatconv/runs           转换记录
     GET  /api/formatconv/preview?path=  产物预览
    （2026-10-18：删掉「模型配置」页签与相关接口；转换直接用 AI 窗口那套模型）
   样式直接借 pages/response.css 的 rs-* 类（与另外两页同一套几何/字号）。
   ============================================================ */
(function () {
  'use strict';

var ICONS = {
  save:   '<path d="M5.2 12.8 9.4 17 18.8 7.2"/>',
  sample: '<path d="M7 4.8h7.4L18 8.4v10.8H7Z"/><path d="M9.6 12h6M9.6 15.2h4.2"/>',
  play:   '<path d="M8.6 5.6v12.8L19 12Z" fill="currentColor" stroke="none"/>',
  stop:   '<path d="M10 6.4v11.2"/><path d="M14 6.4v11.2"/>',
  refresh: '<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',   /* 与预训练页同款（用户 2026-10-20） */
  file:   '<path d="M6.6 3.8h6.6L17.6 8v12.2H6.6Z"/><path d="M13.2 3.8V8h4.4"/>'
};

function iconEl(name) {
  var span = document.createElement('span');
  span.className = 'rs-ic';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
    (ICONS[name] || '') + '</svg>';
  return span;
}

/* 运行态（模块级：重挂时清定时器 / 让旧回调闭嘴） */
var run = { timer: null, cursor: 0, running: false, gen: 0 };
/* 参数缓存（用户 2026-10-17 的口径：切页首帧就要有值，不能先空一拍再冒字） */
var PCACHE = { params: null, model: null };

EAIPages.register('formatconv', {
  title: '格式转换',

  mock: {
    '/api/formatconv/config': {
      ok: true,
      params: { src: '', target: 'auto', out_name: '', max_records: 200, batch: 8 },
      model: { model: 'demo-writer', base_url: 'https://api.example.com', ready: true },
      out_dir: 'D:\\...\\data\\corpora',
    },
        '/api/formatconv/runs': { ok: true, items: [] },
    '/api/formatconv/status': { ok: true, exists: false, running: false, events: [], next: 0 },
  },

  mount: function (root, api) {
    if (!document.querySelector('link[data-rs-css], link[href$="pages/response.css"]')) {
      var lk = document.createElement('link');
      lk.rel = 'stylesheet';
      lk.href = 'pages/response.css';
      lk.setAttribute('data-rs-css', '1');
      document.head.appendChild(lk);
    }
    if (run.timer) { clearInterval(run.timer); run.timer = null; }
    run.gen += 1;
    var GEN = run.gen;

    /* 用户 2026-10-18：删掉「模型配置」页签，转换直接用 AI 窗口的模型 */
    var TABS = [['collect', '采集中心'], ['data', '数据管理']];
    var LAST_TAB_KEY = 'eaiLastTab:formatconv';
    function getLastTab() { return (window.__eaiLastTab || {})[LAST_TAB_KEY] || ''; }
    function setLastTab(k) { (window.__eaiLastTab = window.__eaiLastTab || {})[LAST_TAB_KEY] = k; }

    /* ---------- 小工具（与另外两页逐字一致） ---------- */
    function button(label, variant, iconName, onClick) {
      var b = api.el('button', 'btn rs-btn ' + variant);
      b.type = 'button';
      if (iconName) b.appendChild(iconEl(iconName));
      b.appendChild(api.el('span', 'rs-btn-label', label));
      b.addEventListener('click', function () { onClick(b); });
      return b;
    }

    function field(parent, label, opts) {
      opts = opts || {};
      var box = api.el('div', 'rs-field' + (opts.span2 ? ' rs-span2' : ''));
      var lab = api.el('div', 'rs-flabel', label);
      if (opts.labelTitle) lab.title = String(opts.labelTitle);
      box.appendChild(lab);
      var inp;
      if (opts.select) {
        var holder = api.el('input', 'rs-input');
        holder.type = 'text';
        holder.readOnly = true;
        holder.value = opts.value == null
          ? String(((opts.options || [])[0] || [''])[0])
          : String(opts.value);
        inp = api.pick(holder, opts.options, 'rs-input');
      } else {
        inp = api.el(opts.textarea ? 'textarea' : 'input',
                     'rs-input' + (opts.mono ? ' rs-mono' : '') + (opts.textarea ? ' rs-textarea' : ''));
        if (opts.textarea) { inp.rows = opts.rows || 4; } else { inp.type = opts.type || 'text'; }
        inp.value = opts.value == null ? '' : String(opts.value);
      }
      if (opts.readonly) inp.readOnly = true;
      if (opts.secret != null) { api.secretWire(inp); api.secretLock(inp, !!opts.secret); }
      else if (opts.placeholder != null) inp.placeholder = String(opts.placeholder);
      box.appendChild(inp);
      if (opts.note) box.appendChild(api.el('div', 'rs-note', opts.note));
      parent.appendChild(box);
      return inp;
    }

    /* ---------- 页头 + 分段标签 ---------- */
    /* 用户 2026-10-18：去掉标题下的小字说明 */
    root.appendChild(api.hero('格式转换'));

    var segWrap = api.el('div', 'rs-segwrap');
    var seg = api.el('div', 'seg rs-seg');
    seg.setAttribute('role', 'tablist');
    var segOpts = {};
    TABS.forEach(function (t) {
      var b = api.el('button', 'seg-opt', t[1]);
      b.type = 'button';
      b.dataset.value = t[0];
      b.setAttribute('role', 'tab');
      b.addEventListener('click', function () { pickTab(t[0]); });
      segOpts[t[0]] = b;
      seg.appendChild(b);
    });
    segWrap.appendChild(seg);
    root.appendChild(segWrap);

    var panels = api.el('div', 'rs-panels');
    root.appendChild(panels);
    var bodies = {};
    TABS.forEach(function (t) {
      var p = api.el('section', 'rs-panel');
      p.dataset.tab = t[0];
      p.hidden = true;
      panels.appendChild(p);
      bodies[t[0]] = p;
    });

    var LOADED = {};
    function pickTab(key) {
      TABS.forEach(function (t) {
        var on = t[0] === key;
        segOpts[t[0]].classList.toggle('on', on);
        segOpts[t[0]].setAttribute('aria-selected', on ? 'true' : 'false');
        bodies[t[0]].hidden = !on;
      });
      api.segThumb(seg, true);
      if (key === 'collect' && !LOADED.collect) loadCollect();
      if (key === 'data') {
        if (!LOADED.data) loadData();
        else if (DATA.stale) fetchRuns();
      }
      setLastTab(key);
    }

    /* ==================== 采集中心 ==================== */
    var FORM = { src: '', target: 'auto', out_name: '', use_src_name: 0, max_records: 200, batch: 8 };
    var INFO = null;

    function loadCollect() {
      LOADED.collect = true;
      var host = bodies.collect;
      host.innerHTML = '';
      if (run.timer) { clearInterval(run.timer); run.timer = null; }
      run.cursor = 0;
      run.running = false;

      var stack = api.el('div', 'rs-collect');
      var colL = api.el('div', 'rs-collect-col');
      var colR = api.el('div', 'rs-collect-col');

      /* ---- 左：源文件 ---- */
      var cardSrc = api.card('源文件');          /* 去掉卡头小字（用户 2026-10-20） */
      var pickRow = api.el('div', 'rs-srcrow');
      var inPath = api.el('input', 'rs-input rs-mono');
      inPath.type = 'text';
      inPath.readOnly = true;
      /* 「选择」菜单（用户 2026-10-21）：选择文件 / 选择文件夹；地址栏不留提示小字 */
      var menuEl = null;
      function closeMenu() {
        if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
        menuEl = null;
        document.removeEventListener('mousedown', onMenuAway, true);
        document.removeEventListener('keydown', onMenuKey, true);
      }
      function onMenuAway(e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }
      function onMenuKey(e) { if (e.key === 'Escape') closeMenu(); }
      function openMenu() {
        if (menuEl) { closeMenu(); return; }
        menuEl = api.el('div', 'fc-menu');
        [['选择文件', pickFile], ['选择文件夹', pickFolder]].forEach(function (it) {
          var b = api.el('button', 'fc-menu-item', it[0]);
          b.type = 'button';
          b.addEventListener('click', function () { closeMenu(); it[1](); });
          menuEl.appendChild(b);
        });
        document.body.appendChild(menuEl);
        var r = btnPick.getBoundingClientRect();
        menuEl.style.left = Math.round(r.left) + 'px';
        menuEl.style.top = Math.round(r.bottom + 6) + 'px';
        document.addEventListener('mousedown', onMenuAway, true);
        document.addEventListener('keydown', onMenuKey, true);
      }
      function pickFile() {
        if (run.running) { setStatus('转换中，先停止再换文件', 'rs-warn'); return; }
        Promise.resolve(window.eai && window.eai.pickFile ? window.eai.pickFile({
          title: '选择要转换的数据集文件',
          buttonLabel: '选择',
          multi: false,
          filters: [
            { name: '数据集文件', extensions: ['json', 'jsonl', 'ndjson', 'txt'] },
            { name: '全部文件', extensions: ['*'] },
          ],
        }) : null).then(function (paths) {
          if (!paths || !paths.length) return;
          inPath.value = paths[0];
          FORM.src = paths[0];
          probe();
          if (srcNameBox.checked) checkSrcName();
        });
      }
      function pickFolder() {
        if (run.running) { setStatus('转换中，先停止再换文件', 'rs-warn'); return; }
        Promise.resolve(window.eai && window.eai.pickFolder ? window.eai.pickFolder({
          title: '选择包含数据集的文件夹',
          buttonLabel: '选择',
        }) : null).then(function (dir) {
          if (!dir) return;
          inPath.value = dir;
          FORM.src = dir;
          probe();
          if (srcNameBox.checked) checkSrcName();
        });
      }
      var btnPick = button('选择', 'rs-btn-soft', 'file', openMenu);
      pickRow.appendChild(inPath);
      pickRow.appendChild(btnPick);
      cardSrc.body.appendChild(pickRow);

      var factBox = api.el('div', 'rs-facts fc-facts');   /* fc-facts：空态整块收掉，不留小字 */
      cardSrc.body.appendChild(factBox);
      var sampleBox = api.el('pre', 'rs-preview');
      cardSrc.body.appendChild(sampleBox);

      function fact(k, v, cls) {
        var row = api.el('div', 'rs-fact');
        row.appendChild(api.el('span', 'rs-fact-k', k));
        row.appendChild(api.el('span', 'rs-fact-v' + (cls ? ' ' + cls : ''), v));
        factBox.appendChild(row);
      }

      function renderInfo() {
        factBox.textContent = '';
        if (!INFO) {
          sampleBox.textContent = '';          /* 空态不留小字（用户 2026-10-21） */
          setStats();
          return;
        }
        var kind = INFO.local_kind === 'sft' ? '问答（可直接读）'
          : (INFO.local_kind === 'pretrain' ? '纯文本（可直接读）'
          : (INFO.local_kind === 'mixed' ? '混合（逐个文件各自判定）' : '认不出来 —— 走模型转换'));
        fact('格式判定', kind, INFO.local_kind === 'none' ? 'warn' : 'ok');
        if (INFO.files) fact('文件数', INFO.files + ' 个');
        fact('记录数', INFO.records + ' 条 · ' + (INFO.size_bytes / 1024).toFixed(1) + ' KB');
        fact('本地能解析', '问答 ' + INFO.pairs + ' 条 · 文本 ' + INFO.texts + ' 条');
        if (INFO.issues && INFO.issues.length) fact('提示', INFO.issues.join('；'), 'warn');
        var lines = (INFO.samples || []).slice(0, 2).map(function (s, i) { return (i + 1) + '. ' + s; });
        sampleBox.textContent = lines.length ? lines.join('\n') : '';
        setStats();
      }
      colL.appendChild(cardSrc);

      /* ---- 左：转换设置 ---- */
      var cardSet = api.card('转换设置');        /* 去掉卡头小字（用户 2026-10-20） */
      var form = api.el('div', 'rs-grid2');
      var fTarget = field(form, '目标格式', {
        span2: true, select: true, value: 'auto',
        options: [['auto', '自动识别'], ['sft', '问答 · 指令微调（jsonl）'],
                  ['pretrain', '纯文本 · 预训练（txt）']]
      });
      /* 输出文件名右侧「使用源文件名」勾选（用户 2026-10-21）：删掉“留空 = 用源文件名”兜底 */
      var inName = field(form, '输出文件名', { span2: true });
      var nameRow = api.el('div', 'fc-name-row');
      inName.parentNode.insertBefore(nameRow, inName);
      nameRow.appendChild(inName);
      var srcNameWrap = api.el('label', 'rs-check fc-srcname');
      var srcNameBox = api.el('input', '');
      srcNameBox.type = 'checkbox';
      srcNameWrap.appendChild(srcNameBox);
      srcNameWrap.appendChild(api.el('span', 'rs-check-label', '使用源文件名'));
      nameRow.appendChild(srcNameWrap);
      var inMax = field(form, '最多转换条数', { type: 'number', value: 200 });
      var inBatch = field(form, '每批条数', { type: 'number', value: 8 });
      cardSet.body.appendChild(form);
      colL.appendChild(cardSet);

      /* 先建框、用缓存填一次（防闪）；/config 回来写同一份值不再变 */
      function fillParams(p) {
        p = p || {};
        FORM = {
          src: p.src || '', target: p.target || 'auto', out_name: p.out_name || '',
          use_src_name: p.use_src_name || 0,
          max_records: p.max_records || 200, batch: p.batch || 8
        };
        inPath.value = FORM.src;
        fTarget.value = FORM.target;
        inName.value = FORM.out_name;
        srcNameBox.checked = !!FORM.use_src_name;
        inName.disabled = !!FORM.use_src_name;
        inMax.value = FORM.max_records;
        inBatch.value = FORM.batch;
      }
      fillParams(PCACHE.params);

      fTarget.addEventListener('change', function () { FORM.target = fTarget.value; probe(); });
      inName.addEventListener('input', function () { FORM.out_name = inName.value.trim(); });
      inMax.addEventListener('input', function () { FORM.max_records = parseInt(inMax.value, 10) || 200; });
      inBatch.addEventListener('input', function () { FORM.batch = parseInt(inBatch.value, 10) || 8; });

      /* 「使用源文件名」重名检查（用户 2026-10-21）：重名 → 弹窗警告并取消勾选 */
      function checkSrcName() {
        return api.post('/api/formatconv/name_check', { src: FORM.src }).then(function (r) {
          if (!r || r.ok === false) return [];
          var dups = r.dups || [];
          if (dups.length) {
            api.alertBox({ title: '输出文件名重复',
              message: '按源文件名输出会和已有产物重名：' + dups.join('、') + '。已取消勾选「使用源文件名」。' });
            srcNameBox.checked = false;
            FORM.use_src_name = 0;
            inName.disabled = false;
          }
          return dups;
        }).catch(function () { return []; });
      }
      srcNameBox.addEventListener('change', function () {
        FORM.use_src_name = srcNameBox.checked ? 1 : 0;
        inName.disabled = srcNameBox.checked;
        if (srcNameBox.checked && FORM.src) checkSrcName();
      });

      /* ---- 右：运行状态 + 日志 ---- */
      var statCard = api.card('运行状态');       /* 去掉卡头小字（用户 2026-10-20） */
      var track = api.el('div', 'rs-progress');
      var fill = api.el('div', 'rs-progress-fill');
      track.appendChild(fill);
      statCard.body.appendChild(track);
      var plabel = api.el('div', 'rs-progress-label', '尚未开始');
      statCard.body.appendChild(plabel);
      var factLine = api.el('div', 'rs-facts rs-stats-mini');
      statCard.body.appendChild(factLine);
      colR.appendChild(statCard);

      var logCard = api.card('运行日志', '');
      var logEl = api.el('div', 'rs-log');
      logCard.body.appendChild(logEl);
      colR.appendChild(logCard);

      /* 用户 2026-10-21：不要最底下的小字 —— 底部状态行整个撤掉；
         失败/错误都走运行日志，成功回执静默。setStatus 保留空壳吃掉调用点。 */
      function setStatus() { }

      var STATS = { records: null, done: 0, failed: 0, out: null, cost: null };
      function setStats() {
        factLine.textContent = '';
        var pairs = [
          ['记录数', STATS.records == null ? '—' : STATS.records],
          ['已转换', STATS.done == null ? '—' : STATS.done],
          ['失败', STATS.failed == null ? '—' : STATS.failed],
          ['产物', STATS.out == null ? '—' : STATS.out],
          ['耗时', STATS.cost == null ? '—' : STATS.cost]
        ];
        pairs.forEach(function (p) {
          var row = api.el('div', 'rs-fact');
          row.appendChild(api.el('span', 'rs-fact-k', p[0]));
          row.appendChild(api.el('span', 'rs-fact-v' + (p[1] === '—' ? ' idle' : ''), String(p[1])));
          factLine.appendChild(row);
        });
        if (STATS.records) {
          var pct = Math.max(0, Math.min(1, (Number(STATS.done) || 0) / Number(STATS.records)));
          fill.style.width = (pct * 100).toFixed(1) + '%';
          plabel.textContent = '已转换 ' + (STATS.done || 0) + '/' + STATS.records +
            (STATS.failed ? ' · 失败 ' + STATS.failed : '');
        } else {
          fill.style.width = '0%';
          plabel.textContent = '尚未开始';
        }
      }

      function logLine(text, cls) {
        if (text == null || text === '') return;
        var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 8;
        logEl.appendChild(api.el('span', 'rs-log-line' + (cls ? ' ' + cls : ''), String(text)));
        while (logEl.children.length > 400) logEl.removeChild(logEl.firstChild);
        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
      }

      /* ---- 按钮行 ---- */
      var actions = api.el('div', 'rs-row rs-actions');
      var startBtn = button('开始转换', 'rs-btn-accent', 'play', function () {
        if (run.running) return;
        if (!FORM.src) { setStatus('先选文件', 'rs-err'); return; }
        if (!FORM.use_src_name && !FORM.out_name) {
          setStatus('请填输出文件名，或勾选「使用源文件名」', 'rs-err');
          return;
        }
        var guard = FORM.use_src_name ? checkSrcName() : Promise.resolve([]);
        guard.then(function (dups) {
        if (dups.length) {
          setStatus('输出文件名重复，已取消勾选「使用源文件名」', 'rs-err');
          return;
        }
        run.cursor = 0;
        logEl.innerHTML = '';
        STATS = { records: INFO ? INFO.records : null, done: 0, failed: 0, out: null, cost: null };
        setStats();
        setRunningUI(true);
        setStatus('转换中 …');
        api.post('/api/formatconv/start', {
          src: FORM.src, target: FORM.target, out_name: FORM.out_name,
          use_src_name: FORM.use_src_name,
          max_records: FORM.max_records, batch: FORM.batch,
        }).then(function (r) {
          if (GEN !== run.gen) return;
          if (!r || r.ok === false || r.error) {
            setRunningUI(false);
            setStatus('启动失败：' + ((r && r.error) || '未知错误'), 'rs-err');
            logLine('[启动] ' + ((r && r.error) || '失败'), 'warn');
            return;
          }
          logLine('已启动：' + (r.out_name || '') + ' → ' + (r.path || ''));
          startPolling();
          pollOnce();
        });
        });
      });
      var stopBtn = button('停止', 'rs-btn-soft', 'stop', function () {
        stopBtn.disabled = true;
        api.post('/api/formatconv/stop', {}).then(function (r) {
          if (GEN !== run.gen) return;
          setStatus((r && r.note) || '已请求停止');
        });
      });
      var saveBtn = button('保存参数', 'rs-btn-soft', 'save', function () {
        api.post('/api/formatconv/config/save', {
          src: FORM.src, target: FORM.target, out_name: FORM.out_name,
          use_src_name: FORM.use_src_name,
          max_records: FORM.max_records, batch: FORM.batch,
        }).then(function (r) {
          if (GEN !== run.gen) return;
          if (!r || r.ok === false || r.error) {
            logLine('[保存] 失败：' + ((r && r.error) || '未知错误'), 'warn');
            setStatus('保存失败：' + ((r && r.error) || '未知错误'), 'rs-err'); return; }
          PCACHE.params = {
            src: FORM.src, target: FORM.target, out_name: FORM.out_name,
            max_records: FORM.max_records, batch: FORM.batch
          };
          setStatus('');
        });
      });
      actions.appendChild(startBtn);
      actions.appendChild(stopBtn);
      actions.appendChild(saveBtn);

      stack.appendChild(colL);
      stack.appendChild(colR);
      host.appendChild(stack);
      host.appendChild(actions);

      function setRunningUI(on) {
        run.running = !!on;
        startBtn.disabled = run.running;
        stopBtn.disabled = !run.running;
        btnPick.disabled = run.running;
      }
      setRunningUI(false);
      renderInfo();

      /* ---- 探测 / 轮询 ---- */
      function probe() {
        if (!FORM.src) return Promise.resolve(null);
        return api.post('/api/formatconv/probe', {
          path: FORM.src, target: FORM.target, max_records: FORM.max_records,
        }).then(function (r) {
          if (GEN !== run.gen) return null;
          if (!r || r.ok === false) {
            INFO = null;
            renderInfo();
            setStatus((r && r.error) || '识别失败', 'rs-err');
            logLine('[识别] ' + ((r && r.error) || '失败'), 'warn');
            return null;
          }
          INFO = r.info || null;
          renderInfo();
          if (INFO) {
            setStatus(INFO.local_kind === 'none' ? '这份要交给模型转换' : '本地可直接读，不花 token',
              INFO.local_kind === 'none' ? 'rs-warn' : '');
            logLine('[识别] ' + INFO.records + ' 条记录 · 本地解析 问答 '
              + INFO.pairs + ' / 文本 ' + INFO.texts
              + ' · ' + (INFO.local_kind === 'none' ? '需要模型转换' : '本地可直读'));
          }
          return INFO;
        });
      }

      function handleEvent(ev) {
        var p = ev.payload || {};
        if (ev.kind === 'log') {
          logLine(p.text || '', p.level === 'err' ? 'warn'
            : (p.level === 'warn' ? 'warn' : (p.level === 'ok' ? '' : 'dim')));
        } else if (ev.kind === 'progress') {
          STATS.done = p.done == null ? STATS.done : p.done;
          if (p.total) STATS.records = p.total;
          setStats();
          if (p.batch && p.batches) setStatus('转换中 … 第 ' + p.batch + '/' + p.batches + ' 批');
        } else if (ev.kind === 'result') {
          STATS.out = (p.pairs || p.chars || 0) + (p.target === 'pretrain' ? ' 字' : ' 条');
          STATS.failed = p.failed || 0;
          STATS.cost = ((p.elapsed_ms || 0) / 1000).toFixed(1) + ' s';
          setStats();
          setStatus('完成 · ' + (p.mode === 'ai' ? '模型转换' : '本地直读')
            + ' · ' + (p.target === 'pretrain' ? '纯文本' : '问答'));
          DATA.stale = true;
        } else if (ev.kind === 'error') {
          logLine('[错误] ' + (p.text || p.message || 'error'), 'warn');
          setStatus('失败', 'rs-err');
        }
      }

      function pollOnce() {
        return api.get('/api/formatconv/status?since=' + run.cursor).then(function (d) {
          if (GEN !== run.gen) return;
          if (!d || d.error) { logLine('[状态] ' + ((d && d.error) || '未知错误'), 'warn'); return; }
          (d.events || []).forEach(handleEvent);
          if (typeof d.next === 'number') run.cursor = d.next;
          if (d.running) {
            setRunningUI(true);
          } else {
            setRunningUI(false);
            if (run.timer) { clearInterval(run.timer); run.timer = null; }
          }
        });
      }

      function startPolling() {
        if (run.timer) return;
        run.timer = setInterval(function () {
          pollOnce().then(function () {
            if (!run.running && run.timer) { clearInterval(run.timer); run.timer = null; }
          });
        }, 800);
      }

      api.get('/api/formatconv/config').then(function (d) {
        if (GEN !== run.gen) return;
        if (!d || d.error) {
          logLine('[配置] 读取失败：' + ((d && d.error) || '未知错误'), 'warn');
          setStatus('配置读取失败：' + ((d && d.error) || '未知错误'), 'rs-err');
          return;
        }
        PCACHE.params = d.params || {};
        fillParams(PCACHE.params);
        setStatus('');
        if (FORM.src) probe();
        pollOnce().then(function () {
          if (GEN !== run.gen) return;
          if (run.running && !run.timer) startPolling();
        });
      });
    }

    /* ==================== 数据管理 ==================== */
    var DATA = { items: [], q: '', stale: true, open: {} };

    function fmtDate(it) {
      var at = it.at || '';
      if (at && /\d{4}-\d{2}-\d{2}/.test(at)) return at.slice(0, 10);
      return '—';
    }

    function loadData() {
      LOADED.data = true;
      var host = bodies.data;
      host.innerHTML = '';
      var stack = api.el('div', 'stack');

      var card = api.card('');
      var cardHead = card.querySelector('.card-head');
      if (cardHead) cardHead.remove();

      var toolbar = api.el('div', 'rs-toolbar');
      var status = api.el('p', 'rs-status');
      toolbar.appendChild(button('刷新', 'rs-btn-ghost', 'refresh', function () { fetchRuns(); }));
      var rightWrap = api.el('span', 'rs-row rs-right');
      var search = api.el('input', 'rs-search');
      search.type = 'text';
      search.placeholder = '搜索';
      search.value = DATA.q;
      search.addEventListener('input', function () {
        DATA.q = search.value.trim();
        renderRows();
      });
      rightWrap.appendChild(search);
      toolbar.appendChild(rightWrap);
      card.body.appendChild(toolbar);

      var wrap = api.el('div', 'rs-tablewrap');
      var table = api.el('table', 'rs-table');
      var thead = api.el('thead');
      var htr = api.el('tr');
      [['col-fname', '文件名'], ['col-count', '条数'], ['col-title', '方式'], ['col-date', '日期']]
        .forEach(function (h) { htr.appendChild(api.el('th', h[0], h[1])); });
      thead.appendChild(htr);
      table.appendChild(thead);
      var tbody = api.el('tbody');
      table.appendChild(tbody);
      wrap.appendChild(table);
      card.body.appendChild(wrap);

      var pv = api.el('div', 'rs-pv-box');
      pv.hidden = true;
      card.body.appendChild(pv);

      stack.appendChild(card);
      host.appendChild(stack);

      function renderRows() {
        tbody.textContent = '';
        var q = DATA.q.toLowerCase();
        var items = (DATA.items || []).filter(function (it) {
          if (!q) return true;
          return String(it.name || '').toLowerCase().indexOf(q) >= 0;
        });
        if (!items.length) {
          var tr = api.el('tr');
          var td = api.el('td', 'rs-empty', DATA.items.length ? '没有匹配的文件' :
            '还没有产物：选一个下载来的 json/jsonl，点「开始转换」。');
          td.colSpan = 4;
          tr.appendChild(td);
          tbody.appendChild(tr);
          return;
        }
        items.forEach(function (it) {
          var tr = api.el('tr');
          tr.appendChild(api.el('td', 'col-fname', it.name || ''));
          tr.appendChild(api.el('td', 'col-count',
            (it.target === 'pretrain' ? (it.chars || 0) + ' 字' : (it.pairs || 0) + ' 条')));
          tr.appendChild(api.el('td', 'col-title', it.mode === 'ai' ? '模型转换' : '本地直读'));
          tr.appendChild(api.el('td', 'col-date', fmtDate(it)));
          tr.style.cursor = 'pointer';
          tr.addEventListener('click', function () { togglePreview(it, tr); });
          tbody.appendChild(tr);
        });
      }

      function togglePreview(it, tr) {
        if (!it.exists) {
          status.className = 'rs-status rs-warn';
          status.textContent = '产物文件不在了：' + (it.out || '');
          return;
        }
        pv.hidden = false;
        pv.textContent = '读取中 …';
        api.get('/api/formatconv/preview?path=' + encodeURIComponent(it.out)).then(function (r) {
          if (!r || r.error) {
            status.className = 'rs-status rs-err';
            status.textContent = '预览失败：' + ((r && r.error) || '未知错误');
            pv.hidden = true;
            return;
          }
          status.textContent = '';
          pv.textContent = (r.text || '') + (r.truncated ? '\n\n……（已截断，完整内容看文件）' : '');
        });
      }

      function fetchRuns() {
        return api.get('/api/formatconv/runs').then(function (d) {
          if (GEN !== run.gen) return;
          DATA.items = (d && d.items) || [];
          DATA.stale = false;
          renderRows();
          pv.hidden = true;
        });
      }
      DATA.reload = fetchRuns;
      renderRows();
      fetchRuns();
    }

    /* ---------- 首次进来：落到上次的页签（默认采集中心） ---------- */
    var first = getLastTab() || 'collect';
    pickTab(first in bodies ? first : 'collect');
  },

  unmount: function () {
    if (run.timer) { clearInterval(run.timer); run.timer = null; }
    run.cursor = 0;
  },
});
})();
