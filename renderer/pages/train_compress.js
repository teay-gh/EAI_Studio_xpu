/* ============================================================
   EAI_Studio · pages/train_compress.js ——「模型裁切」「量化」两页（共用工厂）
   导航键 train_prune（模型裁切）/ train_quant（量化）/ 分组「训练中心」
   ------------------------------------------------------------
   基于 llm-compressor（PyPI: llmcompressor）的真压缩：子进程跑
   engine/tools/compress_run.py（.venv_compress 独立 Python 3.11 环境，
   主 venv 的 XPU torch 不受影响）。裁切 = SparseGPT / Wanda（稀疏化）；
   量化 = GPTQ W8A16 / W4A16、FP8_DYNAMIC。
   真实数据全部来自 bridge_api/train_prune.py / train_quant.py：
     /api/train_prune|train_quant/config   参数 + 压缩环境 + 产物探针
     /api/train_prune|train_quant/status   子进程状态 + 增量日志（?since=<游标>）
     /api/train_prune|train_quant/save     保存参数
     /api/train_prune|train_quant/start    自动合并校准语料 → 起压缩子进程
     /api/train_prune|train_quant/stop     停止（写 stop.flag + 强杀子进程）
   权重口径（同训练页）：只认仓库权重，点击弹窗选择（api.weightPicker），
   外部权重先进仓库。校准语料口径（同训练页）：点「开始」自动合并预训练数据集。
   ============================================================ */
(function () {
  'use strict';

  /* 参数说明（用户 2026-10-17：解释写详细 + 怎么定值；点参数名弹窗看） */
  var PARAM_HELP = {
    '权重': '要被裁切/量化的权重文件，必须指向真实存在的 .pt。\n' +
      '怎么定值：选仓库里最新的预训练或续训产物；行里显示「—」代表没选到文件，先去仓库确认。',
    '裁切方法': 'SparseGPT：逐层按权重重要性剪枝，慢一些但精度保持好；Wanda：按权重×激活幅度排序剪，快且几乎不用校准。\n' +
      '怎么定值：先用 Wanda 快速看效果；精度掉得多再换 SparseGPT 重跑对比。',
    '量化方案': 'W8A16：权重 8bit、激活 16bit，精度损失最小；W4A16：权重 4bit，省一半空间、精度略降；FP8_DYNAMIC：权重与激活都 FP8、动态缩放，需要较新的硬件。\n' +
      '怎么定值：先跑 W8A16 把流程走通，再降到 W4A16 看困惑度；掉得明显就回 W8A16，或换 FP8_DYNAMIC 对比。',
    '稀疏度': '剪掉的权重比例，0~1：0.5 表示剪掉一半。\n' +
      '怎么定值：0.3 起步看效果，0.5 是常用档；准确率掉得多就退到 0.3~0.4。超过 0.7 通常会明显掉点。',
    '校准样本数': '用多少条语料样本估计权重重要性 / 量化范围。\n' +
      '怎么定值：64~128 足够；时间和显存紧张就 32。量化后困惑度偏高、而样本数又很少（<32）时加到 128 重测。',
    '最大序列长': '校准 / 量化时每条样本截多长。\n' +
      '怎么定值：取常用推理长度（128 / 256 / 512），至少覆盖语料 95% 的长度；更长只增加耗时，效果提升很小。',
    '校准语料': '校准用的语料：预训练数据集（文本）和指令微调数据集（对话）可多选，两个都选就是「文本 + 对话」，\n'
      + '点「开始」时自动合并选中的数据集，校准样本数在选中的来源之间均分。',
  };

  /* 瓷片终态缓存（用户 2026-10-17：切页时「输出权重」闪）：模块级、不随页面实例重建。
     切回来时首帧直接画上一次的终态，等 /status 回来再写的是同一份值 → 不再 空→文字 跳一下。 */
  var TILE_SNAP = {};

  function makePage(key, opt) {
    var base = '/api/' + key;
    var title = opt.title;

    return {
      title: title,

      mock: (function () {
        var m = {};
        m[base + '/config'] = {
          ok: true, mode: opt.mode, project: '<工程目录>',
          params: { weight: '', method: 'sparsegpt', scheme: 'W8A16', sparsity: 0.5,
            calib_samples: 64, seq_len: 128, out: '' },
          env: { ok: true, python: '<项目目录>\\\\.venv_compress\\\\Scripts\\\\python.exe' },
          outputs: { weight: { path: '', exists: false, size_bytes: 0 },
            out: { path: '', exists: false, size_bytes: 0 } },
          running: false,
        };
        m[base + '/status'] = {
          ok: true, running: false, pid: null, exit_code: null, lines: [],
          total_lines: 0, next: 0, job_exists: false,
          outputs: { weight: { exists: false, size_bytes: 0 }, out: { exists: false, size_bytes: 0 } },
          env: { ok: true },
        };
        m[base + '/start'] = { ok: false, action: 'start', error: '请先点击「权重」行选择仓库权重。' };
        m[base + '/stop'] = { ok: true, action: 'stop', note: '没有正在运行的压缩任务。' };
        m[base + '/save'] = { ok: true, action: 'save', saved: {} };
        return m;
      })(),

      mount: function (root, api) {
        var rows = opt.fields;   /* [标签, 键, 类型, 占位] */
        var inputs = {};
        var segState = { method: 'sparsegpt', scheme: 'W8A16' };
        var weightRaw = '';      /* 选中的仓库权重（绝对路径；仓库口径） */
        var cursor = 0;
        var streamBuf = [];
        var liveStarted = false;
        var MAX_LIVE = 50;   /* 实时日志只留最近 50 行（用户 2026-10-19） */
        var timer = null;
        var mounted = true;

        /* ---------- 小工具 ---------- */
        function fmtSize(n) {
          n = Number(n) || 0;
          if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
          if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
          return n + ' B';
        }
        /* 瓷片数值压成单行（用户 2026-10-16）：单位不留空格、KB 取整、自动升档 */
        function tileSize(n) {
          return api.tileSize(n);   /* 共享口径：三位数不带小数（用户 2026-10-18） */
        }
        /* 初始权重瓷片：有文件就显示体量（与「输出权重」同口径），没有就 未选择 */
        function setInitTile(probe) {
          var v = statVal && statVal.init;
          if (!v) return;
          var ok = !!(probe && probe.exists);
          v.textContent = ok ? tileSize(probe.size_bytes) : '未选择';
          v.title = ok ? baseName(probe.path) : '';
          v.classList.toggle('ok', ok);
          v.classList.toggle('idle', !ok);
          if (typeof snapTiles === 'function') snapTiles();
        }
        function icon(paths) {
          var s = document.createElement('span');
          s.className = 'cm-ico';
          s.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
            'aria-hidden="true">' + paths + '</svg>';
          return s;
        }
        function baseName(p) {
          p = String(p || '').replace(/\\/g, '/');
          return p.split('/').pop() || '—';
        }

        /* ---------- 骨架 ---------- */
        root.appendChild(api.hero(title));
        var stack = api.el('div', 'stack');
        root.appendChild(stack);

        /* 动作行：开始 / 停止 / 刷新（等宽 100×36、间距 8 —— 与预训练页同标准） */
        var actions = api.el('div', 'cm-actions');
        var startBtn = document.createElement('button');
        startBtn.type = 'button';
        startBtn.className = 'btn btn-accent';
        startBtn.appendChild(api.el('span', null, '▶ 开始'));   /* 无图标元素，与预训练一致（用户 2026-10-16） */
        var stopBtn = document.createElement('button');
        stopBtn.type = 'button';
        stopBtn.className = 'btn cm-danger';
        stopBtn.disabled = true;
        stopBtn.appendChild(icon('<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>'));   /* 暂停两竖条，与预训练一致 */
        stopBtn.appendChild(api.el('span', null, '停止'));
        var refreshBtn = document.createElement('button');
        refreshBtn.type = 'button';
        refreshBtn.className = 'btn';
        refreshBtn.appendChild(icon('<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'));   /* 刷新图标，与预训练一致 */
        refreshBtn.appendChild(api.el('span', null, '刷新'));
        actions.appendChild(startBtn);
        actions.appendChild(stopBtn);
        actions.appendChild(refreshBtn);
        /* 用户 2026-10-16：动作行与提示挪进右列顶部（参数左/按钮右上/日志右下 = 预训练页同款排布） */

        /* 提示行已删（用户 2026-10-17：右侧内容/格式对齐预训练，不要状态提示行）；
           错误/提示统一追加进「训练输出」，与预训练页 appendLog 同口径 */
        function setNote(text, isErr) {
          if (!livePre || text == null || text === '') { return; }
          livePre.textContent = (livePre.textContent ? livePre.textContent + '\n' : '') + String(text);
          livePre.classList.toggle('tl-err', !!isErr);
        }

        /* 两列：左参数 / 右（状态 + 日志） */
        var grid = api.el('div', 'cm-grid');

        /* ----- 左：参数 ----- */
        var paramCard = api.card('');   /* 「参数」标题移到命名分隔线下（同其他训练页），卡头不再放标题 */
        var phead = paramCard.querySelector('.card-head');
        if (phead) phead.remove();
        var form = api.el('div', 'cm-rows');
        var nameFallback = opt.mode === 'prune' ? 'eproai_prune' : 'eproai_quant';

        /* 模型权重命名 = 其他训练页同款置顶（用户 2026-10-16） */
        var wname = api.weightName({ inputClass: 'cm-input', fallback: nameFallback });
        form.appendChild(wname.wrap);
        wname.input.addEventListener('input', autoSave);
        wname.input.addEventListener('change', autoSave);

        /* 权重行 = 其他训练页同款选择行（用户 2026-10-16）：标签在上，[状态点][选择][文件名][大小]；
           标签+行包块级一层 = LoRA 同款间距（用户 2026-10-16：标题→行 6） */
        var wBlock = api.el('div', 'cm-path-block');
        var wLabel = api.el('div', 'cm-k', '权重');
        api.help(wLabel, '权重', PARAM_HELP['权重']);   /* 点参数名看说明（用户 2026-10-17） */
        wLabel.style.width = 'auto';
        wLabel.style.margin = '0 0 6px';
        wBlock.appendChild(wLabel);
        var wRow = api.el('div', 'cm-path-row');   /* 用户 2026-10-17：整行不可点，只有「选择」按钮 */
        wRow.title = '';
        var wDot = api.el('span', 'cm-dot');
        var wPick = document.createElement('button');
        wPick.type = 'button';
        wPick.className = 'cm-pick';
        wPick.textContent = '选择';
        var wVal = api.el('span', 'cm-path empty', '—');
        var wSize = api.el('span', 'cm-size', '');
        wRow.appendChild(wDot);
        wRow.appendChild(wPick);
        wRow.appendChild(wVal);
        wRow.appendChild(wSize);
        wPick.title = '从仓库选择权重';
        wPick.addEventListener('click', function () {
          api.weightPicker({
            title: '权重',
            current: weightRaw,
            onPick: function (w) {
              weightRaw = w.path;
              wVal.textContent = baseName(w.path);   /* 只显示文件名（用户 2026-10-15） */
              wVal.title = w.path;
              wVal.classList.remove('empty');
              wDot.classList.toggle('ok', !!w.path);
              wSize.textContent = w.size ? fmtSize(w.size) : '';
              setInitTile({ exists: true, size_bytes: w.size, path: w.path });
              if (ovwBox.classList.contains('on')) { applyOvwLock(false); }   /* 覆盖态：命名跟着源权重名 */
              guardOvw();
              autoSave();
            },
          });
        });
        wBlock.appendChild(wRow);

        /* 覆盖初始权重（用户 2026-10-16）：命名框下、同其他训练页；完成压缩即覆盖——新模型名=源权重名，旧权重更名「初始_」前缀入回收站 */
        var ovwRow = api.el('div', 'cm-check');
        var ovwBox = api.el('span', 'cm-box');
        ovwBox.appendChild(icon('<path d="M5 12.6 9.6 17 19 6.6"/>'));
        ovwRow.appendChild(ovwBox);
        ovwRow.appendChild(api.el('span', null, '覆盖初始权重'));
        /* 权重锁（用户 2026-10-20）：源权重被锁 → 不许勾「覆盖初始权重」 */
        var ovwLocked = false;
        /* 未选权重 / 权重被锁 → 整行置灰、点不动（用户 2026-10-23：没选权重时不许勾「覆盖初始权重」） */
        function syncOvwState() {
          var none = !String(weightRaw || '');
          ovwRow.classList.toggle('lock-off', none || ovwLocked);
          ovwRow.title = none ? '未选择权重，不能勾选覆盖初始权重'
            : (ovwLocked ? ('「' + String(weightRaw).split(/[\\/]/).pop() + '」已锁定，不能勾选覆盖初始权重') : '');
          if ((none || ovwLocked) && ovwBox.classList.contains('on')) {
            ovwBox.classList.remove('on');
            applyOvwLock();
          }
        }
        function guardOvw() {
          var p = weightRaw;
          syncOvwState();                                        /* 先按当前选择置灰/恢复 */
          if (!String(p || '')) { ovwLocked = false; return; }   /* 没选权重就不用查锁 */
          api.isLockedPath(p).then(function (locked) {
            if (p !== weightRaw) { return; }
            ovwLocked = !!locked;
            syncOvwState();
          });
        }

        var ovwWasOn = false;
        function applyOvwLock(save) {
          var on = ovwBox.classList.contains('on');
          if (on) {
            var stem = String(weightRaw || '').split(/[\\/]/).pop().replace(/\.pt$/i, '');
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
          if (!String(weightRaw || '')) { return; }   /* 没选权重不许勾（用户 2026-10-23） */
          if (ovwLocked) { return; }        /* 锁上的权重不许覆盖（用户 2026-10-20） */
          ovwBox.classList.toggle('on');
          applyOvwLock();
        };
        form.appendChild(ovwRow);
        form.appendChild(api.el('hr', 'cm-divider'));
        form.appendChild(api.el('h2', 'card-title', '参数'));   /* 「参数」在命名分隔线下、参数行上（同其他训练页） */

        /* 方法 / 方案 段选（.seg + seg-thumb，与蒸馏页同一套） */
        function segRow(label, options, current, onPick) {
          var row = api.el('div', 'cm-row');
          var segLbl = api.el('span', 'cm-k', label);
          api.help(segLbl, label, PARAM_HELP[label]);   /* 点参数名看说明（用户 2026-10-17） */
          row.appendChild(segLbl);
          var seg = api.el('div', 'seg cm-seg');
          options.forEach(function (o) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'seg-opt' + (o[0] === current ? ' on' : '');
            b.textContent = o[1];
            b.setAttribute('aria-checked', o[0] === current ? 'true' : 'false');
            b.addEventListener('click', function () {
              seg.querySelectorAll('.seg-opt').forEach(function (x) {
                x.classList.remove('on');
                x.setAttribute('aria-checked', 'false');
              });
              b.classList.add('on');
              b.setAttribute('aria-checked', 'true');
              api.segThumb(seg, true);
              onPick(o[0]);
              autoSave();
            });
            seg.appendChild(b);
          });
          row.appendChild(seg);
          row.appendChild(ghostDot());      /* 右缘与数值输入框齐平（用户 2026-10-23） */
          form.appendChild(row);
          setTimeout(function () { api.segThumb(seg, false); }, 0);
          return seg;
        }

        if (opt.mode === 'prune') {
          segRow('裁切方法', [['sparsegpt', 'SparseGPT'], ['wanda', 'Wanda']], segState.method,
            function (v) { segState.method = v; });
        } else {
          segRow('量化方案', [['W8A16', 'W8A16'], ['W4A16', 'W4A16'], ['FP8_DYNAMIC', 'FP8_DYNAMIC']],
            segState.scheme, function (v) { segState.scheme = v; });
        }

        /* 数值行（「—」只当占位符，绝不写进 value —— 老坑三层护栏第一层） */
        /* 隐形小球（用户 2026-10-23）：段选 / 药丸这种「没有状态小球」的行，
           在末尾放一个与 .pd-dot 同尺寸同外边距的空位，右缘就和上面的数值输入框完全对齐；
           宽度不靠猜像素，任何窗口宽度都成立。 */
        function ghostDot() {
          var d = api.el('span', 'pd-dot cm-dot-ghost', '');
          d.setAttribute('aria-hidden', 'true');
          return d;
        }
        var dotRows = {};        /* 参数行状态小球（用户 2026-10-18） */
        rows.forEach(function (f) {
          var row = api.el('div', 'cm-row');
          var numLbl = api.el('span', 'cm-k', f[0]);
          api.help(numLbl, f[0], PARAM_HELP[f[0]]);   /* 点参数名看说明（用户 2026-10-17） */
          row.appendChild(numLbl);
          var inp = document.createElement('input');
          inp.type = 'text';
          inp.spellcheck = false;
          inp.className = 'cm-input pd-input';
          inp.placeholder = f[3] || '—';
          inputs[f[1]] = inp;
          inp.addEventListener('input', autoSave);
          inp.addEventListener('change', autoSave);
          row.appendChild(inp);
          var dot = api.el('span', 'pd-dot', '');
          dot.dataset.level = 'empty';
          row.appendChild(dot);
          dotRows[f[1]] = { input: inp, dot: dot };
          form.appendChild(row);
        });
        api.bindParamDots(function () { return opt.mode === 'quant' ? 'quant' : 'prune'; }, dotRows, collectBody);

        /* 校准语料（用户 2026-10-23）：预训练=文本 / 指令微调=对话，可多选，两个都选 = 文本+对话。
           UI 照 LoRA 页「挂载目标层」的药丸按钮：点一下切换选中，至少留一个。 */
        var CALIBS = [['pretrain', '预训练'], ['sft', '指令微调']];
        var calibChips = {};
        function calibList() {
          return CALIBS.filter(function (p) {
            return calibChips[p[0]] && calibChips[p[0]].classList.contains('on');
          }).map(function (p) { return p[0]; });
        }
        function calibSet(v) {
          var list = String(v == null ? '' : v).replace(/，/g, ',').split(',')
            .map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean);
          if (!list.length) list = ['pretrain', 'sft'];      /* 没存过 = 两种都要（与后端默认一致） */
          CALIBS.forEach(function (p) {
            if (!calibChips[p[0]]) return;
            var on = list.indexOf(p[0]) >= 0;
            calibChips[p[0]].classList.toggle('on', on);
            calibChips[p[0]].setAttribute('aria-pressed', on ? 'true' : 'false');
          });
        }
        var calibRow = api.el('div', 'cm-row');
        var calibK = api.el('span', 'cm-k', '校准语料');
        api.help(calibK, '校准语料', PARAM_HELP['校准语料']);
        calibRow.appendChild(calibK);
        var calibBox = api.el('div', 'cm-chips-2');
        CALIBS.forEach(function (p) {
          var b = api.el('button', 'cm-chip', p[1]);
          b.type = 'button';
          b.classList.add('on');
          b.setAttribute('aria-pressed', 'true');
          b.addEventListener('click', function () {
            var on = b.classList.contains('on');
            if (on && calibList().length === 1) return;      /* 至少留一个：都不选就没校准语料了 */
            b.classList.toggle('on', !on);
            b.setAttribute('aria-pressed', on ? 'false' : 'true');
            autoSave();
          });
          calibChips[p[0]] = b;
          calibBox.appendChild(b);
        });
        calibRow.appendChild(calibBox);
        calibRow.appendChild(ghostDot());                          /* 占住状态小球那一格，右缘与输入框对齐 */
        form.appendChild(calibRow);
        form.appendChild(api.el('hr', 'cm-divider'));
        /* 选择权重放最下面（用户 2026-10-16，同其他训练页） */
        form.appendChild(wBlock);

        paramCard.body.appendChild(form);
        grid.appendChild(paramCard);

        /* ----- 右：状态瓷片 + 日志 ----- */
        var rightCol = api.el('div', 'cm-col');
        rightCol.appendChild(actions);

        /* 状态瓷片：格式与其他训练页同款（.stat 大瓷片），只留 训练状态/占用内存
           （用户 2026-10-16：不要输入/输出权重） */
        var stats = api.el('div', 'stats');   /* 四列格子 = 预训练瓷片同尺寸（用户 2026-10-17） */
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
        var statVal = {
          state: tile('训练状态'),
          mem: tile('占用内存'),
          out: tile('输出权重'),
          init: tile('初始权重'),      /* 用户 2026-10-17：裁切/量化右侧补「初始权重」 */
        };
        statVal.state.textContent = '未启动';
        statVal.state.classList.add('idle');   /* 首帧先放终态，避免切页时空白→文字跳一下（用户 2026-10-17） */
        statVal.init.textContent = '未选择';
        statVal.init.classList.add('idle');
        /* 有上一次的终态就整套盖回去（用户 2026-10-17：输出权重切页闪）——首帧即终态，之后 /status 写同样的值不产生变化 */
        (function () {
          var snap = TILE_SNAP[key];
          if (!snap) return;
          ['state', 'mem', 'out', 'init'].forEach(function (k) {
            if (!statVal[k] || !snap[k]) return;
            statVal[k].textContent = snap[k][0];
            statVal[k].className = snap[k][1];
          });
        })();
        function snapTiles() {
          var s = {};
          ['state', 'mem', 'out', 'init'].forEach(function (k) {
            if (statVal[k]) s[k] = [statVal[k].textContent, statVal[k].className];
          });
          TILE_SNAP[key] = s;
        }
        rightCol.appendChild(stats);

        var logCard = api.card('训练输出');   /* 标题/格式对齐预训练（用户 2026-10-17） */
        var logBox = api.el('div', 'cm-logbox');
        var livePre = document.createElement('pre');
        livePre.className = 'cm-live';
        livePre.textContent = '';
        logBox.appendChild(livePre);
        logCard.body.appendChild(logBox);
        rightCol.appendChild(logCard);

        grid.appendChild(rightCol);
        stack.appendChild(grid);

        /* ---------- 表单 ---------- */
        function collectBody() {
          var body = {};
          if (weightRaw && weightRaw !== '—') body.weight = weightRaw;
          if (opt.mode === 'prune') body.method = segState.method;
          else body.scheme = segState.scheme;
          rows.forEach(function (f) {
            var v = (inputs[f[1]] && inputs[f[1]].value || '').trim();
            if (!v || v === '—' || v === '—') return;   /* 占位破折号绝不当值 */
            body[f[1]] = v;
          });
          body.calib_source = calibList().join(',');               /* 校准语料来源（用户 2026-10-23） */
          body.overwrite_init = ovwBox.classList.contains('on');   /* 覆盖初始权重（用户 2026-10-16） */
          var stemName = wname.stem();      /* 命名由用户自己填；没填就不写 out（2026-10-17） */
          if (stemName) { body.out = stemName + '.pt'; }
          return body;
        }

        function renderConfig(d) {
          if (!d || !d.ok) {
            setNote('取数失败：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            return;
          }
          var p = d.params || {};
          weightRaw = String(p.weight || '');
          var woPre = (d.outputs || {}).weight || {};
          if (woPre.exists === false) weightRaw = '';   /* 文件不存在 = 当没选（用户 2026-10-17） */
          setTimeout(guardOvw, 0);                      /* 回填后查一次锁（用户 2026-10-20） */
          if (weightRaw) {
            wVal.textContent = baseName(weightRaw);  /* 只显示文件名（用户 2026-10-15） */
            wVal.title = weightRaw;
            wVal.classList.remove('empty');
          }
          wDot.classList.toggle('ok', !!weightRaw);   /* 状态点随权重存在（用户 2026-10-16） */
          /* 权重命名不回填（用户 2026-10-17：新工程不要默认命名，名字由用户自己填） */
          ovwBox.classList.toggle('on', !!p.overwrite_init);   /* 覆盖初始权重回填（用户 2026-10-16） */
          applyOvwLock(false);
          var wo = (d.outputs || {}).weight || {};
          if (wo.exists) wSize.textContent = fmtSize(wo.size_bytes);
          setInitTile(wo);
          rows.forEach(function (f) {
            var v = p[f[1]];
            if (v != null && v !== '' && String(v) !== '—') inputs[f[1]].value = String(v);
          });
          calibSet(p.calib_source);                                /* 校准语料来源回填（用户 2026-10-23） */
          if (p.method && opt.mode === 'prune') segState.method = String(p.method);
          if (p.scheme && opt.mode === 'quant') segState.scheme = String(p.scheme);
          if (d.env && !d.env.ok) setNote(d.env.note || '压缩环境未就绪', true);
        }

        function renderStatus(d) {
          if (!mounted) return;
          if (d && d.error) { setNote('状态读取失败：' + d.error, true); return; }
          d = d || {};
          var lines = d.lines || [];
          if (lines.length) appendLines(lines);
          if (typeof d.next === 'number') cursor = d.next;
          var running = !!d.running;
          /* 训练结束/被中止的这一刻输出权重才落盘 → 立刻重判一次命名（用户 2026-10-22）；
             不重判的话产物页还显示「√」，要切页回来才变「重复」。 */
          if (renderStatus._wasRun === true && running === false) {
            setTimeout(function () { if (wname) wname.audit(); }, 250);
            setTimeout(function () { if (wname && !(wname.state && wname.state.dup)) wname.audit(); }, 1600);
          }
          renderStatus._wasRun = running;
          startBtn.disabled = running;
          stopBtn.disabled = !running;

          /* 瓷片 */
          statVal.state.textContent = running ? '运行中' :   /* 文案同预训练（用户 2026-10-17） */
            (d.exit_code === 0 ? '已完成' : (d.exit_code == null ? '未启动' : '异常退出 ' + d.exit_code));
          statVal.state.classList.toggle('ok', d.exit_code === 0 && !running);
          statVal.state.classList.toggle('idle', !running && d.exit_code == null);   /* 未启动=灰（用户 2026-10-17） */
          var mem = d.mem_bytes;
          statVal.mem.textContent = tileSize(mem);
          statVal.mem.classList.toggle('ok', !!mem);
          var op = (d.outputs || {}).out || {};
          statVal.out.textContent = op.exists ? tileSize(op.size_bytes) : '未生成';
          statVal.out.classList.toggle('ok', !!op.exists);
          statVal.out.classList.toggle('idle', !op.exists);   /* 未生成=灰（用户 2026-10-17） */
          setInitTile((d.outputs || {}).weight);
          snapTiles();

          if (running) {
            startPolling();
          } else if (!renderStatus._first) {
            stopPolling();
          }
          renderStatus._first = false;
        }

        function appendLines(lines) {
          if (!lines || !lines.length) return;
          if (!liveStarted) { liveStarted = true; streamBuf = []; }
          for (var i = 0; i < lines.length; i++) streamBuf.push(String(lines[i]));
          if (streamBuf.length > MAX_LIVE) streamBuf = streamBuf.slice(-MAX_LIVE);
          livePre.textContent = streamBuf.join('\n');
          livePre.scrollTop = livePre.scrollHeight;
        }

        /* ---------- 动作 ---------- */
        var saveTimer = null;
        function autoSave() {
          if (saveTimer) clearTimeout(saveTimer);
          saveTimer = setTimeout(function () {
            saveTimer = null;
            api.post(base + '/save', collectBody()).then(function (d) {
              if (!d || !d.ok) setNote('保存失败：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            });
          }, 400);
        }

        function doStart() {
          setNote('正在启动压缩…（合并校准语料并起压缩子进程）');
          api.post(base + '/start', collectBody()).then(function (d) {
            if (d && d.ok) {
              cursor = 0;
              streamBuf = [];
              liveStarted = false;
              livePre.textContent = '';
              setNote('已启动 · pid ' + (d.pid == null ? '—' : d.pid));
              pollOnce();
              startPolling();
            } else {
              setNote('启动被拦：' + ((d && (d.error || d.reason)) || '未知错误'), true);
            }
          });
        }

        function doStop() {
          setNote('正在停止压缩…');
          api.post(base + '/stop', {}).then(function (d) {
            setNote((d && d.note) || '已停止');
            pollOnce();
          });
        }

        startBtn.addEventListener('click', function () {
          if (!wname.stem()) {              /* 命名空 → 不启动，光标回命名框（提示已在框右「未命名」，用户 2026-10-17） */
            wname.input.focus();
            return;
          }
          /* 审查：权重命名与仓库权重同名 → 不启动（同其他训练页；覆盖初始权重=有意同名放行） */
          wname.audit().then(function (st) {
            if (st && st.dup && !ovwBox.classList.contains('on')) {
              setNote('权重命名与仓库权重「' + st.name + '」同名，请改名后再开始。', true);
              return;
            }
            /* 参数审查（用户 2026-10-18）：稀疏度/量化方案不常用时先提示 */
            var kind = (opt && opt.mode === 'quant') ? 'quant' : 'prune';
            return api.paramCheck(kind, collectBody(), kind === 'quant' ? '量化' : '模型裁切').then(function (ok) {
              if (ok) doStart();
            });
          });
        });
        stopBtn.addEventListener('click', doStop);
        refreshBtn.addEventListener('click', function () {
          api.get(base + '/config').then(renderConfig);
          pollOnce();
        });

        /* ---------- 轮询 ---------- */
        function startPolling() {
          if (timer || !mounted) return;
          timer = setInterval(pollOnce, 1000);
        }
        function stopPolling() {
          if (timer) { clearInterval(timer); timer = null; }
        }
        function pollOnce() {
          return api.get(base + '/status?since=' + cursor).then(renderStatus);
        }

        root.__cmCleanup = function () {
          mounted = false;
          stopPolling();
        };

        return Promise.all([
          api.get(base + '/config').then(renderConfig),
          pollOnce(),
        ]);
      },

      unmount: function (root) {
        try {
          if (root && typeof root.__cmCleanup === 'function') root.__cmCleanup();
        } catch (e) { /* ignore */ }
      },
    };
  }

  EAIPages.register('train_prune', makePage('train_prune', {
    title: '模型裁切',
    mode: 'prune',
    fields: [
      ['稀疏度', 'sparsity', 'float', '0.5'],
      ['校准样本数', 'calib_samples', 'int', '64'],
      ['最大序列长', 'seq_len', 'int', '128'],
    ],
  }));

  EAIPages.register('train_quant', makePage('train_quant', {
    title: '量化',
    mode: 'quant',
    fields: [
      ['校准样本数', 'calib_samples', 'int', '64'],
      ['最大序列长', 'seq_len', 'int', '128'],
    ],
  }));
})();
