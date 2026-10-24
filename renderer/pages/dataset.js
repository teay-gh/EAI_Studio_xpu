/* ============================================================
   EAI_Studio · pages/dataset.js ——「数据集」页
   导航键 dataset / 中文名「数据集」/ 分组「数据中心」
   ------------------------------------------------------------
   2026-10-15 整页改版（用户口径）：
     · 只显示卡片 —— 仓库卡片格式（状态点 + 名称 + 类型标 + 信息行），
       卡上加勾选框（框选），点卡片也能勾选/取消；
     · 卡片只显示「在仓库且格式审查通过」的数据集（ok && fmt_ok）；
     · 添加数据集从仓库添加（外部数据集只能从仓库进，入库时做格式审查）；
     · 格式示例挪到仓库页；本页不再有格式提示条/统计瓷片/工程数据目录/格式检查面板。
   顶栏横条：左 = 四个页签（=训练方式），右 = 四种筛选（可用/标注/已选/未选）。
   勾选写盘 data/.corpus_sel.json（四键 pretrain/continue/sft/lora），训练启动按阶段+方法取选。

   真实数据：
     GET  /api/repo/summary       → datasets[]（仓库卡片同款行：文件夹/训练方式/主题/体量/字符/行数/格式）
     GET  /api/dataset/scan       → 勾选态（selected / sel_known，写盘口径同旧版）
     POST /api/dataset/selection  → { kind, paths[] } 勾选落盘
   mock 只在 ?mock=1 预览截图时用；_registry.js 的 mock 按整条 path 精确匹配。
   ============================================================ */
(function () {
  'use strict';

  var SUMMARY_MOCK = {
    ok: true,
    project: '<工程目录>',
    datasets: [
      { name: 'story_ming.txt', folder: 'corpora', path: '…/story_ming.txt', kind: '纯文本', kind_key: 'plain',
        tab: ['pretrain'], chars: 60214, size_bytes: 64120, lines: 180, ok: true, fmt_ok: true, fmt_msg: '',
        themes: ['随笔'], use: 'pretrain' },
      { name: 'mimoZL_20261011_150200.jsonl', folder: 'response', path: '…/mimoZL_20261011_150200.jsonl',
        kind: '对话', kind_key: 'chat', tab: ['sft'], chars: 96412, size_bytes: 102900, lines: 200,
        ok: true, fmt_ok: true, fmt_msg: '', themes: ['机器学习基础'], use: 'sft' },
    ],
    dataset_count: 2, weight_count: 0, scan_error: '', weights_error: '',
  };
  var SCAN_MOCK = {
    kind: 'pretrain',
    project: '<工程目录>',
    sel_known: true,
    files: [
      { name: 'story_ming.txt', path: '…/story_ming.txt', selected: true },
      { name: 'mimoZL_20261011_150200.jsonl', path: '…/mimoZL_20261011_150200.jsonl', selected: false },
    ],
  };

  /* ---------- 取数交接（治「数据集页点开来，框和数字会闪」用户 2026-10-14） ----------
     与 article.js 同款：换页前 _registry 先调 prefetch 把扫描结果取回来放 HANDOFF；
     mount 里的 load() 同步拿走 → 新页出现的第一帧就是满的。一次性交接（拿走即删）。 */
  var HANDOFF = {};
  function takeOnce(url) {
    if (!Object.prototype.hasOwnProperty.call(HANDOFF, url)) return null;
    var d = HANDOFF[url];
    delete HANDOFF[url];
    return d;
  }
  function cget(api, url) {
    var h = takeOnce(url);
    return h ? Promise.resolve(h) : api.get(url);
  }

  EAIPages.register('dataset', {
    title: '数据集',
    sub: '工程内预训练文本与微调对话语料',

    /* 只给 ?mock=1（Edge 预览截图）用，正常跑一律走真实桥接口 */
    mock: {
      '/api/repo/summary': SUMMARY_MOCK,
      '/api/dataset/scan': SCAN_MOCK,
      '/api/dataset/scan?kind=pretrain': SCAN_MOCK,
      '/api/dataset/scan?kind=sft': SCAN_MOCK,
    },

    /* 换页之前先取回扫描结果（勾选态），保险丝 800ms：冷路径扫描约 0.4s */
    prefetchTimeout: 800,
    prefetch: function (api) {
      HANDOFF = {};
      var u = '/api/dataset/scan?kind=pretrain';
      return api.get(u).then(function (d) { if (d && !d.error) HANDOFF[u] = d; });
    },

    mount: function (root, api) {
      /* 卡片与仓库同格式（用户 2026-10-15「格式和仓库一致」）：复用 repo.css 的 rp-card 行样式 */
      if (!document.querySelector('link[data-repo-css]')) {
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = 'pages/repo.css';
        link.setAttribute('data-repo-css', '1');
        document.head.appendChild(link);
      }

      /* 四个页签 = 训练方式（2026-10-15）：蒸馏时的标注只是标注、不限制用途，
         筛选两组独立（用户 2026-10-15）：来源组[可用/标注]×选择组[全部/已选/未选]，各自选择、组合过滤；页签默认[标注+全部]。 */
      var TABS = [
        { key: 'pretrain', label: '预训练', group: 'pretrain' },
        { key: 'continue', label: '继续预训练', group: 'pretrain' },
        { key: 'sft', label: '指令微调', group: 'sft' },
        { key: 'lora', label: 'LoRA 微调', group: 'sft' },
      ];
      /* 筛选两组独立（用户 2026-10-15：「可用/标注」与「已选/未选」是两个维度，分开各自选择、组合过滤） */
      /* 用户 2026-10-15：来源组只留 可用/标注，不要「全部」 */
      var SCOPE_FILTERS = [
        { key: 'usable', label: '可用数据集' },
        { key: 'labeled', label: '标注数据集' },
      ];
      var SEL_FILTERS = [
        { key: 'all', label: '全部' },
        { key: 'picked', label: '已选数据集' },
        { key: 'unpicked', label: '未选数据集' },
      ];
      var USE_LABEL = { pretrain: '预训练', continue: '继续预训练', sft: '指令微调', lora: 'LoRA 微调', '': '未标注' };
      function groupOf(k) { return (k === 'sft' || k === 'lora') ? 'sft' : 'pretrain'; }

      var kind = 'pretrain';   /* 当前页签 = 训练方式键 */
      var fScope = 'labeled';  /* 来源组：默认标注数据集 */
      var fSel = 'all';        /* 选择组：默认全部（已选+未选都显示） */
      /* 页签/筛选记忆（用户 2026-10-16：离开再回来停在上次选的设定，重启回默认）：
         挂 window —— 页面模块会被重新装载、闭包变量会丢，window 在整个应用进程内稳定；
         不落盘，重启软件即回默认（同 article/response 的页签记忆口径） */
      var MEM_KEY = 'eaiLastTab:dataset';
      function getMem() { return (window.__eaiLastTab || {})[MEM_KEY] || null; }
      function setMem() {
        (window.__eaiLastTab = window.__eaiLastTab || {})[MEM_KEY] =
          { kind: kind, fScope: fScope, fSel: fSel };
      }
      function pickMem(list, k, dflt) {
        return list.some(function (t) { return t.key === k; }) ? k : dflt;
      }
      var MEM = getMem();
      if (MEM) {                                   /* 只认合法键，坏值退回默认 */
        kind = pickMem(TABS, MEM.kind, kind);
        fScope = pickMem(SCOPE_FILTERS, MEM.fScope, fScope);
        fSel = pickMem(SEL_FILTERS, MEM.fSel, fSel);
      }
      var q = '';              /* 搜索栏（用户 2026-10-16：同仓库） */
      var useFilter = '';      /* 训练方式筛选框（用户 2026-10-16：处于可用训练集时才出现） */
      var files = [];
      var sel = {};            // path -> true（勾选参与训练取源）

      /* ---------- 小工具 ---------- */
      function fmt(n) { n = Number(n) || 0; return n.toLocaleString('en-US'); }
      function fmtSize(n) {
        n = Number(n) || 0;
        if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
        if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
        if (n >= 1024) return Math.round(n / 1024) + ' KB';
        return n + ' B';
      }
      function dot(cls) { return api.el('span', 'rp-dot ' + cls); }
      function row(k, v, mono) {
        var r = api.el('div', 'rp-row');
        r.appendChild(api.el('span', 'rp-k', k));
        r.appendChild(api.el('span', 'rp-v' + (mono ? ' rp-mono' : ''), String(v)));
        return r;
      }

      /* ---------- 骨架：标题 + 顶栏横条 + 卡片区 ---------- */
      root.appendChild(api.hero('数据集'));        /* 不带副标题 */
      var wrap = api.el('div', 'stack');
      root.appendChild(wrap);

      /* 顶栏横条：左=四个页签（数据中心页签样式）、右=四种筛选 */
      var segRow = api.el('div', 'ds-seg-row');
      var seg = api.el('div', 'seg a-seg');
      seg.setAttribute('role', 'radiogroup');
      seg.setAttribute('aria-label', '训练方式');
      var segBtns = {};
      TABS.forEach(function (t) {
        var b = api.el('button', 'seg-opt', t.label);
        b.type = 'button';
        b.setAttribute('role', 'radio');
        /* 首帧就落 .on（用户 2026-10-17）：原来要等 load → renderSeg 才点亮，
           滑块（seg-thumb）因此晚一拍才出现 = 切页闪 */
        b.classList.toggle('on', t.key === kind);
        b.setAttribute('aria-checked', t.key === kind ? 'true' : 'false');
        b.onclick = function () { if (kind !== t.key) { kind = t.key; fScope = 'labeled'; fSel = 'all'; setMem(); load(); } };
        segBtns[t.key] = b;
        seg.appendChild(b);
      });
      segRow.appendChild(seg);

      var fWrap = api.el('div', 'ds-fsegs');
      function buildFseg(list, aria, onPick, cur) {
        var g = api.el('div', 'seg ds-fseg');
        g.setAttribute('role', 'radiogroup');
        g.setAttribute('aria-label', aria);
        var btns = {};
        list.forEach(function (t) {
          var b = api.el('button', 'seg-opt', t.label);
          b.type = 'button';
          b.setAttribute('role', 'radio');
          b.classList.toggle('on', t.key === cur);          /* 首帧点亮（用户 2026-10-17：滑块晚一拍出现=切页闪） */
          b.setAttribute('aria-checked', t.key === cur ? 'true' : 'false');
          b.onclick = function () { onPick(t.key); };
          btns[t.key] = b;
          g.appendChild(b);
        });
        fWrap.appendChild(g);
        return { seg: g, btns: btns };
      }
      var fsegA = buildFseg(SCOPE_FILTERS, '数据来源筛选', function (k) {
        if (fScope !== k) { fScope = k; setMem(); render(); }
      }, fScope);
      var fsegB = buildFseg(SEL_FILTERS, '选择状态筛选', function (k) {
        if (fSel !== k) { fSel = k; setMem(); render(); }
      }, fSel);
      segRow.appendChild(fWrap);
      wrap.appendChild(segRow);
      api.segThumb(seg, false);                  /* 先落位，避免首次进页面滑一下 */
      api.segThumb(fsegA.seg, false);
      api.segThumb(fsegB.seg, false);

      /* 卡片区：仓库卡片格式 + 勾选框（框选） */
      var cardsWrap = api.el('div', 'ds-cards');
      var dsEnter = true;   /* 只在本次挂载的首批卡片上播进场动画（用户 2026-10-17） */

      /* 搜索/筛选条（用户 2026-10-16：同仓库 rp-bar 同款同排布）：
         用户 2026-10-16 改位：筛选框在左、搜索钉右——标注/可用两页搜索框同位；
         训练方式筛选框只在「可用训练集」时出现 */
      var bar = api.el('div', 'rp-bar rp-bar-split');
      var usePick = api.pick(api.el('input'), [
        ['', '全部'], ['none', '未标注'], ['pretrain', '预训练'], ['continue', '继续预训练'],
        ['sft', '指令微调'], ['lora', 'LoRA 微调'],
      ], 'rp-sel rp-pick-wide');
      usePick._hideSel = true;
      usePick.value = '';
      usePick.addEventListener('change', function () {
        useFilter = (usePick.value == null) ? '' : usePick.value;
        render();
      });
      bar.appendChild(usePick);
      var search = api.el('input', 'rp-search');
      search.type = 'text';
      search.placeholder = '搜索';
      search.addEventListener('input', function () { q = search.value.trim(); render(); });
      bar.appendChild(search);
      wrap.appendChild(bar);
      wrap.appendChild(cardsWrap);

      /* ---------- 勾选落盘（口径同旧版：四键 .corpus_sel.json） ---------- */
      function pickedPaths() {
        var out = [];
        visibleFiles().forEach(function (f) {
          if (f.path && sel[f.path || f.name]) out.push(f.path);
        });
        return out;
      }
      function postSelection() {
        return api.post('/api/dataset/selection', { kind: kind, paths: pickedPaths() });
      }
      /* 选中态就地改：卡片描边、勾选框、上传后端，都不碰 DOM 结构。
         只有当前正按「已选 / 未选」筛选时成员才变，那时才重排卡片（用户 2026-10-18） */
      function setSel(key, next, card, box) {
        sel[key] = next;
        if (card) card.classList.toggle('on', next);
        if (box) box.checked = next;
        postSelection();
        if (fSel === 'picked' || fSel === 'unpicked') renderCards();
      }

      /* ---------- 过滤 ---------- */
      function visibleFiles() {
        /* 本页签这一类：纯文本归预训练/继续预训练、对话归指令微调/LoRA */
        var g = groupOf(kind);
        return files.filter(function (f) { return !f.tab || f.tab.indexOf(g) >= 0; });
      }
      function filteredFiles() {
        var vis = visibleFiles();
        if (fScope === 'usable') vis = vis.filter(usableOf);
        else if (fScope === 'labeled') vis = vis.filter(function (f) {
          /* 两标注槽任一命中（用户 2026-10-16：一数据集可标两次） */
          return String(f.use || '') === kind || String(f.use2 || '') === kind;
        });
        if (fSel === 'picked') vis = vis.filter(function (f) { return !!sel[f.path || f.name]; });
        else if (fSel === 'unpicked') vis = vis.filter(function (f) { return !sel[f.path || f.name]; });
        /* 搜索（用户 2026-10-16）：命中名称/文件夹/主题 */
        if (q) {
          var needle = q.toLowerCase();
          vis = vis.filter(function (f) {
            var hay = ((f.name || '') + ' ' + (f.folder || '') + ' ' + ((f.themes || []).join(' '))).toLowerCase();
            return hay.indexOf(needle) >= 0;
          });
        }
        /* 训练方式筛选（用户 2026-10-16）：只在「可用训练集」下生效，按标注筛选 */
        if (fScope === 'usable' && useFilter) {
          vis = vis.filter(function (f) {
            return useFilter === 'none' ? !f.use : String(f.use || '') === useFilter;
          });
        }
        return vis;
      }
      function usableOf(f) {
        return f.ok !== false && f.fmt_ok !== false;
      }
      function resetSel(d) {
        sel = {};
        var known = !!(d && d.sel_known);
        visibleFiles().forEach(function (f) {
          var p = f.path || f.name;
          sel[p] = known ? !!f.selected : usableOf(f);      /* 没勾过就默认勾上「可用文件」 */
        });
      }

      /* ---------- 渲染 ---------- */
      function renderSeg() {
        TABS.forEach(function (t) {
          var on = t.key === kind;
          segBtns[t.key].classList.toggle('on', on);
          segBtns[t.key].setAttribute('aria-checked', on ? 'true' : 'false');
        });
        api.segThumb(seg);
        SCOPE_FILTERS.forEach(function (t) {
          var on = t.key === fScope;
          fsegA.btns[t.key].classList.toggle('on', on);
          fsegA.btns[t.key].setAttribute('aria-checked', on ? 'true' : 'false');
        });
        api.segThumb(fsegA.seg);
        SEL_FILTERS.forEach(function (t) {
          var on = t.key === fSel;
          fsegB.btns[t.key].classList.toggle('on', on);
          fsegB.btns[t.key].setAttribute('aria-checked', on ? 'true' : 'false');
        });
        api.segThumb(fsegB.seg);
        /* 训练方式筛选框只在「可用训练集」下出现（用户 2026-10-16） */
        usePick.style.display = (fScope === 'usable') ? '' : 'none';
      }

      function renderCards(msg) {
        cardsWrap.innerHTML = '';
        var vis = filteredFiles();
        if (!vis.length) {
          cardsWrap.appendChild(api.el('div', 'ds-cards-empty', msg || '没有符合的数据集。'));
          return;
        }
        vis.forEach(function (f, ci) {
          var key = f.path || f.name;
          var on = !!sel[key];
          /* 仓库卡片格式（rp-card 行样式）+ 勾选框（框选）+ 选中描边 */
          var card = api.el('div', 'card rp-card ds-card2' + (on ? ' on' : ''));
          var head = api.el('div', 'rp-head');
          var pick = api.el('label', 'ds-pick');
          var box = api.el('input', '');
          box.type = 'checkbox';
          box.checked = on;
          box.addEventListener('click', function (e) { e.stopPropagation(); });
          /* 勾选只改这一张卡的状态，不重渲染整页（用户 2026-10-18：整页重画会让右缘渐隐闪一下） */
          box.addEventListener('change', function () {
            setSel(key, box.checked, card, box);
          });
          pick.appendChild(box);
          pick.addEventListener('click', function (e) { e.stopPropagation(); });  /* 点勾选框/标签只走 change，别让卡片再翻一次 */
          head.appendChild(pick);
          /* 状态小球去掉（用户 2026-10-20）：卡片只出审查通过的，点恒为绿色没有信息量；
             合格与否在下方「格式」一行已经写明 */
          /* 文件名照仓库页：超 50 字截断 + 单行 + 右缘渐隐 + 悬停右滚（用户 2026-10-18） */
          var nameTxt = String(f.name || '（未命名）');
          if (nameTxt.length > 50) nameTxt = nameTxt.slice(0, 50);
          var title = api.scrollText(nameTxt, 'rp-title', 'name');
          title.title = f.path || '';
          head.appendChild(title);
          if (f.locked) head.appendChild(lockBadge(f.lock_folder));
          card.appendChild(head);
          /* 数据类型文案与仓库页统一（用户 2026-10-22）：「纯文本」一律写「文本」 */
          card.appendChild(api.el('div', 'rp-tag', f.kind === '纯文本' ? '文本' : (f.kind || '—')));
          card.appendChild(row('文件夹', f.folder || '—'));
          /* 训练方式两标注都显示（用户 2026-10-16：一数据集可标两次） */
          var useTxt = [f.use, f.use2].filter(function (u) { return u; })
            .map(function (u) { return USE_LABEL[u] || u; }).join('、');
          card.appendChild(row('训练方式', useTxt || '未标注'));
          /* 主题是「多字」字段：照仓库页单行排布，超出框宽悬停向右滚（用户 2026-10-18） */
          var themeRow = api.el('div', 'rp-row');
          themeRow.appendChild(api.el('span', 'rp-k', '主题'));
          themeRow.appendChild(api.scrollText((f.themes && f.themes.length) ? f.themes.join('、') : '—',
            'rp-v rp-scroll-v', 'theme'));
          card.appendChild(themeRow);
          card.appendChild(row('体量', fmtSize(f.size_bytes)));
          card.appendChild(row('字符', fmt(f.chars)));
          card.appendChild(row('行数', fmt(f.lines)));
          card.appendChild(row('格式', '合格'));
          card.onclick = function () {
            setSel(key, !sel[key], card, box);
          };
          cardsWrap.appendChild(card);
        });
        /* 进场动画（用户 2026-10-17）：只在切进页面后第一次把卡片画出来时播一次；
           之后搜索/勾选造成的重渲染不再加 .enter，避免打字时整屏重播。 */
        if (dsEnter) {
          [].forEach.call(cardsWrap.querySelectorAll('.ds-card2'), function (c, i) {
            c.style.setProperty('--ds-i', Math.min(i, 8));
          });
          cardsWrap.classList.add('enter');
          dsEnter = false;
          setTimeout(function () { cardsWrap.classList.remove('enter'); }, 900);
        }
      }

      function render() {
        renderSeg();
        renderCards();
      }

      /* 锁标记（用户 2026-10-22）：锁上的数据集在此页也挂小锁，语义同仓库页
         （文件夹锁向下继承；锁定只在 EAI 内生效，不影响读取/训练） */
      function lockBadge(owner) {
        var b = api.el('span', 'rp-lock on');
        b.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
        b.title = owner ? '随文件夹「' + owner + '」锁定：文件夹不解锁就不能单独解锁'
                        : '已锁定：EAI 内不能删除';
        return b;
      }

      function load() {
        return Promise.all([
          api.get('/api/repo/summary'),
          cget(api, '/api/dataset/scan?kind=' + kind),
        ]).then(function (rs) {
          var rep = rs[0] || {};
          var sc = rs[1] || {};
          if (rep.error || sc.error) {
            files = [];
            renderCards('读取失败：' + (rep.error || sc.error || '未知错误'));
            return;
          }
          /* 只显示「在仓库 + 格式审查通过」的数据集（用户 2026-10-15） */
          files = (rep.datasets || []).filter(usableOf);
          /* 仓库置顶在此页也生效（用户 2026-10-22）：置顶的排前面、同组按置顶先后，其余保持仓库原顺序 */
          files.sort(function (a, b) {
            var pa = a.pinned ? 0 : 1, pb = b.pinned ? 0 : 1;
            if (pa !== pb) return pa - pb;
            return (a.pin_rank || 9999) - (b.pin_rank || 9999);
          });
          var selMap = {};
          ((sc && sc.files) || []).forEach(function (x) {
            if (x && x.path) selMap[x.path] = !!x.selected;
          });
          files.forEach(function (f) { f.selected = selMap[f.path]; });
          resetSel(sc || {});
          render();
        });
      }

      /* 先渲染静态骨架（分段），桥不通时也保持正确外观 */
      renderSeg();

      return load();
    },

    unmount: function () {
      /* 无后台轮询需要清理 */
    },
  });
})();
