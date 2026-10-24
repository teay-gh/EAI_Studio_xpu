/* ============================================================
   EAI_Studio · pages/train_pipeline.js ——「训练流水线」页（用户 2026-10-17）
   导航键 train_pipeline / 中文名「训练流水线」/ 分组「训练中心」/ 排在预训练之前
   ------------------------------------------------------------
   左：流水线步骤（拖动排序，↑↓ 也能挪，× 删除）+ 列表下方一张与步骤行同尺寸的「＋」卡片，
       点击弹窗设定「训练方式 + 权重命名（+ 可选步数）」——同一种训练方式可加多次（每步命名不同），
       量化只能加一次；
   右：动作行 + 四张瓷片 + 训练输出（与预训练页同款排版/间距）。
   点「开始」后桥端按顺序依次起训练/压缩（复用各页已有启动实现），页面每秒轮询增量事件。

   接口（bridge_api/train_pipeline.py）：
     GET  /api/train_pipeline/config   任务清单 + 已配步骤
     POST /api/train_pipeline/save     {steps:[{task,name,steps},...]}
     POST /api/train_pipeline/start
     POST /api/train_pipeline/stop
     GET  /api/train_pipeline/status?since=N
   ============================================================ */
(function () {
  'use strict';

  var CACHE = null;      /* 首帧直出用：上次 config 响应 */
  var pollTimer = null;

  /* 切/建工程：清掉上一个工程的流水线步骤缓存，避免首帧画出别人的步骤（用户 2026-10-18） */
  try {
    window.__EAI_API.onProjectChange(function () { CACHE = null; });
  } catch (e) {}

  function icon(paths) {
    var span = document.createElement('span');
    span.className = 'pl-ic';
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      paths + '</svg>';
    return span;
  }

  function mmss(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  EAIPages.register('train_pipeline', {
    title: '训练流水线',

    mount: function (root, api) {
      var steps = [];            /* [{task, name, steps}] */
      var tasks = {};            /* key -> {label, once, has_steps, out} */
      var addableKeys = [];      /* 添加弹窗可选的训练方式（LoRA 已移出，用户 2026-10-20） */
      var cursor = 0;
      var mounted = true;
      var running = false;
      var dragFrom = -1;
      var dragTo = -1;
      var dragMoved = false;     /* 刚拖过一次：抑制随后那次 click（用户 2026-10-17） */

      root.appendChild(api.hero('训练流水线'));

      var grid = api.el('div', 'pl-grid');

      /* ---------------- 左：步骤编排 ---------------- */
      var leftCard = api.card('');
      var head = leftCard.querySelector('.card-head');
      if (head) head.remove();
      leftCard.body.appendChild(api.el('h2', 'card-title pl-title', '流水线步骤'));

      var list = api.el('div', 'pl-steps');
      leftCard.body.appendChild(list);

      /* 与步骤行同尺寸的「＋」卡片（图标同仓库页） */
      var addCard = api.el('div', 'pl-addcard');
      var addBtn = document.createElement('button');
      addBtn.type = 'button';
      addBtn.className = 'pl-addbtn';
      addBtn.title = '添加训练步骤';
      addBtn.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">' +
        '<circle cx="12" cy="12" r="10.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
        '<path d="M12 7.6v8.8M7.6 12h8.8" fill="none" stroke="currentColor" stroke-width="1.9" ' +
        'stroke-linecap="round"/></svg>';
      addCard.appendChild(addBtn);
      leftCard.body.appendChild(addCard);

      grid.appendChild(leftCard);

      /* ---------------- 右：动作 + 瓷片 + 输出 ---------------- */
      var rightCol = api.el('div', 'pl-col');

      var actions = api.el('div', 'pl-actions');
      var startBtn = document.createElement('button');
      startBtn.type = 'button';
      startBtn.className = 'btn btn-accent';
      startBtn.appendChild(api.el('span', null, '▶ 开始'));
      var stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'btn pl-danger';
      stopBtn.disabled = true;
      stopBtn.appendChild(icon('<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>'));
      stopBtn.appendChild(api.el('span', null, '停止'));
      var refreshBtn = document.createElement('button');
      refreshBtn.type = 'button';
      refreshBtn.className = 'pl-btn2';
      refreshBtn.appendChild(icon('<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'));
      refreshBtn.appendChild(api.el('span', null, '刷新'));
      actions.appendChild(startBtn);
      actions.appendChild(stopBtn);
      actions.appendChild(refreshBtn);
      rightCol.appendChild(actions);

      var stats = api.el('div', 'stats');
      function tile(label) {
        var st = api.el('div', 'stat');
        st.appendChild(api.el('div', 'stat-label', label));
        var v = api.el('div', 'stat-val', '')   /* 用户 2026-10-17：先空着，别画占位「—」，否则切页会跟状态文字换一下=闪 */;
        st.appendChild(v);
        stats.appendChild(st);
        return v;
      }
      var vState = tile('训练状态');
      var vStep = tile('当前步骤');
      var vDone = tile('已完成');
      var vTime = tile('已用时间');
      vState.textContent = '未启动';
      vState.classList.add('idle');   /* 首帧先放终态，避免切页时空白→文字跳一下（用户 2026-10-17） */
      rightCol.appendChild(stats);

      var liveCard = api.card('训练输出');
      var liveBox = api.el('div', 'pl-logbox');
      var livePre = api.el('pre', 'pl-log');
      liveBox.appendChild(livePre);
      liveCard.body.appendChild(liveBox);
      rightCol.appendChild(liveCard);

      grid.appendChild(rightCol);
      root.appendChild(grid);

      /* ---------------- 保存 / 步骤行 ---------------- */
      function save() {
        return api.post('/api/train_pipeline/save', { steps: steps }).then(function (d) {
          if (d && d.ok === false) log('保存失败：' + (d.error || '未知错误'), true);
          else if (d && d.steps) steps = d.steps;
        });
      }

      function hasImport() {
        return steps.some(function (s) { return s.task === 'import'; });
      }

      function labelOf(key) {
        if (key === 'import') return '导入权重';
        return (tasks[key] && tasks[key].label) || key;
      }

      function moveItem(from, to) {
        if (steps[from] && steps[from].task === 'import') return;      /* 导入权重固定第一步 */
        if (steps[0] && steps[0].task === 'import' && to === 0) to = 1;  /* 别把别的步骤挤到它前面 */
        var it = steps.splice(from, 1)[0];
        steps.splice(to, 0, it);
        renderList();
        save();
      }

      /* 让位预览（用户 2026-10-17 二轮）：拖到第 to 行时，夹在中间的行整体平移一格，
         松手才真正 splice —— 只改 transform、不动 DOM，避免拖动途中重建节点把 drag 打断。 */
      function shiftPreview(to) {
        if (to === dragTo) return;
        var rows = [].slice.call(list.querySelectorAll('.pl-step'));
        if (!rows.length) return;
        dragTo = to;
        var gap = parseFloat(getComputedStyle(list).rowGap) || 8;
        var stepY = rows[0].offsetHeight + gap;
        var from = dragFrom;
        rows.forEach(function (r, j) {
          if (j === from) { r.style.transform = ''; return; }
          var d = 0;
          if (from < to && j > from && j <= to) d = -stepY;
          else if (from > to && j >= to && j < from) d = stepY;
          r.style.transform = d ? 'translateY(' + d + 'px)' : '';
        });
      }

      function resetShift() {
        [].forEach.call(list.querySelectorAll('.pl-step'), function (r) {
          r.style.transform = '';
          r.classList.remove('pl-dragging');
        });
      }

      function renderList() {
        list.textContent = '';
        /* 空态不写小字提示（用户 2026-10-18）：只剩下面的「＋」卡片 */
        steps.forEach(function (step, i) {
          var row = api.el('div', 'pl-step');
          row.draggable = step.task !== 'import';        /* 导入权重固定第一步，不给拖 */
          var isImportRow = step.task === 'import';
          row.title = paramSummary(step) + '\n左键点击改参数 · 拖动可排序';
          row.appendChild(api.el('span', 'pl-idx', String(i + 1)));
          row.appendChild(api.el('span', 'pl-name', labelOf(step.task)));

          var nm = String(step.name || '');
          var isImport = step.task === 'import';
          var importPath = isImport ? String((step.params || {}).weight || '') : '';
          var shown = isImport ? importPath.split(/[\\/]/).pop() : (nm || '未命名');
          var nameEl = api.el('span', 'pl-out' + ((nm || isImport) ? '' : ' pl-out-empty'), shown);
          nameEl.title = isImport ? importPath
            : (nm ? (nm + '.pt') : '该步还没命名，运行时沿用单页配置的名字');
          row.appendChild(nameEl);

          var ops = api.el('div', 'pl-ops');
          var del = document.createElement('button');
          del.type = 'button'; del.className = 'pl-op pl-op-del'; del.title = '删除';
          del.textContent = '×';
          del.onclick = function (e) {
            e.stopPropagation();
            steps.splice(i, 1);
            renderList(); save();
          };
          ops.appendChild(del);
          row.appendChild(ops);

          /* 拖动排序（用户 2026-10-17 二轮）：拖住某一行时，它前后的行动画让位，
             松手才真正落位；↑↓ 按钮已按用户要求去掉，排序只靠拖动。 */
          /* 左键点这一行 → 打开同一弹窗改这一步的参数（用户 2026-10-17）。
             拖过的那一下不算点击（HTML5 拖动结束后浏览器仍可能派发 click）。 */
          row.addEventListener('click', function (e) {
            if (dragMoved) return;
            if (e.target && e.target.closest && e.target.closest('.pl-ops')) return;
            if (running) { log('流水线运行中，先停止再改步骤。', true); return; }
            if (isImportRow) { openImportPicker(i); return; }   /* 点导入权重 = 重新选一个权重 */
            openDialog(i);
          });
          row.addEventListener('dragstart', function (e) {
            dragMoved = true;
            dragFrom = i; dragTo = i;
            row.classList.add('pl-dragging');
            try {
              e.dataTransfer.effectAllowed = 'move';
              e.dataTransfer.setData('text/plain', String(i));
            } catch (err) {}
          });
          row.addEventListener('dragend', function () {
            dragFrom = -1; dragTo = -1; resetShift();
            setTimeout(function () { dragMoved = false; }, 60);   /* 让紧随其后的 click 先被忽略 */
          });
          row.addEventListener('dragover', function (e) {
            if (dragFrom < 0) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            shiftPreview(i);
          });
          row.addEventListener('drop', function (e) {
            if (dragFrom < 0) return;
            e.preventDefault();
            var from = dragFrom, to = i;
            dragFrom = -1; dragTo = -1;
            resetShift();
            if (from !== to) moveItem(from, to);
          });

          list.appendChild(row);
        });
      }

      /* ---------------- 添加弹窗 ---------------- */
      /* 弹窗 = 训练方式 + 权重命名 + 本类任务的**全部参数**（用户 2026-10-17 二轮）。
         字段表由后端 /config 的 tasks[].fields 给（键 = config.train 的键、默认值 = 引擎默认），
         每步的参数存在自己的 step.params 里，起训练时 persist=False，不读也不写各训练页的配置。 */
      function openDialog(editIndex) {
        var editing = (typeof editIndex === 'number' && editIndex >= 0 && editIndex < steps.length)
          ? editIndex : -1;
        var editStep = editing >= 0 ? steps[editing] : null;
        var chosen = editStep ? editStep.task : '';
        var fields = [];          /* [{key,type,el}] 当前渲染的输入控件 */
        var mask = api.el('div', 'pl-mask');
        var box = api.el('div', 'pl-dlg');
        box.appendChild(api.el('div', 'pl-dlg-title', editing >= 0 ? '修改步骤参数' : '添加训练步骤'));
        mask.appendChild(box);
        document.body.appendChild(mask);

        function close() {
          if (mask.parentNode) mask.parentNode.removeChild(mask);
          document.removeEventListener('keydown', onKey);
        }
        function onKey(ev) { if (ev.key === 'Escape') close(); }
        mask.addEventListener('click', function (ev) { if (ev.target === mask) close(); });
        document.addEventListener('keydown', onKey);

        var chips = api.el('div', 'pl-dlg-chips');
        /* 改参数时不显示「训练方式」（用户 2026-10-17）：编辑时方式已锁死，
           换方式=删掉再加，所以整行连同标题都不出现；新增步骤才要选。 */
        if (editing < 0) box.appendChild(chips);

        /* 权重命名（用户 2026-10-17：名字由用户自己填，不预填） */
        var nameRow = api.el('div', 'pl-dlg-row');
        nameRow.appendChild(api.el('span', 'pl-dlg-k', '权重命名'));
        var nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.className = 'pl-dlg-input pl-dlg-name';
        nameInput.placeholder = '输出权重名';
        nameInput.spellcheck = false;
        nameRow.appendChild(nameInput);
        /* 重复判断（用户 2026-10-20：和单独页一致）：框右 红字「未命名」/ 红字「重复」/ 绿点 √ */
        var hint = api.el('div', 'wn-hint');
        var hintDot = api.el('span', 'wn-dot');
        hintDot.setAttribute('aria-hidden', 'true');
        var hintText = api.el('span', 'wn-text');
        hint.appendChild(hintDot);
        hint.appendChild(hintText);
        nameRow.appendChild(hint);
        box.appendChild(nameRow);

        function stemOf() { return String(nameInput.value || '').trim().replace(/\.pt$/i, ''); }
        function setHint(kind, text) {
          hint.className = 'wn-hint' + (kind ? ' ' + kind : '');
          hintText.textContent = (kind === 'ok') ? '' : (text || '');
        }
        setHint('bad', '未命名');
        var nameTimer = null;
        /* 判重：仓库里已有的权重 + 本流水线其它步骤用的同名 */
        function auditName() {
          var stem = stemOf();
          if (!stem) { hint.title = ''; setHint('bad', '未命名'); return; }
          var file = stem + '.pt';
          var mine = steps.filter(function (s, si) {
            if (si === editing) return false;
            return String(s.name || '').trim().toLowerCase() === stem.toLowerCase();
          })[0];
          if (mine) {
            hint.title = '与第 ' + (steps.indexOf(mine) + 1) + ' 步同名';
            setHint('bad', '重复');
            return;
          }
          api.get('/api/repo/summary').then(function (d) {
            var ws = (d && !d.error) ? (d.weights || []) : [];
            for (var i = 0; i < ws.length; i++) {
              var n = String(ws[i].name || '');
              if (n.toLowerCase() === file.toLowerCase()) {
                hint.title = '与仓库权重「' + n + '」同名';
                setHint('bad', '重复');
                return;
              }
            }
            hint.title = '';
            setHint('ok', '√');
          })['catch'](function () { hint.title = ''; setHint('', ''); });
        }
        nameInput.addEventListener('input', function () {
          auditName();
          if (nameTimer) clearTimeout(nameTimer);
          nameTimer = setTimeout(auditName, 300);
        });

        var fieldsHost = api.el('div', 'pl-dlg-fields');
        box.appendChild(fieldsHost);

        var foot = api.el('div', 'pl-dlg-foot');
        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'pl-dlg-cancel';
        cancel.textContent = '取消';
        cancel.onclick = close;
        var okBtn = document.createElement('button');
        okBtn.type = 'button';
        okBtn.className = 'btn btn-accent pl-dlg-ok';
        okBtn.disabled = true;
        okBtn.appendChild(api.el('span', null, editing >= 0 ? '保存' : '添加'));
        foot.appendChild(cancel);
        foot.appendChild(okBtn);
        box.appendChild(foot);

        function syncOk() { okBtn.disabled = !(chosen && nameInput.value.trim()); }

        /* 参数行状态小球（用户 2026-10-20：和单独页一样，输入框右边一个圆点）。
           规则用后端 /api/train/check（同一套阈值），kind 按训练方式映射：
           sft 页在审查接口里叫 full。 */
        var dotRows = {};          /* key → {input, dot} */
        var dotTimer = null;
        var CHECK_KIND = { pretrain: 'pretrain', continue: 'continue', sft: 'full',
                           lora: 'lora', prune: 'prune', quant: 'quant' };

        function collectCheckParams() {
          var p = {};
          fields.forEach(function (f) {
            if (f.type === 'bool') { p[f.key] = !!f.el.checked; return; }
            var v = (f.get ? f.get() : String(f.el.value == null ? '' : f.el.value)).trim();
            if (v !== '') p[f.key] = v;
          });
          return p;
        }

        function paintDots(map) {
          Object.keys(dotRows).forEach(function (k) {
            var it = dotRows[k];
            if (!it || !it.dot) return;
            var v = String(it.input && it.input.value || '').trim();
            var lv = (!v || v === '—') ? 'empty' : (map[k] || 'ok');
            it.dot.dataset.level = lv;
            it.dot.title = lv === 'error' ? '不可用' : lv === 'warn' ? '有风险' : lv === 'ok' ? '正常' : '未填写';
          });
        }

        function refreshDots(key) {
          if (!key) return;
          var kind = CHECK_KIND[key] || key;
          api.post('/api/train/check', { kind: kind, params: collectCheckParams(),
            lang: (window.I18N && window.I18N.lang() === 'en') ? 'en' : 'zh' }).then(function (d) {
            var map = {};
            ((d && d.issues) || []).forEach(function (i) {
              var f = i.field;
              if (!f) return;
              if (map[f] === 'error') return;
              map[f] = (i.level === 'error' || map[f] === 'warn') ? 'error' : 'warn';
            });
            paintDots(map);
          })['catch'](function () {});
        }

        function renderFields(key, preset) {
          fieldsHost.textContent = '';
          fields = [];
          dotRows = {};
          var pre = preset || {};
          var specs = (tasks[key] && tasks[key].fields) || [];
          if (!specs.length) {
            fieldsHost.appendChild(api.el('p', 'pl-dlg-empty', '先选上面的训练方式。'));
            return;
          }
          specs.forEach(function (f) {
            var row = api.el('div', 'pl-dlg-row');
            row.appendChild(api.el('span', 'pl-dlg-k', f.label));
            var el;
            var hasPre = Object.prototype.hasOwnProperty.call(pre, f.key) &&
              pre[f.key] !== '' && pre[f.key] != null;
            if (f.type === 'bool') {
              var lab = api.el('label', 'pl-dlg-check');
              el = document.createElement('input');
              el.type = 'checkbox';
              el.checked = hasPre ? !!pre[f.key] : !!f.default;
              lab.appendChild(el);
              lab.appendChild(api.el('span', null, '开启'));
              row.appendChild(lab);
            } else if (f.type === 'select') {
              /* 段选（用户 2026-10-20「选择方案参考单独页」）：单独页的裁切方法 / 量化方案就是分段控件，
                 流水线里原来是下拉框，口径不一致 */
              var curSel = String(hasPre ? pre[f.key] : (f.default != null ? f.default : ((f.options || [])[0] || '')));
              var seg = api.el('div', 'pl-seg');
              (f.options || []).forEach(function (o) {
                var b = document.createElement('button');
                b.type = 'button';
                b.className = 'pl-seg-opt' + (o === curSel ? ' on' : '');
                b.textContent = o;
                b.onclick = function () {
                  curSel = o;
                  [].forEach.call(seg.children, function (c) { c.classList.toggle('on', c === b); });
                };
                seg.appendChild(b);
              });
              row.appendChild(seg);
              el = { value: curSel };
              fields.push({ key: f.key, type: f.type, el: el, get: function () { return curSel; } });
              fieldsHost.appendChild(row);
              return;
            } else if (f.type === 'chips') {
              /* 挂载目标层（用户 2026-10-20）：照单独 LoRA 页做成可点选的胶囊，不再是逗号串输入框 */
              var cur = String(hasPre ? pre[f.key] : (f.default != null ? f.default : ''))
                .split(',').map(function (s) { return s.trim(); }).filter(Boolean);
              var group = api.el('div', 'pl-chips-field');
              var picks = {};
              (f.options || []).forEach(function (o) {
                var c = document.createElement('button');
                c.type = 'button';
                c.className = 'pl-chip-opt' + (cur.indexOf(o) >= 0 ? ' on' : '');
                c.textContent = o;
                c.onclick = function () {
                  picks[o] = !picks[o];
                  c.classList.toggle('on', !!picks[o]);
                  if (dotTimer) clearTimeout(dotTimer);
                  dotTimer = setTimeout(function () { refreshDots(chosen); }, 250);
                };
                picks[o] = cur.indexOf(o) >= 0;
                group.appendChild(c);
              });
              el = { value: '' };
              row.appendChild(group);
              fields.push({ key: f.key, type: f.type, el: el,
                get: function () { return (f.options || []).filter(function (o) { return picks[o]; }).join(','); } });
              fieldsHost.appendChild(row);
              return;
            } else {
              el = document.createElement('input');
              el.type = 'text';
              el.className = 'pl-dlg-input';
              el.spellcheck = false;
              el.value = hasPre ? String(pre[f.key]) : (f.default != null ? String(f.default) : '');
              el.placeholder = (f.type === 'float' ? '小数' : '整数');
              row.appendChild(el);
              /* 输入框右边的小球（数字类字段才有；下拉/勾选不加） */
              var dot = api.el('span', 'pd-dot pd-dot-dlg', '');
              dot.dataset.level = 'empty';
              row.appendChild(dot);
              dotRows[f.key] = { input: el, dot: dot };
            }
            fields.push({ key: f.key, type: f.type, el: el });
            fieldsHost.appendChild(row);
          });
          /* 权重行整行不要了（用户 2026-10-20 二次确认）：输入权重一律取上一步的产物，界面上不出现 */
          /* 小球联动：改哪个字段都重审一次（350ms 防抖） */
          Object.keys(dotRows).forEach(function (k) {
            var inp = dotRows[k].input;
            inp.addEventListener('input', function () {
              paintDots({});
              if (dotTimer) clearTimeout(dotTimer);
              dotTimer = setTimeout(function () { refreshDots(chosen); }, 350);
            });
            inp.addEventListener('change', function () { refreshDots(chosen); });
          });
          setTimeout(function () { refreshDots(chosen); }, 0);
        }

        addableKeys.forEach(function (key) {
          var t = tasks[key];
          var used = steps.some(function (s, si) {
            return s.task === key && si !== editing;     /* 编辑时这一步自己不算「已用」 */
          });
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'pl-chip';
          b.textContent = t.label;
          b.disabled = !!(t.once && used);          /* 量化只能加一次（用户 2026-10-17） */
          if (t.once && used) b.title = '量化只能添加一次';
          /* 已经导入权重了就不给加预训练（用户 2026-10-20）：预训练从 0 开始，
             导入的权重根本用不上，应该用「继续预训练」 */
          if (key === 'pretrain' && hasImport() && !(editing >= 0 && editStep.task === 'pretrain')) {
            b.disabled = true;
            b.title = '已导入权重，请用「继续预训练」（预训练会从 0 开始）';
          }
          /* 改参数时不换训练方式（换类型 = 删掉再加一步），只锁住当前那一项 */
          if (editing >= 0 && key !== editStep.task) {
            b.disabled = true;
            b.title = '改参数时不能换训练方式';
          }
          b.onclick = function () {
            /* 再点已选中的那一项不清空表单（用户 2026-10-17：弹窗尺寸固定后，误点会把字段全清空） */
            chosen = key;
            [].forEach.call(chips.children, function (c) { c.classList.toggle('on', c === b); });
            renderFields(key);
            syncOk();
          };
          chips.appendChild(b);
        });

        nameInput.addEventListener('input', syncOk);
        if (editing >= 0) {
          /* 编辑：亮出当前训练方式 + 预填名字与这一步已存的参数 */
          [].forEach.call(chips.children, function (c, ci) {
            c.classList.toggle('on', addableKeys[ci] === chosen);
          });
          nameInput.value = editStep.name || '';
          renderFields(chosen, editStep.params || {});
          /* 预填后立刻判重（用户 2026-10-20：「重新点进训练选项…现在点进去直接显示未命名」）：
             这一步已经训出过权重时，进来就该看到「重复」，而不是「未命名」。 */
          auditName();
        } else {
          renderFields('');        /* 未选方式时先给一句提示，别留大片空白 */
        }
        syncOk();
        nameInput.focus();

        okBtn.onclick = function () {
          var nm = nameInput.value.trim().replace(/\.pt$/i, '');
          if (!chosen || !nm) return;
          var params = {};
          fields.forEach(function (f) {
            if (f.type === 'bool') { params[f.key] = !!f.el.checked; return; }
            var v = (f.get ? f.get() : String(f.el.value == null ? '' : f.el.value)).trim();
            if (v === '') return;
            params[f.key] = v;
          });
          /* 参数审查（用户 2026-10-20）：和单独页一样，填了有问题的参数先弹窗，
             点「仍然继续」才收下（error 级别不给继续） */
          var kind = { sft: 'full' }[chosen] || chosen;
          api.paramCheck(kind, params, labelOf(chosen), { verb: '添加' }).then(function (ok) {
            if (!ok) return;
            if (editing >= 0) steps[editing] = { task: editStep.task, name: nm, params: params };
            else steps.push({ task: chosen, name: nm, params: params });
            close();
            renderList();
            save();
          });
        };
      }

      /* 行内悬浮提示：这一步的实际参数（缺的键按默认值显示） */
      function paramSummary(step) {
        if (step.task === 'import') return '导入权重\n' + String((step.params || {}).weight || '');
        var specs = (tasks[step.task] && tasks[step.task].fields) || [];
        var p = step.params || {};
        var parts = specs.map(function (f) {
          var v = (p[f.key] !== undefined && p[f.key] !== null && p[f.key] !== '') ? p[f.key] : f.default;
          if (f.type === 'bool') return f.label + (v ? ' 开' : ' 关');
          return f.label + ' ' + v;
        });
        return (LABEL_TEXT(step.task) + (step.name ? '（' + step.name + '.pt）' : '')) +
          (parts.length ? '\n' + parts.join(' · ') : '');
      }

      function LABEL_TEXT(key) { return (tasks[key] && tasks[key].label) || key; }

      /* ---------------- 导入权重（第一步） ---------------- */
      /* 用户 2026-10-20：流水线第一步可以是「导入权重」，用仓库权重当起点，不必从 0 训。
         弹窗直接复用仓库权重选择器（卡片 + 搜索），右上角多一个「不导入」= 就从 0 开始。 */
      function openImportPicker(editIndex) {
        var editing = (typeof editIndex === 'number' && editIndex >= 0) ? editIndex : -1;
        var cur = editing >= 0 ? String((steps[editing].params || {}).weight || '') : '';
        api.weightPicker({
          title: '导入权重',
          current: cur,
          extra: { text: '不导入', title: '不导入权重，从 0 开始训练' },
          onPick: function (w) {
            if (!w || !w.path) return;
            var step = { task: 'import', name: '', params: { weight: String(w.path) } };
            if (editing >= 0) steps[editing] = step;
            else {
              if (steps[0] && steps[0].task === 'import') steps[0] = step;
              else steps.unshift(step);
            }
            renderList();
            save();
          }
        });
      }

      addBtn.onclick = function () {
        if (running) { log('流水线运行中，先停止再改步骤。', true); return; }
        /* 空流水线：第一步是「导入权重」（用户 2026-10-20）——弹窗里右上角有不导入 */
        if (!steps.length) { openImportPicker(-1); return; }
        openDialog();
      };

      /* ---------------- 日志 / 瓷片 ---------------- */
      function log(text, isErr) {
        if (!text) return;
        livePre.textContent = (livePre.textContent ? livePre.textContent + '\n' : '') + String(text);
        livePre.classList.toggle('pl-err', !!isErr);
        liveBox.scrollTop = liveBox.scrollHeight;
      }

      function setState(text, cls) {
        vState.textContent = text;
        vState.classList.remove('ok', 'warn', 'danger', 'idle');
        if (cls) vState.classList.add(cls);
      }

      function applyStatus(d) {
        d = d || {};
        var st = d.stats || {};
        running = !!d.running;
        startBtn.disabled = running;
        stopBtn.disabled = !running;
        addCard.classList.toggle('pl-locked', running);

        var state = st.state || (d.exists ? (running ? 'running' : 'idle') : 'idle');
        var map = {
          running: ['运行中', 'ok'], done: ['已完成', 'ok'],
          failed: ['失败', 'danger'], stopped: ['已停止', 'warn'], idle: ['未启动', 'idle'],
        };
        var mm = map[state] || map.idle;
        setState(mm[0], mm[1]);

        var total = st.total || steps.length || 0;
        var cur = st.current || 0;
        var key = st.step || '';
        var finished = (state === 'running') ? Math.max(0, cur - 1) : cur;
        vStep.textContent = (state === 'running' && key) ? labelOf(key) : '—';
        vStep.classList.toggle('ok', state === 'running' && !!key);
        vDone.textContent = total ? (finished + '/' + total) : '—';
        vDone.classList.toggle('ok', total > 0 && finished >= total);
        vTime.textContent = d.exists ? mmss(d.elapsed) : '—';

        (d.events || []).forEach(function (ev) {
          var p = ev.payload || {};
          if (p.text) log(p.text, ev.kind === 'error');
        });
        if (typeof d.next === 'number') cursor = d.next;
        if (d.error) log('流水线异常：' + d.error, true);
      }

      /* ---------------- 轮询 ---------------- */
      function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
      function startPolling() {
        if (pollTimer) return;
        pollTimer = setInterval(function () {
          api.get('/api/train_pipeline/status?since=' + cursor).then(function (d) {
            if (!mounted) return;
            applyStatus(d);
            if (d && !d.running) {
              stopPolling();
              loadConfig();     /* 跑完刷新一次步骤 */
            }
          })['catch'](function () {});
        }, 1000);
      }

      /* ---------------- 取数 ---------------- */
      function renderConfig(d) {
        if (!d || d.ok === false) {
          log('读取失败：' + ((d && d.error) || '未知错误'), true);
          return;
        }
        tasks = {};
        (d.tasks || []).forEach(function (t) { tasks[t.key] = t; });
        /* legacy_tasks：已从可选项里移除、但存量步骤还要能显示/编辑/运行的训练方式
           （LoRA 微调，用户 2026-10-20「移出流水线可选项」）。加进 tasks 只为字段表与
           列表过滤；添加弹窗的芯片只从 addableKeys 出。 */
        addableKeys = Object.keys(tasks);
        (d.legacy_tasks || []).forEach(function (t) {
          if (!tasks[t.key]) tasks[t.key] = t;
        });
        /* import 步不在 tasks 里（它不是一个训练方式），以前这里会把它过滤掉 →
           切页回来「导入权重」这一步就没了，只剩空列表（用户 2026-10-20） */
        steps = (d.steps || []).filter(function (s) {
          return s && (s.task === 'import' || tasks[s.task]);
        });
        renderList();
      }

      function loadConfig() {
        return api.get('/api/train_pipeline/config').then(function (d) {
          CACHE = d;
          renderConfig(d);
        });
      }

      refreshBtn.onclick = function () { loadConfig(); };

      /* 起跑前把每一步的命名审一遍（用户 2026-10-20：「点击开始训练也要审查一遍命名，
         有可能用户忘了进去改」）：未命名 / 步骤间同名 / 与仓库权重同名，任一存在就拦住。
         每步的判定口径与弹窗里的 auditName 同一套。 */
      function auditStepNames() {
        var problems = [];
        var seen = {};
        var repoNames = null;
        var chain = Promise.resolve();
        steps.forEach(function (s, si) {
          if (s.task === 'import') return;
          var nm = String(s.name || '').trim().replace(/\.pt$/i, '');
          var tag = '第 ' + (si + 1) + ' 步（' + labelOf(s.task) + '）';
          if (!nm) {
            problems.push(tag + '：还没命名');
            return;
          }
          if (seen[nm.toLowerCase()]) {
            problems.push(tag + '：与第 ' + seen[nm.toLowerCase()] + ' 步同名「' + nm + '」');
            return;
          }
          seen[nm.toLowerCase()] = si + 1;
          chain = chain.then(function () {
            if (repoNames) return;
            return api.get('/api/repo/summary').then(function (d) {
              repoNames = (d && !d.error) ? (d.weights || []) : [];
            })['catch'](function () { repoNames = []; });
          }).then(function () {
            var file = (nm + '.pt').toLowerCase();
            (repoNames || []).forEach(function (w) {
              if (String(w.name || '').toLowerCase() === file) {
                problems.push(tag + '：与仓库权重「' + w.name + '」同名');
              }
            });
          });
        });
        return chain.then(function () { return problems; });
      }

      startBtn.onclick = function () {
        if (!steps.length) {
          log('流水线还没有步骤：请先添加。', true);
          return;
        }
        auditStepNames().then(function (problems) {
          if (problems.length) {
            api.confirm({
              title: '命名检查',
              message: '这些步骤的输出命名需要先改：',
              detail: problems.join('\n'),
              buttons: ['知道了'],
            });
            return false;
          }
          return true;
        }).then(function (ok) {
        if (!ok) return;
        /* 参数审查（用户 2026-10-18 / 2026-10-20）：每一步的参数按它自己的训练方式审一遍，
           import 步没有参数跳过；有 error 直接拦住，warn 让用户确认 */
        var chain = Promise.resolve(true);
        steps.forEach(function (s) {
          if (s.task === 'import') return;
          chain = chain.then(function (ok) {
            if (!ok) return false;
            var kind = { sft: 'full' }[s.task] || s.task;
            return api.paramCheck(kind, s.params || {}, labelOf(s.task));
          });
        });
        chain.then(function (ok) {
          if (!ok) return;
          api.post('/api/train_pipeline/start', {}).then(function (d) {
            if (!d || d.ok === false) {
              log('启动失败：' + ((d && d.error) || '未知错误'), true);
              return;
            }
            cursor = 0;
            startBtn.disabled = true;
            stopBtn.disabled = false;
            setState('运行中', 'ok');
            startPolling();
          });
        });
        });
      };

      stopBtn.onclick = function () {
        api.post('/api/train_pipeline/stop', {}).then(function (d) {
          if (d && d.ok === false) log('停止失败：' + (d.error || '未知错误'), true);
        });
      };

      if (CACHE) { try { renderConfig(CACHE); } catch (e) {} }
      var init = loadConfig().then(function () {
        return api.get('/api/train_pipeline/status?since=0');
      }).then(function (d) {
        if (!mounted) return;
        cursor = 0;
        applyStatus(d);
        if (d && d.exists && d.running) startPolling();
      });
      return init;
    },

    unmount: function () {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    },
  });
})();
