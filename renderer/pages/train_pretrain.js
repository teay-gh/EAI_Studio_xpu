/* ============================================================
   EAI_Studio · pages/train_pretrain.js ——「预训练」页（Tk PretrainPage 迁移）
   导航键 train_pretrain / 中文名「预训练」/ 分组「训练中心」
   ------------------------------------------------------------
   Tk 源码：ENGINE_DIR/pages/train.py 的 PretrainPage（TrainPageBase 子类）
     title「预训练」（无副标题小字，用户 2026-10-14）
     左：参数表单（FIELDS/BOOLS）· 右：操作 + 训练日志 + Loss 曲线

   真实数据全部来自 bridge_api/train_pretrain.py：
     GET  /api/train_pretrain/status[?since=N]  配置 + 文件状态 + 宿主进程增量日志
     GET  /api/train_pretrain/log               日志文件尾部 + (step,train,val) 数据点
     POST /api/train_pretrain/save              写 config.json（train 段）
     POST /api/train_pretrain/start             起 core/train.py 预训练子进程（全局互斥）
     POST /api/train_pretrain/stop              写 stop.flag 优雅停止
   开始后每 1s 轮询 status?since=<游标> 追加新行到日志框；结束后刷新状态与 Loss 曲线。
   导出 = not_implemented → 无导出按钮（本页不涉及）。

   mock 只在 ?mock=1 预览截图时用；_registry.js 的 mock 按整条 path 精确匹配，
   所以带 query 的路径也各留一份。正常跑一律走真实桥接口。
   ============================================================ */
(function () {
  'use strict';

  /* 表单定义（照 Tk PretrainPage 的 FIELDS / BOOLS）：
     用户口径 2026-10-14 —— 不要小字介绍（含悬停提示）、无「保存配置」（改动即存）、
     采样预览默认开启不可关（无开关）、不显示语料地址（语料只由点「开始训练」时的数据集合并产生）。 */
  var FIELDS = [
    ['模型维度', 'd_model'],
    ['注意力头数', 'n_head'],
    ['网络层数', 'n_layer'],
    ['上下文长度', 'block_size'],
    ['批量大小', 'batch_size'],
    ['丢弃率', 'dropout'],
    ['步数', 'pretrain_steps'],
    ['学习率', 'pretrain_lr'],
  ];
  var BOOLS = [
    ['混合精度 AMP', 'amp'],
    ['Multi-Token Prediction', 'mtp'],
  ];

  /* 参数说明（用户 2026-10-16：左键点参数名 → 弹窗看说明）；键=页面标签 */
  var PARAM_HELP = {
    '模型维度': '隐藏层宽度：每个 token 表示成这么长的向量，各层矩阵的宽都由它决定。越大表达力越强，参数量按平方级涨。\n' +
      '怎么定值：先按显存定档——8GB 用 128，12~16GB 用 256，24GB 以上 384~512。语料小（<10MB）先别超过 256。\n' +
      '约束：d_model 必须等于 n_head × 每头维度，每头 32~64 维最稳（128 配 2~4 头，256 配 4~8 头）。',
    '注意力头数': '把向量切成几份：把 d_model 切成几份并行做注意力，每头维度 = d_model ÷ n_head。\n' +
      '怎么定值：先定 d_model，再按每头 32~64 维反推（128→2~4 头，256→4~8 头，384→6~12 头）。\n' +
      '必须是 d_model 的约数；每头低于 16 维基本学不到长程关系。改 d_model 后要重新算它。',
    '网络层数': 'Transformer 层数：堆几层，决定模型的深度和能建模的抽象层级。\n' +
      '怎么定值：从 4 层起步；128 维模型 4~6 层，256 维 6~12 层。\n' +
      '判断信号：训练 Loss 很快接近 0、验证 Loss 却升高 = 参数量相对语料过大，减层或加数据；两边都降不下去再加层。',
    '上下文长度': '上下文长度：一条样本一次最多看多少个 token，超出的部分会被切开。\n' +
      '怎么定值：统计语料句子长度，取能覆盖约 95% 样本的长度（常见 128 / 256 / 512）。比最长句短会把句子截断，学不到完整语义。\n' +
      '显存占用约与 block_size × batch_size 成正比：先定 block_size，再用 batch_size 把显存填满。',
    '批量大小': '批量大小：每一步同时喂进多少条序列，影响梯度噪声和显存占用。\n' +
      '怎么定值：从 8~16 起，逐步翻倍到显存接近满。等效批量 = batch_size × 梯度累积，想让梯度稳定一般落在 32~128。\n' +
      '显存不够时优先减 batch_size 再补梯度累积——等效批量不变，Loss 曲线还能直接对比。',
    '丢弃率': '丢弃率：训练时按比例随机把激活置零，用来抑制过拟合。\n' +
      '怎么定值：小语料 0.1~0.2，大语料或大模型 0~0.1。\n' +
      '判断信号：验证 Loss 明显高于训练 Loss（差 20% 以上）就往 0.15→0.2 加；两边都高说明欠拟合，应该减小 dropout。',
    '步数': '总优化步数：跑满后自动保存权重并结束。\n' +
      '怎么定值：1 个 epoch 的步数 = 语料 token 数 ÷（batch_size × block_size），一般训练 2~4 个 epoch 的步数。\n' +
      '例：10MB 语料约 500 万 token，batch 32 × block 128 ≈ 4096 → 1 epoch ≈ 1200 步，2~4 epoch 就是 2400~4800 步。Loss 进入平台期就可以停，硬堆步数只会过拟合。',
    '学习率': '每步更新权重的幅度，最影响成败的一个数。\n' +
      '怎么定值：从零预训练常用 1e-3~3e-3（当前 0.002 就在这个档）；续训/微调取它的 1/5~1/10。\n' +
      '判断信号：Loss 抖动上翘或出 NaN → 减半；曲线几乎水平 → 乘 1.5~2 再试。配上 warmup 和余弦衰减更稳。',
    '混合精度 AMP': '用 bf16/fp16 做前向反向、fp32 更新权重：省显存、提速，精度基本无损。\n' +
      '怎么定值：XPU / CUDA 都建议开，优先 bf16（不需要 loss scale）。只有 fp16 的老卡若出现 Loss = NaN，就关掉 AMP 或换 bf16。',
    'Multi-Token Prediction': '一次预测后面 k 个 token 而不是只预测下一个：表征学得更充分、收敛更快。\n' +
      '怎么定值：默认开启、k=3。语料很小（<1MB）或任务对顺序极敏感时可以关掉，避免被迫预测过远反而干扰。',
  };

  /* ---------- 只给 ?mock=1 的预览数据（正常跑不生效） ---------- */
  var STATUS_MOCK = {
    stage: 'pretrain',
    project: '<工程目录>',
    config_path: '<应用目录>\\engine\\config.json',
    source: 'config.json#train',
    running: false,
    running_note: '训练进程由桥宿主托管（core/train.py 子进程）。',
    config: {
      device: 'cpu', method: 'full', seed: 1337, d_model: 128, n_head: 4, n_layer: 4,
      block_size: 128, batch_size: 32, dropout: 0.1, grad_clip: 1.0, grad_accum: 1,
      weight_decay: 0.05, amp: true, amp_dtype: 'bf16', mtp: true, mtp_k: 3,
      sample: true, cosine_lr: true, pretrain_corpus: '<工程目录>\\data/corpora\\pretrain_corpus.txt',
      pretrain_steps: 30000, pretrain_lr: 0.002, pretrain_warmup: 500,
      pretrain_eval_every: 300, pretrain_out: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
      pretrain_log_csv: '<工程目录>\\logs\\pretrain_loss.csv',
    },
    resolved: {
      out: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
      log_csv: '<工程目录>\\logs\\pretrain_loss.csv',
      corpus: '<工程目录>\\data/corpora\\pretrain_corpus.txt',
    },
    corpus: { path: '<工程目录>\\data/corpora\\pretrain_corpus.txt',
      name: 'pretrain_corpus.txt', exists: true, size_bytes: 1843200, size_mb: 1.76, mtime: '2026-10-11 15:02:10' },
    checkpoint: { path: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
      name: 'eproai_pretrain.pt', exists: false, size_bytes: 0, size_mb: null, mtime: null },
    log_file: { path: '<工程目录>\\logs\\pretrain_loss.csv',
      name: 'pretrain_loss.csv', exists: true, size_bytes: 512, size_mb: 0.0,
      mtime: '2026-10-11 15:20:33', lines: 6 },
    runtime: { dir: '<工程目录>\\logs', dir_exists: true,
      runtime_json: '<工程目录>\\logs\\runtime_pretrain_full.json',
      runtime_json_exists: false,
      stop_flag: '<工程目录>\\logs\\stop.flag', stop_flag_exists: false },
    pid: null, exit_code: null, lines: [], next: 0, total_lines: 0,
    elapsed: null, stop_requested: false, job_exists: false, running_job: null,
    outputs: null,
  };

  var LOG_MOCK = {
    path: '<工程目录>\\logs\\pretrain_loss.csv',
    exists: true,
    columns: 'step,train_loss,val_loss,lr,elapsed',
    total_lines: 6, total_rows: 5,
    points: [
      { step: 0, train: 3.9120, val: 3.9301, lr: 0.000004, elapsed: 0.6 },
      { step: 300, train: 3.1044, val: 3.1820, lr: 0.001902, elapsed: 12.4 },
      { step: 600, train: 2.7310, val: 2.8402, lr: 0.001998, elapsed: 24.1 },
      { step: 900, train: 2.4122, val: 2.5590, lr: 0.001935, elapsed: 35.9 },
      { step: 1200, train: 2.2087, val: 2.4011, lr: 0.001740, elapsed: 47.6 },
    ],
    tail: 'step,train_loss,val_loss,lr,elapsed\n' +
      '0,3.9120,3.9301,4.000000e-06,0.6\n' +
      '300,3.1044,3.1820,1.902000e-03,12.4\n' +
      '600,2.7310,2.8402,1.998000e-03,24.1\n' +
      '900,2.4122,2.5590,1.935000e-03,35.9\n' +
      '1200,2.2087,2.4011,1.740000e-03,47.6',
  };

/* ---------- 取数缓存（治切页闪烁，2026-10-15，同 repo.js） ----------
   prefetch 与 mount 共用一趟请求：换页前把数据取回，首帧同步直出，后台再刷新；
   800ms 内的重复取数直接吃缓存（正好盖住 prefetch→mount 的间隙）。 */
var TP_CACHE = null, TP_CACHE_AT = 0, TP_INFLIGHT = null;
  /* 页面级定时器 / 曲线重算监听：必须挂在模块作用域，unmount 才摘得掉（2026-10-17） */
  var pollTimer = null;
  var fitOnResize = null;
function TPFetch(api, force) {{
  if (TP_INFLIGHT) return TP_INFLIGHT;
  if (!force && TP_CACHE && (Date.now() - TP_CACHE_AT) < 800) return Promise.resolve(TP_CACHE);
  TP_INFLIGHT = Promise.all([
    api.get('/api/train_pretrain/status'),
    api.get('/api/train_pretrain/log?tail=200'),
  ]).then(function (a) {{
    TP_CACHE = { status: a[0], log: a[1] };
    TP_CACHE_AT = Date.now();
    TP_INFLIGHT = null;
    return TP_CACHE;
  }})["catch"](function () {{ TP_INFLIGHT = null; return TP_CACHE; }});
  return TP_INFLIGHT;
}}
  EAIPages.register('train_pretrain', {
    title: '预训练',

    mock: {
      '/api/train_pretrain/status': STATUS_MOCK,
      '/api/train_pretrain/status?since=0': STATUS_MOCK,
      '/api/train_pretrain/log': LOG_MOCK,
      '/api/train_pretrain/log?tail=200': LOG_MOCK,
      '/api/train_pretrain/save': { ok: true, config_path: '<应用目录>\\engine\\config.json', saved: {} },
      '/api/train_pretrain/start': { ok: true, job: 'train:pretrain:full', pid: 0,
        config: { steps: 30000, corpus: '<工程目录>\\data/corpora\\pretrain_corpus.txt',
          out: '<工程目录>\\checkpoints\\eproai_pretrain.pt' } },
      '/api/train_pretrain/stop': { ok: true, note: '已写入 stop.flag，训练会保存权重后优雅退出。' },
      '/api/train_pretrain/export': { not_implemented: true, reason: '迁移期不导出/打包权重文件' },
    },

    prefetchTimeout: 800,
    prefetch: function (api) { return TPFetch(api); },

    mount: function (root, api) {
      /* ---------- 小工具 ---------- */
      function fmtInt(n) {
        n = Number(n) || 0;
        return n.toLocaleString('en-US');
      }
      function fmtBytes(n) {
        n = Number(n) || 0;
        if (n <= 0) return '0 B';
        var u = ['B', 'KB', 'MB', 'GB'], i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return n.toFixed(i ? 1 : 0) + ' ' + u[i];
      }
      function icon(paths) {
        var s = document.createElement('span');
        s.className = 'tp-ic';
        s.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
          'aria-hidden="true">' + paths + '</svg>';
        return s;
      }
      function btn(label, cls, title, disabled) {
        var b = api.el('button', cls, label);
        b.type = 'button';
        b.disabled = disabled === true;           /* 只有显式 true 才置灰 */
        if (title) b.title = title;
        return b;
      }
      var NS = 'http://www.w3.org/2000/svg';
      function svgEl(tag, attrs) {
        var e = document.createElementNS(NS, tag);
        for (var k in attrs) e.setAttribute(k, attrs[k]);
        return e;
      }

      /* ---------- 骨架 ---------- */
      /* 用户 2026-10-14：大标题下不要小字介绍 */
      root.appendChild(api.hero('预训练'));

      var grid = api.el('div', 'tp-grid');
      root.appendChild(grid);

      /* ===== 左：参数表单 ===== */
      var leftCol = api.el('div', 'tp-col');
      grid.appendChild(leftCol);

      var paramsCard = api.card('');   /* 「参数」标题移到命名分隔线下（用户 2026-10-16），卡头不再放标题 */
      var phead = paramsCard.querySelector('.card-head');
      if (phead) phead.remove();
      var form = api.el('div', 'tp-form');
      paramsCard.body.appendChild(form);
      leftCol.appendChild(paramsCard);

      /* 模型权重命名（用户 2026-10-15 终版）：在参数卡内、参数上方；
         标题与「参数」同格式，下方同款分隔条；输入框在左、提示在右（√ / 重复红字）。 */
      var wname = api.weightName({ fallback: 'eproai_pretrain' });
      form.appendChild(wname.wrap);
      form.appendChild(api.el('div', 'tp-sep'));
      form.appendChild(api.el('h2', 'card-title', '参数'));   /* 「参数」在命名下划线下、d_model 上（用户 2026-10-16） */
      wname.input.addEventListener('input', autoSave);
      wname.input.addEventListener('change', autoSave);
      var outRaw = '';   /* 当前 out 原值：保留目录部分、只换文件名（相对/绝对口径不变） */
      function outValueFor(stem) {
        var cur = String(outRaw || '');
        var i = Math.max(cur.lastIndexOf('/'), cur.lastIndexOf('\\'));
        return (i >= 0 ? cur.slice(0, i + 1) : 'checkpoints/') + stem + '.pt';
      }

      var fieldEls = {};
      var dotRows = {};        /* 参数行状态小球（用户 2026-10-18） */
      function addField(label, name, wide) {
        var row = api.el('div', 'tp-field' + (wide ? ' wide' : ''));
        var lbl = api.el('label', 'tp-label', label);
        api.help(lbl, label, PARAM_HELP[label]);   /* 点参数名看说明（用户 2026-10-16） */
        var inp = document.createElement('input');
        inp.className = 'tp-input';
        inp.type = 'text';
        inp.value = '';
        row.appendChild(lbl);
        row.appendChild(inp);
        inp.className += ' pd-input';               /* 缩短输入框，右侧留给状态小球（用户 2026-10-18） */
        var dot = api.el('span', 'pd-dot', '');
        dot.dataset.level = 'empty';
        row.appendChild(dot);
        dotRows[name] = { input: inp, dot: dot };
        inp.addEventListener('input', autoSave);    /* 数据一经更改就直接保存 */
        inp.addEventListener('change', autoSave);
        form.appendChild(row);
        fieldEls[name] = inp;
        return inp;
      }
      FIELDS.forEach(function (f) { addField(f[0], f[1], false); });
      api.bindParamDots('pretrain', dotRows, collectUpdates);
      /* 检查点（用户 2026-10-19）：中途按等分点存中间权重，过拟合可取半成品 */
      var ckptEl = (function () {
        var row = api.el('div', 'tp-field tp-field-range');
        var lbl = api.el('label', 'tp-label', '检查点数量');
        api.help(lbl, '检查点数量',
          '训练中途额外保存几份权重：设 1 就在一半处存一次，设 2 就在 1/3、2/3 处各存一次（k/(N+1) 等分点）。\n' +
          '用途：训练后期过拟合（验证 Loss 往上翘）时，可以回到中途那份继续用。\n' +
          '文件存 checkpoints/，名字是「权重名-ck步数.pt」，仓库权重弹窗里能直接选。');
        var box = api.el('div', 'tp-range-box');
        var inp = document.createElement('input');
        inp.type = 'range';
        inp.className = 'tp-range';
        inp.min = '0'; inp.max = '5'; inp.step = '1';
        var val = api.el('span', 'tp-range-v', '');
        function paint() { val.textContent = Number(inp.value) ? String(inp.value) : ((window.I18N && window.I18N.lang() === 'en') ? 'Off' : '关闭'); }   /* 不带单位（用户 2026-10-20） */
        inp.addEventListener('input', paint);
        inp.addEventListener('change', function () { paint(); autoSave(); });
        box.appendChild(inp);
        box.appendChild(val);
        api.rangeEdit(val, inp);                    /* 点数字可直接输入（用户 2026-10-20） */
        row.appendChild(lbl);
        row.appendChild(box);
        form.appendChild(row);
        paint();
        return { input: inp, paint: paint };
      })();

      form.appendChild(api.el('div', 'tp-sep'));
      var boolEls = {};
      BOOLS.forEach(function (b) {
        var row = api.el('div', 'tp-check');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'tp-cb';
        var lbl = api.el('span', 'tp-check-label', b[0]);
        api.help(lbl, b[0], PARAM_HELP[b[0]]);   /* 点参数名看说明（用户 2026-10-16） */
        cb.addEventListener('change', autoSave);    /* 数据一经更改就直接保存 */
        row.appendChild(cb);
        row.appendChild(lbl);
        form.appendChild(row);
        boolEls[b[1]] = cb;
      });

      /* ===== 右：操作 + 日志 + 曲线 ===== */
      var rightCol = api.el('div', 'tp-col');

      /* 动作行：开始 / 停止 / 刷新（等宽、靠左；改动即存，无「保存配置」按钮）
         用户 2026-10-14：删「重读日志」并入本行；文案缩成 开始/停止/刷新 */
      var actions = api.el('div', 'tp-actions');
      var startBtn = btn('', 'btn btn-accent');
      startBtn.appendChild(api.el('span', null, '▶ 开始'));   /* span 包裹与其余三页同款（用户 2026-10-16：裸文本 line-height normal 字框 17px、别页 span 13px，字高不齐） */
      var stopBtn = btn('', 'btn tp-btn-danger', null, true);
      stopBtn.appendChild(icon('<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>'));   /* 暂停图标（两竖条，用户 2026-10-15） */
      stopBtn.appendChild(api.el('span', null, '停止'));
      var refreshBtn = btn('', 'tp-btn2', null, false);
      refreshBtn.appendChild(icon('<path d=\"M23 4v6h-6\"/><path d=\"M20.49 15a9 9 0 1 1-2.12-9.36L23 10\"/>'));
      refreshBtn.appendChild(api.el('span', null, '刷新'));
      actions.appendChild(startBtn);
      actions.appendChild(stopBtn);
      actions.appendChild(refreshBtn);
      rightCol.appendChild(actions);

      /* 概览统计（4 张瓷片，与 Tk 手感一致） */
      var stats = api.el('div', 'stats');
      function tile(label) {
        var st = api.el('div', 'stat');
        var lb = api.el('div', 'stat-label', label);
        st.appendChild(lb);
        var v = api.el('div', 'stat-val', '')   /* 用户 2026-10-17：先空着，别画占位「—」，否则切页会跟状态文字换一下=闪 */;
        st.appendChild(v);
        stats.appendChild(st);
        v.labelEl = lb;                        /* 内存/显存瓷片要按设备改标签文字 */
        return v;
      }
      var vState = tile('训练状态');
      var vMem = tile('占用内存');
      var vCkpt = tile('输出权重');
      var vCorpus = tile('语料体量');
      vState.textContent = '未启动';
      vState.classList.add('idle');   /* 首帧先放终态，避免切页时空白→文字跳一下（用户 2026-10-17） */
      rightCol.appendChild(stats);

      /* 两块日志区互不覆盖：
         ① 训练输出 —— 只由 /status?since= 的 lines 与页面提示行追加，任何刷新都碰不到它；
         ② Loss 日志表 —— 只由 /log 的真实 CSV 尾部写入，「刷新状态」连带重读它。 */
      var liveCard = api.card('训练输出');
      /* 外框定距 + 内层滚动（用户 2026-10-14）：文字滚动时在离边框 12px 处被裁，
         底边间距与顶边一致（以前文本会穿过 padding 区贴住/切在边框上） */
      var liveBox = api.el('div', 'tp-logbox');
      var livePre = api.el('pre', 'tp-log tp-log-live');   /* 空态不写占位文字（用户 2026-10-17） */
      liveBox.appendChild(livePre);
      liveCard.body.appendChild(liveBox);
      rightCol.appendChild(liveCard);

      var csvCard = api.card('Loss 日志表');
      var csvBox = api.el('div', 'tp-logbox');
      var csvPre = api.el('pre', 'tp-log tp-log-csv', '读取中…');
      csvBox.appendChild(csvPre);
      csvCard.body.appendChild(csvBox);
      rightCol.appendChild(csvCard);

      var chartCard = api.card('Loss 曲线');
      var chart = api.el('div', 'tp-chart');
      chartCard.body.appendChild(chart);
      rightCol.appendChild(chartCard);

      grid.appendChild(rightCol);
      fitOnResize = scheduleFit;
      window.addEventListener('resize', fitOnResize);
      scheduleFit();

      /* ---------- 渲染 ---------- */
      function fmtGB(n) {
        if (n == null) return '—';
        return (n / (1024 * 1024 * 1024)).toFixed(1) + ' GB';   /* GB 保留 1 位小数（用户 2026-10-14） */
      }
      /* 瓷片数值压成单行（用户 2026-10-16）：单位不留空格、KB 取整、自动升档 */
      function tileSize(n) {
        return api.tileSize(n);   /* 共享口径：三位数不带小数（用户 2026-10-18） */
      }

      /* 内存/显存水位告警阶梯（用户 2026-10-14）：
         XPU（看本机剩余可用内存）：<6GB 红字 → <3GB 弹窗 → <1GB 暂停训练（存档退出）+ 弹窗
         CUDA（看剩余显存）：<3GB 红字 → <2GB 弹窗 → <1GB 停止训练
         水位只在恶化跨档时触发一次，恢复后复位。 */
      var memAlert = 0;
      function watchMem(mem, isCuda, running) {
        var free = isCuda ? mem.gpu_free : mem.sys_avail;
        if (free == null || !free) { vMem.classList.remove('danger'); memAlert = 0; return; }
        var GB = 1024 * 1024 * 1024;
        var red = (isCuda ? 3 : 6) * GB;
        var pop = (isCuda ? 2 : 3) * GB;
        vMem.classList.toggle('danger', free < red);
        var lvl = free < GB ? 3 : (free < pop ? 2 : (free < red ? 1 : 0));
        if (lvl > memAlert) {
          if (lvl >= 2) {
            api.confirm({
              title: isCuda ? '显存不足' : '内存不足',
              message: bi((isCuda ? '剩余显存 ' : '本机剩余可用内存 ') + fmtGB(free) +
                  '，已低于 ' + (isCuda ? 2 : 3) + ' GB。',
                  (isCuda ? 'Free VRAM ' : 'Free RAM ') + fmtGB(free) +
                  ', below the ' + (isCuda ? 2 : 3) + ' GB threshold.'),   /* 2026-10-23：拼接句自带英文 */
              detail: lvl >= 3
                ? (isCuda ? '已停止训练。' : '已暂停训练（进度已保存）。')
                : '请及时关闭占内存的程序，以免训练中断。',
              buttons: ['知道了'],
            });
          }
          if (lvl >= 3 && running) {
            api.post('/api/train_pretrain/stop', {}).then(function (r) {
              appendLog(r && r.ok
                ? ('⚠ ' + (isCuda ? '显存' : '内存') + '不足 1GB，已自动' + (isCuda ? '停止' : '暂停') + '训练（进度保存后退出）。')
                : ('⚠ 自动停止失败：' + ((r && r.error) || '未知错误')), true);
            });
          }
        }
        memAlert = lvl;
      }

      function renderStatus(d) {
        if (!d || d.error) {
          var err = (d && d.error) || '未知错误';
          /* 状态/配置读取失败属于页面提示行 → 只追加进实时块，不动 CSV 块 */
          appendLog('状态读取失败：' + err, true);
          [vState, vMem, vCkpt, vCorpus].forEach(function (v) { v.textContent = '—'; });
          return;
        }
        var cfg = d.config || {};
        Object.keys(fieldEls).forEach(function (name) {
          if (name in cfg && cfg[name] !== undefined) fieldEls[name].value = String(cfg[name]);
        });
        Object.keys(boolEls).forEach(function (name) {
          boolEls[name].checked = !!cfg[name];
        });
        var cc = parseInt(cfg.ckpt_count, 10);          /* 检查点数量（用户 2026-10-19） */
        ckptEl.input.value = (isFinite(cc) && cc > 0) ? Math.min(5, cc) : 0;
        ckptEl.paint();

        /* 权重命名不回填（用户 2026-10-17：新工程不要默认命名，名字由用户自己填）；
           outRaw 只用于算输出目录，不再往命名框填字。 */
        outRaw = String(cfg.pretrain_out || (d.resolved && d.resolved.out) || '');
        /* 用户自己填过的权重名要回填（用户 2026-10-19：切页回来别把名字弄丢）；默认名不回填 */
        (function () {
          var f = String(outRaw || '').split(/[\\/]/).pop().replace(/\.pt$/i, '');
          if (f && f !== 'eproai_pretrain' && f !== 'eproai' && !wname.stem()) { wname.input.value = f; wname.audit(); }
        })();

        var ck = d.checkpoint || {};
        vCkpt.textContent = ck.exists ? tileSize(ck.size_bytes) : '未生成';
        vCkpt.classList.toggle('ok', !!ck.exists);
        vCkpt.classList.toggle('idle', !ck.exists);   /* 未生成=灰（用户 2026-10-17） */
        if (ck.mtime) vCkpt.title = ck.mtime;

        paintMem(d);

        var cp = d.corpus || {};
        vCorpus.textContent = cp.exists ? tileSize(cp.size_bytes) : '缺失';
        vCorpus.classList.toggle('ok', !!cp.exists);
        vCorpus.classList.toggle('missing', !cp.exists);   /* 缺失=红（用户 2026-10-18，同 LoRA 合并页） */
        vCorpus.classList.remove('warn');

        updateRunningUI(!!d.running, d);
        maybeResume(d);   /* 用户 2026-10-19：切走再回来要接着显示输出、接着轮询 */
        scheduleFit();   /* 瓷片/日志高度一变，曲线卡可用高度跟着重算 */
      }

      /* 切页回来接管（用户 2026-10-19，2026-10-20 放宽）：有输出就恢复显示——不管训练
         还在不在跑。「不开新训练切页，旧的训练输出会消失」就是这里卡的：以前要求
         d.running，跑完的旧输出切页回来就没了。训练在跑才接着轮询。 */
      function maybeResume(d) {
        if (!d || pollTimer || liveStarted) return;
        var lines = d.lines || [];
        if (!lines.length) return;
        livePre.classList.remove('tp-err');
        livePre.textContent = '';
        liveLines = [];
        if (lines.length) appendLog(lines.join('\n'));
        cursor = (typeof d.next === 'number') ? (d.next || lines.length) : (lines.length || 0);
        if (d.running) startPolling();
      }

      /* 占用内存瓷片（用户 2026-10-18）：以前只在进页面/点开始那一刻画一次，
         那时训练进程刚起来，工作集还取不到 → 一直显示「—」。
         现在轮询里每秒重画，训练一跑就有数。 */
      function paintMem(d) {
        var mem = (d && d.mem) || {};
        var isCuda = (mem.device || 'cpu') === 'cuda';
        vMem.labelEl.textContent = isCuda ? '占用显存' : '占用内存';   /* 用户 2026-10-15：不带「EAI 」前缀 */
        var usedBytes = isCuda ? mem.gpu_used : mem.proc_bytes;
        vMem.textContent = tileSize(usedBytes);
        vMem.classList.toggle('ok', usedBytes != null);
        vMem.title = isCuda
          ? ('GPU 显存 已用 ' + fmtGB(mem.gpu_used) + ' / ' + fmtGB(mem.gpu_total) + '，剩余 ' + fmtGB(mem.gpu_free))
          : (mem.proc_bytes == null ? '训练进程未运行'
            : ('训练进程工作集（pid ' + ((d && d.pid) || '') + '），本机剩余可用 ' + fmtGB(mem.sys_avail)));
        watchMem(mem, isCuda, !!(d && d.running));
      }

      /* 训练态：开始禁用 / 停止启用，状态瓷片与页根类跟着变 */
      function updateRunningUI(running, d) {
        training = !!running;
        startBtn.disabled = running;
        stopBtn.disabled = !running;
        vState.textContent = running ? '运行中' : '未启动';
        vState.classList.toggle('ok', running);
        vState.classList.toggle('idle', !running);   /* 未启动=灰（用户 2026-10-17） */
        if (running && d && d.pid != null) vState.title = 'pid ' + d.pid;
        root.classList.toggle('tp-running', running);
        /* 左侧参数训练中禁改（用户 2026-10-20：「无论是否使用流水线功能，当开启训练后，
           左侧框变灰，不让用户更改」）：置灰靠 CSS，表单控件真禁用兜住键盘。 */
        [].forEach.call(form.querySelectorAll('input, select, textarea'), function (el) {
          if (running) el.setAttribute('disabled', ''); else el.removeAttribute('disabled');
        });
      }

      function drawChart(points) {
        chart.textContent = '';
        if (!points || points.length < 2) {
          chart.appendChild(api.el('div', 'tp-chart-empty', '暂无足够数据点'));
          return;
        }
        var W = 640, H = 200, pad = 34;
        var xs = points.map(function (p) { return p.step; });
        var tr = points.map(function (p) { return p.train; });
        var va = points.map(function (p) { return p.val; });
        var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
        var ymin = Math.min.apply(null, tr.concat(va));
        var ymax = Math.max.apply(null, tr.concat(va));
        if (xmax - xmin < 1e-9) xmax = xmin + 1;
        if (ymax - ymin < 1e-9) ymax = ymin + 1;
        function X(x) { return pad + (x - xmin) / (xmax - xmin) * (W - 2 * pad); }
        function Y(y) { return H - pad - (y - ymin) / (ymax - ymin) * (H - 2 * pad); }

        var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none',
          role: 'img', 'aria-label': 'loss 曲线' });
        var i, y;
        for (i = 0; i < 4; i++) {
          y = pad + i * (H - 2 * pad) / 3;
          svg.appendChild(svgEl('line', { x1: pad, y1: y, x2: W - pad, y2: y,
            stroke: 'var(--LINE_SOFT)', 'stroke-width': 1 }));
        }
        svg.appendChild(svgEl('line', { x1: pad, y1: H - pad, x2: W - pad, y2: H - pad,
          stroke: 'var(--LINE)', 'stroke-width': 1 }));

        function poly(ys, color) {
          var pts = ys.map(function (v, k) { return X(xs[k]) + ',' + Y(v); }).join(' ');
          svg.appendChild(svgEl('polyline', { points: pts, fill: 'none', stroke: color,
            'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
        }
        poly(tr, 'var(--ACCENT)');
        poly(va, 'var(--OK)');

        function label(x, yy, text, anchor) {
          var t = svgEl('text', { x: x, y: yy, fill: 'var(--INK3)', 'font-size': 11, 'text-anchor': anchor });
          t.textContent = text;
          svg.appendChild(t);
        }
        label(W - 8, Y(ymax) + 4, ymax.toFixed(2), 'end');
        label(W - 8, Y(ymin) + 4, ymin.toFixed(2), 'end');
        label(pad, pad - 8, '— train', 'start');
        label(pad + 60, pad - 8, '— val', 'start');
        chart.appendChild(svg);
      }

      /* Loss 曲线高度按窗口可用空间收口（用户 2026-10-17：曲线部分跑到窗口/卡片边框外）。
         卡头 + 内边距先量出来扣掉，余量全给图；上限 190、下限 120，再小就让整页滚动。 */
      var fitQueued = false;
      function fitChart() {
        fitQueued = false;
        if (!chartCard || !chartCard.isConnected) { return; }
        var content = document.querySelector('.content');
        if (!content) { return; }
        var cr = content.getBoundingClientRect();
        var top = chartCard.getBoundingClientRect().top - cr.top + content.scrollTop;
        var chrome = chartCard.offsetHeight - chart.offsetHeight;   /* 卡头 + 内边距 + 描边 */
        var avail = content.clientHeight - top - 20;   /* 保留：仅用于 190 上限的判断，高度已固定 */
        var h = 180;   /* Loss 曲线固定高度（用户 2026-10-18）：不随数据点/可用空间变化 */
        if (h > 0) { chart.style.height = h + 'px'; }
      }
      function scheduleFit() {
        if (fitQueued) { return; }
        fitQueued = true;
        requestAnimationFrame(fitChart);
      }

      /* CSV 日志表：只写 csvPre；训练输出的实时块不受影响 */
      function renderLog(d) {
        if (!d || d.error) {
          csvPre.textContent = '日志读取失败：' + ((d && d.error) || '未知错误');
          csvPre.classList.add('tp-err');
          drawChart([]);
          scheduleFit();
          return;
        }
        csvPre.classList.remove('tp-err');
        if (!d.exists || !d.total_rows) {
          csvPre.textContent = '暂无训练日志';   /* 不带地址；只有表头也算未训练（用户 2026-10-17） */
        } else {
          csvPre.textContent = d.tail && d.tail.length ? d.tail : '（日志为空）';
        }
        csvPre.scrollTop = csvPre.scrollHeight;   /* 用户 2026-10-19：永远停在最新一行，别停在表头 */
        drawChart(d.points || []);
        scheduleFit();
      }

      /* ---------- 训练实时日志 ---------- */
      var cursor = 0;
      var training = false;
      var logTick = 0;

      /* 实时输出：行缓冲追加，只写 livePre。dedupe=true 时连续重复行只留一行
         （轮询期的错误提示不至于刷屏）；上限 2000 行，防长跑把页面拖死。 */
      var MAX_LIVE = 50;   /* 实时日志只留最近 50 行（用户 2026-10-19） */
      var liveLines = [];
      var liveStarted = false;

      function appendLog(text, dedupe) {
        if (text == null || text === '') return;
        if (!liveStarted) { liveStarted = true; liveLines = []; }  /* 首行到来 → 丢掉占位提示 */
        var parts = String(text).split('\n');
        for (var i = 0; i < parts.length; i++) {
          if (i === parts.length - 1 && parts[i] === '') continue;
          if (dedupe && liveLines.length && liveLines[liveLines.length - 1] === parts[i]) continue;
          liveLines.push(parts[i]);
        }
        if (liveLines.length > MAX_LIVE) liveLines = liveLines.slice(-MAX_LIVE);
        livePre.textContent = liveLines.join('\n');
        livePre.scrollTop = livePre.scrollHeight;
        scheduleFit();   /* 训练输出变长 → 曲线卡要重新收口 */
      }

      function collectUpdates() {
        var body = {};
        Object.keys(fieldEls).forEach(function (name) {
          body[name] = fieldEls[name].value;
        });
        Object.keys(boolEls).forEach(function (name) {
          body[name] = !!boolEls[name].checked;
        });
        body.sample = true;   /* 采样预览默认开启、不可关闭（页面无开关） */
        if (ckptEl) { body.ckpt_count = Number(ckptEl.input.value); }   /* 0~5 个检查点（用户 2026-10-19） */
        var stemName = wname.stem();      /* 命名由用户自己填；没填就不写 out（2026-10-17） */
        if (stemName) { body.pretrain_out = outValueFor(stemName); }
        return body;
      }

      function pollOnce() {
        return api.get('/api/train_pretrain/status?since=' + cursor).then(function (d) {
          if (!d || d.error) {
            appendLog('[状态错误] ' + ((d && d.error) || '未知错误'), true);
            return;
          }
          var lines = d.lines || [];
          if (lines.length) appendLog(lines.join('\n'));
          if (typeof d.next === 'number') cursor = d.next;
          if (d.running) {
            updateRunningUI(true, d);
            paintMem(d);                       /* 占用内存随训练实时刷新（用户 2026-10-18） */
            if (++logTick % 5 === 0) loadLog();   /* Loss 表/曲线每 5 秒跟一次（用户 2026-10-19） */
          } else {
            finishRun(d);
          }
        });
      }

      function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollOnce, 1000);
        pollOnce();
      }

      function finishRun(d) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        var code = (d && d.exit_code != null) ? ('（退出码 ' + d.exit_code + '）') : '';
        appendLog('训练进程结束' + code + '。');
        /* 训练结束/被中止的这一刻权重才落盘 → 立刻重判一次命名（用户 2026-10-22）。
           不重判的话，本页仍显示「√ / 可训练」，要切页再回来才变「重复」。
           两次：250ms 抢在落盘后立刻判，1600ms 兜一次写盘慢的。 */
        setTimeout(function () { if (wname) wname.audit(); }, 250);
        setTimeout(function () { if (wname && !(wname.state && wname.state.dup)) wname.audit(); }, 1600);
        updateRunningUI(false, d);
        loadStatus();
        loadLog();
      }

      /* ---------- 动作 ---------- */
      /* 数据一经更改就直接保存（无「保存配置」按钮；成功静默，失败才提示） */
      var saveTimer = null;
      function autoSave() {
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
          saveTimer = null;
          api.post('/api/train_pretrain/save', collectUpdates()).then(function (d) {
            if (!d || d.error || d.ok === false) {
              appendLog('保存失败：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            }
          });
        }, 400);
      }

      function doStart() {
        if (training) return;
        livePre.classList.remove('tp-err');
        liveStarted = false;
        liveLines = [];
        livePre.textContent = '';
        appendLog('正在保存配置并启动预训练…');
        startBtn.disabled = true;
        stopBtn.disabled = true;
        api.post('/api/train_pretrain/start', collectUpdates()).then(function (d) {
          if (!d || d.error || d.ok === false) {
            livePre.classList.add('tp-err');
            appendLog('启动失败：' + ((d && (d.error || d.reason)) || '未知错误'));
            updateRunningUI(false, d);
            return;
          }
          appendLog('已启动：' + (d.job || 'train:pretrain:full') + ' · pid ' + (d.pid || '-'));
          if (d.config) {
            appendLog('步数 ' + d.config.steps + ' · 输出 ' + (d.config.out || ''));
          }
          cursor = 0;
          updateRunningUI(true, d);
          startPolling();
        });
      }
      startBtn.onclick = function () {
        if (!wname.stem()) {              /* 命名空 → 不启动，光标回命名框（提示已在框右「未命名」，用户 2026-10-17） */
          wname.input.focus();
          return;
        }
        /* 审查：模型权重命名与仓库权重同名 → 不启动（用户 2026-10-15） */
        wname.audit().then(function (st) {
          if (st && st.dup) {
            livePre.classList.add('tp-err');
            appendLog('模型权重命名与仓库权重「' + st.name + '」同名，请改名后再开始。', true);
            return;
          }
          /* 参数审查（用户 2026-10-18）：不常用/不可用的参数先弹窗，确认了再起训练 */
          return api.paramCheck('pretrain', collectUpdates(), '预训练').then(function (ok) {
            if (ok) doStart();
          });
        });
      };

      stopBtn.onclick = function () {
        stopBtn.disabled = true;
        api.post('/api/train_pretrain/stop', {}).then(function (d) {
          appendLog((d && d.note) ? ('停止请求：' + d.note) : '已请求停止。');
          if (!pollTimer) pollOnce();
        });
      };

      /* ---------- 取数 ---------- */
      function loadStatus() {
        return api.get('/api/train_pretrain/status').then(renderStatus);
      }
      function loadLog() {
        return api.get('/api/train_pretrain/log?tail=200').then(renderLog);
      }
      function loadAll() { return Promise.all([loadStatus(), loadLog()]); }

      refreshBtn.onclick = function () { if (!training) loadAll(); };

      /* 首帧直出（治切页闪烁）：模块缓存的上次数据直接画，后台再刷新 */
      if (TP_CACHE) {
        try {
          renderStatus(TP_CACHE.status);
          renderLog(TP_CACHE.log);
        } catch (e) {}
      }
      return loadAll();
    },

    unmount: function () {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (fitOnResize) { window.removeEventListener('resize', fitOnResize); fitOnResize = null; }
    },
  });
/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
(window.__EAI_API || {}).onProjectChange && window.__EAI_API.onProjectChange(function () { TP_CACHE = null; TP_CACHE_AT = 0; TP_INFLIGHT = null; });

})();
