/* ============================================================
   模型转换（「模型」分组，排在「导出模型」上面）—— 用户 2026-10-18

   后端 bridge_api/model_convert.py：
     GET  /api/modelconv/config  → {project, project_ready, checkpoints}
     POST /api/modelconv/probe   → {format, target, tensors, params, has_vocab, can_project, …}
     POST /api/modelconv/start   → {ok, out, target}   后台线程真转换
     GET  /api/modelconv/status?since= → {running, events:[{kind,payload}], next}
     POST /api/modelconv/stop

   规则：safetensors ⇄ pt 双向；输出二选一（另存文件夹 / 存进 EAI 当前工程 checkpoints/）；
         只有 safetensors → .pt 能存进 EAI；没有工程时页面照常可用，只是「保存到 EAI 权重」不可选。
   ============================================================ */
var MC_STATE = { since: 0, timer: null };

EAIPages.register('model_convert', {
  title: '模型转换',
  mock: {
    '/api/modelconv/config': { ok: true, project: '', project_ready: false, checkpoints: '', home: '（预览）' },
    '/api/modelconv/probe': {
      ok: true, path: '（预览）\\model.safetensors', name: 'model.safetensors',
      format: 'safetensors', target: 'pt', bytes: 6710886, size_mb: 6.4,
      tensors: 58, params: 1678984, has_vocab: true, has_args: true,
      can_project: true, project_note: '',
    },
    '/api/modelconv/start': { ok: true, out: '（预览）\\checkpoints\\model.pt', target: 'pt' },
    '/api/modelconv/status': { ok: true, exists: true, running: false, events: [], next: 0 },
  },

  mount(root, api) {
    root.appendChild(api.hero('模型转换'));

    var S = {
      cfg: null, src: '', info: null, mode: 'folder',
      running: false, out: '', dot: null,
    };

    var grid = api.el('div', 'mc-grid');
    var colL = api.el('div', 'mc-col');
    var colR = api.el('div', 'mc-col');
    grid.appendChild(colL);
    grid.appendChild(colR);
    root.appendChild(grid);

    /* ---------- 小工具 ---------- */
    function button(text, cls, onClick) {
      var b = api.el('button', 'mc-btn ' + cls, text);
      b.type = 'button';
      b.addEventListener('click', onClick);
      return b;
    }
    function fmtSize(n) {
      n = Number(n) || 0;
      if (n >= 1048576) return (n / 1048576).toFixed(2) + ' MB';
      if (n >= 1024) return Math.round(n / 1024) + ' KB';
      return n + ' B';
    }
    function fmtInt(n) {
      n = Number(n) || 0;
      if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
      return String(n);
    }
    function baseName(p) {
      return String(p || '').replace(/\\/g, '/').split('/').pop();
    }
    function stripExt(n) { return String(n || '').replace(/\.[^.]+$/, ''); }

    /* ---------- 左 1：源权重 ---------- */
    var srcCard = api.card('源权重');
    var srcRow = api.el('div', 'mc-srcrow');
    var srcIn = api.el('input', 'mc-input mc-mono');
    srcIn.type = 'text';
    /* 允许直接粘贴路径（外部模型的常见用法），回车即识别（用户 2026-10-18） */
    srcIn.placeholder = '请选择.safetensors/.pt';
    srcIn.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); takePath(srcIn.value); }
    });
    /* 用户 2026-10-19：拆成「选择」（工程内/仓库里的权重，卡片弹窗）+「导入」（外部文件） */
    var pickBtn = button('选择', 'mc-soft', onPickRepo);
    var impBtn = button('导入', 'mc-soft', onPickFile);
    srcRow.appendChild(srcIn);
    srcRow.appendChild(pickBtn);
    srcRow.appendChild(impBtn);
    srcCard.body.appendChild(srcRow);

    var facts = api.el('div', 'mc-facts');
    var fFmt = fact('格式');
    var fTensor = fact('张量');
    var fParam = fact('参数');
    var fVocab = fact('词表');
    srcCard.body.appendChild(facts);
    colL.appendChild(srcCard);

    function fact(label) {
      var box = api.el('div', 'mc-fact');
      box.appendChild(api.el('span', 'mc-fact-k', label));
      var v = api.el('span', 'mc-fact-v', '—');
      box.appendChild(v);
      facts.appendChild(box);
      return v;
    }

    /* ---------- 左 2：输出 ---------- */
    var outCard = api.card('输出');

    /* 权重命名（用户 2026-10-18）：放在文件夹框上面，左侧小字标签，右侧同训练页那样
       显示「未命名 / 重复」红字或绿点；合格与否按目标目录里是否已有同名文件判。 */
    var nameRow = api.el('div', 'mc-name');
    nameRow.appendChild(api.el('span', 'mc-name-k', '权重命名'));
    var nameBox = api.el('div', 'wn-row');
    var nameIn = document.createElement('input');
    nameIn.type = 'text';
    nameIn.className = 'mc-input mc-mono wn-input';
    nameIn.placeholder = '输出权重名';
    nameBox.appendChild(nameIn);
    var nameHint = api.el('div', 'wn-hint');
    nameHint.appendChild(api.el('span', 'wn-dot'));
    var nameHintText = api.el('span', 'wn-text');
    nameHint.appendChild(nameHintText);
    nameBox.appendChild(nameHint);
    nameRow.appendChild(nameBox);
    outCard.body.appendChild(nameRow);

    var dirRow = api.el('div', 'mc-srcrow');
    var dirIn = api.el('input', 'mc-input mc-mono');
    dirIn.type = 'text';
    dirIn.readOnly = true;
    dirIn.placeholder = '输出文件夹';
    var dirBtn = button('选择文件夹', 'mc-ghost', onPickDir);
    dirRow.appendChild(dirIn);
    dirRow.appendChild(dirBtn);
    outCard.body.appendChild(dirRow);

    function setNameHint(kind, text) {
      nameHint.className = 'wn-hint' + (kind ? ' ' + kind : '');
      nameHintText.textContent = (kind === 'ok') ? '' : (text || '');
    }
    var auditTimer = null;
    function auditName() {
      clearTimeout(auditTimer);
      var stem = stripExt(String(nameIn.value || '').trim());
      if (!stem) { setNameHint('bad', '未命名'); return; }
      var ext = (S.info && S.info.target === 'pt') ? '.pt' : '.safetensors';
      var dir = (S.mode === 'project') ? ((S.cfg && S.cfg.checkpoints) || '') : (S.dir || '');
      if (!dir) { setNameHint('pending', ''); return; }   /* 目标目录还没定 = 未判断，灰球 */
      setNameHint('pending', '');                          /* 判重中 → 灰球（用户 2026-10-21） */
      auditTimer = setTimeout(function () {
        api.get('/api/modelconv/check?dir=' + encodeURIComponent(dir)
          + '&name=' + encodeURIComponent(stem) + '&ext=' + encodeURIComponent(ext)).then(function (d) {
          if (!d || d.ok === false) { setNameHint('pending', ''); return; }
          if (d.exists) { setNameHint('bad', '重复'); }
          else { setNameHint('ok', ''); }
        })['catch'](function () { setNameHint('pending', ''); });
      }, 220);
    }
    nameIn.addEventListener('input', function () { auditName(); syncRun(); });
    nameIn.addEventListener('change', function () { auditName(); });
    setNameHint('pending', '');   /* 初始=未判断 → 灰球，判完再切（用户 2026-10-21） */

    /* 保存至工程（用户 2026-10-18）：文件名框下方的勾选项，样式同训练页「混合精度 AMP」那行。
       只有 safetensors → pt 且当前有工程时才能勾；.pt → safetensors 一律灰掉。 */
    outCard.body.appendChild(api.el('div', 'mc-sep'));
    var projRow = api.el('label', 'mc-check-row');
    var projCb = document.createElement('input');
    projCb.type = 'checkbox';
    projCb.className = 'tl-check';
    var projLbl = api.el('span', 'mc-check-label', '保存至工程');
    projRow.appendChild(projCb);
    projRow.appendChild(projLbl);
    outCard.body.appendChild(projRow);
    projCb.addEventListener('change', function () { setProjectMode(!!projCb.checked); });

    var btnRow = api.el('div', 'mc-row');
    var runBtn = button('开始转换', 'mc-accent', onStart);
    var stopBtn = button('停止', 'mc-ghost', onStop);
    stopBtn.disabled = true;
    btnRow.appendChild(runBtn);
    btnRow.appendChild(stopBtn);
    outCard.body.appendChild(btnRow);
    colL.appendChild(outCard);

    /* ---------- 右：状态 + 日志 ---------- */
    var stCard = api.card('转换状态');
    stCard.classList.add('mc-statcard');   /* 「耗时→卡底 = 转换状态→卡顶」用（用户 2026-10-21） */
    var stState = kv(stCard.body, '状态');
    var stFmt = kv(stCard.body, '方向');
    var stOut = kv(stCard.body, '输出');
    var stSize = kv(stCard.body, '大小');
    var stMs = kv(stCard.body, '耗时');
    colR.appendChild(stCard);

    var logCard = api.card('转换日志');
    var logEl = api.el('div', 'mc-log');
    logCard.body.appendChild(logEl);
    colR.appendChild(logCard);

    /* 转换日志卡底 = 左列「输出」卡底（用户 2026-10-21）：日志框按两列底对齐收缩，
       窗口缩放跟着重算。chrome = 卡头+内边距（卡高 - 日志框高）。 */
    function fitLog() {
      var lt = logCard.getBoundingClientRect().top;
      var oh = outCard.getBoundingClientRect().bottom;
      var chrome = logCard.getBoundingClientRect().height - logEl.getBoundingClientRect().height;
      logEl.style.height = Math.max(80, Math.round((oh - lt - chrome) * 10) / 10) + 'px';
    }
    window.addEventListener('resize', fitLog);
    requestAnimationFrame(fitLog);

    function kv(host, label) {
      var row = api.el('div', 'mc-kv');
      row.appendChild(api.el('span', 'mc-kv-k', label));
      var v = api.el('span', 'mc-kv-v', '—');
      row.appendChild(v);
      host.appendChild(row);
      return v;
    }
    function logLine(text, cls) {
      logEl.appendChild(api.el('span', 'mc-log-line' + (cls ? ' ' + cls : ''), String(text)));
      logEl.scrollTop = logEl.scrollHeight;
    }

    /* ---------- 状态同步 ---------- */
    function setProjectMode(on) {
      if (on && projCb.disabled) { projCb.checked = false; return; }
      S.mode = on ? 'project' : 'folder';
      projCb.checked = !!on;
      dirBtn.disabled = !!on;
      dirIn.disabled = !!on;
      dirIn.value = on ? ((S.cfg && S.cfg.checkpoints) || '') : (S.dir || '');
      auditName();
      syncRun();
    }
    function syncProjectCb() {
      var ok = !!(S.cfg && S.cfg.project_ready && S.info && S.info.ok && S.info.can_project);
      projCb.disabled = !ok;
      if (!ok && projCb.checked) setProjectMode(false);
    }
    function syncRun() {
      var ready = !!(S.info && S.info.ok);
      var dirOk = (S.mode === 'project') ? !!(S.cfg && S.cfg.project_ready) : !!S.dir;
      runBtn.disabled = !ready || S.running || !outName() || !dirOk;
      stopBtn.disabled = !S.running;
      nameIn.disabled = S.running;
      pickBtn.disabled = S.running;
      if (impBtn) { impBtn.disabled = S.running; }
    }
    function outName() {
      var n = String(nameIn.value || '').trim();
      if (!n) return '';
      var ext = (S.info && S.info.target === 'pt') ? '.pt' : '.safetensors';
      return stripExt(n).replace(/[\\/:*?"<>|]/g, '_') + ext;
    }
    /* ---------- 数据 ---------- */
    function loadConfig() {
      return api.get('/api/modelconv/config').then(function (d) {
        if (!d || d.ok === false) return;
        S.cfg = d;
        S.dir = S.dir || (d.project_ready ? d.checkpoints : d.home || '');
        syncProjectCb();
        setProjectMode(S.mode === 'project');
      })['catch'](function () {});
    }

    /* 从输入框/对话框拿到路径后统一走这里 */
    function takePath(raw) {
      var v = String(raw || '').trim().replace(/^"|"$/g, '');
      if (!v || S.running) return;
      S.src = v;
      srcIn.value = v;
      nameIn.value = stripExt(baseName(v));
      if (S.mode === 'folder' && !S.dirPicked) {
        var dir = v.replace(/\\/g, '/').split('/').slice(0, -1).join('/');
        if (dir) { S.dir = dir.replace(/\//g, '\\'); dirIn.value = S.dir; }
      }
      probe();
    }

    /* 选择：仓库式卡片弹窗，只列工程里已有的权重（用户 2026-10-19） */
    function onPickRepo() {
      if (S.running) return;
      api.weightPicker({
        title: '源权重',
        current: srcIn.value,
        onPick: function (w) { takePath(w.path); },
      });
    }

    /* 导入：系统文件对话框，选外部文件（用户 2026-10-19） */
    function onPickFile() {
      if (S.running) return;
      Promise.resolve(window.eai && window.eai.pickFile ? window.eai.pickFile({
        /* 原生对话框的文案不进 DOM，词典翻不到，直接两份（用户 2026-10-23） */
        title: bi('选择要转换的权重', 'Select weights to convert'),
        buttonLabel: bi('选择', 'Select'),
        multi: false,
        filters: [
          { name: bi('权重文件', 'Weight files'), extensions: ['safetensors', 'pt', 'pth', 'bin'] },
          { name: bi('全部文件', 'All files'), extensions: ['*'] },
        ],
      }) : null).then(function (paths) {
        if (!paths || !paths.length) return;
        takePath(paths[0]);
      });
    }

    function onPickDir() {
      if (S.running || S.mode === 'project') return;
      Promise.resolve(window.eai && window.eai.pickFolder ? window.eai.pickFolder({
        title: bi('选择输出文件夹', 'Choose output folder'), buttonLabel: bi('选择', 'Select'),
      }) : null).then(function (p) {
        if (!p) return;
        S.dir = p;
        S.dirPicked = true;
        dirIn.value = p;
        auditName();
      });
    }

    function probe() {
      if (!S.src) return;
      fFmt.textContent = '…';
      fTensor.textContent = '—';
      fParam.textContent = '—';
      fVocab.textContent = '—';
      api.post('/api/modelconv/probe', { path: S.src }).then(function (d) {
        S.info = d || null;
        if (!d || d.ok === false) {
          fFmt.textContent = '—';
          logLine(bi('识别失败：', 'Recognition failed: ') + ((d && d.error) || bi('未知错误', 'Unknown error')), 'mc-err');
          S.info = null;
          syncRun();
          return;
        }
        var names = { 'safetensors': 'safetensors', 'pt': 'pt' };
        fFmt.textContent = (names[d.format] || d.format) + ' → ' + (names[d.target] || d.target);
        fTensor.textContent = fmtInt(d.tensors);
        fParam.textContent = fmtInt(d.params);
        fVocab.textContent = d.has_vocab ? '有' : '无';
        syncProjectCb();
        if (S.mode === 'project' && projCb.disabled) setProjectMode(false);
        dirIn.value = (S.mode === 'project') ? (S.cfg && S.cfg.checkpoints) || '' : (S.dir || '');
        auditName();
        syncRun();
      })['catch'](function (e) {
        fFmt.textContent = '—';
        logLine(bi('识别失败：', 'Recognition failed: ') + e.message, 'mc-err');
        S.info = null;
        syncRun();
      });
    }

    /* ---------- 转换 ---------- */
    function onStart() {
      if (S.running || !S.info) return;
      var body = {
        path: S.src,
        mode: S.mode,
        out_dir: (S.mode === 'project') ? '' : (S.dir || ''),
        out_name: stripExt(nameIn.value || ''),
      };
      logEl.innerHTML = '';
      logLine(bi('开始转换 · ', 'Starting · ') + (S.mode === 'project'
        ? bi('保存到 EAI 权重', 'Save into EAI weights')
        : bi('另存到文件夹', 'Save to a folder')));
      S.since = 0;
      S.seenResult = false;
      S.seenError = false;
      S.idleTries = 0;
      api.post('/api/modelconv/start', body).then(function (d) {
        if (!d || d.ok === false) {
          logLine(bi('启动失败：', 'Failed to start: ') + ((d && d.error) || bi('未知错误', 'Unknown error')), 'mc-err');
          dot.set('err', '启动失败');
          return;
        }
        S.running = true;
        S.pending = true;
        setDot('warn', '转换中…');
        syncRun();
        poll();
      });
    }

    function onStop() {
      if (!S.running) return;
      api.post('/api/modelconv/stop', {}).then(function () {
        logLine('已请求停止…');
      });
    }

    /* 原来按钮行右侧有个状态球（用户 2026-10-18：不需要），状态统一写在右栏「转换状态 · 状态」行 */
    function setDot(kind, text) {
      S.lastKind = kind;
      if (!stState) return;
      stState.textContent = text || '—';
      stState.className = 'mc-kv-v' + (kind === 'ok' ? ' mc-ok'
        : kind === 'err' ? ' mc-err' : kind === 'warn' ? ' mc-warn' : '');
    }

    function poll() {
      clearTimeout(MC_STATE.timer);
      MC_STATE.timer = setTimeout(function () {
        api.get('/api/modelconv/status?since=' + S.since).then(function (d) {
          if (!d) return;
          (d.events || []).forEach(function (ev) {
            var p = ev.payload || {};
            /* 桥层事件带 text_en（用户 2026-10-23）：日志正文也是页面的一部分，英文界面要跟着换 */
            if (ev.kind === 'log') logLine(bi(p.text || '', p.text_en || p.text || ''),
              p.level === 'warn' ? 'mc-warn' : (p.level === 'ok' ? 'mc-ok' : ''));
            else if (ev.kind === 'error') { logLine(p.text || bi('失败', 'Failed'), 'mc-err'); setDot('err', '转换失败'); S.seenError = true; }
            else if (ev.kind === 'result') {
              S.seenResult = true;
              S.out = p.out || '';
              stFmt.textContent = (S.info ? S.info.format : '') + ' → ' + (p.target || S.info.target);
              stOut.textContent = S.out || '—';
              stOut.title = S.out || '';
              stSize.textContent = fmtSize(p.bytes);
              stMs.textContent = (p.ms || 0) + ' ms';
              setDot('ok', '已完成');
              logLine(bi('完成：', 'Done: ') + S.out, 'mc-ok');
            }
          });
          if (typeof d.next === 'number') S.since = d.next;
          if (d.running) { S.running = true; syncRun(); poll(); return; }
          /* 首次转换要 import torch（约 2s），状态可能先回 running=false 再补事件：
             没看到结果时再多问两轮，避免界面停在「转换中」不刷新（用户 2026-10-18） */
          if (!S.seenResult && !S.seenError && (S.idleTries || 0) < 2) {
            S.idleTries = (S.idleTries || 0) + 1;
            MC_STATE.timer = setTimeout(poll, 400);
            return;
          }
          S.running = false;
          S.idleTries = 0;
          syncRun();
          /* 只有「没拿到结果就结束」时才把黄点收回灰（成功/失败的点保持原色） */
          if (S.pending && (S.lastKind === 'warn' || !S.lastKind)) { S.pending = false; setDot('idle', '已结束'); }
        })['catch'](function () {
          MC_STATE.timer = setTimeout(poll, 800);
        });
      }, 250);
    }

    /* ---------- 启动 ---------- */
    setProjectMode(false);
    syncProjectCb();
    setDot('idle', '待转换');
    syncRun();
    loadConfig();
    return null;
  },

  unmount() {
    clearTimeout(MC_STATE.timer);
  },
});
