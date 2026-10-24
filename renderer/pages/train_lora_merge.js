/* ============================================================
   EAI_Studio · pages/train_lora_merge.js ——「LoRA 合并」页（用户 2026-10-17）
   导航键 train_lora_merge / 中文名「LoRA 合并」/ 分组「训练中心」/ 排在量化之后
   ------------------------------------------------------------
   左：权重命名（输出名）+ 基座权重 + LoRA 适配器（都可从仓库挑）；
   右：动作行 + 四张瓷片 + 训练输出（与预训练页同款排版/间距）。
   点「合并」后桥端调 engine/core/lora.py 的 merge_adapter_file（与导出页勾合并同一条链路）。

   接口（bridge_api/train_lora_merge.py）：
     GET  /api/train_lora_merge/config
     POST /api/train_lora_merge/save    {base|adapter|out}
     POST /api/train_lora_merge/run     {base, adapter, out}
   ============================================================ */
(function () {
  'use strict';

  var CACHE = null;

  function icon(paths) {
    var span = document.createElement('span');
    span.className = 'lm-ic';
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      paths + '</svg>';
    return span;
  }

  function tileSize(n) {
    /* 共享口径：三位数不带小数（用户 2026-10-18）。
       注意 api 只是 mount 的形参，模块级拿不到 → 走调试钩子（用户 2026-10-22 修：
       原来这里裸用 api，页面一渲染就 ReferenceError，LoRA 合并页直接空白）。 */
    var a = window.__EAI_API;
    return (a && a.tileSize) ? a.tileSize(n) : '—';
  }

  EAIPages.register('train_lora_merge', {
    title: 'LoRA 合并',

    mount: function (root, api) {
      var paths = { base: '', adapter: '' };
      var busy = false;

      root.appendChild(api.hero('LoRA 合并'));

      var grid = api.el('div', 'lm-grid');

      /* ---------------- 左：命名 + 两行权重 ---------- */
      var card = api.card('');
      var head = card.querySelector('.card-head');
      if (head) head.remove();

      var wname = api.weightName({ inputClass: 'lm-input' });
      card.body.appendChild(wname.wrap);
      card.body.appendChild(api.el('hr', 'lm-divider'));

      function pickRow(label, key) {
        var block = api.el('div', 'lm-path-block');
        block.appendChild(api.el('div', 'lm-k', label));
        var row = api.el('div', 'lm-path-row');
        var dot = api.el('span', 'lm-dot');
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lm-pick';
        btn.textContent = '选择';
        var nameEl = api.el('span', 'lm-file', '—');
        var sizeEl = api.el('span', 'lm-size', '—');
        row.appendChild(dot);
        row.appendChild(btn);
        row.appendChild(nameEl);
        row.appendChild(sizeEl);
        block.appendChild(row);
        btn.onclick = function () {
          if (busy) return;
          api.weightPicker({
            title: '选择' + label,
            current: paths[key],
            /* 用户 2026-10-17：适配器只允许选 *.lora.pt，基座只允许选非 *.lora.pt */
            accept: key === 'adapter' ? 'lora' : 'base',
            onPick: function (w) {
              setPath(key, w.path, w.size);
              save({});
            },
          });
        };
        return { block: block, dot: dot, nameEl: nameEl, sizeEl: sizeEl };
      }

      var rowBase = pickRow('基座权重', 'base');
      var rowAdapter = pickRow('LoRA 适配器', 'adapter');
      card.body.appendChild(rowBase.block);
      card.body.appendChild(rowAdapter.block);
      grid.appendChild(card);

      /* ---------------- 右：动作 + 瓷片 + 输出 ---------------- */
      var rightCol = api.el('div', 'lm-col');
      var actions = api.el('div', 'lm-actions');
      var runBtn = document.createElement('button');
      runBtn.type = 'button';
      runBtn.className = 'btn btn-accent';
      runBtn.appendChild(api.el('span', null, '▶ 合并'));
      var refreshBtn = document.createElement('button');
      refreshBtn.type = 'button';
      refreshBtn.className = 'lm-btn2';
      refreshBtn.appendChild(icon('<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'));
      refreshBtn.appendChild(api.el('span', null, '刷新'));
      actions.appendChild(runBtn);
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
      var vState = tile('合并状态');
      var vBase = tile('基座权重');
      var vAdapter = tile('LoRA 适配器');
      var vOut = tile('输出权重');
      vState.textContent = '未启动';
      vState.classList.add('idle');   /* 首帧先放终态，避免切页时空白→文字跳一下（用户 2026-10-17） */
      rightCol.appendChild(stats);

      var logCard = api.card('训练输出');
      var logBox = api.el('div', 'lm-logbox');
      var logPre = api.el('pre', 'lm-log');
      logBox.appendChild(logPre);
      logCard.body.appendChild(logBox);
      rightCol.appendChild(logCard);

      grid.appendChild(rightCol);
      root.appendChild(grid);

      /* ---------------- 工具 ---------------- */
      function log(text, isErr) {
        if (!text) return;
        logPre.textContent = (logPre.textContent ? logPre.textContent + '\n' : '') + String(text);
        logPre.classList.toggle('lm-err', !!isErr);
        logBox.scrollTop = logBox.scrollHeight;
      }

      function setPath(key, path, size, exists) {
        /* 文件不存在 = 当没选（用户 2026-10-17：适配器已经删了却还显示成已选） */
        var ok = !!path && exists !== false;
        paths[key] = ok ? path : '';
        var row = key === 'base' ? rowBase : rowAdapter;
        var name = ok ? (String(path).split(/[\\/]/).pop() || '—') : '—';
        row.nameEl.textContent = name;
        row.nameEl.classList.toggle('empty', !ok);   /* 未选 = 淡灰占位（同训练页 .tc-path.empty） */
        row.nameEl.title = ok ? path : '';
        row.sizeEl.textContent = (ok && size) ? tileSize(size) : '—';
        row.dot.className = 'lm-dot' + (ok ? ' ok' : '');
      }

      function save(extra) {
        var body = {};
        if (paths.base) body.base = paths.base;
        if (paths.adapter) body.adapter = paths.adapter;
        var stem = wname.stem();
        if (stem) body.out = stem;
        Object.keys(extra || {}).forEach(function (k) { body[k] = extra[k]; });
        return api.post('/api/train_lora_merge/save', body).then(function (d) {
          if (d && d.ok === false) log('保存失败：' + (d.error || '未知错误'), true);
        });
      }

      /* ---------------- 渲染 ---------------- */
      function render(d) {
        if (!d || d.ok === false) {
          log('读取失败：' + ((d && d.error) || '未知错误'), true);
          return;
        }
        CACHE = d;
        var f = d.facts || {};
        setPath('base', d.base, f.base && f.base.size_bytes, f.base ? !!f.base.exists : false);
        setPath('adapter', d.adapter, f.adapter && f.adapter.size_bytes, f.adapter ? !!f.adapter.exists : false);
        var merged = f.out || {};
        vBase.textContent = (f.base && f.base.exists) ? tileSize(f.base.size_bytes) : '缺失';
        vBase.classList.toggle('ok', !!(f.base && f.base.exists));
        vBase.classList.toggle('missing', !(f.base && f.base.exists));   /* 缺失=浅红（用户 2026-10-17） */
        vAdapter.textContent = (f.adapter && f.adapter.exists) ? tileSize(f.adapter.size_bytes) : '缺失';
        vAdapter.classList.toggle('ok', !!(f.adapter && f.adapter.exists));
        vAdapter.classList.toggle('missing', !(f.adapter && f.adapter.exists));   /* 缺失=浅红（用户 2026-10-17） */
        vOut.textContent = merged.exists ? tileSize(merged.size_bytes) : '未生成';
        vOut.classList.toggle('ok', !!merged.exists);
        vOut.classList.toggle('idle', !merged.exists);   /* 未生成=灰（用户 2026-10-17） */
      }

      function load() {
        return api.get('/api/train_lora_merge/config').then(render);
      }

      wname.input.addEventListener('change', function () { save({}); });
      refreshBtn.onclick = function () { if (!busy) load(); };

      runBtn.onclick = function () {
        if (busy) return;
        if (!wname.stem()) {
          wname.input.focus();
          return;
        }
        if (!paths.base) { log('请先选择基座权重。', true); return; }
        if (!paths.adapter) { log('请先选择 LoRA 适配器。', true); return; }
        busy = true;
        runBtn.disabled = true;
        vState.textContent = '合并中';
        vState.classList.remove('ok', 'warn', 'danger');
        log('正在合并：' + paths.adapter.split(/[\\/]/).pop() + ' + ' + paths.base.split(/[\\/]/).pop());
        api.post('/api/train_lora_merge/run', { base: paths.base, adapter: paths.adapter, out: wname.stem() })
          .then(function (d) {
            busy = false;
            runBtn.disabled = false;
            if (!d || d.ok === false) {
              vState.textContent = '失败';
              vState.classList.add('danger');
              log('合并失败：' + ((d && d.error) || '未知错误'), true);
              return;
            }
            vState.textContent = '已完成';
            vState.classList.add('ok');
            log('已合并 → ' + d.out + '（' + (d.size_human || '—') + '）');
            /* 基座不一致的提醒（用户 2026-10-22 排查）：能合，但效果通常比两个原件都差 */
            if (d.warning) log('提醒：' + d.warning, true);
            load();
          })['catch'](function (e) {
            busy = false;
            runBtn.disabled = false;
            vState.textContent = '失败';
            vState.classList.add('danger');
            log('合并失败：' + e, true);
          });
      };

      if (CACHE) { try { render(CACHE); } catch (e) {} }
      return load().then(function () {
        vState.textContent = '未启动';
        vState.classList.add('idle');   /* 未启动=灰（用户 2026-10-17） */
      });
    },
  });
})();
