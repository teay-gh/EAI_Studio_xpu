/* ============================================================
   对话验证（导航键 chat，分组「模型」）。
   用法：EAIPages.register('chat', {...})，api 见 pages/_registry.js。
   样式复用 styles.css（.card/.hero/.btn…），本页补充见 pages/chat.css。

   后端 bridge_api/chat.py（已接真引擎）：
     POST /api/chat/load      真加载工程权重（后台线程任务，边加载边发进度事件）
     POST /api/chat/generate  用已加载模型真生成（逐 token 流式发增量事件）
     POST /api/chat/stop      真停止生成
     POST /api/chat/clear     清空当前会话（不删磁盘历史）
     GET  /api/chat/status    模型状态 + 加载/生成任务的增量事件（load_since/gen_since 游标）
     GET  /api/chat/history   真实历史会话（core/chat_history）
     GET  /api/chat/config    真实生成参数与权重路径（只读）

   生成时每 ~250ms 轮询 status?gen_since=<游标>，把 token 增量追加到助手气泡；
   本页用工程里真实的 eproai_pretrain.pt 生成，模型只训了极少步数，输出多为乱码 —— 如实显示。
   ============================================================ */
(function () {
  'use strict';

  /* 历史浮层的文档级监听（mount 里挂、unmount 里摘；放模块级才能跨作用域引用） */
  var DOC_CLICK = null, DOC_ESC = null;

  /* 离开本页的自动退出（用户 2026-10-23）：
     - 人还在这页时，只有点「停止」才会停模型 —— 任何自动逻辑都不许插手；
     - 切到别的页后开始计时，2 分钟内切回来就取消计时，下次离开重新计时；
     - 满 2 分钟仍在别的页 → 调 /api/chat/unload 退出运行并释放模型内存。
     计时器放模块级（页面 unmount 后还要活着），不走页面自己的 clearTimers。 */
  var LEAVE_MS = 120000;
  var LEAVE_TIMER = null;
  var AUTO_EXITED = false;                 /* 下次回到本页时提示一句 */

  function cancelLeaveTimer() {
    if (LEAVE_TIMER) { clearTimeout(LEAVE_TIMER); LEAVE_TIMER = null; }
  }
  function armLeaveTimer() {
    cancelLeaveTimer();
    LEAVE_TIMER = setTimeout(function () {
      LEAVE_TIMER = null;
      AUTO_EXITED = true;
      try {
        var api2 = window.__EAI_API;
        if (api2 && api2.post) api2.post('/api/chat/unload', {}).then(null, function () {});
      } catch (e) { /* noop */ }
    }, LEAVE_MS);
  }


  /* ---------- 只给 ?mock=1 预览截图用（正常跑一律走真实桥接口） ---------- */
  var STATUS_MOCK = {
    ok: true,
    model: {
      loaded: true, path: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
      name: 'eproai_pretrain.pt', source: 'pretrain_out（预训练权重）', device: 'cpu',
      vocab: 1116, params: 1222400, d_model: 128, n_head: 4, n_layer: 4, block_size: 128,
      d_ff: 512, tie_weights: true, mtp: true, mtp_k: 3, size_mb: 4.71, load_ms: 118,
      loaded_at: '2026-10-11 18:10:00', missing_keys: 0, unexpected_keys: 0
    },
    session: null,
    load: { exists: false, running: false, events: [], next: 0, result: null, error: '' },
    generate: { exists: false, running: false, events: [], next: 0, result: null, error: '' }
  };

  var HISTORY_MOCK = {
    total: 2,
    _meta: { source: 'core.chat_history+disk', project: '<工程目录>',
      scanned: ['<工程目录>\\chats'], errors: [] },
    items: [
      {
        id: 'demo0001', title: '演示对话 · mock', project: 'EAI_GC',
        created: '2026-10-11 02:54:45', updated: '2026-10-11 02:54:45', count: 4,
        messages: [
          { role: 'user', content: '你好' },
          { role: 'assistant', content: '可务快供破套箭灌过放伤束送二印使程稠失块提忍景箭供融才损转默尽融虚底槽虚说' },
          { role: 'user', content: '这里故意放一段比较长的问题，用来检查气泡是否能正确换行而不会横向溢出容器。' },
          { role: 'assistant', content: '一条样本写作 \\x01问题\\x02回答\\x03，生成遇到 \\x03 即停止。' }
        ]
      },
      {
        id: 'demo0002', title: '历史会话 · mock', project: 'default',
        created: '2026-10-09 18:45:38', updated: '2026-10-09 18:45:38', count: 1,
        messages: [{ role: 'user', content: 'old' }]
      }
    ]
  };

  var CONFIG_MOCK = {
    device: 'cpu', sample_chars: 120, block_size: 128, prompt: '你好',
    training_format: '\\x01问\\x02答\\x03', model_ready: true, model_loaded: true,
    project: '<工程目录>',
    checkpoints_dir: '<工程目录>\\checkpoints',
    checkpoint_files: [{ name: 'eproai_pretrain.pt', size_mb: 4.71 }],
    sft_out: { configured: '', path: '<工程目录>\\checkpoints\\eproai_model.pt', exists: false, size_mb: null },
    pretrain_out: { configured: '', path: '<工程目录>\\checkpoints\\eproai_pretrain.pt', exists: true, size_mb: 4.71 }
  };

  EAIPages.register('chat', {
    title: '对话验证',
    sub: '加载本地权重进行生成',

    mock: {
      '/api/chat/status?load_since=0&gen_since=0': STATUS_MOCK,
      '/api/chat/status': STATUS_MOCK,
      '/api/chat/history': HISTORY_MOCK,
      '/api/chat/config': CONFIG_MOCK,
      '/api/chat/load': { ok: true, task: 'chat:load', path: '<工程目录>\\checkpoints\\eproai_pretrain.pt', source: 'pretrain_out（预训练权重）', size_mb: 4.71 },
      '/api/chat/generate': { ok: true, task: 'chat:generate', prompt: '你好', sample_chars: 120, block_size: 128 },
      '/api/chat/stop': { ok: true, stopped: true, note: '已请求停止' },
      '/api/chat/clear': { ok: true, note: '已清空当前对话（不影响磁盘上的历史记录）。' }
    },

    mount: function (root, api) {
      /* ---------- 小工具 ---------- */
      function fmtInt(n) {
        n = Number(n) || 0;
        return n.toLocaleString('en-US');
      }

      /* ---------- 对话记录 ---------- */
      var log = api.el('div', 'chat-log');
      cancelLeaveTimer();                  /* 回到本页：取消「离开 2 分钟自动退出」计时（用户 2026-10-23） */

      function line(kind, text) {
        var row;
        if (kind === 'sys') {
          row = api.el('div', 'msg sys');
          row.appendChild(api.el('div', 'sys-line', text));
        } else {
          row = api.el('div', 'msg ' + (kind === 'user' ? 'user' : 'bot'));
          row.appendChild(api.el('div', 'bubble', text));
        }
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
        return row;
      }
      function addBotBubble() {
        var row = api.el('div', 'msg bot');
        var b = api.el('div', 'bubble streaming');
        row.appendChild(b);
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
        return { el: b, row: row };
      }

      /* ---------- 状态 / 变量 ---------- */
      var genCursor = 0, loadCursor = 0;
      var generating = false, loading = false;
      var modelLoaded = false;
      var running = false;                  /* 「开始」之后才为真（用户 2026-10-18） */
      var modelTrained = 0;                 /* 已加载权重的训练上下文长度（0=未知，用户 2026-10-18） */
      var lenSample = null, lenCtx = null;
      var lenRep = null;                    /* 重复惩罚横条（用户 2026-10-19） */
      var lenNoteEl = null, lenNoteMsg = ''; /* 「重新加载后生效」只在已加载模型时显示 */
      var paintNote = function () {};        /* 由长度框构建时替换成真实实现 */
      var ctxWanted = 0;                     /* 上下文长度的目标值（未加载模型时只把滑块停在最左） */
      var bot = null, botText = '';
      var checkpointFiles = [];
      var checkpointsDir = '';
      var pollTimers = [];

      function clearTimers() {
        pollTimers.forEach(function (t) { clearTimeout(t); });
        pollTimers = [];
      }
      function later(fn, ms) { var t = setTimeout(fn, ms); pollTimers.push(t); return t; }

      /* ---------- 工具条 ---------- */
      /* 「未加载模型」小字提示已删除（用户 2026-10-18），状态只由标题旁的小圆点表达；
         status 保留为一个不挂到 DOM 的占位对象，避免下面各处赋值报错。 */
      var status = { textContent: '', className: '', title: '' };
      var toolbar = api.el('div', 'chat-toolbar');
      function tbtn(text, cls, fn) {
        var b = api.el('button', 'chat-btn ' + cls, text);
        b.type = 'button';
        b.addEventListener('click', fn);
        return b;
      }

      /* 两个入口分开（用户 2026-10-17）：「加载基座权重」只列完整 ckpt，*.lora.pt 走「加载 LoRA 权重」 */
      var loadBtn = tbtn('加载基座权重', 'soft', function () {
        if (loading || generating) return;
        showPicker('base');
      });
      var defaultBtn = tbtn('加载 LoRA 权重', 'soft', function () {
        if (loading || generating) return;
        showPicker('lora');
      });
      var clearBtn = tbtn('清空对话', 'ghost', onClear);
      /* 开始 / 停止运行（用户 2026-10-18）：加载权重后必须点「开始」才允许对话 */
      var startBtn = tbtn('开始', 'primary', onStart);
      var stopBtn = tbtn('停止', 'danger', onStop);
      startBtn.disabled = true;
      stopBtn.disabled = true;

      toolbar.appendChild(loadBtn);
      toolbar.appendChild(defaultBtn);
      toolbar.appendChild(clearBtn);
      root.appendChild(toolbar);

      /* ---------- 权重选择（用户 2026-10-17）：改用训练页同款大弹窗 + 仓库卡片 + 搜索，
         替换原来贴在工具条下面的行内小列表；两个入口按类型过滤（适配器 *.lora.pt、基座其余）。 ---------- */
      function showPicker(mode) {
        mode = mode === 'lora' ? 'lora' : 'base';
        api.weightPicker({
          title: mode === 'lora' ? '加载 LoRA 权重' : '加载基座权重',
          accept: mode,          /* 训练页同款过滤：适配器只列 *.lora.pt，基座只列非 *.lora.pt */
          onPick: function (w) {
            if (mode === 'lora') doLoad('', w.path);   /* 适配器要先与基座合并再加载 */
            else doLoad(w.path);
          },
        });
      }

      /* ---------- 主体：左=对话，右=参数+历史 ---------- */
      var main = api.el('div', 'chat-main');

      var leftCol = api.el('div', 'chat-col chat-col-main');
      /* 卡头改成「● 对话验证」，小球用颜色表示运行状态（用户 2026-10-18） */
      var chatCard = api.card('');
      var chatHead = chatCard.querySelector('.card-head');
      var titleWrap = api.el('span', 'chat-titlewrap');
      var dot = api.el('span', 'chat-dot');
      titleWrap.appendChild(dot);
      titleWrap.appendChild(api.el('h2', 'card-title', '对话验证'));
      /* 会话名放进卡头（用户 2026-10-18）：最多显示 12 字，超出按仓库卡片的省略号方式截断 */
      var convNameEl = api.el('span', 'chat-conv', '');
      titleWrap.appendChild(convNameEl);
      function setConvName(t) {
        var s = String(t == null ? '' : t).replace(/\s+/g, ' ').trim();
        convNameEl.title = s;
        convNameEl.textContent = (!s) ? '' : (s.length > 12 ? s.slice(0, 12) + '…' : s);
      }
      chatHead.insertBefore(titleWrap, chatHead.firstChild);
      chatCard.classList.add('chat-card-fill');    /* 撑到底：卡片里只有对话区滚动（用户 2026-10-17） */
      /* 历史会话切换放进对话卡头（用户 2026-10-17：右侧不再单列历史卡） */
      /* 卡头右侧：历史 / 新对话 用图标（用户 2026-10-18：与 AI 助手面板那两颗图标一致） */
      var histSlot = api.el('div', 'chat-hist-slot');
      chatCard.querySelector('.card-head').appendChild(histSlot);
      var histBtn = histIcon('hist');
      var newBtn = histIcon('new');
      histSlot.appendChild(histBtn);
      histSlot.appendChild(newBtn);
      var histPop = api.el('div', 'chat-hist-pop');
      histSlot.appendChild(histPop);
      chatCard.body.appendChild(log);

      var inputRow = api.el('div', 'chat-input-row');
      var input = api.el('input', 'chat-input');
      input.type = 'text';
      input.placeholder = '输入问题，回车或点「发送」';
      var sendBtn = api.el('button', 'chat-send', '发送');
      sendBtn.type = 'button';
      sendBtn.disabled = true;
      inputRow.appendChild(input);
      inputRow.appendChild(sendBtn);
      chatCard.body.appendChild(inputRow);
      dot.className = 'chat-dot idle';     /* 首帧就是终态，避免闪一下（用户 2026-10-18） */
      syncRun();

      leftCol.appendChild(chatCard);
      main.appendChild(leftCol);

      var rightCol = api.el('div', 'chat-col');

      /* 资源占用瓷片（用户 2026-10-17：仿训练页，给权重大小与内存/显存开销；
         小字介绍全部去掉（用户 2026-10-17），空数据用「—」跟训练页一致） */
      var resCard = api.card('资源占用');
      var resStats = api.el('div', 'stats two chat-stats');
      resCard.body.appendChild(resStats);
      rightCol.appendChild(resCard);

      function tile(label) {
        var el = api.el('div', 'stat');
        var lab = api.el('div', 'stat-label', label);
        el.appendChild(lab);
        var val = api.el('div', 'stat-val', '—');
        val.classList.add('idle');
        el.appendChild(val);
        resStats.appendChild(el);
        return { val: val, label: lab, setLabel: function (t) { lab.textContent = t; },
          text: function (t, cls) {
            val.textContent = t == null ? '—' : String(t);
            val.className = 'stat-val' + (cls ? (' ' + cls) : '');
          } };
      }
      /* 六块瓷片（用户 2026-10-18）：LoRA权重/基座权重 · 参数量/推理设备 · 内存占用/剩余内存
         2026-10-20（用户）：文案改口径 —— 微调权重 → LoRA权重、预训练权重 → 基座权重 */
      /* 2026-10-20（用户）：两块换位 —— 基座权重在左，LoRA 权重在右 */
      var tPre = tile('基座权重');
      var tSft = tile('LoRA权重');
      var tPar = tile('参数量');
      var tDev = tile('推理设备');
      var tMemU = tile('内存占用');
      var tMemF = tile('剩余内存');

      function compactNum(n) {
        var a = window.__EAI_API;
        return (a && a.compactNum) ? a.compactNum(n)
          : (n >= 100 ? String(Math.round(n)) : String(Math.round(n * 10) / 10));
      }
      function fmtMb(v) {
        if (v == null || v === '') return '—';
        var n = Number(v) || 0;
        return n >= 1024 ? compactNum(n / 1024) + 'GB' : compactNum(n) + 'MB';
      }
      function devName(s) { return String(s || '').toUpperCase() || '—'; }
      /* 本机状态（设备 / EAI 占用 / 剩余内存）与托管页同一数据源 */
      function paintMachine(fallbackDev) {
        return api.get('/api/host/machine').then(function (m) {
          if (!m || m.ok === false) {
            tDev.text(devName(fallbackDev), 'idle');
            return;
          }
          tDev.text(devName(m.device || fallbackDev), '');
          tMemU.text(fmtMb(m.eai_mb), m.eai_mb == null ? 'idle' : '');
          tMemF.text(fmtMb(m.free_mb), m.free_mb == null ? 'idle' : '');
        })['catch'](function () {});
      }

      function fmtNum(n) {
        n = Number(n) || 0;
        if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
        if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
        return String(Math.round(n));
      }
      /* 已知参数量 → 参数/显存两块一起刷新 */
      function paintCost(params, fromSize) {
        if (!params || params <= 0) {
          tPar.text('—', 'idle');
          return;
        }
        /* 用户 2026-10-20：不要「≈」前缀，数字一律绿色（以前按体积估的会带 ≈ 且变棕） */
        tPar.text(fmtNum(params), 'ok');
      }

      /* 用户 2026-10-18：去掉「生成参数」外框，两个大格直接排在「资源占用」下面：
               ① 生成长度 + 上下文长度   ② 推理设备 + 预训练 / LoRA 权重 */
            var kv = api.el('div', 'kv');
            var lenBox = api.el('div', 'kv-panel');
            var infoBox = api.el('div', 'kv-panel');
            rightCol.appendChild(lenBox);
            rightCol.appendChild(infoBox);

            /* 开始 / 停止 移到右栏最下方（用户 2026-10-18） */
            var runRow = api.el('div', 'chat-runrow');
            runRow.appendChild(startBtn);
            runRow.appendChild(stopBtn);
            rightCol.appendChild(runRow);

      main.appendChild(rightCol);
      root.appendChild(main);

      function kvRow(key, val) {
        var row = api.el('div', 'kv-row');
        row.appendChild(api.el('span', 'kv-key', key));
        var v = api.el('span', 'kv-val');
        if (val instanceof Node) v.appendChild(val);
        else v.textContent = String(val == null ? '—' : val);
        row.appendChild(v);
        kv.appendChild(row);
      }
      function mono(s) { return api.el('span', 'mono', String(s == null ? '—' : s)); }
      function badge(text, kind) { return api.el('span', 'badge ' + kind, text); }
      function weightNode(w) {
        w = w || {};
        var box = api.el('span', 'weight');
        box.appendChild(mono(w.path || w.configured || '—'));
        box.appendChild(document.createTextNode(' '));
        box.appendChild(w.exists
          ? badge(w.size_mb != null ? w.size_mb + ' MB' : '存在', 'ok')
          : badge('缺失', 'err'));   /* 缺失=红（用户 2026-10-18） */
        return box;
      }

      /* ---------- 运行状态（用户 2026-10-18）---------- */
      function syncRun() {
        if (loading) {
          status.textContent = '加载中…';
          status.className = 'chat-status warn';
          dot.className = 'chat-dot warn';
        } else if (!modelLoaded) {
          status.textContent = '未加载模型';
          status.className = 'chat-status';
        } else if (generating) {
          status.textContent = '生成中…';
          status.className = 'chat-status ok';
        } else if (running) {
          status.textContent = '运行中';
          status.className = 'chat-status ok';
        } else {
          status.textContent = '已加载 · 未开始';
          status.className = 'chat-status warn';
        }
        dot.className = 'chat-dot ' + (!modelLoaded ? 'idle' : (running ? (generating ? 'busy' : 'ok') : 'warn'));
        startBtn.disabled = !(modelLoaded && !running && !generating && !loading);
        /* 只要有东西可停（会话在跑，或还在吐 token）就允许点停止；真停了才是灰的（用户 2026-10-20） */
        stopBtn.disabled = !(running || generating);
        sendBtn.disabled = !running || generating;
        input.disabled = !running || generating;
        loadBtn.disabled = loading || generating || running;
        defaultBtn.disabled = loading || generating || running;
        root.classList.toggle('chat-running', running);
        syncLimits();
      }

      /* 上下文长度的可用上限（用户 2026-10-18）：优先已加载权重的训练上下文长度；
         识别不到（未导入模型）就返回 0，此时该横条显示「-」且不可拖。 */
      function ctxTrained() {
        return Number(modelTrained || 0) || 0;
      }

      function syncLimits() {
        if (!lenSample || !lenCtx) return;
        var locked = running || generating || loading;
        lenSample.input.disabled = locked || !modelLoaded;
        lenCtx.input.disabled = locked || !modelLoaded;
        lenSample.out.classList.toggle('off', lenSample.input.disabled);
        lenCtx.out.classList.toggle('off', lenCtx.input.disabled);
        lenSample.input.parentNode.classList.toggle('off', lenSample.input.disabled);
        lenCtx.input.parentNode.classList.toggle('off', lenCtx.input.disabled);
        if (lenRep) {   /* 未加载权重时重复惩罚同样变灰（用户 2026-10-19） */
          lenRep.input.disabled = locked || !modelLoaded;
          lenRep.out.classList.toggle('off', lenRep.input.disabled);
          lenRep.input.parentNode.classList.toggle('off', lenRep.input.disabled);
        }

        paintNote();
        var t = ctxTrained();
        if (!modelLoaded || !t) {
          lenCtx.out.textContent = '-';
          lenCtx.input.value = lenCtx.input.min;   /* 未知上限时滑块停在最左，避免看起来像 128/256 */
          return;
        }
        var max = Math.max(32, Math.round(t / 32) * 32);
        lenCtx.input.max = max;
        var want = Number(ctxWanted || 0) || max;
        lenCtx.input.value = String(Math.min(want, max));
        lenCtx.out.textContent = lenCtx.input.value;
      }
      function onStart() {
        if (!modelLoaded) { line('sys', '请先加载模型权重。'); return; }
        if (running) return;
        api.post('/api/chat/start', {}).then(function (d) {
          if (!d || d.ok === false) { line('sys', '开始失败：' + ((d && d.error) || '未知错误')); return; }
          running = true;
          syncRun();
          line('sys', '已开始运行。');
        });
      }

      /* ---------- 载入状态（模型） ---------- */
      /* 权重两行可重画（用户 2026-10-19）：加载完权重后立刻按「真正加载的模型」刷新，
         以前这两行只在进页面时画一次，用「加载基座权重」导入的文件会一直挂在 LoRA 行上。 */
      var repaintWeights = null;
      var repaintTiles = null;      /* 预训练 / 微调 两块权重瓷片（用户 2026-10-20） */
      var loadedModel = null;      /* 当前真正加载的模型（status/加载结果都会更新它） */

      function applyModel(m) {
        loadedModel = (m && m.loaded) ? m : null;
        if (repaintWeights) { try { repaintWeights(); } catch (e) { /* noop */ } }
        if (repaintTiles) { try { repaintTiles(); } catch (e) { /* noop */ } }
        if (m && m.loaded) {
          modelLoaded = true;
          modelTrained = Number(m.loaded_trained_max_len || m.trained_max_len || 0) || 0;
          if (m.params) paintCost(m.params, false);   /* 真实参数量优先于按体积估算（用户 2026-10-17） */
          status.title = m.path || '';
          syncRun();
        } else {
          modelLoaded = false;
          running = false;
          syncRun();
        }
      }

      /* 补查模型状态（用户 2026-10-18）：挂载时加载刚结束，_INFO 可能还没落到 status 里 */
      function refreshModelOnce(tries) {
        api.get('/api/chat/status').then(function (d) {
          if (d && d.model && d.model.loaded) { applyModel(d.model); return; }
          if (tries > 0) setTimeout(function () { refreshModelOnce(tries - 1); }, 900);
        })['catch'](function () { /* noop */ });
      }

      /* ================= 加载权重 ================= */
      function setLoadUI(on) {
        loading = on;
        loadBtn.disabled = on || generating;
        defaultBtn.disabled = on || generating;
        if (on) { status.textContent = '加载中…'; status.className = 'chat-status warn'; dot.className = 'chat-dot warn'; }
      }
      function renderLoadEvent(ev) {
        var p = ev.payload || {};
        if (ev.kind === 'start') line('sys', '开始加载权重：' + (p.name || p.path || ''));
        else if (ev.kind === 'progress') line('sys', '· ' + (p.stage || ''));
        else if (ev.kind === 'warn') line('sys', p.text || '');
      }
      function doLoad(path, adapter) {
        if (loading || generating) return;
        setLoadUI(true);
        if (adapter) line('sys', '正在加载 LoRA：' + adapter + '（会先与基座合并）');
        else line('sys', '正在加载' + (path ? ('：' + path) : '（默认：优先 LoRA 权重，缺失回退基座权重）'));
        loadCursor = 0;
        var payload = adapter ? { adapter: adapter } : (path ? { path: path } : {});
        api.post('/api/chat/load', payload).then(function (r) {
          if (!r || r.ok === false || r.error) {
            setLoadUI(false);
            status.textContent = '加载失败'; status.className = 'chat-status warn';
            line('sys', '加载失败：' + ((r && r.error) || '未知错误'));
            return;
          }
          pollLoad();
        });
      }
      function pollLoad() {
        api.get('/api/chat/status?load_since=' + loadCursor + '&gen_since=' + genCursor).then(function (d) {
          if (!d || d.error) { setLoadUI(false); line('sys', '状态读取失败：' + ((d && d.error) || '未知错误')); return; }
          var t = d.load || {};
          (t.events || []).forEach(renderLoadEvent);
          if (typeof t.next === 'number') loadCursor = t.next;
          if (t.running) { later(pollLoad, 350); return; }
          setLoadUI(false);
          if (d.model && d.model.loaded) {
            applyModel(d.model);
            var m = d.model;
            line('sys', '模型就绪 · device=' + m.device + ' · vocab=' + m.vocab
              + ' · 参数 ' + fmtInt(m.params) + ' · 耗时 ' + m.load_ms + ' ms');
            if (m.missing_keys || m.unexpected_keys) {
              line('sys', '权重键不完全匹配（缺失 ' + m.missing_keys + ' / 多余 ' + m.unexpected_keys + '），已按 strict=False 加载。');
            }
          } else {
            status.textContent = '加载失败'; status.className = 'chat-status warn';
            line('sys', '加载失败：' + ((t && t.error) || '未知错误'));
          }
        });
      }

      /* ================= 生成 ================= */
      function setGenUI(on) {
        generating = on;
        syncRun();
        root.classList.toggle('chat-generating', on);
      }
      function paintBot() { if (bot) { bot.el.textContent = botText; log.scrollTop = log.scrollHeight; } }
      function send() {
        if (generating) return;
        var text = (input.value || '').trim();
        if (!text) return;
        if (!modelLoaded) { line('sys', '请先加载模型权重。'); return; }
        if (!running) { line('sys', '请先点「开始」，对话才会运行。'); return; }
        input.value = '';
        if (!currentConvId) setConvName(text);
        line('user', text);
        bot = addBotBubble();
        botText = '';
        setGenUI(true);
        genCursor = 0;
        api.post('/api/chat/generate', { prompt: text, persist: true }).then(function (r) {
          if (!r || r.ok === false || r.error) {
            setGenUI(false);
            botText = '生成失败：' + ((r && r.error) || '未知错误');
            bot.el.classList.remove('streaming');
            paintBot();
            line('sys', '生成失败：' + ((r && r.error) || '未知错误'));
            return;
          }
          pollGen();
        });
      }
      function pollGen() {
        api.get('/api/chat/status?gen_since=' + genCursor + '&load_since=' + loadCursor).then(function (d) {
          if (!d || d.error) { later(pollGen, 400); return; }
          var t = d.generate || {};
          (t.events || []).forEach(function (ev) {
            if (ev.kind === 'token') { botText += (ev.payload && ev.payload.text) || ''; paintBot(); }
          });
          if (typeof t.next === 'number') genCursor = t.next;
          if (t.running) { later(pollGen, 250); return; }

          setGenUI(false);
          var res = t.result;
          if (res && typeof res.text === 'string') {
            botText = res.text;
            paintBot();
            line('sys', '生成完成 · ' + res.tokens + ' tokens · ' + res.elapsed_ms + ' ms · device=' + res.device
              + (res.stopped ? ' · 已停止' : ''));
          } else if (t.error) {
            botText = '生成失败：' + t.error;
            paintBot();
            line('sys', '生成失败：' + t.error);
          } else {
            line('sys', '生成结束（无结果返回）。');
          }
          if (bot) bot.el.classList.remove('streaming');
          loadHistory();
        });
      }
      function onStop() {
        if (!running && !generating) return;
        stopBtn.disabled = true;
        api.post('/api/chat/stop', {}).then(function (d) {
          if (d && d.ok === false) line('sys', '停止失败：' + ((d && d.error) || '未知错误'));
          running = false;
          if (!generating) { syncRun(); line('sys', '已停止运行。'); }
        });
      }

      /* ================= 清空 / 历史 ================= */
      function onClear() {
        /* 用户 2026-10-20：清空对话 = 清空全部对话和历史，并且要二次确认 */
        api.confirm({
          title: '清空对话',
          message: '清空全部对话和历史？',
          detail: '当前工程里这一页的所有会话记录都会被删除，删除后无法恢复。',
          buttons: ['取消', '清空'],
        }).then(function (ok) {
          if (!ok) return;
          api.post('/api/chat/clear_all', {}).then(function (d) {
            if (!d || d.ok === false) { notice((d && d.error) || '清空失败'); return; }
            currentConvId = '';
            setConvName('');
            log.innerHTML = '';
            bot = null;
            botText = '';
            line('sys', '已清空全部对话和历史（' + ((d.count != null) ? d.count : (d.deleted || []).length) + ' 条）。');
            loadHistory();
          });
        });
      }

      var histItems = [];
      var currentConvId = '';

      /* 2026-10-18：切工程 → 断开当前会话。
         后端 /api/chat/history 已只列本工程，但页面还挂着上个工程的会话 id，
         继续发消息会把旧会话写进新工程的 chats/。 */
      var offProj = (api.onProjectChange ? api.onProjectChange(function () {
        currentConvId = '';
        setConvName('');
        log.innerHTML = '';
        bot = null;
        botText = '';
        loadHistory();
      }) : null);

      /* 点空白处 / Esc 收起历史浮层（卸载时摘掉，避免监听越堆越多） */
      function docClickClose() { closeHistPop(); }
      function docEscClose(e) { if (e.key === 'Escape') closeHistPop(); }

      function convLabel(it) {
        return (it.title || it.id || '新对话') + ' · ' + (it.count || 0) + ' 条'
          + (it.project ? (' · ' + it.project) : '');
      }

      /* 卡头两颗图标按钮（用户 2026-10-18）：⏱ 历史 / ＋ 新对话 —— 形状、尺寸、悬停都照 AI 助手面板。
         历史改成浮层列表（原来是下拉），点一条就载入；＋ 直接开新对话。 */
      function histIcon(name) {
        var b = api.el('button', 'chat-hbtn chat-hbtn-' + name);
        b.type = 'button';
        b.title = name === 'hist' ? '历史会话' : '新对话';
        b.setAttribute('aria-label', b.title);
        b.innerHTML = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          + 'stroke-width="' + (name === 'hist' ? '1.6' : '1.7') + '" stroke-linecap="round" '
          + 'stroke-linejoin="round" aria-hidden="true">'
          + (name === 'hist'
              ? '<path d="M12 7.6v4.8l3.1 1.9"/><circle cx="12" cy="12" r="7.6"/>'
              : '<path d="M12 5.6v12.8"/><path d="M5.6 12h12.8"/>')
          + '</svg>';
        return b;
      }

      function closeHistPop() {
        histPop.classList.remove('on');
        histBtn.classList.remove('on');
      }

      function openHistPop() {
        histPop.classList.add('on');
        histBtn.classList.add('on');
        renderHistList();
        loadHistory(false);            /* 每次打开都取一次：别让列表停在旧快照 */
      }

      function renderHistList() {
        histPop.textContent = '';
        if (!histItems.length) {
          histPop.appendChild(api.el('div', 'chat-hist-empty', '暂无历史会话'));
          return;
        }
        histItems.forEach(function (it) {
          var row = api.el('div', 'chat-hist-row'
            + (String(it.id) === String(currentConvId) ? ' on' : ''));
          row.setAttribute('role', 'button');
          row.tabIndex = 0;
          row.title = convLabel(it);
          var main = api.el('div', 'chat-hist-main');
          /* 标题超字照仓库页：右缘渐变 + 悬停向右滚（用户 2026-10-20） */
          main.appendChild(api.scrollText ? api.scrollText(it.title || it.id || '新对话', 'chat-hist-title', '')
                                          : api.el('div', 'chat-hist-title', it.title || it.id || '新对话'));
          main.appendChild(api.el('div', 'chat-hist-meta',
            (it.count || 0) + ' 条'
            + (it.project ? (' · ' + it.project) : '')
            + (it.updated ? (' · ' + it.updated) : '')));
          row.appendChild(main);
          /* 每条历史一个删除键（照 AI 窗口历史那条，用户 2026-10-20） */
          var delHist = api.el('button', 'chat-hist-del');
          delHist.type = 'button';
          delHist.title = '删除这条会话';
          delHist.setAttribute('aria-label', '删除这条会话');
          delHist.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"'
                            + ' stroke-linecap="round" aria-hidden="true"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>';
          delHist.addEventListener('click', function (e) {
            e.stopPropagation();                  /* 别顺手把这条打开 */
            e.preventDefault();
            if (delHist.disabled) return;
            delHist.disabled = true;
            api.post('/api/chat/delete', { id: it.id }).then(function (r) {
              if (r && r.ok === false) {
                delHist.disabled = false;
                notice((r && r.error) || '删除失败');
                return;
              }
              if (String(it.id) === String(currentConvId)) {   /* 删的是当前这条：一起清屏 */
                currentConvId = '';
                setConvName('');
                log.innerHTML = '';
                bot = null;
                botText = '';
              }
              loadHistory();
            })['catch'](function () { delHist.disabled = false; });
          });
          row.appendChild(delHist);
          row.addEventListener('click', function () {
            closeHistPop();
            loadHistoryItem(it);
          });
          row.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); closeHistPop(); loadHistoryItem(it); }
          });
          histPop.appendChild(row);
        });
      }

      histBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (histPop.classList.contains('on')) closeHistPop(); else openHistPop();
      });
      newBtn.addEventListener('click', function () {
        closeHistPop();
        currentConvId = '';
        setConvName('');
        log.innerHTML = '';
        bot = null;
        line('sys', '已开始新对话。');
      });
      /* 点别处 / Esc 收起浮层 */
      histPop.addEventListener('click', function (e) { e.stopPropagation(); });
      DOC_CLICK = docClickClose;
      DOC_ESC = docEscClose;
      document.addEventListener('click', DOC_CLICK);
      document.addEventListener('keydown', DOC_ESC);

      function loadHistoryItem(conv) {
        currentConvId = String(conv.id || '');
        renderHistList();
        log.innerHTML = '';
        bot = null;
        setConvName(conv.title || conv.id || '');
        /* 历史信息只留上一次对话的时间（用户 2026-10-18） */
        line('sys', conv.updated || '');
        (conv.messages || []).slice(-80).forEach(function (m) {
          line(m.role === 'user' ? 'user' : 'bot', String(m.content || ''));
        });
      }

      function loadHistory(auto) {
        return api.get('/api/chat/history').then(function (d) {
          histItems = (!d || d.error) ? [] : (d.items || []);
          renderHistList();
          if (!d || d.error) {
            line('sys', '历史读取失败：' + ((d && d.error) || '未知错误'));
            return [];
          }
          return histItems;
        }).then(function (items) {
          /* AI 助手的历史列表点进来 → 优先载入指定的那条会话 */
          var want = window.EAIOpenConv;
          if (auto && want && items && items.length) {
            window.EAIOpenConv = null;
            for (var w = 0; w < items.length; w++) {
              if (items[w].id === want.id) { loadHistoryItem(items[w]); return items; }
            }
          }
          /* 仅在首次挂载时自动载入最近一条有消息的会话（与旧版行为一致），
             生成 / 清空后的刷新不覆盖当前对话。 */
          if (auto && items && items.length && !log.querySelector('.msg.user, .msg.bot')) {
            var conv = null;
            for (var i = 0; i < items.length; i++) {
              if ((items[i].messages || []).length) { conv = items[i]; break; }
            }
            if (conv) loadHistoryItem(conv);
          }
          return items;
        });
      }

      /* ---------- 输入区事件 ---------- */
      sendBtn.addEventListener('click', send);
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); send(); }
      });

      /* ---------- 载入真实参数 ---------- */
      api.get('/api/chat/config').then(function (d) {
        if (!d || d.error) {
          lenBox.textContent = '';
          lenBox.appendChild(api.el('div', 'kv-note', '读取失败：' + ((d && d.error) || '未知错误')));
          return;
        }
        checkpointFiles = d.checkpoint_files || [];
        checkpointsDir = d.checkpoints_dir || '';

        /* 长度：一个框里两行横条（用户 2026-10-18）。拖动即时改值，松手落盘。
           2026-10-18 追加：上下文长度的上限 = 权重训练时的上下文长度（识别不到就不给调）；
           未导入模型时上下文显示「-」，两个横条都灰掉不可拖；点「开始」运行后同样灰掉。 */
        var trained = Number(d.block_size == null ? 128 : d.block_size) || 128;
        lenBox.textContent = '';
        function rangeRow(label, min, max, step, value, unit, onCommit, fmt, parse) {
          function show(v) { return (fmt ? fmt(Number(v)) : v) + (unit || ''); }
          function disp(v) { return fmt ? fmt(Number(v)) : String(v); }      /* 不带单位的展示值 */
          var row = api.el('div', 'kv-range-row');
          row.appendChild(api.el('span', 'kv-range-k', label));
          var inp = document.createElement('input');
          inp.type = 'range';
          inp.className = 'kv-range';
          inp.min = min; inp.max = max; inp.step = step; inp.value = value;
          var out = api.el('span', 'kv-range-v', show(value));
          inp.addEventListener('input', function () { out.textContent = show(inp.value); });
          inp.addEventListener('change', function () { onCommit(Number(inp.value)); });

          /* 数值就地输入（用户 2026-10-20）：默认外观不动，鼠标移到数字上提示可改，
             点一下换成输入框，回车/失焦生效（按 min/max 夹紧并吸到步进），Esc 放弃。 */
          out.addEventListener('click', function () {
            if (inp.disabled) return;                 /* 未加载权重 / 运行中：和滑块一起锁 */
            if (row.querySelector('.kv-range-edit')) return;
            var ed = document.createElement('input');
            ed.type = 'text';
            ed.inputMode = 'decimal';
            ed.className = 'kv-range-edit';
            ed.value = disp(inp.value);
            out.style.display = 'none';
            row.appendChild(ed);
            ed.focus();
            ed.select();
            var done = false;
            function finish(ok) {
              if (done) return;
              done = true;
              var txt = String(ed.value || '').trim();
              if (ok && txt) {
                var raw = parse ? parse(txt) : Number(txt);
                if (isFinite(raw)) {
                  var lo = Number(inp.min), hi = Number(inp.max), st = Number(inp.step) || 0;
                  raw = Math.min(hi, Math.max(lo, raw));
                  if (st > 0) raw = lo + Math.round((raw - lo) / st) * st;   /* 吸到步进 */
                  raw = Math.round(raw * 1000) / 1000;
                  inp.value = String(raw);
                  out.textContent = show(raw);
                  onCommit(raw);                    /* 和拖完松手同一条落盘路径 */
                }
              }
              if (ed.parentNode) ed.parentNode.removeChild(ed);
              out.style.display = '';
            }
            ed.addEventListener('keydown', function (e) {
              if (e.key === 'Enter') { e.preventDefault(); finish(true); }
              else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
            });
            ed.addEventListener('blur', function () { finish(true); });
            ed.addEventListener('click', function (e) { e.stopPropagation(); });
          });

          row.appendChild(inp);
          row.appendChild(out);
          lenBox.appendChild(row);
          return { input: inp, out: out, unit: unit || '' };
        }
        var sampleNow = Number(d.sample_chars == null ? 120 : d.sample_chars) || 120;
        lenSample = rangeRow('生成长度', 16, 4096, 8, sampleNow, '', function (v) {   /* 上限 4096（用户 2026-10-19）；不写单位（用户 2026-10-19） */
          api.post('/api/chat/limits', { sample_chars: v }).then(function (r) {
            if (!r || r.ok === false) notice((r && r.error) || '保存失败');
          });
        });
        var ctxNow = Number(d.chat_max_len || 0) || trained;
        ctxWanted = ctxNow;
        lenCtx = rangeRow('上下文长度', 32, 256, 32, ctxNow, '', function (v) {
          ctxWanted = v;
          /* 和服务端训练值一致就写 0（跟随训练值），避免无意义的覆盖 */
          var val = (v === ctxTrained()) ? 0 : v;
          api.post('/api/chat/limits', { chat_max_len: val }).then(function (r) {
            if (!r || r.ok === false) { notice((r && r.error) || '保存失败'); return; }
            notice(val ? '重新加载后生效' : '');
          });
        });
        /* 重复惩罚（用户 2026-10-19：只影响输出，所以放对话页、不放训练页） */
        var repNow = Number(d.repetition_penalty);
        if (!isFinite(repNow) || repNow <= 0) { repNow = 1.15; }
        lenRep = rangeRow('重复惩罚', 100, 150, 1, Math.round(repNow * 100), '', function (v) {
          api.post('/api/chat/limits', { repetition_penalty: v / 100 }).then(function (r) {
            if (!r || r.ok === false) { notice((r && r.error) || '保存失败'); }
          });
        }, function (v) { return (v / 100).toFixed(2); },
           function (t) { return Math.round(Number(t) * 100); });   /* 输入 1.20 → 内部 120 */
        function notice(t) {
          lenNoteMsg = t || '';
          paintNote();
        }
        paintNote = function () {
          if (lenNoteEl) lenNoteEl.textContent = modelLoaded ? (lenNoteMsg || '') : '';
        };
        var lenNote = api.el('div', 'kv-note', '');
        lenNoteEl = lenNote;
        lenNoteMsg = (d.chat_max_len && Number(d.chat_max_len) !== trained) ? '重新加载后生效' : '';
        paintNote();
        syncLimits();
        lenBox.appendChild(lenNote);

        /* 推理设备 + 权重：一个大框（用户 2026-10-18），权重小框预训练在上、LoRA 在下，只显示文件名 */
        function weightBox(label, path, exists) {
          var row = api.el('div', 'kv-wt');
          row.appendChild(api.el('span', 'kv-wt-k', label));
          var full = String(path || '');
          /* 文件不存在就当「未选择」（用户 2026-10-18）：不再显示别的工程留下的输出权重名 */
          var nm = api.el('span', 'kv-wt-v' + (exists ? '' : ' idle'),
                          exists ? full.split(/[\\/]/).pop() : '未选择');
          nm.title = exists ? full : '';
          row.appendChild(nm);
          if (exists) row.appendChild(badge('可用', 'ok'));
          return row;
        }
        /* 推理设备已移到「资源占用」瓷片（用户 2026-10-18），这里只留权重。
           2026-10-19：已经加载了模型就按「真正加载的是谁」显示 —— 原来照抄 config 的
           pretrain_out / sft_out，用「加载基座权重」导入的文件会挂在 LoRA 行上。 */
        repaintWeights = function () {
          var lm = loadedModel;
          infoBox.textContent = '';
          if (lm) {
            infoBox.appendChild(weightBox('基座', lm.base || lm.path, true));
            infoBox.appendChild(weightBox('LoRA', lm.adapter || '', !!lm.adapter));
          } else {
            /* 用户 2026-10-20：没点「加载」就不显示任何权重 —— 训练产出的 pretrain_out / sft_out
               不再自动同步到对话验证；LoRA 行也只认真正的适配器（以前这里挂的是 sft_out，普通
               微调权重会跑到 LoRA 框里）。 */
            infoBox.appendChild(weightBox('基座', '', false));
            infoBox.appendChild(weightBox('LoRA', '', false));
          }
        };
        repaintWeights();

        /* 预训练 / 微调 两块权重瓷片：有加载才填，没加载就是「—」（用户 2026-10-20） */
        function paintWeightTiles() {
          var lm = loadedModel;
          if (lm && lm.size_mb != null) {
            var src = String(lm.source || '');
            var isFinetuned = !!lm.adapter || /sft|微调|LoRA/i.test(src);
            if (isFinetuned) {
              tSft.text(lm.size_mb + ' MB', 'ok');
              tPre.text('—', 'idle');
            } else {
              tPre.text(lm.size_mb + ' MB', 'ok');
              tSft.text('—', 'idle');
            }
            paintCost((lm.size_mb * 1048576) / 4, true);
          } else {
            tSft.text('—', 'idle');
            tPre.text('—', 'idle');
            paintCost(0);
          }
        }

        /* 瓷片：权重大小 + 参数量/显存估算（用户 2026-10-17）
           2026-10-20：也改成只认「真正加载的权重」，不再照抄 config 里的训练产出。 */
        paintMachine(d.device || d.device_resolved);
        repaintTiles = paintWeightTiles;
        paintWeightTiles();
        /* 检查点文件 / 训练提示词 / 当前工程：按用户 2026-10-18 从卡片里去掉 */
      });

      /* ---------- 初始状态 + 历史 ---------- */
      api.get('/api/chat/status?load_since=0&gen_since=0').then(function (d) {
        if (d && d.ok) {
          if (d.model && d.model.loaded) {
            running = !!d.running;
            applyModel(d.model);
            line('sys', '模型已就绪 · device=' + d.model.device + ' · vocab=' + d.model.vocab
              + ' · 参数 ' + fmtInt(d.model.params));
          }
          loadCursor = (d.load && d.load.next) || 0;
          genCursor = (d.generate && d.generate.next) || 0;
          /* 切页面时权重正在加载：接手继续跟事件，加载完再刷新（用户 2026-10-18） */
          if (d.load && d.load.running) { setLoadUI(true); pollLoad(); }
          /* 挂载瞬间恰好加载刚结束（loading 已回 false 但状态里还没 loaded）时补查几次 */
          if (!(d.model && d.model.loaded) && d.load && d.load.exists) refreshModelOnce(4);
        }
      });

      /* 历史画完再写提示：loadHistory 会先 log.innerHTML = '' 清空，写在前面会被冲掉 */
      loadHistory(true).then(function () {
        if (!AUTO_EXITED) return;
        AUTO_EXITED = false;
        line('sys', '已离开本页超过 2 分钟，模型已自动退出运行。');
      });

      return null;
    },

    unmount: function () {
      try { armLeaveTimer(); } catch (e) { /* noop */ }   /* 离开本页：2 分钟没回来就自动退出运行 */
      try { if (offProj) offProj(); } catch (e) { /* noop */ }
      try { clearTimers(); } catch (e) { /* noop */ }
      try {
        if (DOC_CLICK) document.removeEventListener('click', DOC_CLICK);
        if (DOC_ESC) document.removeEventListener('keydown', DOC_ESC);
        DOC_CLICK = null; DOC_ESC = null;
      } catch (e) { /* noop */ }
    }
  });
})();
