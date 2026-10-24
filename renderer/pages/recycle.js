/* ============================================================
   EAI_Studio · pages/recycle.js ——「回收站」页（全局，2026-10-14）
   2026-10-15 拆两页（用户：「回收站分页，分为数据集页和权重页」）：
     recycle_dataset（数据集）= 样本（文章/响应行级回收）+ 数据集整文件
     recycle_weight （权重）  = 模型权重整文件（kind=weights）
   导航键 recycle_dataset / recycle_weight / 分组「回收站」
   ------------------------------------------------------------
   站里收三类东西，按页分列：
     样本     文章 / 响应两板块的行级回收站（删除所选进来，恢复=原字节写回来源文件）
     数据集   数据集文件（kind=dataset，整文件进 <工程>/.trash/files/）
     模型权重 checkpoints 下的 *.pt（kind=weights，整文件进站）

   真实数据全部来自 bridge_api/recycle.py：
     GET  /api/recycle/list                    全部条目（samples + files）
     POST /api/recycle/restore  {targets}      恢复所选
     POST /api/recycle/purge    {targets}      彻底删除所选（永久）/ 本页「清空回收站」
     POST /api/recycle/clear    {}             清空全站（后台保留；页面清空按本页条目 purge）
     POST /api/recycle/trash_file {path,kind}  整文件入站（数据集 / 模型权重）
   ============================================================ */
(function () {
  'use strict';

  var KIND_LABEL = {
    sample: '样本',
    article: '文章样本',
    response: '响应样本',
    rejected: '隔离样本',
    dataset: '数据集',
    weights: '模型权重',
    folder: '文件夹',
  };
  /* 文件夹条目排在文件后面（用户 2026-10-20：删掉的文件夹以文件夹形式显示） */
  var KIND_ORDER = { sample: 0, rejected: 1, article: 0, response: 1, dataset: 2, weights: 3, folder: 4 };

  function rowId(it) { return (it.board || '') + '|' + it.key; }

  /* 类型标签：行级样本按板块出「文章样本/响应样本」，隔离样本单列（用户 2026-10-16） */
  function kindLabel(it) {
    if (it.kind === 'sample') return KIND_LABEL[it.board] || '样本';
    return KIND_LABEL[it.kind] || it.kind || '—';
  }

  /* 统一 target 形状：样本/隔离样本走行级，其余（数据集/权重）走整文件 */
  function targetOf(it) {
    /* 文件夹条目整组恢复 / 整组彻底删除（后端按 key=folder:<名> 展开成员，用户 2026-10-20） */
    if (it.kind === 'folder') return { kind: 'folder', board: '', key: it.key };
    return { kind: (it.kind === 'sample' || it.kind === 'rejected') ? it.kind : 'file',
             board: it.board || '', key: it.key };
  }

  /* 一页一套：scope='dataset'（样本 + 数据集文件）| 'weight'（模型权重文件） */
  function makePage(scope) {
    var isWeight = scope === 'weight';

    return {
      title: isWeight ? '权重' : '数据集',

      mock: {
        '/api/recycle/list': { ok: true, samples: [], files: [], total: 0 },
        '/api/recycle/restore': { ok: true, restored: 0 },
        '/api/recycle/purge': { ok: true, purged: 0 },
        '/api/recycle/clear': { ok: true, purged: 0 },
        '/api/recycle/trash_file': { ok: true, item: {} },
        '/api/recycle/content': { ok: true, text: '' },
      },

      mount: function (root, api) {
          /* 单条数据弹窗的问答卡样式在 repo.css（同数据管理页，2026-10-21），按需挂一份 */
          if (!document.querySelector('link[data-repo-css]')) {
            var rcss = document.createElement('link');
            rcss.rel = 'stylesheet';
            rcss.href = 'pages/repo.css';
            rcss.setAttribute('data-repo-css', '1');
            document.head.appendChild(rcss);
          }
          /* view：数据集页横条（dataset|record）；open/openItems：正在浏览哪个删掉的文件夹（用户 2026-10-20） */
        var DATA = { items: [], sel: {}, view: 'dataset', open: '', openItems: [] };

        /* ---------- 小工具 ---------- */
        function button(label, cls, onClick) {
          var b = api.el('button', cls, label);
          b.type = 'button';
          b.addEventListener('click', onClick);
          return b;
        }

        function icon(paths) {
          var s = document.createElement('span');
          s.className = 'ds-ic';
          s.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
            'aria-hidden="true">' + paths + '</svg>';
          return s;
        }
        /* 与数据集页一致的自绘勾选框（.ds-pick + 对勾） */
        function pickBox(on) {
          var s = api.el('span', 'ds-pick' + (on ? ' on' : ''));
          s.setAttribute('role', 'checkbox');
          s.setAttribute('aria-checked', on ? 'true' : 'false');
          s.appendChild(icon('<path d="M5 12.6 9.6 17 19 6.6"/>'));
          return s;
        }

        /* ---------- 骨架 ---------- */
        root.appendChild(api.hero(isWeight ? '权重' : '数据集'));

        /* 数据集页上方横条：切换「数据集 / 单条数据」（2026-10-15）：
           数据集 = 整文件卡片排布；单条数据 = 行级样本表格（维持原排布） */
        var seg = null;
        if (!isWeight) {
          var segWrap = api.el('div', 'rc-segwrap');
          seg = api.el('div', 'seg rc-seg');
          seg.setAttribute('role', 'tablist');
          [['dataset', '数据集'], ['record', '单条数据']].forEach(function (t) {
            var b = api.el('button', 'seg-opt', t[1]);
            b.type = 'button';
            b.dataset.value = t[0];
            b.setAttribute('role', 'tab');
            b.addEventListener('click', function () { setView(t[0]); });
            seg.appendChild(b);
          });
          segWrap.appendChild(seg);
          root.appendChild(segWrap);
        }
        function setView(v) {
          DATA.view = v;
          if (seg) {
            Array.prototype.forEach.call(seg.querySelectorAll('.seg-opt'), function (b) {
              var on = b.dataset.value === v;
              b.classList.toggle('on', on);
              b.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            api.segThumb(seg, true);
          }
          renderRows();
        }

        var stack = api.el('div', 'stack');
        root.appendChild(stack);

        /* 不再套一层白卡（用户 2026-10-20：回收站卡片 UI 参考仓库，卡片底下不要白框）；
           工具行和卡片网格直接落在页面背景上 */
        var card = api.el('div', 'rc-box');
        card.body = api.el('div', 'rc-box-body');
        card.appendChild(card.body);

        var toolbar = api.el('div', 'rc-toolbar');
        var status = api.el('p', 'rc-status');
        function say(msg, isErr) {
          /* 成功回执不进这行小字（用户 2026-10-21：「不要这个清空回收站的小字」）：
             只显示失败/错误提示，成功静默（列表自己刷新即看得出）。 */
          if (!isErr) { status.textContent = ''; status.classList.remove('err'); return; }
          status.textContent = msg || '';
          status.classList.toggle('err', true);
        }

        /* 文件夹内：左边文件夹名、返回按钮钉在「刷新」左边（用户 2026-10-20） */
        var tbLead = api.el('div', 'rc-tb-lead');
        tbLead.hidden = true;
        toolbar.appendChild(tbLead);
        var backBtn = button('返回', 'rc-btn rc-tb-back', leaveFolder);
        backBtn.hidden = true;
        toolbar.appendChild(backBtn);
        toolbar.appendChild(button('刷新', 'rc-btn', function () { load(); }));
        toolbar.appendChild(button('恢复所选', 'rc-btn', onRestore));
        toolbar.appendChild(button('彻底删除所选', 'rc-btn-danger', onPurge));
        /* 类型区分由上方横条承担（数据集/单条数据），原「全部类型」下拉撤销 */

        var clearWrap = api.el('span', 'rc-right');
        clearWrap.appendChild(button('清空回收站', 'rc-btn-danger', onClearAll));
        toolbar.appendChild(clearWrap);
        card.body.appendChild(toolbar);

        var tableWrap = api.el('div', 'rc-tablewrap');
        var cardsWrap = api.el('div', 'rc-cards');
        card.body.appendChild(tableWrap);
        card.body.appendChild(cardsWrap);

        stack.appendChild(card);
        stack.appendChild(status);

        /* ---------- 表格 ---------- */
        function inScope(it) {
          if (it.kind === 'folder') {           /* 文件夹条目按里面装的东西分流（mixed=两种都有） */
            var fk = String(it.file_kind || '');
            return isWeight ? (fk === 'weights' || fk === 'mixed')
                            : (fk === 'dataset' || fk === 'mixed');
          }
          return isWeight ? it.kind === 'weights'
                          : (it.kind === 'sample' || it.kind === 'dataset' || it.kind === 'rejected');
        }
        function filtered() {
          /* 进了删掉的文件夹：列它自己的成员（只读浏览，用户 2026-10-20） */
          if (DATA.open) return DATA.openItems.slice();
          return DATA.items.filter(function (it) {
            if (!inScope(it)) return false;
            if (isWeight) return true;
            /* 数据集页按横条分流：数据集=整文件卡片；单条数据=行级样本（删除回收 + 隔离样本） */
            return DATA.view === 'dataset'
              ? (it.kind === 'dataset' || it.kind === 'folder')   /* 文件夹卡也在数据集视图里（2026-10-20） */
              : (it.kind === 'sample' || it.kind === 'rejected');
          });
        }

        function fmtSize(n) {
          n = Number(n) || 0;
          if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
          if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
          if (n >= 1024) return Math.round(n / 1024) + ' KB';
          return n + ' B';
        }
        function kv(k, v) {
          var r = api.el('div', 'rc-kv');
          r.appendChild(api.el('span', 'k', k));
          r.appendChild(api.el('span', 'v', String(v)));
          return r;
        }

        /* ---------- 右键菜单（数据集/权重卡，用户 2026-10-16） ---------- */
        var menuEl = null;
        function onDocDown(ev) { if (menuEl && !menuEl.contains(ev.target)) closeMenu(); }
        function onEsc(ev) { if (ev.key === 'Escape') closeMenu(); }
        function closeMenu() {
          if (menuEl) { menuEl.remove(); menuEl = null; }
          document.removeEventListener('mousedown', onDocDown, true);
          document.removeEventListener('keydown', onEsc, true);
        }
        function showMenu(x, y, items) {
          closeMenu();
          var m = api.el('div', 'rc-menu');
          items.forEach(function (it) {
            if (it === '-') { m.appendChild(api.el('div', 'rc-menu-sep')); return; }
            var mi = api.el('button', 'rc-menu-item' + (it.danger ? ' danger' : ''));
            mi.type = 'button';
            mi.textContent = it.label;
            mi.addEventListener('click', function () { closeMenu(); it.run(); });
            m.appendChild(mi);
          });
          m.style.left = Math.min(x, window.innerWidth - 170) + 'px';
          m.style.top = Math.min(y, window.innerHeight - 130) + 'px';
          document.body.appendChild(m);
          menuEl = m;
          document.addEventListener('mousedown', onDocDown, true);
          document.addEventListener('keydown', onEsc, true);
        }

        /* ---------- 查看内容（右键弹窗，2026-10-16） ---------- */
        function openModal(title, text) {
          /* title/text 都收字符串或节点（2026-10-21 单条数据弹窗：来源=渐隐滚动标题、问答卡正文） */
          var mask = api.el('div', 'rc-modal');
          var box = api.el('div', 'rc-modal-box');
          var head = api.el('div', 'rc-modal-head');
          head.appendChild(typeof title === 'string' ? api.el('span', 'rc-modal-title', title) : title);
          head.appendChild(button('关闭', 'rc-btn', function () { mask.remove(); }));
          box.appendChild(head);
          if (typeof text === 'string') box.appendChild(api.el('pre', 'rc-pre', text));
          else if (text) box.appendChild(text);
          mask.appendChild(box);
          mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) mask.remove(); });
          document.body.appendChild(mask);
        }

        function openContent(it) {
          if (it.kind === 'folder') {          /* 文件夹条目没有单一原文，列出成员文件名（2026-10-20） */
            var names = it.names || [];
            openModal(String(it.name || '') + ' · ' + names.length + ' 个文件',
              names.length ? names.join('\n') : '（文件夹里没有文件）');
            return;
          }
          api.post('/api/recycle/content', { kind: it.kind, key: it.key, board: it.board || '' }).then(function (d) {
            if (!d || d.error) { say('查看失败：' + ((d && d.error) || '未知错误'), true); return; }
            var text = d.binary
              ? ('二进制文件（不显示原文）\n大小：' + fmtSize(d.size_bytes) + '\nHEX 头：\n' +
                 String(d.hex || '').replace(/(.{32})/g, '$1\n').trim())
              : String(d.text || '');
            openModal(String(it.name || it.src || ''), text);
          }).catch(function (e) { say('查看失败：' + e, true); });
        }

        /* 应用内自绘确认框（用户 2026-10-20）：彻底删除等二次确认不再用系统原生框，
           外壳与「api.prompt」同一套（.ask-mask / .ask-box），主按钮是危险色。 */
        /* 拼接文案（用户 2026-10-20）：词典只按整段文字匹配，带数字的拼接串得自己给两套 */
        function L(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }

        function askConfirm(opt) {
          var o = opt || {};
          return new Promise(function (resolve) {
            var mask = api.el('div', 'ask-mask');
            var box = api.el('div', 'ask-box rc-ask-box');
            box.appendChild(api.el('div', 'ask-title', o.title || L('确认', 'Confirm')));
            box.appendChild(api.el('div', 'ask-msg', o.message || L('确定执行该操作？', 'Run this action?')));
            if (o.detail) box.appendChild(api.el('div', 'ask-sub', o.detail));
            var btns = api.el('div', 'ask-btns');
            var cancel = api.el('button', 'btn ask-btn', o.cancelText || '取消');
            var ok = api.el('button', 'btn ask-btn rc-ask-danger', o.okText || '确定');
            cancel.type = 'button';
            ok.type = 'button';
            btns.appendChild(cancel);
            btns.appendChild(ok);
            box.appendChild(btns);
            mask.appendChild(box);
            var done = false;
            function close(v) {
              if (done) return;
              done = true;
              if (mask.parentNode) mask.parentNode.removeChild(mask);
              document.removeEventListener('keydown', onKey, true);
              resolve(v);
            }
            function onKey(e) {
              if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
              else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(true); }
            }
            cancel.addEventListener('click', function () { close(false); });
            ok.addEventListener('click', function () { close(true); });
            mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) close(false); });
            document.addEventListener('keydown', onKey, true);
            document.body.appendChild(mask);
            setTimeout(function () { try { ok.focus(); } catch (e) {} }, 0);
          });
        }

        function actOne(it, mode) {
          var targets = [targetOf(it)];
          function run() {
            var url = mode === 'restore' ? '/api/recycle/restore' : '/api/recycle/purge';
            api.post(url, { targets: targets }).then(function (d) {
              if (!d || d.error) {
                say((mode === 'restore' ? '恢复' : '删除') + '失败：' + ((d && d.error) || '未知错误'), true);
              } else {
                /* 成功回执小字去掉（用户 2026-10-21） */
              }
              DATA.sel = {};
              load();
            }).catch(function (e) { say('操作失败：' + e, true); });
          }
          if (mode === 'restore') { run(); return; }
          askConfirm({
            title: L('彻底删除', 'Delete permanently'),
            message: L('确定彻底删除这条？', 'Delete this item permanently?'),
            detail: L('删除后无法恢复。', 'This cannot be undone.'),
            okText: L('彻底删除', 'Delete'),
          }).then(function (ok) { if (ok) run(); });
        }

        /* 右键菜单（用户 2026-10-20）：一律 恢复 / 彻底删除；
           数据集多一项「查看内容」、权重多一项只读的「查看详细信息」、文件夹里多一项「返回」 */
        function cardMenu(it) {
          var items = [];
          /* 查看类操作与下面的恢复/删除之间加分割线（用户 2026-10-20） */
          if (DATA.open) { items.push({ label: '返回', run: leaveFolder }); items.push('-'); }
          if (it.kind === 'weights') {
            items.push({ label: '查看详细信息', run: function () { detailModal(it); } });
            items.push('-');
          } else if (it.kind !== 'folder') {
            items.push({ label: '查看内容', run: function () { openContent(it); } });
            items.push('-');
          }
          items.push({ label: '恢复', run: function () { actOne(it, 'restore'); } });
          items.push({ label: '彻底删除', danger: true, run: function () { actOne(it, 'purge'); } });
          return items;
        }

        /* 进入 / 离开「删掉的文件夹」（只读浏览成员，用户 2026-10-20） */
        function enterFolder(it) {
          DATA.open = String(it.name || '');
          DATA.openItems = (it.items || []).map(function (x) {
            return Object.assign({}, x, { board: '' });
          });
          DATA.sel = {};
          renderRows();
        }
        function leaveFolder() {
          DATA.open = '';
          DATA.openItems = [];
          DATA.sel = {};
          renderRows();
        }

        /* 工具行上的「文件夹名 + 返回」跟着当前浏览位置显示/隐藏 */
        function syncCrumb() {
          if (tbLead) { tbLead.textContent = DATA.open ? DATA.open : ''; tbLead.hidden = !DATA.open; }
          if (backBtn) backBtn.hidden = !DATA.open;
        }

        /* ---------- 卡片（用户 2026-10-20 第四轮：样式照抄仓库，只留回收站要的信息） ---------- */
        /* 训练方式标签分色，与仓库同一套口径 */
        var TAG_CLS = {
          '预训练': 'rc-tag-pretrain', '继续预训练': 'rc-tag-continue',
          '指令微调': 'rc-tag-sft', 'LoRA 微调': 'rc-tag-lora',
          'LoRA 合并': 'rc-tag-merge',
          '模型裁切': 'rc-tag-prune', '量化': 'rc-tag-quant',
        };
        function methodTag(label) {
          var text = String(label || '其他');
          return api.el('span', 'rc-tag ' + (TAG_CLS[text] || ''), text);
        }
        /* 三条信息：大小 / 删除时间 / 来源，等间隙撑满卡片下半区 */
        function rows3(it) {
          var box = api.el('div', 'rc-rows');
          box.appendChild(kv('大小', fmtSize(it.size_bytes)));
          box.appendChild(kv('删除时间', it.deleted_at || '—'));
          box.appendChild(kv('来源', it.orig_path || '—'));
          return box;
        }
        function headOf(it, id, extraIcon) {
          var head = api.el('div', 'rc-card-head');
          var cb = pickBox(!!DATA.sel[id]);
          cb.addEventListener('click', function (ev) {
            ev.stopPropagation();
            DATA.sel[id] = !DATA.sel[id];
            renderRows();
          });
          head.appendChild(cb);
          if (extraIcon) head.appendChild(extraIcon);
          var title = api.el('div', 'rc-card-title', String(it.name || '—'));
          title.title = String(it.orig_path || '');
          head.appendChild(title);
          return head;
        }
        function folderIcon() {
          var ico = api.el('span', 'rc-fico');
          ico.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
            'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.2h7A1.5 1.5 0 0 1 19 9.7v7.8A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"/></svg>';
          return ico;
        }

        /* 数据集 / 权重卡：标题行 + （权重）训练方式标签 + 大小 / 删除时间 / 来源 */
        function fileCard(it) {
          var id = rowId(it);
          var c = api.el('div', 'card rc-card' + (DATA.sel[id] ? ' on' : ''));
          c.appendChild(headOf(it, id, null));
          if (it.kind === 'weights') {                 /* 数据集不显示标注（用户 2026-10-20） */
            var tags = api.el('div', 'rc-tags');
            tags.appendChild(methodTag(it.method_label));
            c.appendChild(tags);
          }
          c.appendChild(rows3(it));
          /* Loss 曲线只在「查看详细信息」里看（用户 2026-10-20：卡片照抄仓库，下方 3 条等间隙） */
          c.addEventListener('contextmenu', function (ev) {
            ev.preventDefault();
            showMenu(ev.clientX, ev.clientY, cardMenu(it));
          });
          return c;
        }

        /* 文件夹卡：照抄仓库（标题行 + 分割线 + 文件夹/个数 标签 + 三条信息），双击进去浏览 */
        function folderCard(it) {
          var id = rowId(it);
          var c = api.el('div', 'card rc-card rc-folder' + (DATA.sel[id] ? ' on' : ''));
          c.appendChild(headOf(it, id, folderIcon()));
          var tags = api.el('div', 'rc-tags');
          tags.appendChild(api.el('span', 'rc-tag rc-tag-folder', '文件夹'));
          tags.appendChild(api.el('span', 'rc-tag rc-tag-count', (it.count || 0) + ' 个'));
          c.appendChild(tags);
          c.appendChild(rows3(it));
          c.addEventListener('contextmenu', function (ev) {
            ev.preventDefault();
            showMenu(ev.clientX, ev.clientY, cardMenu(it));
          });
          c.addEventListener('dblclick', function (ev) {
            ev.preventDefault();
            enterFolder(it);
          });
          return c;
        }

        /* Loss 曲线（用户 2026-10-20：显示口径与仓库「详细信息」完全一致）——
           同一套：4 条水平网格线 + 底线、右上/右下标最大/最小 loss、左上「— train  — val」图例、
           560×168 视框、线宽 2、train 用 --ACCENT、val 用 --OK。 */
        function svgTag(tag, attrs) {
          var e = document.createElementNS('http://www.w3.org/2000/svg', tag);
          Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
          return e;
        }
        function drawLoss(points) {
          var W = 560, H = 168, pad = 30;
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
          var el = svgTag('svg', { viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none',
            role: 'img', 'aria-label': 'Loss 曲线' });
          for (var i = 0; i < 4; i++) {
            var gy = pad + i * (H - 2 * pad) / 3;
            el.appendChild(svgTag('line', { x1: pad, y1: gy, x2: W - pad, y2: gy,
              stroke: 'var(--LINE_SOFT)', 'stroke-width': 1 }));
          }
          el.appendChild(svgTag('line', { x1: pad, y1: H - pad, x2: W - pad, y2: H - pad,
            stroke: 'var(--LINE)', 'stroke-width': 1 }));
          function poly(ys, color) {
            var pts = ys.map(function (v, k) { return X(xs[k]) + ',' + Y(v); }).join(' ');
            el.appendChild(svgTag('polyline', { points: pts, fill: 'none', stroke: color,
              'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
          }
          poly(tr, 'var(--ACCENT)');
          poly(va, 'var(--OK)');
          function label(x, y, text, anchor) {
            var t = svgTag('text', { x: x, y: y, fill: 'var(--INK3)', 'font-size': 11, 'text-anchor': anchor });
            t.textContent = text;
            el.appendChild(t);
          }
          label(W - 8, Y(ymax) + 4, ymax.toFixed(2), 'end');
          label(W - 8, Y(ymin) + 4, ymin.toFixed(2), 'end');
          label(pad, pad - 8, '— train', 'start');
          label(pad + 60, pad - 8, '— val', 'start');
          return el;
        }

        /* 只读「详细信息」（用户 2026-10-20：权重可看，不可编辑） */
        function detailModal(it) {
          var mask = api.el('div', 'rc-modal');
          var box = api.el('div', 'rc-modal-box rc-det');
          var head = api.el('div', 'rc-modal-head');
          head.appendChild(api.el('span', 'rc-modal-title', '详细信息'));
          head.appendChild(button('关闭', 'rc-btn', function () { mask.remove(); }));
          box.appendChild(head);
          var body = api.el('div', 'rc-det-body');
          function line(k, v) {
            var r = api.el('div', 'rc-det-row');
            r.appendChild(api.el('span', 'rc-det-k', k));
            r.appendChild(api.el('span', 'rc-det-v', String(v == null || v === '' ? '—' : v)));
            return r;
          }
          body.appendChild(line('名称', it.name));
          body.appendChild(line('训练方式', it.method_label));
          body.appendChild(line('大小', fmtSize(it.size_bytes)));
          body.appendChild(line('删除时间', it.deleted_at));
          body.appendChild(line('来源', it.orig_path));
          var lr = api.el('div', 'rc-det-row');
          lr.appendChild(api.el('span', 'rc-det-k', 'Loss 曲线'));
          var lv = api.el('div', 'rc-det-v rc-det-loss');
          var chart = api.el('div', 'rc-loss-chart');
          chart.appendChild(api.el('div', 'rc-loss-empty', '读取中…'));
          lv.appendChild(chart);
          lr.appendChild(lv);
          body.appendChild(lr);
          box.appendChild(body);
          mask.appendChild(box);
          mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) mask.remove(); });
          document.body.appendChild(mask);
          /* 曲线：与仓库「详细信息」同一个接口、同一套画法 */
          api.get('/api/repo/weight_loss?name=' + encodeURIComponent(String(it.name || ''))
            + '&method=' + encodeURIComponent(String(it.method || ''))).then(function (d) {
            chart.textContent = '';
            if (!d || d.error || d.ok === false) {
              chart.appendChild(api.el('div', 'rc-loss-empty', '读取失败：' + String((d && d.error) || '未知错误')));
              return;
            }
            var pts = (d.points || []).filter(function (p) {
              return p && isFinite(p.step) && isFinite(p.train) && isFinite(p.val);
            });
            if (pts.length < 2) {
              chart.appendChild(api.el('div', 'rc-loss-empty', d.note || '这条权重还没有 Loss 曲线'));
              return;
            }
            chart.appendChild(drawLoss(pts));
            if (d.rows) chart.title = '共 ' + d.rows + ' 个点';
          }, function () {
            chart.textContent = '';
            chart.appendChild(api.el('div', 'rc-loss-empty', '读取失败'));
          });
        }

        /* 数据集/权重视图：整文件卡片排布 */
        function renderCards(rows) {
          tableWrap.hidden = true;
          cardsWrap.hidden = false;
          cardsWrap.textContent = '';
          syncCrumb();
          if (!rows.length) {
            cardsWrap.appendChild(api.el('div', 'rc-empty', DATA.open ? '这个文件夹里没有文件' : '回收站是空的'));
            return;
          }
          rows.forEach(function (it) {
            cardsWrap.appendChild(it.kind === 'folder' ? folderCard(it) : fileCard(it));
          });
        }

        function renderTable(rows) {
          tableWrap.hidden = false;
          cardsWrap.hidden = true;
          tableWrap.textContent = '';
          var allOn = rows.length > 0 && rows.every(function (it) { return !!DATA.sel[rowId(it)]; });

          var head = api.el('div', 'rc-tr rc-thead');
          var all = pickBox(allOn);            /* 全选框跟着行选择同步（点了全选自己也要勾上） */
          all.title = '全选';
          all.addEventListener('click', function () {
            var next = !allOn;
            rows.forEach(function (it) { DATA.sel[rowId(it)] = next; });
            renderRows();
          });
          head.appendChild(all);
          ['类型', '名称', '来源', '删除时间'].forEach(function (t) {
            head.appendChild(api.el('span', 'rc-th', t));
          });
          tableWrap.appendChild(head);

          if (!rows.length) {
            tableWrap.appendChild(api.el('div', 'rc-empty', '回收站是空的'));
            return;
          }
          rows.forEach(function (it) {
            var id = rowId(it);
            var tr = api.el('div', 'rc-tr' + (DATA.sel[id] ? ' on' : ''));
            var cb = pickBox(!!DATA.sel[id]);
            cb.addEventListener('click', function (ev) {
              ev.stopPropagation();
              DATA.sel[id] = !DATA.sel[id];
              renderRows();
            });
            tr.appendChild(cb);
            tr.appendChild(api.el('span', 'rc-kind-tag', kindLabel(it)));

            var name = it.kind === 'sample' || it.kind === 'rejected'
              ? String(it.title_short || it.question_short || it.title || it.question || it.src || '')
              : String(it.name || '');
            var detail = (it.kind === 'sample' || it.kind === 'rejected')
              ? String(it.article || it.answer || '')
              : '';
            var nameEl = api.el('span', 'rc-name', name || '—');
            nameEl.title = name + (detail ? '\n' + detail.slice(0, 200) : '');
            tr.appendChild(nameEl);

            var srcText = (it.kind === 'sample' || it.kind === 'rejected')
              ? String(it.src || '') + (it.theme || it.topic ? ' · ' + (it.theme || it.topic) : '')
              : String(it.orig_path || '');
            var srcEl = api.el('span', 'rc-src', srcText);
            srcEl.title = srcText;
            tr.appendChild(srcEl);

            tr.appendChild(api.el('span', 'rc-time', String(it.deleted_at || '')));
            /* 双击行 = 弹窗看内容（用户 2026-10-21：回收站单条数据不在下方展示内容）；勾选框只管选中（已 stopPropagation） */
            tr.addEventListener('dblclick', function () { showSampleModal(it); });
            tableWrap.appendChild(tr);
          });
        }

        /* 行内容改弹窗（用户 2026-10-21）：回收站单条数据不在下方展示内容，双击弹窗看全文。
           排版参考数据管理页（2026-10-21）：来源放标题位（过长渐隐右滚，同仓库卡片标题）、
           主题在问题上面、正文同数据管理的问答卡（问/答）；文章样本同文章预览（'# 标题'+正文）。 */
        function showSampleModal(it) {
          var title = api.scrollText(String(it.src || '—'), 'rc-modal-title', '');
          var body = api.el('div', 'rc-pre');
          var rec = api.el('div', 'rp-ds-rec');
          if (it.theme || it.topic) rec.appendChild(api.el('div', 'rp-ds-rec-head', String(it.theme || it.topic)));
          var q = it.question != null && String(it.question) !== '' ? String(it.question) : '';
          var a = it.answer != null && String(it.answer) !== '' ? String(it.answer) : '';
          if (q || a) {
            if (q) {
              var qr = api.el('div', 'rp-ds-qa');
              qr.appendChild(api.el('span', 'rp-ds-lab rp-ds-q', '问'));
              qr.appendChild(api.el('div', 'rp-ds-qtext', q));
              rec.appendChild(qr);
            }
            if (a) {
              var ar = api.el('div', 'rp-ds-qa');
              ar.appendChild(api.el('span', 'rp-ds-lab rp-ds-a', '答'));
              ar.appendChild(api.el('div', 'rp-ds-atext', a));
              rec.appendChild(ar);
            }
          } else {
            rec.appendChild(api.el('pre', 'rp-ds-pre',
              '# ' + (it.title || '') + '\n\n' + (it.article != null ? it.article : (it.text != null ? it.text : ''))));
          }
          body.appendChild(rec);
          openModal(title, body);
        }

        function renderRows() {
          var rows = filtered();
          if (DATA.view === 'dataset') renderCards(rows);   /* 权重页同款卡片排布（用户 2026-10-16） */
          else renderTable(rows);
        }

        function selectedTargets() {
          var out = [];
          /* 进了删掉的文件夹：勾选的是成员（key=tid），目标也从成员里取（用户 2026-10-20） */
          var pool = DATA.open ? DATA.openItems : DATA.items;
          pool.forEach(function (it) {
            if (DATA.sel[rowId(it)] && (DATA.open || inScope(it))) out.push(targetOf(it));
          });
          return out;
        }

        /* ---------- 动作 ---------- */
        function onRestore() {
          var targets = selectedTargets();
          if (!targets.length) { return; }   /* 没勾选不弹红字（用户 2026-10-23：不要这类提示） */
          api.post('/api/recycle/restore', { targets: targets }).then(function (d) {
            if (!d || d.error) {
              say('恢复失败：' + ((d && d.error) || '未知错误'), true);
            } else {
              /* 成功回执小字去掉（用户 2026-10-21） */
              if (d.errors && d.errors.length) say(d.errors.join('；'), true);
            }
            DATA.sel = {};
            load();
          }).catch(function (e) { say('恢复失败：' + e, true); });
        }

        function onPurge() {
          var targets = selectedTargets();
          if (!targets.length) { return; }   /* 没勾选不弹红字（用户 2026-10-23） */
          askConfirm({
            title: L('彻底删除', 'Delete permanently'),
            message: L('确定彻底删除选中的 ' + targets.length + ' 条？',
                      'Delete the ' + targets.length + ' selected item(s) permanently?'),
            detail: L('删除后无法恢复。', 'This cannot be undone.'),
            okText: L('彻底删除', 'Delete'),
          }).then(function (ok) {
            if (!ok) return;
            api.post('/api/recycle/purge', { targets: targets }).then(function (d) {
              if (!d || d.error) {
                say('删除失败：' + ((d && d.error) || '未知错误'), true);
              } else {
                /* 成功回执小字去掉（用户 2026-10-21） */
              }
              DATA.sel = {};
              load();
            }).catch(function (e) { say('删除失败：' + e, true); });
          });
        }

        function onClearAll() {
          /* 本页「清空」= 按本页条目逐个 purge（拆页后各清各的，不动另一页） */
          var targets = DATA.items.filter(inScope).map(targetOf);
          if (!targets.length) { return; }   /* 回收站本来就空 → 不弹红字，中间空态已经写了「回收站是空的」（用户 2026-10-23） */
          askConfirm({
            title: L('清空回收站', 'Empty recycle bin'),
            message: L('确定清空回收站？', 'Empty the recycle bin?'),
            detail: L('共 ' + targets.length + ' 条，删除后无法恢复。',
                      targets.length + ' item(s). This cannot be undone.'),
            okText: L('清空', 'Empty'),
          }).then(function (ok) {
            if (!ok) return;
            api.post('/api/recycle/purge', { targets: targets }).then(function (d) {
              if (!d || d.error) {
                say('清空失败：' + ((d && d.error) || '未知错误'), true);
              } else {
                /* 成功回执小字去掉（用户 2026-10-21：「不要这个清空回收站的小字」） */
              }
              DATA.sel = {};
              load();
            }).catch(function (e) { say('清空失败：' + e, true); });
          });
        }

        /* ---------- 取数 ---------- */
        function load() {
          return api.get('/api/recycle/list').then(function (d) {
            if (!d || d.error) {
              say('读取失败：' + ((d && d.error) || '未知错误'), true);
              return;
            }
            var items = [];
            (d.samples || []).forEach(function (e) {
              var row = Object.assign({}, e);
              row.kind = e.kind || 'sample';          /* 隔离样本自带 kind=rejected（2026-10-16） */
              items.push(row);
            });
            (d.folders || []).forEach(function (e) {     /* 删掉的文件夹：整组显示成一张文件夹卡 */
              var row = Object.assign({}, e);
              row.kind = 'folder';
              items.push(row);
            });
            (d.files || []).forEach(function (e) {
              var row = Object.assign({}, e);
              row.kind = e.kind || 'file';
              items.push(row);
            });
            items = items.filter(inScope);          /* 只留本页的 */
            items.sort(function (a, b) {
              var k = (KIND_ORDER[a.kind] || 9) - (KIND_ORDER[b.kind] || 9);
              if (k) return k;
              return String(b.deleted_at || '').localeCompare(String(a.deleted_at || ''));
            });
            DATA.items = items;
            if (DATA.open) {                      /* 正在浏览的文件夹被恢复/删干净 → 退回顶层 */
              var still = items.filter(function (x) { return x.kind === 'folder' && x.name === DATA.open; })[0];
              if (still) DATA.openItems = (still.items || []).map(function (x) {
                return Object.assign({}, x, { board: '' });
              });
              else { DATA.open = ''; DATA.openItems = []; }
            }
            renderRows();
          });
        }

        if (!isWeight) setView('dataset');     /* 横条初始态 + 首帧视图 */
        return load();
      },
    };
  }

  EAIPages.register('recycle_dataset', makePage('dataset'));
  EAIPages.register('recycle_weight', makePage('weight'));
})();
