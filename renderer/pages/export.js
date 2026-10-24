/* ============================================================
   导出模型（「模型」分组下的独立一页）

   后端 bridge_api/export.py：
     GET  /api/export/state  → {items:[{key,label,path,name,exists,size,mtime,source}], project, default_target}
     POST /api/export/run    → {ok, target_dir, copied:[{name,size,size_ok,sha256_ok,sha256,ms}], errors, skipped}

   口径（与全站一致）：卡片不写小字说明；导出状态进日志（用户 2026-10-21：导出旁不再放状态小球）；
   日志只写真实事件（复制的文件名/大小/sha256 校验、失败原因），不写介绍句。
   ============================================================ */
/* 上一次读到的清单（模块级，不随页面实例重建）：
   切页回来时先用它把「全选 + 各行勾选」同步画好，避免全选框先空着再被 syncAll 勾上（用户 2026-10-17）。 */
var EX_CACHE = null;

EAIPages.register('export', {
  title: '导出模型',
  mock: {
    '/api/export/state': {
      ok: true, project: '（预览）EAI_GC', default_target: 'D:\\（预览）\\EAI_GC\\exports',
      items: [
        { key: 'pretrain', label: '预训练权重', name: 'eproai_pretrain.pt', exists: true,
          size: 6710886, mtime: 1760000000, source: 'config.train.pretrain_out', path: '（预览）',
          method: 'pretrain', method_label: '预训练' },
        { key: 'continue', label: '继续预训练权重', name: 'eproai_continue.pt', exists: false,
          size: 0, mtime: 0, source: 'config.train.continue_out', path: '（预览）',
          method: 'continue', method_label: '继续预训练' },
        { key: 'sft', label: '指令微调权重', name: 'eproai_model.pt', exists: false,
          size: 0, mtime: 0, source: 'config.train.sft_out', path: '（预览）',
          method: 'sft', method_label: '指令微调' },
      ],
    },
    '/api/export/run': { ok: true, target_dir: '（预览）', copied: [], errors: [], skipped: [] },
  },

  mount(root, api) {
    root.appendChild(api.hero('导出模型'));

    var grid = api.el('div', 'ex-grid');
    var colL = api.el('div', 'ex-col');
    var colR = api.el('div', 'ex-col');
    grid.appendChild(colL);
    grid.appendChild(colR);
    root.appendChild(grid);

    var items = [];        /* 当前可见（筛选/搜索后） */
    var ALL = [];          /* 重新扫描读到的全量 */
    var picked = {};

    /* ---------- 小工具 ---------- */
    function button(text, cls, onClick) {
      var b = api.el('button', 'ex-btn ' + cls, text);
      b.type = 'button';
      b.addEventListener('click', onClick);
      return b;
    }
    function fmtSize(n) {
      n = Number(n) || 0;
      if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
      if (n >= 1024) return Math.round(n / 1024) + ' KB';
      return n + ' B';
    }
    function fmtTime(sec) {
      if (!sec) return '—';
      var d = new Date(sec * 1000);
      var p = function (x) { return (x < 10 ? '0' : '') + x; };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
        ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }
    /* 训练方式的中文短标签（i18n 词典里有对应英文，切语言能跟着换）—— 用户 2026-10-23 */
    function methodZh(it) {
      return { pretrain: '预训练权重', 'continue': '继续预训练权重', sft: '指令微调权重',
        lora: 'LoRA 权重', merge: 'LoRA 合并权重' }[it.method] || '其他权重';
    }
    /* 悬停里的来源：按结构化 source_kind 拼，不再用后端那份中文 source（用户 2026-10-23）*/
    function srcText(it) {
      if (!it.source) return '';
      var ck = it.source_kind === 'project-checkpoints';
      var zh = ck ? '工程 checkpoints/' : it.source;
      return bi('（' + zh + '）', ' (' + (ck ? 'project checkpoints/' : it.source) + ')');
    }
    function pickedKeys() {
      return Object.keys(picked).filter(function (k) { return picked[k]; });
    }

    /* ---------- 左：模型文件清单 ---------- */
    var listCard = api.card('模型文件');
    var head = api.el('div', 'ex-listhead');
    var allBox = api.el('input', '');
    allBox.type = 'checkbox';
    var allWrap = api.el('label', 'ex-check ex-all');
    allWrap.hidden = true;          /* 清单为空时不显示全选（第一次进页面读数据那 200ms 也不闪） */
    allWrap.appendChild(allBox);
    allWrap.appendChild(api.el('span', 'ex-check-label', '全选'));
    head.appendChild(allWrap);
    /* 筛选 + 搜索（用户 2026-10-21）：筛选训练方式、搜索权重名称，放在「重新扫描」左边。
       下拉换仓库同款自绘 api.pick（用户 2026-10-21）：默认文案「全部」、已选项不再出现在下拉里、
       圆角处理与仓库一致（井+弹层接缝侧拉平拼成一体，全局 .pick-pop/.pick-open 样式）。 */
    var selEl = api.pick(api.el('input', ''), [['', '全部']], 'ex-pick');
    selEl._hideSel = true;   /* 当前已选项不列在下拉里（同仓库筛选下拉） */
    var searchEl = api.el('input', 'ex-input ex-search');
    searchEl.type = 'text';
    searchEl.placeholder = '搜索权重名称';
    head.appendChild(selEl);
    head.appendChild(searchEl);
    head.appendChild(button('重新扫描', 'ex-ghost', function () { load(); }));
    listCard.body.appendChild(head);

    /* 滚动条隐藏 + 上下渐隐（用户 2026-10-20，参考仓库「查看内容」）：
       列表本体仍是滚动容器，两条渐隐浮层压在上下边缘，按距边距离淡入淡出 */
    var listWrap = api.el('div', 'ex-listwrap');
    var listEl = api.el('div', 'ex-list');
    var fadeTop = api.el('div', 'ex-fade ex-fade-top');
    var fadeBot = api.el('div', 'ex-fade ex-fade-bot');
    listWrap.appendChild(listEl);
    listWrap.appendChild(fadeTop);
    listWrap.appendChild(fadeBot);
    listCard.body.appendChild(listWrap);

    /* 高度固定为 6.5 行（用户 2026-10-20）：行高按实际条目的 rect 量，
       半行露在渐隐下面，提示下面还有内容 */
    function sizeList() {
      var one = listEl.querySelector('.ex-item');
      var h = one ? one.getBoundingClientRect().height : 0;
      if (h > 4) listEl.style.height = Math.round(h * 6.5) + 'px';
      updFade();
    }
    function updFade() {
      var H = 22;
      var t = listEl.scrollTop;
      var b = listEl.scrollHeight - listEl.clientHeight - t;
      fadeTop.style.opacity = String(Math.min(1, Math.max(0, t / H)));
      fadeBot.style.opacity = String(Math.min(1, Math.max(0, b / H)));   /* 滚到底 → 下方渐隐自动归零 */
    }
    listEl.addEventListener('scroll', updFade);
    var booted = false;
    function paintCache() {
      if (booted || !EX_CACHE || !EX_CACHE.length) return;
      booted = true;
      renderList(EX_CACHE.slice());
    }
    colL.appendChild(listCard);

    /* ---------- 右：导出到 + 日志 ---------- */
    var outCard = api.card('导出到');
    var target = api.el('input', 'ex-input ex-mono');
    target.type = 'text';
    /* 可编辑可选中（用户 2026-10-21）：路径能直接改/粘贴，也能选中复制 */
    target.placeholder = '（未选择）';
    target.addEventListener('input', syncAll);
    outCard.body.appendChild(target);

    var row = api.el('div', 'ex-row');
    var pickBtn = button('选择文件夹', 'ex-soft', onPick);
    var runBtn = button('导出', 'ex-accent', onExport);
    runBtn.disabled = true;
    row.appendChild(pickBtn);
    row.appendChild(runBtn);
    outCard.body.appendChild(row);
    colR.appendChild(outCard);

    var logCard = api.card('导出日志');
    var logEl = api.el('div', 'ex-log');
    logCard.body.appendChild(logEl);
    colR.appendChild(logCard);

    function logLine(text, cls) {
      logEl.appendChild(api.el('span', 'ex-log-line' + (cls ? ' ' + cls : ''), String(text)));
      logEl.scrollTop = logEl.scrollHeight;
    }

    function syncAll() {
      var live = items.filter(function (it) { return it.exists; });   /* 全选只看当前筛选出的行 */
      var visPicked = live.filter(function (it) { return picked[it.key]; }).length;
      allBox.checked = live.length > 0 && visPicked >= live.length;
      allBox.indeterminate = visPicked > 0 && visPicked < live.length;
      runBtn.disabled = !target.value || !pickedKeys().length;
    }

    /* 筛选/搜索（用户 2026-10-21）：全量留 ALL，视图 = 筛选后的行 */
    function filtered() {
      var m = selEl.value;
      var q = (searchEl.value || '').trim().toLowerCase();
      return ALL.filter(function (it) {
        if (m && (it.method_label || '') !== m) return false;
        if (q && String(it.name || '').toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
    }
    function syncFilterOptions() {
      var m = selEl.value;
      var seen = {};
      var list = [['', '全部']];
      ALL.forEach(function (it) {
        var v = it.method_label || '';
        if (v && !seen[v]) { seen[v] = 1; list.push([v, v]); }
      });
      api.pick(selEl, list, 'ex-pick');   /* isPick 分支原地重建弹层选项 */
      selEl._hideSel = true;
      selEl.value = list.some(function (o) { return o[0] === m; }) ? m : '';
    }

    /* 把清单画出来（行 + 全选）在同一帧内完成：
       全选框在建列表之前不显示，避免「空的未选 → 勾上」这一拍（用户 2026-10-17） */
    function renderList(list) {
      ALL = list || [];
      syncFilterOptions();
      paint(true);
    }
    function paint(reset) {
      if (reset) picked = {};
      items = filtered();
      listEl.innerHTML = '';
      items.forEach(function (it) { listEl.appendChild(itemRow(it, reset)); });
      allWrap.hidden = !ALL.length;
      syncAll();
      sizeList();          /* 渲染完量一次行高，锁定 6.5 行（用户 2026-10-20） */
    }

    function itemRow(it, reset) {
      var r = api.el('div', 'ex-item' + (it.exists ? '' : ' ex-missing'));
      var box = api.el('input', '');
      box.type = 'checkbox';
      box.disabled = !it.exists;
      if (reset || picked[it.key] === undefined) picked[it.key] = !!it.exists;
      box.checked = !!picked[it.key];
      box.addEventListener('change', function () {
        picked[it.key] = box.checked;
        syncAll();
      });
      var wrap = api.el('label', 'ex-check');
      wrap.appendChild(box);
      r.appendChild(wrap);

      var info = api.el('div', 'ex-info');
      info.appendChild(api.el('div', 'ex-name', it.name || it.label));
      /* 副标题（用户 2026-10-23）：训练方式单独一个文本节点，词典才能整串命中 ——
         '预训练权重 · 5.7 MB · …' 这种拼串永远翻不到；大小/时间/文件名与语言无关 */
      var meta = api.el('div', 'ex-meta');
      /* 用后端 label（含训练溯源的真实方式），不要按文件名 method 猜 —— 历史权重会掉成「其他权重」 */
      meta.appendChild(api.el('span', 'ex-meta-m', it.label || methodZh(it)));
      if (it.exists) {
        meta.appendChild(document.createTextNode(' · ' + fmtSize(it.size) + ' · ' + fmtTime(it.mtime)));
      } else {
        meta.appendChild(document.createTextNode(' · '));
        meta.appendChild(api.el('span', 'ex-meta-m', '尚无此文件'));
      }
      info.appendChild(meta);
      info.title = (it.path || '') + srcText(it);
      r.appendChild(info);
      /* 状态球去掉（用户 2026-10-20）：有没有文件、多大、什么时候，ex-meta 那行已经写了 */
      return r;
    }

    function load() {
      return api.get('/api/export/state').then(function (d) {
        if (!d || d.error) {
          logLine(bi('读取失败：', 'Read failed: ') + ((d && (d.error || d.reason)) || bi('未知错误', 'Unknown error')), 'err');
          return;
        }
        if (!target.value) target.value = d.default_target || '';
        renderList(d.items || []);
        EX_CACHE = ALL.slice();
      }).catch(function (e) {
        logLine(bi('读取失败：', 'Read failed: ') + (e && e.message ? e.message : e), 'err');
      });
    }

    function onPick() {
      var p;
      /* 原生对话框文案不进 DOM，词典翻不到（用户 2026-10-23） */
      try { p = eai.pickFolder({ title: bi('选择导出文件夹', 'Choose export folder'), buttonLabel: bi('导出到此', 'Export here') }); }
      catch (e) { p = Promise.resolve(null); }
      Promise.resolve(p).then(function (dir) {
        if (!dir) return;
        target.value = dir;
        syncAll();
      }).catch(function () {});
    }

    function onExport() {
      var keys = pickedKeys();
      runBtn.disabled = true;
      pickBtn.disabled = true;
      logEl.innerHTML = '';
      logLine(bi('导出到 ', 'Export to ') + target.value, null);
      api.post('/api/export/run', { target_dir: target.value, keys: keys }).then(function (d) {
        (d.skipped || []).forEach(function (s) { logLine(bi('跳过 ', 'Skipped ') + s.name + bi('：', ': ') + s.note, 'warn'); });
        (d.copied || []).forEach(function (c) {
          var ok = c.size_ok && c.sha256_ok;
          var same = function (b) { return b ? bi('一致', 'match') : bi('不一致', 'mismatch'); };
          logLine(bi('已复制 ', 'Copied ') + c.name + ' · ' + fmtSize(c.size) +
            (ok ? bi(' · sha256 校验通过 ', ' · sha256 verified ') + c.sha256
                : bi(' · 校验不一致（大小 ', ' · checksum mismatch (size ') + same(c.size_ok) +
                  bi('，sha256 ', ', sha256 ') + same(c.sha256_ok) + bi('）', ')')) +
            ' · ' + c.ms + 'ms', ok ? 'ok' : 'err');
        });
        (d.errors || []).forEach(function (e) { logLine(bi('失败 ', 'Failed ') + e.name + bi('：', ': ') + e.error, 'err'); });
        if (d.ok) {
          var unit = bi(' 个文件 → ', ' ' + window.enPlural(d.count, 'file → ', 'files → '));
          logLine(bi('导出完成：', 'Export complete: ') + d.count + unit + d.target_dir, 'ok');
        } else {
          var why = (d.errors || []).length ? d.errors[0].error
            : (d.error || bi('没有任何文件被复制', 'No files were copied'));
          logLine(bi('导出未完成：', 'Export incomplete: ') + why, 'err');
        }
      }).catch(function (e) {
        logLine(bi('导出失败：', 'Export failed: ') + (e && e.message ? e.message : e), 'err');
      }).then(function () {
        pickBtn.disabled = false;
        syncAll();
      });
    }

    /* 筛选/搜索即时过滤（用户 2026-10-21）：换筛选/输入不清勾选 */
    selEl.addEventListener('change', function () { paint(false); });
    searchEl.addEventListener('input', function () { paint(false); });

    allBox.addEventListener('change', function () {
      items.forEach(function (it) {
        if (!it.exists) return;
        picked[it.key] = allBox.checked;
      });
      listEl.querySelectorAll('.ex-item').forEach(function (r, i) {
        var box = r.querySelector('input[type="checkbox"]');
        if (box && !box.disabled) box.checked = allBox.checked;
      });
      syncAll();
    });

    paintCache();      /* 有上次的清单就先把行 + 全选一起画好，再去刷新 */
    return load();
  },

  unmount(root) {},
});

/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
window.__EAI_API.onProjectChange && window.__EAI_API.onProjectChange(function () {
  EX_CACHE = null;
});
