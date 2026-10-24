/* ============================================================
   EAI_Studio · pages/train_full.js ——「指令微调」页（Tk FullFTPage 迁移）
   导航键 train_full / 中文名「指令微调」/ 分组「训练中心」
   ------------------------------------------------------------
   Tk 源码：ENGINE_DIR/pages/train.py（FullFTPage，stage=sft / method=full）

   真实数据全部来自 bridge_api/train_full.py（已接真后端）：
     GET  /api/train_full/config   真实 train 配置 + 运行时预览 + 路径状态 + 宿主字段
     GET  /api/train_full/assets   工程内语料/检查点/日志/导出清单（真实磁盘扫描）
     GET  /api/train_full/log      训练日志尾部（sft_loss.csv）
     GET  /api/train_full/loss     loss 数据点（供曲线）
     GET  /api/train_full/status   训练子进程状态 + 增量日志（?since=<游标>）
     POST /api/train_full/save     写回 config.json
     POST /api/train_full/start    起真训练子进程（stage=sft / method=full）
     POST /api/train_full/stop     优雅停止（写 stop.flag）

   交互：
     - 「保存配置」把表单写回 config.json；
     - 「开始指令微调」起真子进程，之后每 1 秒轮询 status?since=<游标>，
       把新行追到日志框、刷新 Loss 曲线与产出清单；
     - 训练中禁用开始、启用停止；结束/报错如实显示（含 exit_code）。

   mock 只在 ?mock=1 预览截图时用；_registry.js 的 mock 按整条 path 精确匹配，
   本页所有 GET 都不带 query 的（轮询仅在真起训练后发生），故每个接口一份 mock。
   ============================================================ */
(function () {
  'use strict';

  var START_TIP = '指令微调：优化器更新模型全部参数。效果上限高，'
    + '但步数多时容易灾难性遗忘，学习率建议压到预训练的 1/4～1/10。';

  /* 参数说明（用户 2026-10-16：左键点参数名 → 弹窗看说明）；键=页面标签 */
  var PARAM_HELP = {
    '批量大小': '每一步同时喂进多少条指令样本。\n' +
      '怎么定值：指令数据通常几千条，batch 8~32 就够；显存不够先减它，再用梯度累积把等效批量补到 32~64。',
    '丢弃率': '随机置零激活的比例。\n' +
      '怎么定值：数据量小，0.1 是安全默认。\n' +
      '判断信号：训练 Loss 很低但验证 Loss 高（在背答案）→ 加到 0.15~0.2；两边都高就是欠拟合，减小它并加步数。',
    '步数': '微调总步数，别太多，否则会遗忘基座能力。\n' +
      '怎么定值：1 个 epoch = 样本数 ÷ batch_size，一般跑 2~3 个 epoch；先跑 1 个 epoch 看回答质量，不够再加。\n' +
      '超过 5 个 epoch 往往开始照抄训练集答案，泛化反而变差。',
    '学习率': '微调的更新幅度，通常是从零预训练的 1/4~1/10。\n' +
      '怎么定值：2e-4~8e-4，先用 5e-4：回答开始像样就保持；答非所问或重复就加到 8e-4；Loss 抖动、输出格式崩坏就减到 2e-4。\n' +
      '微调数据少时学习率偏大很容易过拟合。',
    '混合精度 AMP': '用 bf16/fp16 做前向反向、fp32 更新权重：省显存、提速。\n' +
      '怎么定值：建议开启，优先 bf16；出现 NaN 就关掉。',
    '初始权重': '微调的起点权重。\n' +
      '怎么定值：优先用预训练 / 续训产物（eproai_pretrain.pt 等），从零做指令微调效果通常很差；留空表示从零训练，只用于调试。',
  };

  /* 页面级轮询定时器（unmount 时清理；同一时刻只会挂载一个页面） */
  var pollTimer = null;
  /* Loss 曲线高度重算的 resize 监听（unmount 时摘掉） */
  var fitOnResize = null;

  /* ---------- ?mock=1 预览数据（真实运行一律走桥） ---------- */
  var CONFIG_MOCK = {
    ok: true,
    config_path: '<应用目录>\\engine\\config.json',
    base_dir: '<应用目录>\\engine',
    project: '<工程目录>',
    device: 'cpu',
    fields: { batch_size: 32, dropout: 0.1, sft_steps: 8000, sft_lr: 0.0005 },
    bools: { amp: true, sample: true },
    corpus: {
      sft_corpus: '<工程目录>\\data/corpora\\sft_corpus.txt',
      sft_init_from: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
    },
    runtime: {
      stage: 'sft', method: 'full',
      corpus: '<工程目录>\\data/corpora\\sft_corpus.txt',
      steps: 8000, lr: 0.0005, warmup: 200, eval_every: 200,
      init_from: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
      out: '<工程目录>\\checkpoints\\eproai_model.pt',
      log_csv: '<工程目录>\\logs\\sft_loss.csv',
    },
    paths: {
      corpus: { raw: '<工程目录>\\data/corpora\\sft_corpus.txt',
        abs: '<工程目录>\\data\\corpora\\sft_corpus.txt', exists: true,
        size_bytes: 96412, mtime: 1789000000 },
      init_from: { raw: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
        abs: '<工程目录>\\checkpoints\\eproai_pretrain.pt', exists: true,
        size_bytes: 44188224, mtime: 1789000000 },
      out: { raw: '<工程目录>\\checkpoints\\eproai_model.pt',
        abs: '<工程目录>\\checkpoints\\eproai_model.pt', exists: false,
        size_bytes: null, mtime: null },
      log_csv: { raw: '<工程目录>\\logs\\sft_loss.csv',
        abs: '<工程目录>\\logs\\sft_loss.csv', exists: true,
        size_bytes: 2048, mtime: 1789000000 },
    },
    dirs: {
      corpora: '<工程目录>\\data\\corpora',
      checkpoints: '<工程目录>\\checkpoints',
      logs: '<工程目录>\\logs',
      exports: '<工程目录>\\exports',
    },
    running: false, running_job: null, pid: null, exit_code: null, elapsed: 0,
    stop_requested: false, stop_flag: false, next: 0, job_exists: false,
    outputs: {
      corpus: { path: '<工程目录>\\data\\corpora\\sft_corpus.txt',
        exists: true, size_bytes: 96412, mtime: 1789000000 },
      out: { path: '<工程目录>\\checkpoints\\eproai_model.pt',
        exists: false, size_bytes: 0, mtime: null },
      log_csv: { path: '<工程目录>\\logs\\sft_loss.csv',
        exists: true, size_bytes: 2048, mtime: 1789000000 },
      init_from: { path: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
        exists: true, size_bytes: 44188224, mtime: 1789000000 },
    },
  };

  var STATUS_MOCK = {
    ok: true, stage: 'sft', method: 'full',
    project: CONFIG_MOCK.project, config_path: CONFIG_MOCK.config_path,
    running: false, running_job: null, pid: null, exit_code: null, elapsed: 0,
    stop_requested: false, stop_flag: false, job_exists: false,
    lines: [], total_lines: 0, next: 0, error: '',
    config: CONFIG_MOCK.runtime, outputs: CONFIG_MOCK.outputs,
  };

  var ASSETS_MOCK = {
    ok: true,
    project: '<工程目录>',
    dirs: CONFIG_MOCK.dirs,
    dirs_missing: ['<工程目录>\\exports'],
    corpora: [
      { name: 'sft_corpus.txt', path: '<工程目录>\\data\\corpora\\sft_corpus.txt',
        ext: 'txt', size_bytes: 96412, mtime: 1789000000, chars: 60214 },
    ],
    checkpoints: [
      { name: 'eproai_pretrain.pt', path: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
        ext: 'pt', size_bytes: 44188224, mtime: 1789000000 },
    ],
    logs: [
      { name: 'sft_loss.csv', path: '<工程目录>\\logs\\sft_loss.csv',
        ext: 'csv', size_bytes: 2048, mtime: 1789000000 },
    ],
    exports: [],
  };

  var LOG_MOCK = {
    ok: true,
    path: '<工程目录>\\logs\\sft_loss.csv',
    exists: true,
    total_lines: 11,
    tail: [
      'step,train_loss,val_loss,lr,elapsed',
      '0,4.3120,4.3251,2.500000e-05,4.1',
      '200,3.1044,3.1892,4.980000e-04,22.6',
      '400,2.7120,2.8301,4.960000e-04,41.2',
      '600,2.4381,2.5640,4.920000e-04,60.0',
      '800,2.2210,2.3718,4.860000e-04,78.9',
      '1000,2.0742,2.2402,4.790000e-04,97.7',
      '1200,1.9620,2.1588,4.700000e-04,116.4',
      '1400,1.8871,2.1010,4.590000e-04,135.2',
      '1600,1.8320,2.0642,4.460000e-04,154.0',
      '1800,1.7933,2.0401,4.310000e-04,172.8',
    ],
  };

  var LOSS_MOCK = {
    ok: true,
    path: LOG_MOCK.path,
    exists: true,
    points: [
      { step: 0, train: 4.3120, val: 4.3251, lr: 2.5e-5, elapsed: 4.1 },
      { step: 200, train: 3.1044, val: 3.1892, lr: 4.98e-4, elapsed: 22.6 },
      { step: 400, train: 2.7120, val: 2.8301, lr: 4.96e-4, elapsed: 41.2 },
      { step: 600, train: 2.4381, val: 2.5640, lr: 4.92e-4, elapsed: 60.0 },
      { step: 800, train: 2.2210, val: 2.3718, lr: 4.86e-4, elapsed: 78.9 },
      { step: 1000, train: 2.0742, val: 2.2402, lr: 4.79e-4, elapsed: 97.7 },
      { step: 1200, train: 1.9620, val: 2.1588, lr: 4.70e-4, elapsed: 116.4 },
      { step: 1400, train: 1.8871, val: 2.1010, lr: 4.59e-4, elapsed: 135.2 },
      { step: 1600, train: 1.8320, val: 2.0642, lr: 4.46e-4, elapsed: 154.0 },
      { step: 1800, train: 1.7933, val: 2.0401, lr: 4.31e-4, elapsed: 172.8 },
    ],
  };

  var OK_MOCK = { ok: true, pid: 12345, job: 'train:sft:full', config: CONFIG_MOCK.runtime };
  var SAVE_MOCK = { ok: true, config_path: CONFIG_MOCK.config_path,
    saved: { batch_size: 32 }, train: { batch_size: 32 } };

  /* ---------- 小工具 ---------- */
  var SVGNS = 'http://www.w3.org/2000/svg';

  function icon(paths) {
    var s = document.createElement('span');
    s.className = 'tf-ic';
    s.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' + paths + '</svg>';
    return s;
  }

  function svg(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    if (attrs) { for (var k in attrs) { n.setAttribute(k, attrs[k]); } }
    return n;
  }

  function fmtInt(n) {
    n = Number(n);
    if (!isFinite(n)) { return '—'; }
    return n.toLocaleString('en-US');
  }

  function humanSize(n) {
    n = Number(n);
    if (!isFinite(n) || n < 0) { return '—'; }
    if (n < 1024) { return n + ' B'; }
    if (n < 1024 * 1024) { return (n / 1024).toFixed(1) + ' KB'; }
    if (n < 1024 * 1024 * 1024) { return (n / 1048576).toFixed(1) + ' MB'; }
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  function fmtVal(v) {
    if (v == null || v === '') { return '—'; }
    return String(v);
  }


/* ---------- 取数缓存（治切页闪烁，2026-10-15，同 repo.js） ----------
   prefetch 与 mount 共用一趟请求：换页前把数据取回，首帧同步直出，后台再刷新；
   800ms 内的重复取数直接吃缓存（正好盖住 prefetch→mount 的间隙）。 */
var TF_CACHE = null, TF_CACHE_AT = 0, TF_INFLIGHT = null;
function TFFetch(api, force) {{
  if (TF_INFLIGHT) return TF_INFLIGHT;
  if (!force && TF_CACHE && (Date.now() - TF_CACHE_AT) < 800) return Promise.resolve(TF_CACHE);
  TF_INFLIGHT = Promise.all([
    api.get('/api/train_full/config'),
    api.get('/api/train_full/loss'),
    api.get('/api/train_full/log'),
    api.get('/api/train_full/status'),
  ]).then(function (a) {{
    TF_CACHE = { config: a[0], loss: a[1], log: a[2], status: a[3] };
    TF_CACHE_AT = Date.now();
    TF_INFLIGHT = null;
    return TF_CACHE;
  }})["catch"](function () {{ TF_INFLIGHT = null; return TF_CACHE; }});
  return TF_INFLIGHT;
}}
  EAIPages.register('train_full', {
    title: '指令微调',

    mock: {
      '/api/train_full/config': CONFIG_MOCK,
      '/api/train_full/assets': ASSETS_MOCK,
      '/api/train_full/log': LOG_MOCK,
      '/api/train_full/loss': LOSS_MOCK,
      '/api/train_full/status': STATUS_MOCK,
      '/api/train_full/start': OK_MOCK,
      '/api/train_full/stop': { ok: true, note: '已写入 stop.flag' },
      '/api/train_full/save': SAVE_MOCK,
    },

    prefetchTimeout: 800,
    prefetch: function (api) { return TFFetch(api); },

    mount: function (root, api) {
      var FIELDS = [
        ['批量大小', 'batch_size'],
        ['丢弃率', 'dropout'],
        ['步数', 'sft_steps'],
        ['学习率', 'sft_lr'],
      ];
      var BOOLS = [['混合精度 AMP', 'amp']];

      /* 本页运行时状态 */
      var cursor = 0;          /* status 增量游标 */
      var liveLines = [];      /* 训练子进程实时输出行（只进 livePre） */
      var liveStarted = false; /* 实时块是否已有输出（用于丢掉占位提示） */
      var MAX_LIVE = 50;   /* 实时日志只留最近 50 行（用户 2026-10-19：原来 2000） */
      var isRunning = false;
      var pollTick = 0;

      /* ---------- 骨架 ---------- */
      /* 用户 2026-10-14：大标题下不要小字介绍 */
      root.appendChild(api.hero('指令微调'));

      var wrap = api.el('div', 'stack');
      root.appendChild(wrap);

      /* 状态提示条（随训练状态变色） */

      var grid = api.el('div', 'tf-grid');
      wrap.appendChild(grid);

      /* ================= 左：参数 ================= */
      var leftCol = api.el('div', 'tf-col');

      var paramCard = api.card('');   /* 「参数」标题移到命名分隔线下（用户 2026-10-16），卡头不再放标题 */
      var phead = paramCard.querySelector('.card-head');
      if (phead) phead.remove();
      var form = api.el('div', 'tf-form');

      /* 模型权重命名（用户 2026-10-15 终版）：在参数卡内、参数上方；
         标题与「参数」同格式，下方同款分隔条；输入框在左、提示在右（√ / 重复红字）。 */
      var wname = api.weightName({ inputClass: 'tf-input', fallback: 'eproai_model' });
      form.appendChild(wname.wrap);
      /* 覆盖初始权重（用户 2026-10-16）：勾选后完成训练即覆盖——新模型名=初始权重名，旧权重更名「初始_」前缀入回收站 */
      var ovwRow = api.el('div', 'tf-check');
      var ovwBox = api.el('span', 'tf-box');
      ovwBox.appendChild(icon('<path d="M5 12.6 9.6 17 19 6.6"/>'));
      ovwRow.appendChild(ovwBox);
      ovwRow.appendChild(api.el('span', null, '覆盖初始权重'));
      /* 权重锁（用户 2026-10-20）：初始权重被锁 → 不许勾「覆盖初始权重」 */
      var ovwLocked = false;
      /* 未选权重 / 权重被锁 → 整行置灰、点不动（用户 2026-10-23：没选权重时不许勾「覆盖初始权重」） */
      function syncOvwState() {
        var none = !String(initFromRaw || '');
        ovwRow.classList.toggle('lock-off', none || ovwLocked);
        ovwRow.title = none ? '未选择权重，不能勾选覆盖初始权重'
          : (ovwLocked ? ('「' + String(initFromRaw).split(/[\\/]/).pop() + '」已锁定，不能勾选覆盖初始权重') : '');
        if ((none || ovwLocked) && ovwBox.classList.contains('on')) {
          ovwBox.classList.remove('on');
          applyOvwLock();
        }
      }
      function guardOvw() {
        var p = initFromRaw;
        syncOvwState();                                        /* 先按当前选择置灰/恢复 */
        if (!String(p || '')) { ovwLocked = false; return; }   /* 没选权重就不用查锁 */
        api.isLockedPath(p).then(function (locked) {
          if (p !== initFromRaw) { return; }        /* 期间又换了权重，以新的那次为准 */
          ovwLocked = !!locked;
          syncOvwState();
        });
      }

      var ovwWasOn = false;
      function applyOvwLock(save) {
        var on = ovwBox.classList.contains('on');
        if (on) {
          var stem = String(initFromRaw || '').split(/[\\/]/).pop().replace(/\.pt$/i, '');
          if (stem) { wname.input.value = stem; }
          wname.input.disabled = true;
          if (wname.hint) { wname.hint.style.visibility = 'hidden'; }
        } else {
          wname.input.disabled = false;
          if (wname.hint) { wname.hint.style.visibility = ''; }
          if (ovwWasOn) { wname.input.dispatchEvent(new Event('input')); }   /* 解锁才重审重名 */
        }
        ovwWasOn = on;
        if (save !== false) { autoSave(); }
      }
      ovwRow.onclick = function () {
        if (!String(initFromRaw || '')) { return; }   /* 没选权重不许勾（用户 2026-10-23） */
        if (ovwLocked) { return; }          /* 锁上的权重不许覆盖（用户 2026-10-20） */
        ovwBox.classList.toggle('on');
        applyOvwLock();
      };
      form.appendChild(ovwRow);
      form.appendChild(api.el('hr', 'tf-divider'));
      form.appendChild(api.el('h2', 'card-title', '参数'));   /* 「参数」在命名下划线下、参数行上（用户 2026-10-16） */
      wname.input.addEventListener('input', autoSave);
      wname.input.addEventListener('change', autoSave);
      var outRaw = '';   /* out 原值：保留目录部分、只换文件名（相对/绝对口径不变） */
      function outValueFor(stem) {
        var cur = String(outRaw || '');
        var i = Math.max(cur.lastIndexOf('/'), cur.lastIndexOf('\\'));
        return (i >= 0 ? cur.slice(0, i + 1) : 'checkpoints/') + stem + '.pt';
      }

      var fieldInputs = {};
      var dotRows = {};        /* 参数行状态小球（用户 2026-10-18） */
      FIELDS.forEach(function (f) {
        var row = api.el('div', 'tf-row');
        var lbl = api.el('span', 'tf-label', f[0]);
        api.help(lbl, f[0], PARAM_HELP[f[0]]);   /* 点参数名看说明（用户 2026-10-16） */
        row.appendChild(lbl);
        var inp = document.createElement('input');
        inp.type = 'text';
        inp.className = 'tf-input pd-input';
        inp.placeholder = '—';
        inp.addEventListener('input', autoSave);    /* 数据一经更改就直接保存 */
        inp.addEventListener('change', autoSave);
        row.appendChild(inp);
        var dot = api.el('span', 'pd-dot', '');
        dot.dataset.level = 'empty';
        row.appendChild(dot);
        dotRows[f[1]] = { input: inp, dot: dot };
        fieldInputs[f[1]] = inp;
        form.appendChild(row);
      });
      api.bindParamDots('full', dotRows, collectForm);

      /* 验证集比例 / 重复惩罚（用户 2026-10-19）：横条样式与对话页「上下文长度」一致 */
      var sliderEls = {};
      function sliderRow(label, min, max, step, fmt, help) {
        var row = api.el('div', 'tf-row');
        var lbl = api.el('span', 'tf-label', label);
        api.help(lbl, label, help);
        row.appendChild(lbl);
        var box = api.el('div', 'tf-range-box');
        var inp = document.createElement('input');
        inp.type = 'range';
        inp.className = 'tf-range';
        inp.min = min; inp.max = max; inp.step = step;
        var val = api.el('span', 'tf-range-v', '');
        function paint() { val.textContent = fmt(Number(inp.value)); }
        inp.addEventListener('input', paint);
        inp.addEventListener('change', function () { paint(); autoSave(); });
        box.appendChild(inp);
        box.appendChild(val);
        api.rangeEdit(val, inp);                    /* 点数字可直接输入（用户 2026-10-20） */
        row.appendChild(box);
        form.appendChild(row);
        paint();
        return { input: inp, out: val, fmt: fmt, paint: paint };
      }
      sliderEls.val_ratio = sliderRow('验证集比例', 5, 40, 1, function (v) { return v + '%'; },
        '从语料里留出多少做验证集：越大越能看出过拟合，但可用于训练的样本更少。常用 10%。');

      /* 检查点（用户 2026-10-19）：训练中途按等分点存中间权重，过拟合就取半成品。
         N=1 → 1/2 处；N=2 → 1/3、2/3 处；0 = 关闭。放在参数区最后（用户 2026-10-19）。 */
      sliderEls.ckpt_count = sliderRow('检查点数量', 0, 5, 1, function (v) {
        return v ? String(v) : ((window.I18N && window.I18N.lang() === 'en') ? 'Off' : '关闭');   /* 不带单位（用户 2026-10-20） */
      }, '训练中途额外保存几份权重：设 1 就在一半处存一次，设 2 就在 1/3、2/3 处各存一次。\n' +
         '用途：训练后期过拟合（验证 Loss 往上翘）时，可以回到中途那份继续用。\n' +
         '文件存在 checkpoints/，名字是「权重名-ck步数.pt」，仓库权重弹窗里能直接选。');

      form.appendChild(api.el('hr', 'tf-divider'));

      var boolBoxes = {};
      BOOLS.forEach(function (b) {
        var row = api.el('div', 'tf-check');
        var box = api.el('span', 'tf-box');
        box.appendChild(icon('<path d="M5 12.6 9.6 17 19 6.6"/>'));
        var txt = api.el('span', null, b[0]);
        api.help(txt, b[0], PARAM_HELP[b[0]]);   /* 点参数名看说明（用户 2026-10-16） */
        row.appendChild(box);
        row.appendChild(txt);
        row.onclick = function () {
          box.classList.toggle('on');
          autoSave();                                /* 数据一经更改就直接保存 */
        };
        boolBoxes[b[1]] = box;
        form.appendChild(row);
      });

      form.appendChild(api.el('hr', 'tf-divider'));

      /* 语料 / 初始权重 路径行 */
      function pathRow(label) {
        /* 标签在小框上方、与参数标签同字体同大小（用户 2026-10-15）；
           标签+选择行包块级一层 = LoRA 同款间距（用户 2026-10-16：分隔线→标题 14、标题→行 6） */
        var block = api.el('div', 'tf-path-block');
        var lbl = api.el('div', 'tf-label', label);
        api.help(lbl, label, PARAM_HELP[label]);   /* 点参数名看说明（用户 2026-10-16） */
        lbl.style.width = 'auto';
        lbl.style.margin = '0 0 6px';
        block.appendChild(lbl);
        var wrapRow = api.el('div', 'tf-path-row');
        var dot = api.el('span', 'tf-dot');
        /* 「选择」按钮在权重左侧（用户 2026-10-15）；点它 = 点整行 = 弹仓库权重弹窗 */
        var pickBtn = document.createElement('button');
        pickBtn.type = 'button';
        pickBtn.className = 'tf-pick';
        pickBtn.textContent = '选择';
        var val = api.el('span', 'tf-path empty', '—');
        var size = api.el('span', 'tf-size', '');
        wrapRow.appendChild(dot);
        wrapRow.appendChild(pickBtn);
        wrapRow.appendChild(val);
        wrapRow.appendChild(size);
        block.appendChild(wrapRow);
        form.appendChild(block);
        return { dot: dot, val: val, size: size, wrap: wrapRow, pick: pickBtn, label: lbl };
      }
      var corpusRow = null;   /* 用户口径 2026-10-14：不显示语料地址（语料只由点「开始训练」时的数据集合并产生） */
      var initRow = pathRow('初始权重');
      var initFromRaw = '';   /* 初始权重（从仓库权重弹窗选择；外部权重先进仓库，训练页不提供添加） */
      /* 用户 2026-10-17：只有「选择」按钮可点（参考 LoRA 合并页），整行不再响应点击 */
      initRow.pick.title = '从仓库选择权重';
      initRow.pick.addEventListener('click', function () {
        api.weightPicker({
          title: '初始权重',
          current: initFromRaw,
          onPick: function (w) {
            initFromRaw = w.path;
            renderPathRow(initRow, { abs: w.path, exists: true, size_bytes: w.size }, w.path);
            if (ovwBox.classList.contains('on')) { applyOvwLock(false); }   /* 覆盖态：命名跟着初始权重名（用户 2026-10-16） */
            guardOvw();
            autoSave();
          },
        });
      });

      paramCard.body.appendChild(form);
      leftCol.appendChild(paramCard);
      grid.appendChild(leftCol);

      /* ================= 右：操作 + 日志 + 曲线 + 产出 ================= */
      var rightCol = api.el('div', 'tf-col');

      /* 动作行：开始 / 停止（等宽、靠左）+ 刷新；改动即存，无「保存配置」按钮 */
      var actions = api.el('div', 'tf-actions');

      var startBtn = document.createElement('button');
      startBtn.type = 'button';
      startBtn.className = 'btn btn-accent';   /* 与预训练一致（用户 2026-10-15） */
      startBtn.appendChild(api.el('span', null, '▶ 开始'));
      actions.appendChild(startBtn);

      var stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'btn tf-btn-danger';
      stopBtn.disabled = true;
      stopBtn.appendChild(icon('<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>'));   /* 暂停图标，与预训练一致 */
      stopBtn.appendChild(api.el('span', null, '停止'));
      actions.appendChild(stopBtn);

      var refreshBtn = document.createElement('button');
      refreshBtn.type = 'button';
      refreshBtn.className = 'tf-btn2';
      refreshBtn.appendChild(icon('<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'));
      refreshBtn.appendChild(api.el('span', null, '刷新'));
      actions.appendChild(refreshBtn);

      rightCol.appendChild(actions);

      /* 概览统计（4 张瓷片，与预训练页同款：训练状态/占用内存/输出权重/语料体量） */
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
         ① 训练输出（实时）—— 只由 /status?since= 的 lines 与页面提示行追加，刷新碰不到；
         ② Loss 日志表（CSV）—— 只由 /log 的真实 CSV 尾部写入，「刷新 / 重读」只刷这一块。 */
      var liveCard = api.card('训练输出');
      var liveBox = api.el('div', 'tf-logbox');   /* 外框定距 + 内层滚动（与预训练页同款） */
      var livePre = api.el('pre', 'tf-pre tf-live');   /* 空态不写占位文字（用户 2026-10-17） */
      liveBox.appendChild(livePre);
      liveCard.body.appendChild(liveBox);
      rightCol.appendChild(liveCard);

      var csvCard = api.card('Loss 日志表');
      var csvBox = api.el('div', 'tf-logbox');
      var csvPre = api.el('pre', 'tf-pre tf-csv', '读取中…');
      csvBox.appendChild(csvPre);
      csvCard.body.appendChild(csvBox);
      rightCol.appendChild(csvCard);

      /* Loss 曲线（与预训练页同款：svg 内图例 + 空态「暂无足够数据点」） */
      var chartCard = api.card('Loss 曲线');
      var chart = api.el('div', 'tf-chart');
      chartCard.body.appendChild(chart);
      rightCol.appendChild(chartCard);

      grid.appendChild(rightCol);
      fitOnResize = scheduleFit;
      window.addEventListener('resize', fitOnResize);
      scheduleFit();

      /* ---------- 渲染：参数 ---------- */
      function renderConfig(d) {
        if (d && d.error) {
          fieldInputs.batch_size.placeholder = '读取失败';
          /* 配置读取失败属于页面提示行 → 只追加进实时块，不动 CSV 块 */
          appendLiveText('配置读取失败：' + d.error, true);
          return;
        }
        var fields = (d && d.fields) || {};
        FIELDS.forEach(function (f) {
          fieldInputs[f[1]].value = fmtVal(fields[f[1]]);
        });
        var bools = (d && d.bools) || {};
        BOOLS.forEach(function (b) {
          boolBoxes[b[1]].classList.toggle('on', !!bools[b[1]]);
        });
        /* 两个横条（用户 2026-10-19）：验证集比例存 0~1，界面上按百分比拖 */
        (function () {
          var vr = Number(fields.val_ratio);
          if (!isFinite(vr) || vr <= 0) { vr = 0.1; }
          sliderEls.val_ratio.input.value = Math.round(vr * 100);
          sliderEls.val_ratio.paint();
          var cc = Number(fields.ckpt_count);
          if (!isFinite(cc) || cc < 0) { cc = 0; }
          sliderEls.ckpt_count.input.value = Math.min(5, Math.round(cc));
          sliderEls.ckpt_count.paint();
        })();
        var initInfo = (d.paths || {}).init_from || {};
        /* 文件不存在就别当成已选（用户 2026-10-17） */
        initFromRaw = (initInfo.exists === false) ? '' : String((d.corpus || {}).sft_init_from || '');
        guardOvw();
        renderPathRow(initRow, initInfo, initFromRaw);

        /* 权重命名（用户 2026-10-19 修）：默认名不回填（2026-10-17 口径），
           但用户自己填过并保存过的名字要回填——否则切页回来命名框空了、点开始还得重填。 */
        outRaw = String((d.runtime || {}).out || ((d.paths || {}).out || {}).abs || '');
        (function () {
          var f = String(outRaw || '').split(/[\\/]/).pop().replace(/\.pt$/i, '');
          if (f && f !== 'eproai_model' && f !== 'eproai' && !wname.stem()) {
            wname.input.value = f;
            wname.audit();
          }
        })();
        /* 覆盖初始权重回填（用户 2026-10-16）：勾选态命名锁为初始权重名（bools/runtime 双源兜底） */
        ovwBox.classList.toggle('on', !!((d.bools || {}).overwrite_init) || !!(d.runtime || {}).overwrite_init);
        applyOvwLock(false);
      }

      function renderPathRow(row, info, raw) {
        info = info || {};
        /* 文件不存在 = 当没选（用户 2026-10-17）：不显示文件名、不亮绿点、不写「不存在」 */
        var text = (info.exists === false) ? '' : (info.abs || raw || '');
        /* 只显示文件名、不显示路径（用户 2026-10-15）；完整路径收进 title 悬停可见 */
        var disp = text ? String(text).split(/[\\/]/).pop() : '';
        row.val.textContent = disp || '-';   /* 没选权重只显示一个小横杠（用户 2026-10-23，同裁切/量化页） */
        row.val.title = text;
        row.val.classList.toggle('empty', !text);
        row.dot.className = 'tf-dot ' + (text ? (info.exists ? 'ok' : 'err') : '');
        row.size.textContent = info.exists ? humanSize(info.size_bytes) : (text ? '不存在' : '');
        row.size.classList.toggle('tf-err', !!text && !info.exists);
      }

      /* ---------- 渲染：CSV 日志表（只写 csvPre，与实时块互不覆盖） ---------- */
      function renderLog(d) {
        csvPre.classList.remove('tf-err');
        if (d && d.error) {
          csvPre.textContent = '读取失败：' + d.error;
          csvPre.classList.add('tf-err');
          return;
        }
        if (!d || !d.exists || !(d.tail || []).length || (d.total_lines || 0) <= 1) {
          csvPre.textContent = '暂无训练日志';   /* 不带地址；只有表头也算未训练（用户 2026-10-17） */
          return;
        }
        csvPre.textContent = d.tail.join('\n') + '\n— 共 ' + fmtInt(d.total_lines) + ' 行 —';
        csvPre.scrollTop = csvPre.scrollHeight;   /* 跟到最新一行（用户 2026-10-19） */
      }

      /* ---------- 渲染：训练输出（只追加；没有任何刷新路径会覆盖它） ---------- */
      function appendLiveText(text, dedupe) {
        if (text == null || text === '') { return; }
        if (!liveStarted) { liveStarted = true; liveLines = []; }   /* 首行到来 → 丢掉占位提示 */
        var parts = String(text).split('\n');
        for (var i = 0; i < parts.length; i++) {
          if (i === parts.length - 1 && parts[i] === '') { continue; }
          if (dedupe && liveLines.length && liveLines[liveLines.length - 1] === parts[i]) { continue; }
          liveLines.push(parts[i]);
        }
        if (liveLines.length > MAX_LIVE) { liveLines = liveLines.slice(-MAX_LIVE); }
        livePre.textContent = liveLines.join('\n');
        livePre.scrollTop = livePre.scrollHeight;
        scheduleFit();   /* 训练输出变长 → 曲线卡要重新收口 */
      }

      function appendLiveLines(lines) {
        if (!lines || !lines.length) { return; }
        appendLiveText(lines.join('\n'));
      }

      /* ---------- 渲染：Loss 曲线（与预训练页同款 svg 绘制） ---------- */
      function drawChart(points) {
        chart.textContent = '';
        if (!points || points.length < 2) {
          chart.appendChild(api.el('div', 'tf-chart-empty', '暂无足够数据点'));
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

        var svgEl = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none',
          role: 'img', 'aria-label': 'loss 曲线' });
        var i, y;
        for (i = 0; i < 4; i++) {
          y = pad + i * (H - 2 * pad) / 3;
          svgEl.appendChild(svg('line', { x1: pad, y1: y, x2: W - pad, y2: y,
            stroke: 'var(--LINE_SOFT)', 'stroke-width': 1 }));
        }
        svgEl.appendChild(svg('line', { x1: pad, y1: H - pad, x2: W - pad, y2: H - pad,
          stroke: 'var(--LINE)', 'stroke-width': 1 }));

        function poly(ys, color) {
          var pts = ys.map(function (v, k) { return X(xs[k]) + ',' + Y(v); }).join(' ');
          svgEl.appendChild(svg('polyline', { points: pts, fill: 'none', stroke: color,
            'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
        }
        poly(tr, 'var(--ACCENT)');
        poly(va, 'var(--OK)');

        function label(x, yy, text, anchor) {
          var t = svg('text', { x: x, y: yy, fill: 'var(--INK3)', 'font-size': 11, 'text-anchor': anchor });
          t.textContent = text;
          svgEl.appendChild(t);
        }
        label(W - 8, Y(ymax) + 4, ymax.toFixed(2), 'end');
        label(W - 8, Y(ymin) + 4, ymin.toFixed(2), 'end');
        label(pad, pad - 8, '— train', 'start');
        label(pad + 60, pad - 8, '— val', 'start');
        chart.appendChild(svgEl);
      }

      function renderChart(d) {
        if (d && d.error) {
          chart.textContent = '';
          chart.appendChild(api.el('div', 'tf-chart-empty tf-err', '读取失败：' + d.error));
          scheduleFit();
          return;
        }
        drawChart((d && d.points) || []);
        scheduleFit();
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

      /* ---------- 瓷片 + 内存/显存水位（与预训练页同款） ---------- */
      function fmtGB(n) {
        if (n == null) return '—';
        return (n / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
      }
      /* 瓷片数值压成单行（用户 2026-10-16）：单位不留空格、KB 取整、自动升档 */
      function tileSize(n) {
        return api.tileSize(n);   /* 共享口径：三位数不带小数（用户 2026-10-18） */
      }
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
            api.post('/api/train_full/stop', {}).then(function (r) {
              appendLiveText(r && r.ok
                ? ('⚠ ' + (isCuda ? '显存' : '内存') + '不足 1GB，已自动' + (isCuda ? '停止' : '暂停') + '训练（进度保存后退出）。')
                : ('⚠ 自动停止失败：' + ((r && r.error) || '未知错误')), true);
            });
          }
        }
        memAlert = lvl;
      }

      function renderFacts(d) {
        d = d || {};
        var outF = (d.outputs || {}).out || {};
        vCkpt.textContent = outF.exists ? tileSize(outF.size_bytes) : '未生成';
        vCkpt.classList.toggle('ok', !!outF.exists);
        vCkpt.classList.toggle('idle', !outF.exists);   /* 未生成=灰（用户 2026-10-17） */
        if (outF.mtime) vCkpt.title = String(outF.path || '');

        var mem = d.mem || {};
        var dev = mem.device || 'cpu';
        var isCuda = dev === 'cuda';
        vMem.labelEl.textContent = isCuda ? '占用显存' : '占用内存';
        var usedBytes = isCuda ? mem.gpu_used : mem.proc_bytes;
        vMem.textContent = tileSize(usedBytes);
        vMem.classList.toggle('ok', usedBytes != null);
        vMem.title = isCuda
          ? ('GPU 显存 已用 ' + fmtGB(mem.gpu_used) + ' / ' + fmtGB(mem.gpu_total) + '，剩余 ' + fmtGB(mem.gpu_free))
          : (mem.proc_bytes == null ? '训练进程未运行'
            : ('训练进程工作集（pid ' + d.pid + '），本机剩余可用 ' + fmtGB(mem.sys_avail)));
        watchMem(mem, isCuda, !!d.running);

        var cpF = (d.outputs || {}).corpus || {};
        vCorpus.textContent = cpF.exists ? tileSize(cpF.size_bytes) : '缺失';
        vCorpus.classList.toggle('ok', !!cpF.exists);
        vCorpus.classList.toggle('missing', !cpF.exists);   /* 缺失=红（用户 2026-10-18，同 LoRA 合并页） */
        vCorpus.classList.remove('warn');
        scheduleFit();   /* 瓷片/日志高度一变，曲线卡可用高度跟着重算 */
      }

      function renderStateTile(running, d) {
        vState.textContent = running ? '运行中' : '未启动';
        vState.classList.toggle('ok', !!running);
        vState.classList.toggle('idle', !running);   /* 未启动=灰（用户 2026-10-17） */
        if (running && d && d.pid != null) vState.title = 'pid ' + d.pid;
        scheduleFit();
      }

      /* ---------- 状态显示 ---------- */
      /* 用户 2026-10-15：去掉就绪/状态横幅——事件与问题都写训练输出日志（风格同预训练）；
         轮询会反复刷同一状态（标题不变）→ 不重复记。 */
      var lastBannerTitle = '';
      function setBanner(kind, title, text) {
        if (title === lastBannerTitle) return;
        lastBannerTitle = title;
        appendLiveText((title ? title + '：' : '') + (text || ''), true);
      }

      /* 左侧参数训练中禁改（用户 2026-10-20：「无论是否使用流水线功能，当开启训练后，
         左侧框变灰，不让用户更改」）：置灰靠 CSS（.tf-running），控件真禁用兜住键盘。 */
      function setFormLocked(on) {
        root.classList.toggle('tf-running', on);
        [].forEach.call(form.querySelectorAll('input, select, textarea'), function (el) {
          if (on) el.setAttribute('disabled', ''); else el.removeAttribute('disabled');
        });
      }

      function applyIdle(msg) {
        isRunning = false;
        startBtn.disabled = false;
        stopBtn.disabled = true;
        renderStateTile(false, null);
        setFormLocked(false);
        if (msg) appendLiveText(String(msg), true);   /* 用户 2026-10-15：问题在日志显示 */
      }

      function applyRunning(d) {
        isRunning = true;
        startBtn.disabled = true;
        stopBtn.disabled = false;
        renderStateTile(true, d);
        setFormLocked(true);
        var pid = (d && d.pid) != null ? d.pid : '—';
        var el = (d && d.elapsed != null) ? d.elapsed : '—';
        setBanner('run', '训练中 · pid ' + pid,
          '已运行 ' + el + 's；实时日志每 1 秒刷新（stage=sft / method=full）。');
      }

      function applyFinished(d) {
        isRunning = false;
        startBtn.disabled = false;
        stopBtn.disabled = true;
        renderStateTile(false, d);
        setFormLocked(false);
        var code = (d && d.exit_code);
        var okExit = (code === 0);
        var stopped = d && d.stop_requested;
        var title = okExit ? '训练完成' : (stopped ? '已停止' : '训练结束（非 0 退出）');
        var kind = okExit ? 'ok' : (stopped ? '' : 'err');
        setBanner(kind, title,
          'exit_code=' + (code == null ? '—' : code) + ' · 用时 ' +
          ((d && d.elapsed != null) ? d.elapsed : '—') + 's；已刷新 Loss 曲线。');
        /* 训练结束/被中止的这一刻权重才落盘 → 立刻重判一次命名（用户 2026-10-22）。
           不重判的话本页还显示「√」，要切页再回来才变「重复」。
           两次：250ms 抢在落盘后立刻判，1600ms 兜一次写盘慢的。 */
        setTimeout(function () { if (wname) wname.audit(); }, 250);
        setTimeout(function () { if (wname && !(wname.state && wname.state.dup)) wname.audit(); }, 1600);
      }

      /* ---------- 取数 ---------- */
      function loadConfig() { return api.get('/api/train_full/config').then(renderConfig); }
      function loadLoss() { return api.get('/api/train_full/loss').then(renderChart); }
      function loadCsvLog() { return api.get('/api/train_full/log').then(renderLog); }

      function loadEverything() {
        return Promise.all([loadConfig(), loadLoss(), loadCsvLog()]);
      }

      /* ---------- 轮询 ---------- */
      function pollOnce() {
        return api.get('/api/train_full/status?since=' + cursor).then(function (d) {
          if (!d || d.error) { return; }
          if (typeof d.next === 'number') { cursor = d.next; }
          appendLiveLines(d.lines || []);
          renderFacts(d);
          if (d.running) {
            applyRunning(d);
            pollTick++;
            if (pollTick % 3 === 0) { loadLoss(); }
          } else {
            stopPolling();
            applyFinished(d);
            /* 收尾：CSV 日志表与曲线各自刷新（实时块不受影响） */
            loadLoss();
            loadCsvLog();
          }
        }).catch(function () { /* 单次失败不打断轮询 */ });
      }

      function startPolling() {
        if (pollTimer) { return; }
        pollTick = 0;
        pollTimer = setInterval(pollOnce, 1000);
        pollOnce();
      }

      function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      }

      /* ---------- 表单 → 请求体 ---------- */
      function collectForm() {
        var body = {};
        FIELDS.forEach(function (f) {
          var v = (fieldInputs[f[1]].value || '').trim();
          if (v !== '' && v !== '—') { body[f[1]] = v; }   /* 占位破折号不当值发 */
        });
        body.amp = boolBoxes.amp.classList.contains('on');
        body.val_ratio = Number(sliderEls.val_ratio.input.value) / 100;          /* 0.05~0.4 */
        body.ckpt_count = Number(sliderEls.ckpt_count.input.value);              /* 0~5 个检查点 */
        body.sample = true;   /* 采样预览默认开启、不可关闭（页面无开关） */
        body.overwrite_init = ovwBox.classList.contains('on');   /* 覆盖初始权重（用户 2026-10-16） */
        if (initFromRaw) { body.sft_init_from = initFromRaw; }
        var stemName = wname.stem();      /* 命名由用户自己填；没填就不写 out（2026-10-17） */
        if (stemName) { body.sft_out = outValueFor(stemName); }
        return body;
      }

      /* ---------- 三个真动作 ---------- */
      /* 数据一经更改就直接保存（用户口径：无「保存配置」按钮；成功静默，失败才提示） */
      var saveTimer = null;
      function autoSave() {
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
          saveTimer = null;
          api.post('/api/train_full/save', collectForm()).then(function (r) {
            if (!r || r.error || r.ok === false) {
              setBanner('err', '保存失败', (r && r.error) || '未知错误');
            }
          });
        }, 400);
      }

      function doStart() {
        startBtn.disabled = true;
        setBanner('', '启动中', '正在组装 runtime 配置并起子进程…');
        api.post('/api/train_full/start', collectForm()).then(function (r) {
          if (!r || r.error || r.ok === false) {
            setBanner('err', '启动失败', (r && r.error) || '未知错误');
            startBtn.disabled = false;
            return;
          }
          cursor = 0;
          liveLines = [];
          liveStarted = false;
          livePre.textContent = '[宿主] 已启动，等待子进程输出…';
          var steps = (r.config && r.config.steps) != null ? r.config.steps : '—';
          setBanner('run', '训练中 · pid ' + (r.pid == null ? '—' : r.pid),
            '已启动 train:sft:full · ' + steps + ' 步；每 1 秒增量拉取日志。');
          startPolling();
          loadConfig();
        }).catch(function (e) {
          setBanner('err', '启动失败', String(e));
          startBtn.disabled = false;
        });
      }
      startBtn.onclick = function () {
        if (!wname.stem()) {              /* 命名空 → 不启动，光标回命名框（提示已在框右「未命名」，用户 2026-10-17） */
          wname.input.focus();
          return;
        }
        /* 审查：模型权重命名与仓库权重同名 → 不启动（用户 2026-10-15） */
        wname.audit().then(function (st) {
          if (st && st.dup && !ovwBox.classList.contains('on')) {   /* 覆盖初始权重=有意同名（用户 2026-10-16） */
            setBanner('err', '命名与仓库同名', '模型权重命名与仓库权重「' + st.name + '」同名，请改名后再开始。');
            return;
          }
          /* 参数审查（用户 2026-10-18）：不常用/不可用的参数先弹窗，确认了再起训练 */
          return api.paramCheck('full', collectForm(), '指令微调').then(function (ok) {
            if (ok) doStart();
          });
        });
      };

      stopBtn.onclick = function () {
        stopBtn.disabled = true;
        api.post('/api/train_full/stop', {}).then(function (r) {
          if (!r || r.error || r.ok === false) {
            setBanner('err', '停止失败', (r && r.error) || '未知错误');
            stopBtn.disabled = false;
            return;
          }
          setBanner('', '停止中', '已写 stop.flag，等待子进程保存权重后退出…');
        }).catch(function (e) {
          setBanner('err', '停止失败', String(e));
          stopBtn.disabled = false;
        });
      };

      /* 状态探测：挂载 / 刷新 / 启动期重探都用它（用户 2026-10-19）
         原来只有挂载时探一次；点「开始」后马上切页再回来，探到的是「还没起进程」，
         页面就永远停在「未启动」，而训练其实在跑。 */
      function probeStatus(keepLive) {
        return api.get('/api/train_full/status').then(function (d) {
          if (!d || d.error) { applyIdle(); return false; }
          try {
          renderFacts(d);
          if (d.running) {
            if (!(keepLive && isRunning)) {          /* 刷新时保留已有输出，不清屏重放（用户 2026-10-19） */
              var lines = d.lines || [];
              liveStarted = false;
              liveLines = [];
              livePre.classList.remove('tp-err');
              livePre.textContent = '';
              if (lines.length) { appendLiveLines(lines); }
              cursor = (typeof d.next === 'number') ? d.next : (lines.length || 0);
            }
            applyRunning(d);
            startPolling();
            return true;
          }
          applyIdle();
          if (d.job_exists) {
            /* 没在跑也要把旧输出恢复出来（用户 2026-10-20：「不开新训练切页，旧的训练
               输出会消失」）：跑完的输出从 status.lines 回放，不新开轮询。 */
            var oldLines = d.lines || [];
            if (!(keepLive && liveStarted) && oldLines.length) {
              liveStarted = false;
              liveLines = [];
              livePre.classList.remove('tp-err');
              livePre.textContent = '';
              appendLiveLines(oldLines);
              cursor = (typeof d.next === 'number') ? d.next : (oldLines.length || 0);
            }
            applyFinished(d);
          }
          return false;
          } catch (e) {                             /* 局部渲染出错别把整页打进「未启动」（排查用） */
            console.error('[probeStatus]', (e && e.stack) || e);
            applyIdle();
            return false;
          }
        }).catch(function () { applyIdle(); return false; });
      }

      /* 启动期重探：没有在跑时每 2 秒看一次，最多 60 秒（挂着不动，切页就停） */
      var watchTimer = null, watchTries = 0;
      function watchStartup() {
        if (watchTimer || isRunning) { return; }
        watchTries = 0;
        watchTimer = setInterval(function () {
          if (isRunning || ++watchTries > 30) { clearInterval(watchTimer); watchTimer = null; return; }
          api.get('/api/train_full/status').then(function (d) {
            if (d && !d.error && d.running) {
              clearInterval(watchTimer); watchTimer = null;
              probeStatus();
            }
          }).catch(function () {});
        }, 2000);
      }

      refreshBtn.onclick = function () {   /* 用户 2026-10-19：刷新不往训练输出里打日志，只重读数据 */
        refreshBtn.disabled = true;
        loadEverything().then(function () { return probeStatus(true); }).then(function () {
          refreshBtn.disabled = false;
        }, function () { refreshBtn.disabled = false; });
      };

      /* ---------- 初始加载 ---------- */
      /* 首帧直出（治切页闪烁）：模块缓存的上次数据直接画，后台再刷新 */
      if (TF_CACHE) {
        try {
          renderConfig(TF_CACHE.config);
          renderChart(TF_CACHE.loss);
          renderLog(TF_CACHE.log);
          if (TF_CACHE.status) renderFacts(TF_CACHE.status);
        } catch (e) {}
      } else {
        fieldInputs.batch_size.placeholder = '读取中…';
      }

      return loadEverything().then(function () {
        /* 探测是否已有训练在跑（页面切走再回来也能接管）；没探到就重探一会儿 */
        return probeStatus().then(function (running) { if (!running) { watchStartup(); } });
      });
    },

    unmount: function () {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (watchTimer) { clearInterval(watchTimer); watchTimer = null; }
      if (fitOnResize) { window.removeEventListener('resize', fitOnResize); fitOnResize = null; }
    },
  });
/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
(window.__EAI_API || {}).onProjectChange && window.__EAI_API.onProjectChange(function () { TF_CACHE = null; TF_CACHE_AT = 0; TF_INFLIGHT = null; });

})();
