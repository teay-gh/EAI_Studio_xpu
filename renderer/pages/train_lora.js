/* ============================================================
   EAI_Studio · pages/train_lora.js ——「LoRA 微调」页（Tk LoraPage 迁移）
   导航键 train_lora / 中文名「LoRA 微调」/ 分组「训练中心」
   ------------------------------------------------------------
   Tk 源码：ENGINE_DIR/pages/train.py（LoraPage : TrainPageBase）
     title    = "LoRA 微调"
     subtitle 不再显示（用户 2026-10-14：大标题下不要小字）
     start_text = "▶ 开始 LoRA 微调"
     左「参数」：lora_r / lora_alpha / lora_dropout / lora_targets /
                 batch_size / sft_steps / sft_lr + amp / sample
                 + 基座权重（仓库权重弹窗选择行，用户 2026-10-16）
     右：开始 / 停止 / 保存配置 · 训练日志 · Loss 曲线
   ------------------------------------------------------------
   真实数据全部来自 bridge_api/train_lora.py：
     /api/train_lora/config      LoRA 超参（真）+ 宿主字段
     /api/train_lora/status      训练状态增量（每秒轮询：新行 / 游标 / running）
     /api/train_lora/artifacts   权重 / 导出目录 / 语料（存在性 + 大小）
     /api/train_lora/log         训练日志 CSV 尾部 + 曲线数据点
   动作接真：
     保存配置 → POST /api/train_lora/save   （真·写 config.json 的 train 段）
     开始    → POST /api/train_lora/start  （真·起 core/train.py 子进程，stage=sft/method=lora）
     停止    → POST /api/train_lora/stop   （真·写 stop.flag，优雅退出）
   开始后每 1 秒轮询 /status?since=<游标>：新行追到日志框、刷新 Loss 曲线与权重清单；
   训练中禁用「开始」、启用「停止」；被拦（如「LoRA 需要基座权重…」）时原样显示文案。
   mock 只在 ?mock=1 预览截图时用；_registry.js 按整条 path 精确匹配。
   ============================================================ */
(function () {
  'use strict';

  /* 只给 ?mock=1（Edge 预览截图）用；正常跑一律走真实桥接口 */
  var MOCK_CONFIG = {
    project: '<工程目录>',
    config_path: '<应用目录>\\..\\EAI_Studio/engine\\config.json',
    device: 'cpu',
    method_in_config: 'full',
    r: 8, alpha: 8, dropout: 0.05, scale: 1,
    targets: 'qkv',
    targets_list: ['qkv'],
    targets_count: 4,
    batch_size: 32, lora_steps: 1200, lora_lr: 0.0002,
    amp: true, sample: true,
    sft_corpus: '<工程目录>\\data\\corpora\\sft_corpus.txt',
    lora_init_from: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
    lora_init_from_raw: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
    lora_out: '<工程目录>\\checkpoints\\eproai_lora.pt',
    lora_log_csv: '<工程目录>\\logs\\lora_loss.csv',
    stage: 'sft', method: 'lora',
    running: false, pid: null, exit_code: null, stop_flag: false,
    lines: [], total_lines: 0, next: 0, job_exists: false, outputs: null,
  };

  var MOCK_STATUS = {
    ok: true, since: 0, stage: 'sft', method: 'lora',
    running: false, running_job: null, pid: null, exit_code: null, elapsed: null,
    stop_flag: false, lines: [], total_lines: 0, next: 0, job_exists: false,
    outputs: null, project: '<工程目录>',
  };

  var MOCK_ARTIFACTS = {
    project: '<工程目录>',
    items: [
      { label: '基座权重', key: 'base',
        path: '<工程目录>\\checkpoints\\eproai_pretrain.pt',
        exists: true, is_file: true, size_bytes: 5872023, size_human: '5.6 MB' },
      { label: 'LoRA 适配器', key: 'adapter',
        path: '<工程目录>\\checkpoints\\eproai_lora.lora.pt',
        exists: false, is_file: false, size_bytes: 0, size_human: '—' },
      { label: '微调语料', key: 'corpus',
        path: '<工程目录>\\data\\corpora\\sft_corpus.txt',
        exists: true, is_file: true, size_bytes: 96412, size_human: '94.2 KB', lines: 200 },
      { label: '训练日志 CSV', key: 'log_csv',
        path: '<工程目录>\\logs\\lora_loss.csv',
        exists: true, is_file: true, size_bytes: 1820, size_human: '1.8 KB' },
    ],
    dirs: [
      { label: 'checkpoints', path: '<工程目录>\\checkpoints', exists: true, count: 1,
        files: [{ name: 'eproai_pretrain.pt', size_bytes: 5872023, size_human: '5.6 MB' }] },
      { label: 'logs', path: '<工程目录>\\logs', exists: true, count: 1,
        files: [{ name: 'lora_loss.csv', size_bytes: 1820, size_human: '1.8 KB' }] },
    ],
    adapter_note: '训练只存适配器（命名 = 输出权重同名 + .lora.pt）；合并放到「导出模型」页可选做',
  };

  var MOCK_LOG = {
    path: '<工程目录>\\logs\\lora_loss.csv',
    exists: true, size_bytes: 1820, size_human: '1.8 KB',
    header: ['step', 'train_loss', 'val_loss', 'lr', 'elapsed'],
    rows: [],
    points: [
      { step: 200, train: 3.1245, val: 3.2071 },
      { step: 400, train: 2.6410, val: 2.7196 },
      { step: 600, train: 2.3302, val: 2.4018 },
      { step: 800, train: 2.1518, val: 2.2203 },
      { step: 1000, train: 2.0499, val: 2.1187 },
      { step: 1200, train: 1.9826, val: 2.0564 },
    ],
    tail_text: 'step 200    | train 3.1245    | val 3.2071    | lr 0.000125\n' +
      'step 400    | train 2.641     | val 2.7196    | lr 0.00025\n' +
      'step 600    | train 2.3302    | val 2.4018    | lr 0.000375\n' +
      'step 800    | train 2.1518    | val 2.2203    | lr 0.0005\n' +
      'step 1000   | train 2.0499    | val 2.1187    | lr 0.000438\n' +
      'step 1200   | train 1.9826    | val 2.0564    | lr 0.000375',
    total: 6,
  };

/* ---------- 取数缓存（治切页闪烁，2026-10-15，同 repo.js） ----------
   prefetch 与 mount 共用一趟请求：换页前把数据取回，首帧同步直出，后台再刷新；
   800ms 内的重复取数直接吃缓存（正好盖住 prefetch→mount 的间隙）。 */
var TL_CACHE = null, TL_CACHE_AT = 0, TL_INFLIGHT = null;
function TLFetch(api, force) {{
  if (TL_INFLIGHT) return TL_INFLIGHT;
  if (!force && TL_CACHE && (Date.now() - TL_CACHE_AT) < 800) return Promise.resolve(TL_CACHE);
  TL_INFLIGHT = Promise.all([
    api.get('/api/train_lora/config'),
    api.get('/api/train_lora/artifacts'),
    api.get('/api/train_lora/log'),
  ]).then(function (a) {{
    TL_CACHE = { config: a[0], artifacts: a[1], log: a[2] };
    TL_CACHE_AT = Date.now();
    TL_INFLIGHT = null;
    return TL_CACHE;
  }})["catch"](function () {{ TL_INFLIGHT = null; return TL_CACHE; }});
  return TL_INFLIGHT;
}}
  EAIPages.register('train_lora', {
    title: 'LoRA 微调',

    mock: {
      '/api/train_lora/config': MOCK_CONFIG,
      '/api/train_lora/status': MOCK_STATUS,
      '/api/train_lora/artifacts': MOCK_ARTIFACTS,
      '/api/train_lora/log': MOCK_LOG,
      '/api/train_lora/start': { ok: false, action: 'start',
        error: 'LoRA 需要基座权重：请先完成预训练，或指定基座权重。' },
      '/api/train_lora/stop': { ok: true, action: 'stop', note: '已写入 stop.flag，训练会保存权重后优雅退出。' },
      '/api/train_lora/save': { ok: true, action: 'save', config_path: 'config.json', saved: {} },
    },

    prefetchTimeout: 800,
    prefetch: function (api) { return TLFetch(api); },

    mount: function (root, api) {
      /* Tk LoraPage.FIELDS：(标签, 响应键, 配置键, 类型) */
      var FIELDS = [
        ['LoRA 秩 r', 'r', 'lora_r', 'int'],
        ['缩放 α', 'alpha', 'lora_alpha', 'float'],
        ['LoRA 丢弃率', 'dropout', 'lora_dropout', 'float'],
        ['挂载目标层', 'targets', 'lora_targets', 'text'],
        ['批量大小', 'batch_size', 'batch_size', 'int'],
        /* 2026-10-23：步数/学习率独立成 lora_* 键（用户要求：LoRA 页与指令微调页互不影响） */
        ['步数', 'lora_steps', 'lora_steps', 'int'],
        ['学习率', 'lora_lr', 'lora_lr', 'float'],
      ];
      /* 用户口径 2026-10-14：不显示语料地址（语料只由点「开始训练」时的数据集合并产生）；
         采样预览默认开启不可关（无开关）。基座权重走仓库权重弹窗（用户 2026-10-16）。 */
      var BOOLS = [['混合精度 AMP', 'amp']];

      /* 参数说明（用户 2026-10-16：左键点参数名 → 弹窗看说明）；键=页面标签 */
      var PARAM_HELP = {
        'LoRA 秩 r': '低秩适配矩阵的秩：只训练两个 r 很窄的矩阵，参数量随 r 线性增长。\n' +
          '怎么定值：r=8 起步。任务离基座越远（新领域、新说话风格）越要加：数据 >5k 条或 r=8 效果不够就上 16、32；只调口吻这类小改动 r=4 也够。\n' +
          'r 翻倍 ≈ 可训练参数翻倍，显存和每步耗时也会跟着涨。',
        '缩放 α': '低秩增量按 α/r 缩放，决定 LoRA 分支的相对强度。\n' +
          '怎么定值：常用 α = r（比例 1）或 α = 2r（比例 2）。效果偏弱先把比例提到 2；训练不稳、Loss 抖动就把比例降回 1。\n' +
          '改 r 时要同步改 α，否则强度会跟着漂。',
        'LoRA 丢弃率': '只在 LoRA 分支上做 dropout，基座权重不受影响。\n' +
          '怎么定值：0.05 是常用默认；数据少或明显过拟合（训练/验证 Loss 差距大）加到 0.1~0.15。',
        '挂载目标层': '把低秩适配器挂到哪些线性层上，用逗号分隔。\n' +
          '怎么定值：q_proj,v_proj 最省参数、效果最稳，先只挂这两个；效果不够再加 k_proj,o_proj，最后才考虑全部线性层。\n' +
          '挂得越多参数越多、越接近全参数微调；显存吃紧时优先减少挂载层。',
        'batch_size': '每一步喂进多少条序列。\n' +
          '怎么定值：LoRA 只训练少量参数，显存比全参微调省，batch 8~32 一般够；等效批量用梯度累积补到 32~64。',
        '步数': 'LoRA 训练总步数。\n' +
          '怎么定值：默认 1200 步 —— 按「基座已经做过指令微调」给的档。已 SFT 的基座上再挂 LoRA 通常 1000~1500 步就见顶，再练验证集 Loss 会回升；\n' +
          '基座只做过预训练（还没 SFT）时把它调大（3000~8000）。1 个 epoch = 样本数 ÷ batch_size。',
        '学习率': 'LoRA 的学习率，只更新适配矩阵。\n' +
          '怎么定值：默认 2e-4 —— 基座已经做完 SFT 时用这个档，再大会把已经学好的东西拆掉（实测 3e-4 会把基座自己的训练集 Loss 也练差）；\n' +
          '基座只做过预训练时可以用 3e-4~1e-3。输出几乎没变化 → 乘 2；Loss 抖动或输出崩坏 → 除以 2。',
        '混合精度 AMP': '用 bf16/fp16 做前向反向、fp32 更新权重：省显存、提速。\n' +
          '怎么定值：建议开启，优先 bf16；出现 NaN 就关掉。',
        '基座权重': '要挂 LoRA 的基座模型，必填，且必须指向存在的 .pt。\n' +
          '怎么定值：选后续推理想用的同一个基座（如 eproai_pretrain.pt）；行里显示「—」代表没选到文件，先去仓库确认。\n' +
          '换基座后旧适配器不能复用，必须重新训练。',
      };

      var inputs = {};   /* 配置键 → input */
      var checks = {};   /* 配置键 → checkbox */
      var cursor = 0;    /* /status?since= 游标 */
      var streamBuf = [];/* 宿主 stdout 行缓冲（实时输出，只进 livePre） */
      var liveStarted = false; /* 实时块是否已有输出（丢掉占位提示） */
      var MAX_LIVE = 50;   /* 实时日志只留最近 50 行（用户 2026-10-19：原来 2000） */
      var timer = null;
      var mounted = true;

      /* ---------- 小工具 ---------- */
      function fmt(n) {
        return (Number(n) || 0).toLocaleString('en-US');
      }
      function gnum(v) {
        if (typeof v !== 'number' || !isFinite(v)) return String(v == null ? '—' : v);
        return String(v);
      }
      function icon(paths, cls) {
        var s = document.createElement('span');
        s.className = 'tl-ico' + (cls ? ' ' + cls : '');
        s.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
          'aria-hidden="true">' + paths + '</svg>';
        return s;
      }
      var dotRows = {};        /* 参数行状态小球（用户 2026-10-18） */

      /* 横条参数行（用户 2026-10-19）：与指令微调页「验证集比例」同一个滑条样式 */
      var sliderEls = {};
      function sliderRow(host, label, key, min, max, step, fmt, help) {
        var r = api.el('div', 'tl-row tl-row-range');
        var k = api.el('span', 'tl-k', label);
        api.help(k, label, help);
        r.appendChild(k);
        var box = api.el('div', 'tl-range-box');
        var inp = document.createElement('input');
        inp.type = 'range';
        inp.className = 'tl-range';
        inp.min = min; inp.max = max; inp.step = step;
        var val = api.el('span', 'tl-range-v', '');
        function paint() { val.textContent = fmt(Number(inp.value)); }
        inp.addEventListener('input', paint);
        inp.addEventListener('change', function () { paint(); autoSave(); });
        box.appendChild(inp);
        box.appendChild(val);
        api.rangeEdit(val, inp);                    /* 点数字可直接输入（用户 2026-10-20） */
        r.appendChild(box);
        host.appendChild(r);
        paint();
        sliderEls[key] = { input: inp, paint: paint };
        return sliderEls[key];
      }

      function inputRow(host, label, key, cls) {
        var r = api.el('div', 'tl-row');
        var k = api.el('span', 'tl-k', label);
        api.help(k, label, PARAM_HELP[label]);   /* 点参数名看说明（用户 2026-10-16） */
        r.appendChild(k);
        var inp = api.el('input', 'tl-input pd-input' + (cls ? ' ' + cls : ''));
        inp.type = 'text';
        inp.spellcheck = false;
        inp.value = '';
        inputs[key] = inp;
        inp.addEventListener('input', autoSave);    /* 数据一经更改就直接保存 */
        inp.addEventListener('change', autoSave);
        r.appendChild(inp);
        var dot = api.el('span', 'pd-dot', '');
        dot.dataset.level = 'empty';
        r.appendChild(dot);
        dotRows[key] = { input: inp, dot: dot };
        host.appendChild(r);
        return inp;
      }

      /* 挂载目标层 = 四个标签（用户 2026-10-17）：qkv / proj / fc_in / fc_out，
         四列均分、总宽与输入框一致；点一下切换选中（至少留一个，空集合后端会回落成全挂）。 */
      var TARGETS = ['qkv', 'proj', 'fc_in', 'fc_out'];
      var tChips = {};
      function targetRow(host, label) {
        var r = api.el('div', 'tl-row tl-row-chips');
        var k = api.el('span', 'tl-k', label);
        api.help(k, label, PARAM_HELP[label]);
        r.appendChild(k);
        var box = api.el('div', 'tl-chips-4');
        TARGETS.forEach(function (t) {
          var b = api.el('button', 'tl-chip', t);
          b.type = 'button';
          b.classList.add('on');                 /* 默认全挂（与后端默认值一致） */
          b.setAttribute('aria-pressed', 'true');
          b.addEventListener('click', function () {
            var on = b.classList.contains('on');
            if (on && selectedCount() === 1) return;   /* 至少留一个：全不挂会被后端当成全挂 */
            b.classList.toggle('on', !on);
            b.setAttribute('aria-pressed', on ? 'false' : 'true');
            autoSave();
          });
          tChips[t] = b;
          box.appendChild(b);
        });
        r.appendChild(box);
        /* 隐形状态点（用户 2026-10-23）：参数行右侧都有一颗 8px 状态点，药丸行没有 ——
           补一个同尺寸、同间距的空位，药丸右缘才与上面的输入框右缘齐平。 */
        var ghost = api.el('span', 'pd-dot tl-dot-ghost', '');   /* 与参数行的状态点同一套外边距（pd-dot） */
        ghost.setAttribute('aria-hidden', 'true');
        r.appendChild(ghost);
        host.appendChild(r);
        return box;
      }
      function selectedCount() {
        return TARGETS.filter(function (t) { return tChips[t] && tChips[t].classList.contains('on'); }).length;
      }
      function chipsSet(v) {
        var list = String(v == null ? '' : v).replace(/，/g, ',').split(',')
          .map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean);
        if (!list.length) list = TARGETS.slice();          /* 空 → 全挂（后端同口径） */
        TARGETS.forEach(function (t) {
          var on = list.indexOf(t) >= 0;
          if (tChips[t]) { tChips[t].classList.toggle('on', on); tChips[t].setAttribute('aria-pressed', on ? 'true' : 'false'); }
        });
      }

      /* ---------- 骨架 ---------- */
      /* 用户 2026-10-14：大标题下不要小字介绍 */
      root.appendChild(api.hero('LoRA 微调'));
      var stack = api.el('div', 'stack');
      root.appendChild(stack);

      /* 动作行：开始 / 停止（等宽、靠左；改动即存，无「保存配置」按钮） */
      var actions = api.el('div', 'tl-actions');

      var startBtn = api.el('button', 'btn btn-accent tl-start');
      startBtn.type = 'button';
      startBtn.appendChild(api.el('span', null, '▶ 开始'));   /* 与预训练一致（用户 2026-10-15） */

      var stopBtn = api.el('button', 'btn tl-btn-danger');
      stopBtn.type = 'button';
      stopBtn.disabled = true;   /* 未运行时置灰 */
      stopBtn.appendChild(icon('<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>'));   /* 暂停图标，与预训练一致 */
      stopBtn.appendChild(api.el('span', null, '停止'));

      var refreshBtn = api.el('button', 'tl-btn2');   /* 与预训练一致（用户 2026-10-16：每页三钮开始/停止/刷新） */
      refreshBtn.type = 'button';
      refreshBtn.appendChild(icon('<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'));   /* 刷新图标，与预训练一致 */
      refreshBtn.appendChild(api.el('span', null, '刷新'));

      actions.appendChild(startBtn);
      actions.appendChild(stopBtn);
      actions.appendChild(refreshBtn);

      /* 「刷新」行为与预训练一致：未在训练时重读配置/产出/日志（用户 2026-10-16） */
      refreshBtn.onclick = function () {
        if (startBtn.disabled) return;
        Promise.all([
          api.get('/api/train_lora/config').then(renderConfig),
          api.get('/api/train_lora/artifacts').then(renderArtifacts),
          api.get('/api/train_lora/log').then(renderLog),
        ]);
      };
      /* 用户 2026-10-16：动作行挪进右列顶部（参数左/按钮右上/日志右下 = 预训练页同款排布） */
      /* 挂载点见下方 rightCol 创建处 */

      /* 用户 2026-10-15：去掉状态提示行——事件与问题都写训练输出日志（风格同预训练）；
         轮询反复刷同一状态（「训练进行中 · pid…」秒数会变）→ 按首个「·」前的键去重。 */
      var lastNoteKey = '';
      function setNote(text, isErr) {
        var t = String(text == null ? '' : text);
        if (!t) return;
        var key = t.split('·')[0].trim();
        if (!isErr && key === lastNoteKey) return;
        lastNoteKey = key;
        appendLines([t]);
      }

      /* 两列：左「参数」(权重 2) / 右「日志 + 曲线」(权重 3)，对齐 Tk 2:3 */
      var grid = api.el('div', 'tl-grid');

      /* ----- 左：参数（可编辑，接真保存/启动） ----- */
      var paramCard = api.card('');   /* 「参数」标题移到命名分隔线下（用户 2026-10-16），卡头不再放标题 */
      var phead = paramCard.querySelector('.card-head');
      if (phead) phead.remove();
      var rowsHost = api.el('div', 'tl-rows');
      FIELDS.forEach(function (f) {
        if (f[2] === 'lora_targets') { targetRow(rowsHost, f[0]); return; }
        inputRow(rowsHost, f[0], f[2], '');
      });
      sliderRow(rowsHost, '验证集比例', 'val_ratio', 5, 40, 1,
        function (v) { return v + '%'; },
        '从语料里留出多少做验证集：越大越能看出过拟合，但可用于训练的样本更少。常用 10%。\n' +
          '这个比例是全局的：预训练 / 继续预训练 / 指令微调 / LoRA 微调都按它切（config 里的 train.val_ratio）。');
      sliderRow(rowsHost, '检查点数量', 'ckpt_count', 0, 5, 1,
        function (v) { return v ? String(v) : ((window.I18N && window.I18N.lang() === 'en') ? 'Off' : '关闭'); },   /* 不带单位（用户 2026-10-20） */
        '训练中途额外保存几份权重：设 1 就在一半处存一次，设 2 就在 1/3、2/3 处各存一次（k/(N+1) 等分点）。\n' +
          '用途：训练后期过拟合（验证 Loss 往上翘）时，可以回到中途那份继续用。\n' +
          '文件存 checkpoints/，名字是「权重名-ck步数.pt」，仓库权重弹窗里能直接选。');
      api.bindParamDots('lora', dotRows, collectBody);   /* 参数行状态小球（用户 2026-10-18） */
      /* 给 collectBody / renderConfig 留一个 .value 接口：读=当前选中的逗号串，写=按串点亮标签 */
      inputs.lora_targets = {
        get value() { return TARGETS.filter(function (t) { return tChips[t] && tChips[t].classList.contains('on'); }).join(','); },
        set value(v) { chipsSet(v); },
      };
      /* 模型权重命名（用户 2026-10-15 终版）：参数卡内、参数上方；输入即审仓库同名 */
      /* 判重要同时看「填的名字 + .pt」和「填的名字 + .lora.pt」（用户 2026-10-22）：
         LoRA 权重真实落盘名是 <名>.lora.pt，只比 <名>.pt 会漏，导出那刻才撞名。 */
      var wname = api.weightName({
        inputClass: 'tl-input', fallback: 'eproai_lora',
        checkNames: function (stem) {
          var base = String(stem || '').replace(/\.lora$/i, '');
          return [base + '.pt', base + '.lora.pt'];
        },
      });
      var outRaw = '';   /* out 原值：保留目录部分、只换文件名 */
      function outValueFor(stem) {
        var cur = String(outRaw || '');
        var i = Math.max(cur.lastIndexOf('/'), cur.lastIndexOf('\\'));
        return (i >= 0 ? cur.slice(0, i + 1) : 'checkpoints/') + stem + '.pt';
      }
      wname.input.addEventListener('input', autoSave);
      wname.input.addEventListener('change', autoSave);
      /* 2026-10-15 终版：命名在参数卡内、参数上方；标题与「参数」同格式，下方同款分隔条 */
      paramCard.body.appendChild(wname.wrap);
      paramCard.body.appendChild(api.el('div', 'tl-sep'));
      paramCard.body.appendChild(api.el('h2', 'card-title', '参数'));   /* 「参数」在命名下划线下、参数行上（用户 2026-10-16） */
      paramCard.body.appendChild(rowsHost);

      var sep1 = api.el('div', 'tl-sep');
      paramCard.body.appendChild(sep1);
      var boolsHost = api.el('div', 'tl-rows');
      BOOLS.forEach(function (b) {
        /* 混合精度行 = 预训练 tp-check 同款格式（用户 2026-10-16）：行内 [勾选框][参数名]，无左标签列 */
        var row = api.el('div', 'tl-bool-row');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'tl-check';
        checks[b[1]] = cb;
        cb.addEventListener('change', autoSave);    /* 数据一经更改就直接保存 */
        var lbl = api.el('span', 'tl-bool-label', b[0]);
        api.help(lbl, b[0], PARAM_HELP[b[0]]);   /* 点参数名看说明（用户 2026-10-16） */
        row.appendChild(cb);
        row.appendChild(lbl);
        boolsHost.appendChild(row);
      });
      paramCard.body.appendChild(boolsHost);

      var sep2 = api.el('div', 'tl-sep');
      paramCard.body.appendChild(sep2);

      /* 基座权重行 = 前几页同款（用户 2026-10-16）：标签 + [状态点][选择][文件名][大小]，
         点「选择」= 点整行 = 弹仓库权重弹窗；完整路径收进 title。 */
      function tileSize(n) {
        return api.tileSize(n);   /* 共享口径：三位数不带小数（用户 2026-10-18） */
      }

      function humanSize(n) {
        n = Number(n);
        if (!isFinite(n) || n < 0) { return '—'; }
        if (n < 1024) { return n + ' B'; }
        if (n < 1024 * 1024) { return (n / 1024).toFixed(1) + ' KB'; }
        if (n < 1024 * 1024 * 1024) { return (n / 1048576).toFixed(1) + ' MB'; }
        return (n / 1073741824).toFixed(2) + ' GB';
      }
      function pathRow(label) {
        var lbl = api.el('div', 'tl-k', label);
        api.help(lbl, label, PARAM_HELP[label]);   /* 点参数名看说明（用户 2026-10-16） */
        lbl.style.width = 'auto';
        lbl.style.margin = '12px 0 6px';
        paramCard.body.appendChild(lbl);
        var wrapRow = api.el('div', 'tl-path-row');
        var dot = api.el('span', 'tl-dot');
        var pickBtn = document.createElement('button');
        pickBtn.type = 'button';
        pickBtn.className = 'tl-pick';
        pickBtn.textContent = '选择';
        var val = api.el('span', 'tl-path empty', '—');
        var size = api.el('span', 'tl-size', '');
        wrapRow.appendChild(dot);
        wrapRow.appendChild(pickBtn);
        wrapRow.appendChild(val);
        wrapRow.appendChild(size);
        return { dot: dot, val: val, size: size, wrap: wrapRow, pick: pickBtn, label: lbl };
      }
      function renderPathRow(row, info, raw) {
        info = info || {};
        /* 文件不存在 = 当没选（用户 2026-10-17） */
        var text = (info.exists === false) ? '' : (info.abs || raw || '');
        var disp = text ? String(text).split(/[\\/]/).pop() : '';   /* 只显示文件名（同前几页） */
        row.val.textContent = disp || '-';   /* 没选权重只显示一个小横杠（用户 2026-10-23） */
        row.val.title = text;
        row.val.classList.toggle('empty', !text);
        row.dot.className = 'tl-dot ' + (text ? (info.exists == null ? '' : (info.exists ? 'ok' : 'err')) : '');
        row.size.textContent = info.exists ? humanSize(info.size_bytes) : (text && info.exists === false ? '不存在' : '');
        row.size.classList.toggle('tl-err', !!text && info.exists === false);
      }
      var baseRow = pathRow('基座权重');
      var baseRaw = '';
      var baseInfo = null;
      function renderBaseRow() {
        var info = (baseInfo && baseInfo.abs === baseRaw) ? baseInfo : (baseRaw ? { abs: baseRaw } : null);
        renderPathRow(baseRow, info, baseRaw);
      }
      /* 用户 2026-10-17：只有「选择」按钮可点（参考 LoRA 合并页），整行不再响应点击 */
      baseRow.pick.title = '从仓库选择权重';
      baseRow.pick.addEventListener('click', function () {
        api.weightPicker({
          title: '基座权重',
          current: baseRaw,
          onPick: function (w) {
            baseRaw = w.path;
            baseInfo = { abs: w.path, exists: true, size_bytes: w.size };
            renderBaseRow();
            autoSave();
          },
        });
      });
      paramCard.body.appendChild(baseRow.wrap);

      /* ----- 右：动作行 + 日志 + 曲线（用户 2026-10-16：排布参考预训练页；权重文件卡已删 2026-10-17） ----- */
      var rightCol = api.el('div', 'tl-col');
      rightCol.appendChild(actions);

      /* 概览瓷片：四张，与预训练页同款（用户 2026-10-17：右侧内容/格式/间距参考预训练） */
      var stats = api.el('div', 'stats');
      function tile(label) {
        var st = api.el('div', 'stat');
        var lb = api.el('div', 'stat-label', label);
        st.appendChild(lb);
        var v = api.el('div', 'stat-val', '')   /* 用户 2026-10-17：先空着，别画占位「—」，否则切页会跟状态文字换一下=闪 */;
        st.appendChild(v);
        stats.appendChild(st);
        v.labelEl = lb;
        return v;
      }
      var vState = tile('训练状态');
      var vMem = tile('占用内存');
      var vCkpt = tile('输出权重');
      var vCorpus = tile('语料体量');
      vState.textContent = '未启动';
      vState.classList.add('idle');   /* 首帧先放终态，避免切页时空白→文字跳一下（用户 2026-10-17） */
      rightCol.appendChild(stats);

      /* 权重文件卡整块已删（用户 2026-10-17）：文件/目录清单与上面四张瓷片、参数卡里的「基座权重」行重复，左列只留参数卡。 */

      /* 两块日志区互不覆盖：
         ① 训练输出（实时）—— 只由 /status?since= 的 lines 追加，任何刷新都不覆盖；
         ② Loss 日志表（CSV）—— 只由 /log 的真实 CSV 尾部写入（曲线同源）。 */
      var liveCard = api.card('训练输出');
      var liveBox = api.el('div', 'tl-logbox');   /* 外框定距 + 内层滚动（与预训练页同款） */
      var livePre = api.el('pre', 'tl-log tl-log-live');   /* 空态不写占位文字（用户 2026-10-17） */
      liveBox.appendChild(livePre);
      liveCard.body.appendChild(liveBox);
      rightCol.appendChild(liveCard);

      var logCard = api.card('Loss 日志表');   /* 卡头不带副标题，与预训练同款（用户 2026-10-17） */
      var csvBox = api.el('div', 'tl-logbox');
      var csvPre = api.el('pre', 'tl-log tl-log-csv', '读取中…');
      csvBox.appendChild(csvPre);
      logCard.body.appendChild(csvBox);
      rightCol.appendChild(logCard);

      var chartCard = api.card('Loss 曲线');
      var chartHost = api.el('div', 'tl-chart');
      chartCard.body.appendChild(chartHost);
      rightCol.appendChild(chartCard);

      var leftCol = api.el('div', 'tl-col');
      leftCol.appendChild(paramCard);
      grid.appendChild(leftCol);
      grid.appendChild(rightCol);
      stack.appendChild(grid);
      scheduleFit();


      /* ---------- 渲染 · 参数 ---------- */
      function renderConfig(d) {
        if (d && d.error) {
          setNote('读取失败：' + d.error, true);   /* 信息框已删（用户 2026-10-16）：问题写训练输出日志 */
          return;
        }
        d = d || {};
        inputs.lora_r.value = gnum(d.r);
        inputs.lora_alpha.value = gnum(d.alpha);
        inputs.lora_dropout.value = gnum(d.dropout);
        inputs.lora_targets.value = d.targets || '';
        inputs.batch_size.value = gnum(d.batch_size);
        inputs.lora_steps.value = gnum(d.lora_steps);
        inputs.lora_lr.value = gnum(d.lora_lr);
        var vr = Number(d.val_ratio);          /* 验证集比例（用户 2026-10-19） */
        if (!isFinite(vr) || vr <= 0) { vr = 0.1; }
        sliderEls.val_ratio.input.value = Math.round(vr * 100);
        sliderEls.val_ratio.paint();
        var cc = parseInt(d.ckpt_count, 10);   /* 检查点数量（用户 2026-10-19） */
        sliderEls.ckpt_count.input.value = (isFinite(cc) && cc > 0) ? Math.min(5, cc) : 0;
        sliderEls.ckpt_count.paint();
        baseRaw = d.lora_init_from || '';   /* 基座权重：选择弹窗行回填（2026-10-23 独立键） */
        renderBaseRow();
        if (checks.amp) checks.amp.checked = !!d.amp;
        if (checks.sample) checks.sample.checked = !!d.sample;

        /* 权重命名不回填（用户 2026-10-17：新工程不要默认命名，名字由用户自己填）；
           outRaw 只用于算输出目录，不再往命名框填字。 */
        outRaw = String(d.lora_out || '');
        /* 用户自己填过的权重名要回填（用户 2026-10-19）；默认名不回填 */
        (function () {
          var f = String(outRaw || '').split(/[\\/]/).pop().replace(/\.pt$/i, '');
          if (f && f !== 'eproai_lora' && f !== 'eproai' && !wname.stem()) { wname.input.value = f; wname.audit(); }
        })();
      }

      /* ---------- 渲染 · 权重 / 语料 ---------- */
      function renderArtifacts(d) {
        /* 权重文件卡已删（用户 2026-10-17）：这里只把数据回写到瓷片与参数卡的「基座权重」行 */
        if (d && d.error) {
          setNote('读取失败：' + d.error, true);
          return;
        }
        (d.items || []).forEach(function (it) {
          if (it.key === 'corpus') {   /* 语料体量瓷片（用户 2026-10-17：右侧对齐预训练）；口径：不显示训练语料地址 */
            vCorpus.textContent = it.exists ? (it.size_human || tileSize(it.size_bytes)) : '缺失';
            vCorpus.classList.toggle('ok', !!it.exists);
            vCorpus.classList.toggle('missing', !it.exists);   /* 缺失=红（用户 2026-10-18，同 LoRA 合并页） */
            vCorpus.classList.remove('warn');
            return;
          }
          if (it.key === 'base') {   /* 基座权重存在/大小回写选择行（用户 2026-10-16） */
            baseInfo = { abs: it.path, exists: !!it.exists, size_bytes: it.size_bytes };
            if (!it.exists) baseRaw = '';   /* 文件不存在就别当成已选（用户 2026-10-17） */
            renderBaseRow();
          }
          if (it.key === 'out' || it.key === 'adapter') {   /* 输出权重瓷片（用户 2026-10-17）：LoRA 的产物是适配器 */
            vCkpt.textContent = it.exists ? (it.size_human || tileSize(it.size_bytes)) : '未生成';
            vCkpt.classList.toggle('ok', !!it.exists);
            vCkpt.classList.toggle('idle', !it.exists);   /* 未生成=灰（用户 2026-10-17） */
            if (it.mtime) vCkpt.title = it.mtime;
          }
        });
      }

      /* ---------- 渲染 · 日志 + 曲线 ---------- */
      function drawChart(points) {
        chartHost.textContent = '';
        if (!points || points.length < 2) {
          chartHost.appendChild(api.el('div', 'tl-chart-empty', '暂无足够数据点'));
          return;
        }
        var W = 600, H = 180, pad = 30;
        var xs = points.map(function (p) { return p.step; });
        var tr = points.map(function (p) { return p.train; });
        var va = points.map(function (p) { return p.val == null ? p.train : p.val; });
        var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
        var ymin = Math.min.apply(null, tr.concat(va));
        var ymax = Math.max.apply(null, tr.concat(va));
        if (xmax === xmin) xmax = xmin + 1;
        if (ymax === ymin) ymax = ymin + 1;
        function X(x) { return pad + (x - xmin) / (xmax - xmin) * (W - 2 * pad); }
        function Y(y) { return H - pad - (y - ymin) / (ymax - ymin) * (H - 2 * pad); }
        function poly(arr) {
          return arr.map(function (v, i) { return X(xs[i]).toFixed(1) + ',' + Y(v).toFixed(1); })
            .join(' ');
        }
        var grid = '';
        for (var i = 0; i < 4; i++) {
          var gy = (pad + i * (H - 2 * pad) / 3).toFixed(1);
          grid += '<line x1="' + pad + '" y1="' + gy + '" x2="' + (W - pad) + '" y2="' + gy +
            '" stroke="var(--LINE_SOFT)" stroke-width="1"/>';
        }
        chartHost.innerHTML =
          '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="180" ' +
          'preserveAspectRatio="none" role="img" aria-label="Loss 曲线">' + grid +
          '<polyline fill="none" stroke="var(--ACCENT)" stroke-width="2" ' +
          'points="' + poly(tr) + '"/>' +
          '<polyline fill="none" stroke="var(--OK)" stroke-width="2" ' +
          'points="' + poly(va) + '"/>' +
          '<text x="' + pad + '" y="14" fill="var(--INK3)" font-size="9">' +
          '<tspan fill="var(--ACCENT)">—— train</tspan>    ' +
          '<tspan fill="var(--OK)">—— val</tspan></text>' +
          '<text x="' + (W - 8) + '" y="' + Y(ymax).toFixed(1) +
          '" text-anchor="end" fill="var(--INK3)" font-size="9">' + ymax.toFixed(2) + '</text>' +
          '<text x="' + (W - 8) + '" y="' + Y(ymin).toFixed(1) +
          '" text-anchor="end" fill="var(--INK3)" font-size="9">' + ymin.toFixed(2) + '</text>' +
          '</svg>';
      }

      /* CSV 日志表：只写 csvPre / logSub；训练中照常刷新，绝不会碰实时输出块 */
      function renderLog(d) {
        if (d && d.error) {
          csvPre.textContent = '读取失败：' + d.error;
          csvPre.classList.add('tl-err');
          drawChart([]);
          scheduleFit();
          return;
        }
        csvPre.classList.remove('tl-err');
        d = d || {};
        if (!d.exists || !d.total) {
          csvPre.textContent = '暂无训练日志';   /* 不带地址；只有表头也算未训练（用户 2026-10-17） */
          drawChart([]);
          scheduleFit();
          return;
        }
        csvPre.textContent = d.tail_text || '（日志为空）';
        drawChart(d.points || []);
        scheduleFit();
      }

      /* Loss 曲线高度按窗口可用空间收口（用户 2026-10-17：曲线部分跑到窗口/卡片边框外）。
         卡头 + 内边距先量出来扣掉，余量全给图；上限 180、下限 120，再小就让整页滚动。 */
      var fitQueued = false;
      function fitChart() {
        fitQueued = false;
        if (!chartCard || !chartCard.isConnected || !chartHost) { return; }
        var content = document.querySelector('.content');
        if (!content) { return; }
        var cr = content.getBoundingClientRect();
        var top = chartCard.getBoundingClientRect().top - cr.top + content.scrollTop;
        var chrome = chartCard.offsetHeight - chartHost.offsetHeight;
        var avail = content.clientHeight - top - 20;                /* 20 = 页面底部留白 */
        var h = 180;   /* Loss 曲线固定高度（用户 2026-10-18）：不随数据点/可用空间变化 */
        if (h > 0) { chartHost.style.height = h + 'px'; }
      }
      function scheduleFit() {
        if (fitQueued) { return; }
        fitQueued = true;
        requestAnimationFrame(fitChart);
      }
      window.addEventListener('resize', scheduleFit);

      /* 训练输出（实时）：只追加到 livePre，上限 2000 行 */
      function appendLines(lines) {
        if (!lines || !lines.length) return;
        if (!liveStarted) { liveStarted = true; streamBuf = []; }
        for (var i = 0; i < lines.length; i++) streamBuf.push(String(lines[i]));
        if (streamBuf.length > MAX_LIVE) streamBuf = streamBuf.slice(-MAX_LIVE);
        livePre.textContent = streamBuf.join('\n');
        livePre.scrollTop = livePre.scrollHeight;
        scheduleFit();   /* 训练输出变长 → 曲线卡要重新收口 */
      }

      function refreshLogAndArtifacts() {
        api.get('/api/train_lora/log').then(renderLog);
        api.get('/api/train_lora/artifacts').then(renderArtifacts);
      }

      /* ---------- 表单取值 ---------- */
      function collectBody() {
        var body = {};
        /* 2026-10-23：空值不写。config 读取失败时表单是空的，原来的写法会把好配置覆盖成 0
           （实测把 lora_lr 写成 0 —— 这种值再去训练就是学习率 0，等于没训）。 */
        FIELDS.forEach(function (f) {
          var box = inputs[f[2]];
          var v = box && box.value != null ? String(box.value).trim() : '';
          if (v !== '') body[f[2]] = v;
        });
        body.lora_init_from = baseRaw;   /* 基座权重：选择弹窗行的值（2026-10-23 独立键） */
        BOOLS.forEach(function (b) { body[b[1]] = !!(checks[b[1]] && checks[b[1]].checked); });
        body.sample = true;   /* 采样预览默认开启、不可关闭（页面无开关） */
        body.val_ratio = Number(sliderEls.val_ratio.input.value) / 100;   /* 0.05~0.4（用户 2026-10-19） */
        body.ckpt_count = Number(sliderEls.ckpt_count.input.value);       /* 0~5 个检查点 */
        var stemName = wname.stem();      /* 命名由用户自己填；没填就不写 out（2026-10-17） */
        if (stemName) { body.lora_out = outValueFor(stemName); }
        return body;
      }

      /* ---------- 动作 ---------- */
      var wasRunning = null;
      /* 训练结束/被中止的这一刻权重才落盘 → 立刻重判一次命名（用户 2026-10-22）。
         两次：250ms 抢在落盘后立刻判；1.6s 只在前一次还没判出重复时补一次（写盘慢的情况），
         避免已经判出「重复」后又闪一次灰球。 */
      function reAuditName() {
        if (!wname) return;
        setTimeout(function () { wname.audit(); }, 250);
        setTimeout(function () { if (!wname.state || !wname.state.dup) wname.audit(); }, 1600);
      }
      function setRunningState(running) {
        /* 训练结束/被中止的这一刻权重才落盘 → 立刻重判一次命名（用户 2026-10-22）。
           不重判的话本页的「重复」要切页回来才出现。 */
        if (wasRunning === true && running === false) {
          reAuditName();
        }
        wasRunning = !!running;
        startBtn.disabled = !!running;
        stopBtn.disabled = !running;
        /* 左侧参数训练中禁改（用户 2026-10-20：「无论是否使用流水线功能，当开启训练后，
           左侧框变灰，不让用户更改」）：置灰靠 CSS（.tl-running），控件真禁用兜住键盘。 */
        root.classList.toggle('tl-running', !!running);
        [].forEach.call(leftCol.querySelectorAll('input, select, textarea, button'), function (el) {
          if (running) el.setAttribute('disabled', ''); else el.removeAttribute('disabled');
        });
      }

      /* 数据一经更改就直接保存（用户口径：无「保存配置」按钮；成功静默，失败才提示） */
      var saveTimer = null;
      function autoSave() {
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
          saveTimer = null;
          api.post('/api/train_lora/save', collectBody()).then(function (d) {
            if (!d || !d.ok) {
              setNote('保存失败：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            }
          });
        }, 400);
      }

      function doStart() {
        setNote('正在启动 LoRA 微调…（组装 runtime 配置并起 core/train.py）');
        api.post('/api/train_lora/start', collectBody()).then(function (d) {
          if (d && d.ok) {
            cursor = 0;
            streamBuf = [];
            liveStarted = false;
            livePre.textContent = '';
            setRunningState(true);
            setNote('已启动 · pid ' + (d.pid == null ? '—' : d.pid) +
              ' · 运行时配置 ' + (d.runtime_cfg || '—'));
            refreshLogAndArtifacts();
            pollOnce();
            startPolling();
          } else {
            setRunningState(false);
            setNote('启动被拦：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            api.get('/api/train_lora/artifacts').then(renderArtifacts);
          }
        });
      }

      function doStop() {
        setNote('正在请求优雅停止（写 stop.flag）…');
        api.post('/api/train_lora/stop', {}).then(function (d) {
          if (d && d.ok) setNote((d.note) || '已请求停止');
          else setNote('停止失败：' + ((d && d.error) || '未知错误'), true);
        });
      }

      startBtn.addEventListener('click', function () {
        if (!wname.stem()) {              /* 命名空 → 不启动，光标回命名框（提示已在框右「未命名」，用户 2026-10-17） */
          wname.input.focus();
          return;
        }
        /* 审查：模型权重命名与仓库权重同名 → 不启动（用户 2026-10-15） */
        wname.audit().then(function (st) {
          if (st && st.dup) {
            setNote('模型权重命名与仓库权重「' + st.name + '」同名，请改名后再开始。', true);
            return;
          }
          /* 参数审查（用户 2026-10-18）：不常用/不可用的参数先弹窗，确认了再起训练 */
          return api.paramCheck('lora', collectBody(), 'LoRA 微调').then(function (ok) {
            if (ok) doStart();
          });
        });
      });
      stopBtn.addEventListener('click', doStop);

      /* ---------- 轮询 ---------- */
      function startPolling() {
        if (timer || !mounted) return;
        timer = setInterval(pollOnce, 1000);
      }
      function stopPolling() {
        if (timer) { clearInterval(timer); timer = null; }
      }

      function pollOnce(initial) {
        return api.get('/api/train_lora/status?since=' + cursor).then(function (d) {
          if (!mounted) return;
          if (d && d.error) {
            if (!initial) setNote('状态读取失败：' + d.error, true);
            return;
          }
          d = d || {};
          var lines = d.lines || [];
          if (lines.length) appendLines(lines);
          if (typeof d.next === 'number') cursor = d.next;

          var running = !!d.running;
          setRunningState(running);
          /* 瓷片跟预训练同口径（用户 2026-10-17） */
          vState.textContent = running ? '运行中' : '未启动';
          vState.classList.toggle('ok', running);
          vState.classList.toggle('idle', !running);   /* 未启动=灰（用户 2026-10-17） */
          if (running && d.pid != null) vState.title = 'pid ' + d.pid;
          vMem.textContent = tileSize(d.mem_bytes);
          vMem.classList.toggle('ok', d.mem_bytes != null);

          if (running) {
            setNote('训练进行中 · pid ' + (d.pid == null ? '—' : d.pid) +
              ' · 已输出 ' + (d.total_lines || 0) + ' 行' +
              (d.stop_flag ? ' · 已请求停止' : ''));
            if (lines.length) refreshLogAndArtifacts();
            startPolling();
          } else {
            stopPolling();
            if (!initial) {
              setNote('训练进程已结束' +
                (d.exit_code == null ? '' : ('（退出码 ' + d.exit_code + '）')));
              refreshLogAndArtifacts();
            }
          }
        });
      }

      /* ---------- 取数 ---------- */
      /* 首帧直出（治切页闪烁）：模块缓存的上次数据直接画，后台再刷新 */
      if (TL_CACHE) {
        try {
          renderConfig(TL_CACHE.config);
          renderArtifacts(TL_CACHE.artifacts);
          renderLog(TL_CACHE.log);
        } catch (e) {}
      }
      var initPromise = Promise.all([
        api.get('/api/train_lora/config').then(renderConfig),
        api.get('/api/train_lora/artifacts').then(renderArtifacts),
        api.get('/api/train_lora/log').then(renderLog),
      ]).then(function () {
        /* 挂载时同步一次训练状态：若已有训练在跑，直接接管轮询 */
        return pollOnce(true);
      }, function (e) {
        setNote('页面取数失败：' + e, true);   /* 信息框已删（用户 2026-10-16）：问题写训练输出日志 */
      });

      /* 供 unmount 清理定时器（挂在 registry 对象上由 mount 返回的清理闭包不方便，
         这里把 stop 回调存到 root.__tlCleanup）。 */
      root.__tlCleanup = function () {
        mounted = false;
        stopPolling();
        window.removeEventListener('resize', scheduleFit);
      };
      return initPromise;
    },

    unmount: function (root) {
      try {
        if (root && typeof root.__tlCleanup === 'function') root.__tlCleanup();
      } catch (e) { /* ignore */ }
    },
  });
/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
(window.__EAI_API || {}).onProjectChange && window.__EAI_API.onProjectChange(function () { TL_CACHE = null; TL_CACHE_AT = 0; TL_INFLIGHT = null; });

})();
