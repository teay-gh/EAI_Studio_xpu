/* ============================================================
   EAI Studio · 渲染层逻辑
   宿主注入 window.eai（Electron preload）。缺失或 ?mock=1 时
   用内置假数据优雅降级，窗口按钮变为无操作。
   ============================================================ */
(function () {
  'use strict';

  var params = new URLSearchParams(location.search);
  var FORCE_MOCK = params.get('mock') === '1';
  var root = document.documentElement;

  /* ---------- 内置假数据（与 window.eai 契约同形） ---------- */
  function makeMock() {
    var theme = params.get('theme') === 'light' ? 'light' : 'dark';
    return {
      state: function () {
        return Promise.resolve({ theme: theme, gpu: true, gpuApplied: true, bridgePort: 5017, version: '2.2.0' });
      },
      setTheme: function (t) { theme = t; return Promise.resolve({ ok: true, theme: t }); },
      setGpu: function (on) { return Promise.resolve({ ok: true, gpu: on, needRestart: on !== true }); },
      restart: function () { return Promise.resolve(); },
      gpuStatus: function () {
        return Promise.resolve({
          enabled: true,
          disableRequested: false,
          feature: {
            gpu_compositing: 'enabled',
            webgl: 'enabled',
            webgl2: 'enabled',
            rasterization: 'enabled',
            video_decode: 'enabled'
          },
          gpuInfo: { vendor: 'Intel', device: 'Intel(R) Iris Xe Graphics', driver: '31.0.101' }
        });
      },
      overview: function () {
        return Promise.resolve({
          article: 128,
          response: 642,
          device: 'xpu',
          device_hint: 'Intel(R) Arc A770 · 16384MB',
          model: { ready: true, label: 'eproai_model.pt · 42.7 MB', size_mb: 42.7 }
        });
      },
      win: {
        minimize: function () {},
        toggleMax: function () {},
        close: function () {},
        isMax: function () { return Promise.resolve(false); }
      },
      onMaxChange: function () {},
      openExternal: function () {}
    };
  }

  var realEai = window.eai || null;
  var USING_MOCK = FORCE_MOCK || !realEai;
  var eai = USING_MOCK ? makeMock() : realEai;

  /* ---------- 导航数据（与 app.py NAV_GROUPS 对齐） ---------- */
  var NAV = [
    { group: '工作台', items: [['home', '总览'], ['repo', '仓库'], ['host', '托管']] },   /* 托管 2026-10-17 */
    { group: '数据中心', items: [['dataset', '数据集'], ['article', '语料蒸馏'], ['response', '响应蒸馏'], ['webcurate', '网页整理'], ['formatconv', '格式转换']] },   /* 数据集排第一（用户 2026-10-13）；网页整理 2026-10-17 */
    { group: '训练中心', items: [['train_pipeline', '训练流水线'], ['train_pretrain', '预训练'], ['train_continue', '继续预训练'], ['train_full', '指令微调'], ['train_lora', 'LoRA 微调'], ['train_lora_merge', 'LoRA 合并'], ['train_prune', '模型裁切'], ['train_quant', '量化']] },
    { group: '模型', items: [['chat', '对话验证'], ['model_convert', '模型转换'], ['export', '导出模型']] },
    { group: '回收站', items: [['recycle_dataset', '数据集'], ['recycle_weight', '权重']] },   /* 2026-10-15 拆两页（用户口径） */
    { group: '系统', items: [['settings', '全局设置']] }
  ];

  var LABEL = {};
  NAV.forEach(function (g) { g.items.forEach(function (it) { LABEL[it[0]] = it[1]; }); });
  window.__EAI_LABEL = LABEL;      /* 页面插件（pages/*.js）要用它拿中文名 */

  var state = { theme: 'dark', page: 'home', gpu: false, hostRun: false };

  /* ---------- 小工具 ---------- */
  function $(id) { return document.getElementById(id); }
  function $$(sel, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(sel));
  }
  function text(id, v) { var n = $(id); if (n) n.textContent = v; }
  function settle(v) { (v && v.then ? v : Promise.resolve(v)).catch(function () {}); }

  /* ---------- 主题 ---------- */
  function applyLocalTheme(t) {
    if (t !== 'light' && t !== 'dark') t = 'dark';
    state.theme = t;
    root.setAttribute('data-theme', t);
    syncSeg();
  }
  function syncSeg() {
    $$('#themeSeg .seg-opt').forEach(function (o) {
      var on = o.dataset.value === state.theme;
      o.classList.toggle('on', on);
      o.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    segThumb($('themeSeg'));                     /* 主题段选也有滑动块（和训练设备一致） */
  }
  function segThumb(seg) {
    var api = window.EAIPages && window.EAIPages.api;
    if (api && api.segThumb) api.segThumb(seg);
  }
  /* 点击分段控件：本地立即切换（无闪烁）+ 通知宿主 */
  function chooseTheme(t) {
    if (t !== 'light' && t !== 'dark') return;
    applyLocalTheme(t);
    try { settle(eai.setTheme(t)); } catch (e) { /* ignore */ }
    settle(bpost('/api/settings/app', { theme: t }));   /* 同时落盘 config.app.theme，助手读的就是它 */
  }

  /* ---------- 导航 / 侧栏 ---------- */
  function groupOf(key) {
    for (var i = 0; i < NAV.length; i++) {
      for (var j = 0; j < NAV[i].items.length; j++) {
        if (NAV[i].items[j][0] === key) return NAV[i].group;
      }
    }
    return NAV[0].group;
  }
  function setActiveGroup(group) {
    $$('.act-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.group === group);
    });
  }
  function renderNav(group) {
    if (window.__STEPS) window.__STEPS.push('renderNav:' + group);
    var data = NAV.filter(function (g) { return g.group === group; })[0] || NAV[0];
    $('sideHead').textContent = data.group;
    var host = $('sideNav');
    host.innerHTML = '';
    host.dataset.group = group;   /* 真渲染标记：静态 HTML 没有它，首帧判重才不会被 sideHead 的静态文本骗过去 */
    data.items.forEach(function (it) {
      /* 未完成功能默认隐藏（用户 2026-10-21）：托管、格式转换只在设置里开启后出现 */
      if (!state.unfinished && UNFINISHED_KEYS[it[0]]) return;
      var row = document.createElement('div');
      row.className = 'nav-row';
      row.dataset.key = it[0];
      row.setAttribute('role', 'button');
      row.tabIndex = 0;

      var bar = document.createElement('span');
      bar.className = 'bar';
      var lbl = document.createElement('span');
      lbl.className = 'nav-label';
      lbl.textContent = it[1];
      row.appendChild(bar);
      row.appendChild(lbl);

      row.addEventListener('click', function () { showPage(it[0]); });
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
          e.preventDefault();
          showPage(it[0]);
        }
      });
      host.appendChild(row);
    });
    markActiveNav();
    if (typeof applyTrainLock === 'function') applyTrainLock();
  }
  /* ---------- 托管运行 → 训练页灰化（用户 2026-10-18） ---------- */
  /* 训练页内容区遮罩（用户 2026-10-18：灰的是右侧内容区，左侧导航照常可点） */
  var hostMask = null;
  function syncHostMask() {
    var on = state.hostRun && String(state.page || '').indexOf('train_') === 0;
    var cur = $$('.page').filter(function (p) { return p.classList.contains('active'); })[0];
    if (cur) cur.classList.toggle('host-masked', on);
    if (on) {
      if (!hostMask || !hostMask.isConnected) {
        hostMask = document.createElement('div');
        hostMask.className = 'host-mask';
        hostMask.setAttribute('aria-hidden', 'true');
        $('content').appendChild(hostMask);
      }
    } else if (hostMask) {
      hostMask.remove();
      hostMask = null;
    }
  }
  function applyHostLock(on) {
    state.hostRun = on;
    document.body.classList.toggle('host-locked', on);
    syncHostMask();
  }
  function hostPoll() {
    if (document.hidden) return;
    settle(bget('/api/host/running').then(function (d) {
      applyHostLock(!!(d && d.running));
    }));
  }
  setInterval(hostPoll, 2500);
  setTimeout(hostPoll, 800);

  /* ---------- 训练中心互斥（用户 2026-10-19） ----------
     有一页在训练时，训练中心其余页的导航置灰、点不动；训练结束自动恢复。
     轮询放在外壳里，所以人不在训练页也照样锁得住。 */
  var trainRunKey = null;
  var trainPipeRun = false;      /* 流水线自己是否在跑（用户 2026-10-22） */
  /* 能不能进这一页（用户 2026-10-22 口径）：
     - 单页训练在跑：只有那一页能进，训练流水线跟着置灰、点不进；
     - 流水线在跑：流水线页 + 正在训练的那一页能进，其余照旧置灰；
     - 都没跑：全通。 */
  function trainNavOpen(k) {
    if (!trainRunKey) return true;
    if (k === trainRunKey) return true;
    if (k === 'train_pipeline') return trainPipeRun;
    return false;
  }
  function applyTrainLock() {
    $$('.nav-row').forEach(function (r) {
      var k = r.dataset.key || '';
      var lock = !!(trainRunKey && k.indexOf('train_') === 0 && !trainNavOpen(k));
      r.classList.toggle('locked', lock);
      if (lock) r.setAttribute('aria-disabled', 'true'); else r.removeAttribute('aria-disabled');
    });
  }
  /* 作业名 train:<stage>:<method> → 导航键（用户 2026-10-19）：
     后端 nav 字段为空时，前端自己算，别让「指令微调在训、其它页却不变灰」再犯。 */
  function navFromStage(stage, method) {
    if (stage === 'sft') { return method === 'lora' ? 'train_lora' : 'train_full'; }
    if (stage === 'pretrain') { return 'train_pretrain'; }
    if (stage === 'continue') { return 'train_continue'; }
    return '';
  }
  function trainPoll() {
    if (document.hidden) return;
    settle(bget('/api/train/running').then(function (d) {
      var k = (d && d.running) ? (d.nav || navFromStage(d.stage, d.method)) : null;
      var pipe = !!(d && d.pipeline);   /* 流水线线程在跑（此时 k 是它当前那一步的训练页） */
      if (k === trainRunKey && pipe === trainPipeRun) return;
      trainRunKey = k;
      trainPipeRun = pipe;
      applyTrainLock();
    }));
  }
  window.__EAI_TRAIN_KEY = function () { return trainRunKey; };
  setInterval(trainPoll, 2500);
  setTimeout(trainPoll, 900);

  function markActiveNav() {
    $$('.nav-row').forEach(function (r) {
      r.classList.toggle('active', r.dataset.key === state.page);
    });
  }
  function setGroup(group) {
    /* 同分组不重建导航：点当前分组的图标时别闪。判重用 sideNav.dataset.group（真渲染过才有），
       不能用 sideHead 文本 —— 静态 HTML 里就写着「工作台」，冷启动首帧会被误判成已渲染、导航整排空白。 */
    if ($('sideNav').dataset.group === group) { setActiveGroup(group); return; }
    renderNav(group);
    setActiveGroup(group);
  }

  /* ---------- 切页 ---------- */
  function showPage(key) {
    if (!LABEL[key]) return;
    /* 训练中：训练中心其他页点不动（用户 2026-10-19）；
       口径见 trainNavOpen（用户 2026-10-22：单页训练时流水线也进不去）。 */
    if (String(key).indexOf('train_') === 0 && !trainNavOpen(key)) return;
    var plug = window.EAIPages;
    var target = (key === 'home' || key === 'settings') ? key
               : (plug ? 'plug' : 'stub');
    /* 已经站在这一页 → 直接返回。原来无论点哪都重挂载一次，画面会闪（实测点当页也重建 24 处 DOM）。 */
    if (key === state.page) {
      var cur = $$('.page').filter(function (p) { return p.classList.contains('active'); })[0];
      var mounted = target !== 'plug' || $('plugRoot').childElementCount > 0;
      if (cur && cur.dataset.page === target && mounted) return;
    }
    /* 切到另一个插件页：旧内容不再先清空（用户 2026-10-21：切页别闪）——
       换页改由 EAIPages.mount 的「暂存盒建好再同一帧换掉」原子完成，
       旧内容在新页就绪前一直挂着，中间没有空白帧。 */
    state.page = key;
    $$('.page').forEach(function (p) {
      p.classList.toggle('active', p.dataset.page === target);
    });
    if (target === 'stub') setStub(key);
    /* 横条按「当前语言」重新落位（用户 2026-10-20）：切换语言后第一次打开某页时，
       滑块会先按上一门语言量出来的宽度画一帧再缩回去 —— 这里在页面显示后立刻纠正，
       纠正走 seg-thumb-init（无过渡），第一眼就是对的。 */
    function fitSeg() {
      if (window.I18N && window.I18N.reflowSegThumbs) window.I18N.reflowSegThumbs();
    }
    fitSeg();
    requestAnimationFrame(fitSeg);
    if (target === 'plug') {
      plug.mount(key, $('plugRoot')).then(function (ok) {
        fitSeg();
        if (ok) return;
        $('plugRoot').innerHTML = '';      /* 没迁的页面：清掉上一页残留内容 */
        $$('.page').forEach(function (p) {
          p.classList.toggle('active', p.dataset.page === 'stub');
        });
        setStub(key);                       /* 页面模块还没迁 → 空态卡 */
      });
    }

    syncHostMask();                     /* 进/出训练页时遮罩跟着换（用户 2026-10-18） */
    var g = groupOf(key);
    if (g !== $('sideNav').dataset.group) setGroup(g);
    markActiveNav();
    setActiveGroup(g);
    $('content').scrollTop = 0;

    if (key === 'home') refreshOverview();
    if (key === 'settings') refreshSettings();
  }
  function setStub(key) {
    var label = LABEL[key] || key;
    text('stubTitle', label);
    text('stubSub', '页面迁移中');
    text('stubCardTitle', label);
  }

  /* ---------- 总览数据 ---------- */
  function mkEl(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function refreshOverview(tryNo) {
    tryNo = tryNo || 0;
    /* 桥刚起来时主进程取不到 /api/overview（bridgePort 还没握手完），原来 catch 一吞就永远停在「—」，
       表现为「不点一下别的页、总览信息不出来」。照 loadSettingsExtra 的做法重试。 */
    var retry = function () {
      if (tryNo < 8) setTimeout(function () { refreshOverview(tryNo + 1); }, 900);
    };
    var p;
    try { p = eai.overview(); } catch (e) { p = Promise.resolve(null); }
    settle(
      (p && p.then ? p : Promise.resolve(p)).then(function (ov) {
        ov = ov || {};
        if (ov.error || (ov.storage == null && ov.article == null)) { retry(); return; }
        text('stArticle', fmtNum(ov.article));
        text('stResponse', fmtNum(ov.response));
        text('stDevice', ov.device == null ? '—' : String(ov.device));

        /* 文件占用瓷片（用户 2026-10-19：切页时这里的单位会抖）
           根因同托管页（2026-10-18 那次）：后端 human_size() 给的是「1.9 MB」（数字与单位间带空格），
           首帧先按原文写进去，80ms 后 api.fitStats() 又把同一段文本压成「1.9MB」→ 单位整体左跳一格。
           这里直接用 bytes 走 fitStats 同款口径（紧凑单位、无空格）输出，首帧即终态。 */
        var st = ov.storage || {};
        var stTxt = (st.bytes != null && window.__EAI_API && window.__EAI_API.tileSize)
          ? window.__EAI_API.tileSize(st.bytes)
          : (st.text ? String(st.text) : '—');
        text('stStorage', stTxt);

        var list = $('logList');
        if (list) {
          list.textContent = '';
          (ov.logs || []).forEach(function (r) {
            var row = mkEl('div', 'log-row');
            row.appendChild(mkEl('span', 'log-op', r.op == null ? '' : String(r.op)));
            row.appendChild(mkEl('span', 'log-detail', r.detail == null ? '' : String(r.detail)));
            row.appendChild(mkEl('span', 'log-time', r.time == null ? '' : String(r.time)));
            /* 行尾 ×：删除单条（用户 2026-10-16） */
            var del = mkEl('button', 'log-del');
            del.type = 'button';
            del.setAttribute('aria-label', '删除该条');
            del.textContent = '\u00d7';
            del.addEventListener('click', function () {
              postJson('/api/log/delete', {
                op: r.op == null ? '' : String(r.op),
                detail: r.detail == null ? '' : String(r.detail),
              }).then(function () { refreshOverview(0); });
            });
            row.appendChild(del);
            if (r.path) row.title = String(r.path);
            list.appendChild(row);
          });
          updLogFade();
          bindLogBox();
        }
      }).catch(retry)
    );
  }
  /* ---------- 总览日志：渐隐 / 删除 / 右键全部清除（用户 2026-10-16） ---------- */
  function postJson(path, body) {
    if (window.__EAI_API && window.__EAI_API.post) return window.__EAI_API.post(path, body);
    return Promise.resolve({ error: '桥未就绪' });
  }
  function clearAllLogs() {
    /* 全部清除需二次弹窗确认（用户 2026-10-16）；右键菜单与「一键清空」按钮共用（2026-10-21） */
    var notice = window.__EAI_API && window.__EAI_API.notice;
    if (notice) {
      notice('全部清除', ['确定清除全部日志？清除后不可恢复。'], {
        okText: '清除', cancelText: '取消',
        onOk: function () {
          postJson('/api/log/clear', {}).then(function () { refreshOverview(0); });
        },
      });
    } else if (window.confirm('确定清除全部日志？')) {
      postJson('/api/log/clear', {}).then(function () { refreshOverview(0); });
    }
  }
  function updLogFade() {
    var list = $('logList');
    if (!list) return;
    var wrap = list.parentNode;
    if (!wrap || !wrap.classList || !wrap.classList.contains('log-wrap')) return;
    var top = wrap.querySelector('.log-fade-top');
    var bot = wrap.querySelector('.log-fade-bot');
    var d = 22;   /* 一行高的渐隐距离，按距边滚动距离线性显隐 */
    if (top) top.style.opacity = list.scrollTop <= 0 ? 0 : Math.min(1, list.scrollTop / d);
    if (bot) {
      var rest = list.scrollHeight - list.clientHeight - list.scrollTop;
      bot.style.opacity = rest <= 0 ? 0 : Math.min(1, rest / d);
    }
  }
  function closeLogMenu() {
    var m = document.querySelector('.log-menu');
    if (m && m.parentNode) m.parentNode.removeChild(m);
    document.removeEventListener('mousedown', onLogMenuAway, true);
    document.removeEventListener('keydown', onLogMenuKey, true);
  }
  function onLogMenuAway(ev) {
    var m = document.querySelector('.log-menu');
    if (m && !m.contains(ev.target)) closeLogMenu();
  }
  function onLogMenuKey(ev) { if (ev.key === 'Escape') closeLogMenu(); }
  function bindLogBox() {
    var list = $('logList');
    if (!list || list.__bound) return;
    list.__bound = true;
    var clearBtn = $('logClearBtn');   /* 卡头右上角「一键清空」（用户 2026-10-21）：悬停显形同「明细」 */
    if (clearBtn && !clearBtn.__bound) {
      clearBtn.__bound = true;
      clearBtn.addEventListener('click', clearAllLogs);
    }
    list.addEventListener('scroll', updLogFade);
    var wrap = list.parentNode;
    (wrap || list).addEventListener('contextmenu', function (ev) {
      ev.preventDefault();
      closeLogMenu();
      var m = mkEl('div', 'log-menu');
      var it = mkEl('button', 'log-menu-item danger', '全部清除');
      it.type = 'button';
      it.addEventListener('click', function () {
        closeLogMenu();
        clearAllLogs();
      });
      m.appendChild(it);
      document.body.appendChild(m);
      var r = m.getBoundingClientRect();
      var px = Math.min(ev.clientX, window.innerWidth - r.width - 8);
      var py = Math.min(ev.clientY, window.innerHeight - r.height - 8);
      m.style.left = Math.max(8, px) + 'px';
      m.style.top = Math.max(8, py) + 'px';
      setTimeout(function () {
        document.addEventListener('mousedown', onLogMenuAway, true);
        document.addEventListener('keydown', onLogMenuKey, true);
      }, 0);
    });
  }
  bindLogBox();

  function fmtNum(v) {
    if (typeof v === 'number' && isFinite(v)) return String(v);
    return v == null ? '—' : String(v);
  }

  /* ---------- 设置：GPU ---------- */
  function setSwitch(on) {
    state.gpu = !!on;
    var sw = $('gpuSwitch');
    if (sw) {
      sw.classList.toggle('on', !!on);
      sw.setAttribute('aria-checked', on ? 'true' : 'false');
    }
  }
  /* 重启提示行显隐动效（用户 2026-10-15「此行消失时增加过渡动效」）：
     hidden 直接切是硬闪（display:none 不参与过渡）→ 先跑 高度/透明度/margin 过渡，210ms 后再落 hidden；
     消失=收拢+淡出、出现=反向展开；中途改状态就地反转（从当前高度接着跑）。
     「目标没变不重放」看 restartTarget 而不是 hidden —— 动画途中 hidden 还没落，
     只看 hidden 会把反转误判成同态返回、行卡在半截（实测）。 */
  var restartTarget = false;   /* 提示行当前的显隐「目标态」（HTML 初始 hidden） */
  function showRestart(show) {
    var el = $('restartHint');
    if (!el) return;
    if (el.__animTimer) { clearTimeout(el.__animTimer); el.__animTimer = null; }
    if (restartTarget === !!show) return;   /* 刷新/切页会反复调：目标没变不重放动画 */
    restartTarget = !!show;
    var T = 'height 180ms cubic-bezier(.2,.9,.3,1), padding 180ms cubic-bezier(.2,.9,.3,1), opacity 150ms ease, margin-top 180ms cubic-bezier(.2,.9,.3,1)';
    function settleRow() {
      el.style.height = ''; el.style.overflow = ''; el.style.opacity = '';
      el.style.marginTop = ''; el.style.padding = ''; el.style.transition = '';
      el.__animTimer = null;
    }
    /* 起手帧先「瞬时」回到量测态（transition:none 同任务内完成，不会被画出来），
       量完自然高再挂过渡跑目标值 —— 中途反转才不会从半截高度跳一下。
       padding 一起动：border-box 下 height:0 会被 padding 撑到 20px 地板，收拢会卡在半空。 */
    el.style.transition = 'none';
    el.style.height = ''; el.style.opacity = ''; el.style.marginTop = ''; el.style.padding = '';
    if (show) el.hidden = false;     /* 先现身再量：display:none 量出来是 0 */
    var h = el.offsetHeight;
    el.style.overflow = 'hidden';
    if (show) {
      el.style.height = '0px';
      el.style.padding = '0px 12px';
      el.style.opacity = '0';
      el.style.marginTop = '0px';
      el.getBoundingClientRect();          /* 强制回流：不给这帧会和目标值合成一步、没动画 */
      el.style.transition = T;
      el.style.height = h + 'px';
      el.style.padding = '10px 12px';
      el.style.opacity = '1';
      el.style.marginTop = '12px';
      el.__animTimer = setTimeout(settleRow, 210);
    } else {
      el.style.height = h + 'px';
      el.style.padding = '10px 12px';
      el.style.opacity = '1';
      el.style.marginTop = '12px';
      el.getBoundingClientRect();
      el.style.transition = T;
      el.style.height = '0px';
      el.style.padding = '0px 12px';
      el.style.opacity = '0';
      el.style.marginTop = '0px';
      el.__animTimer = setTimeout(function () {
        el.hidden = true;
        settleRow();
      }, 210);
    }
  }
  /* 核显加速开关的初值（用户 2026-10-22）：原来只有 refreshSettings() 里查 state().gpu，
     而它只在「进设置页」时才跑 —— 首帧是 HTML 里写死的 off，IPC 回来再翻成 on，
     于是每次开机第一次进设置都会看到开关「先关后开」闪一下。
     启动时就查一次（和主题共用一个 state 调用），进页时值已经是对的，不再跳。 */
  function syncGpuSwitchAtBoot() {
    var p;
    try { p = eai.state(); } catch (e) { p = Promise.resolve(null); }
    return (p && p.then ? p : Promise.resolve(p)).then(function (st) {
      if (st && typeof st.gpu === 'boolean') {
        if (typeof st.gpuApplied === 'boolean') state.gpuApplied = st.gpuApplied;
        setSwitch(st.gpu);
      }
    }).catch(function () {});
  }
  function refreshSettings() {
    // 开关初值：state().gpu 优先；重启提示 = 期望态 ≠ 运行态（跨页面持久，用户 2026-10-15）
    var p;
    try { p = eai.state(); } catch (e) { p = Promise.resolve(null); }
    settle(
      (p && p.then ? p : Promise.resolve(p)).then(function (st) {
        if (st && typeof st.gpu === 'boolean') {
          if (typeof st.gpuApplied === 'boolean') state.gpuApplied = st.gpuApplied;
          setSwitch(st.gpu);
          showRestart(typeof state.gpuApplied === 'boolean' && st.gpu !== state.gpuApplied);
        }
      }).catch(function () {})
    );
    syncSeg();                                   /* 进设置页时把主题滑块重新量一次（隐藏时量不到尺寸） */
    loadSettingsExtra();
  }
  /* loadGpuStatus/renderGpuStatus 已删（用户 2026-10-14）：每次开关顺带查 GPU 全量信息
     （eai:gpu-status → app.getGPUInfo('complete')）会卡 GPU 进程约 0.65~0.85s（实测掉帧
     650/783/817/850ms），而结果自从状态文案删掉后根本没人渲染 —— 纯白白卡。 */
  function toggleGpu() {
    var on = !state.gpu;
    setSwitch(on);
    var p;
    try { p = eai.setGpu(on); } catch (e) { p = Promise.resolve(null); }
    settle(
      (p && p.then ? p : Promise.resolve(p)).then(function (res) {
        showRestart(!!(res && res.needRestart));
      }).catch(function () {
        showRestart(typeof state.gpuApplied === 'boolean' && state.gpu !== state.gpuApplied);
      })
    );
  }

  /* ---------- 未完成功能（用户 2026-10-21）：默认关 = 隐藏 托管 / 格式转换 ---------- */
  var UNFINISHED_KEYS = { host: 1, formatconv: 1 };
  function unfinishedSet(on) {
    state.unfinished = !!on;
    var sw = $('unfinishedSwitch');
    if (sw) {
      sw.classList.toggle('on', !!on);
      sw.setAttribute('aria-checked', on ? 'true' : 'false');
      sw.dataset.on = on ? '1' : '0';
    }
    var hc = $('hostSettingCard');
    if (hc) hc.hidden = !on;      /* 托管的设置卡跟着功能一起隐（功能都没了不该留配置项） */
    var cur = $('sideNav') && $('sideNav').dataset.group;
    if (cur) renderNav(cur);      /* 导航里去掉/恢复 托管、格式转换 */
    if (!on && state.page && UNFINISHED_KEYS[state.page]) showPage('home');
  }
  function toggleUnfinished() {
    var on = state.unfinished !== true;
    unfinishedSet(on);                                    /* 先动再存，失败回滚 */
    settle(bpost('/api/settings/app', { unfinished: on }).then(function (r) {
      if (!r || r.ok === false) {
        unfinishedSet(!on);
        setSt('projStatus', '保存失败：' + ((r && r.error) || '未知错误'), 'err');
      }
    }));
  }

  /* ---------- 设置：蒸馏数据集默认引入 ---------- */
  function dsAutoSet(on) {
    var sw = $('dsAutoSwitch');
    if (!sw) return;
    sw.classList.toggle('on', !!on);
    sw.setAttribute('aria-checked', on ? 'true' : 'false');
    sw.dataset.on = on ? '1' : '0';
  }
  function toggleDsAuto() {
    var sw = $('dsAutoSwitch');
    if (!sw) return;
    var on = sw.dataset.on !== '1';
    dsAutoSet(on);                                   /* 先动再存，失败回滚 */
    settle(bpost('/api/settings/app', { auto_distill_dirs: on }).then(function (r) {
      if (!r || r.ok === false) {
        dsAutoSet(!on);
        setSt('projStatus', '保存失败：' + ((r && r.error) || '未知错误'), 'err');
      }
    }));
  }

  /* 托管：强制托管训练完成（用户 2026-10-17）——勾上后托管 AI 只提示风险、不中止；卡死照样停 */
  function hostForceSet(on) {
    var sw = $('hostForceSwitch');
    if (!sw) return;
    sw.classList.toggle('on', !!on);
    sw.setAttribute('aria-checked', on ? 'true' : 'false');
    sw.dataset.on = on ? '1' : '0';
  }
  function toggleHostForce() {
    var sw = $('hostForceSwitch');
    if (!sw) return;
    var on = sw.dataset.on !== '1';
    hostForceSet(on);                                /* 先动再存，失败回滚 */
    settle(bpost('/api/settings/app', { host_force_complete: on }).then(function (r) {
      if (!r || r.ok === false) {
        hostForceSet(!on);
        setSt('projStatus', '保存失败：' + ((r && r.error) || '未知错误'), 'err');
      }
    }));
  }

  /* ---------- 设置页：工程 / 默认 API / 训练设备（真写 config.json，走 Python 桥） ---------- */
  /* ---------------- AI 助手：顶栏按钮 → 顶部挂载、不可拖动的面板 ---------------- */
  var AI = { open: false, busy: false, since: 0, timer: null, wait: null, booted: false, ov: null,
             minH: 32, maxH: 130 };  /* 发送栏高度：默认 32~130 = 6 行封顶（用户 2026-10-14：最多显示 6 行，超出滚动、滚动条隐藏） */

  function aiFitInput() {
    var i = $('aiInput');
    if (!i) return;
    i.style.height = 'auto';
    i.style.maxHeight = AI.maxH + 'px';
    /* box-sizing:border-box 下 height 还含 2px 边框 → 要补 bw，否则最后一行被切掉 2px */
    var bw = i.offsetHeight - i.clientHeight;
    i.style.height = Math.max(Math.min(i.scrollHeight + bw, AI.maxH), AI.minH) + 'px';
  }

  function aiDot(cls, tip) {
    var want = 'ai-dot' + (cls ? ' ' + cls : '');
    ['aiDot'].forEach(function (id) {               /* 面板头部的状态点 */
      var d = $(id);
      if (!d) return;
      d.className = want;
      d.title = tip || '';
    });
  }

  function aiMsg(role, text) {
    var box = $('aiMsgs');
    if (!box) return null;
    var el = document.createElement('div');
    el.className = 'ai-msg ai-' + role;
    el.textContent = String(text == null ? '' : text);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return el;
  }

  function aiState() {
    return bget('/api/assistant/state').then(function (s) {
      if (!s || s.ok === false) { aiDot('red', '桥未就绪'); return; }
      if (s.available) {
        var src = s.source || '';
        aiDot('green', '可用 · ' + src + ((s.model && src.indexOf(s.model) < 0) ? ' · ' + s.model : ''));
      } else {
        aiDot('red', '不可用：' + (s.error || '未配置 API（设置页「默认 API」填 base_url / api_key）'));
      }
    });
  }

  /* 助手改了参数就把界面同步过来：主题、跳页、以及设置/总览刷新 */
  /* 改配置/换主题/切页面这类动作**不直接执行**：先在对话里挂一张确认卡，用户点「执行」才落地。
     用户 2026-10-13：「都询问需要的话帮我打开，我都没说需要直接帮我开了」。只读动作(get_config)的结果照常播报。 */
  function aiApplyActions(p) {
    (p.results || []).forEach(function (r) { aiMsg('act', r); });
    var acts = p.pending || [];
    if (p.needs_confirm && acts.length) { aiConfirm(acts); return; }
    aiApplyUi(p);
  }

  function aiApplyUi(p) {
    var ui = p.ui || {};
    if (ui.theme) {
      applyLocalTheme(ui.theme);
      try { settle(eai.setTheme(ui.theme)); } catch (e) {}
      settle(bpost('/api/settings/app', { theme: ui.theme }));   /* 助手自己换的主题也要落盘 */
    }
    if (ui.page) { try { showPage(ui.page); } catch (e) {} }
    if ((p.changed || []).length) {
      try { refreshSettings(); } catch (e) {}
      settle(loadSettingsExtra());
      try { refreshOverview(); } catch (e) {}
    }
  }

  var AI_PAGE_NAME = { home: '总览', repo: '仓库', article: '语料蒸馏', response: '响应蒸馏', dataset: '数据集', webcurate: '网页整理',
    train_pretrain: '预训练', train_continue: '继续预训练', train_full: '指令微调', train_lora: 'LoRA 微调',
    train_prune: '模型裁切', train_quant: '量化',
    chat: '对话验证', settings: '设置' };

  function aiActionLabel(a) {
    var tool = String((a && a.tool) || '');
    /* 页名可能写在 key / value / path 任一个字段里（后端三个都认），这里跟着兜住，别把 JSON 原样贴出来 */
    var k = (a && (a.key || a.value || a.path)) || '';
    if (tool === 'navigate') return '打开「' + (AI_PAGE_NAME[k] || k || '') + '」页';
    if (tool === 'set_theme') return a.name === 'dark' ? '主题换成深色' : '主题换成浅色';
    if (a && a.path) return '把 ' + a.path + ' 设为 ' + JSON.stringify(a.value);
    return JSON.stringify(a);
  }

  function aiConfirm(acts) {
    var box = $('aiMsgs');
    if (!box) return;
    var wrap = document.createElement('div');
    wrap.className = 'ai-confirm';
    acts.forEach(function (a) {
      var line = document.createElement('div');
      line.className = 'ai-confirm-line';
      line.textContent = aiActionLabel(a);
      wrap.appendChild(line);
    });
    var btns = document.createElement('div');
    btns.className = 'ai-confirm-btns';
    var go = document.createElement('button');
    go.type = 'button'; go.className = 'ai-cbtn ai-cbtn-go'; go.textContent = '执行';
    var no = document.createElement('button');
    no.type = 'button'; no.className = 'ai-cbtn'; no.textContent = '取消';
    btns.appendChild(go); btns.appendChild(no);
    wrap.appendChild(btns);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    function mark(txt) {
      btns.textContent = '';
      var s = document.createElement('span');
      s.className = 'ai-confirm-done';
      s.textContent = txt;
      btns.appendChild(s);
    }
    go.addEventListener('click', function () {
      go.disabled = true; no.disabled = true;
      bpost('/api/assistant/apply', {}).then(function (r) {
        var ok = !!(r && r.ok !== false);
        /* 成功就不显示结果小字（「navigate → dataset」这类），要的是干净；只有失败才把 ✗ 那行摆出来 */
        if (!ok) { (r && r.results || []).forEach(function (t) { aiMsg('act', t); }); }
        if (ok) { aiApplyUi(r); mark('已执行'); }
        else { mark('执行失败'); }
      })['catch'](function () { mark('执行失败'); });
    });
    /* 取消就只是不动，不弹任何提示 */
    no.addEventListener('click', function () { go.disabled = true; no.disabled = true; mark('已取消'); });
  }

  function aiPoll() {
    bget('/api/assistant/status?since=' + AI.since).then(function (s) {
      if (!s || s.ok === false) return;
      AI.since = s.next || AI.since;
      (s.events || []).forEach(function (e) {
        var p = e.payload || {};
        if (e.kind === 'reply') {
          var txt = p.text || '';
          if (AI.wait) { AI.wait.className = 'ai-msg ai-assistant'; AI.wait.textContent = txt; AI.wait = null; }
          else { aiMsg('assistant', txt); }
        } else if (e.kind === 'error') {
          if (AI.wait) { AI.wait.className = 'ai-msg ai-err'; AI.wait.textContent = String(p); AI.wait = null; }
          else { aiMsg('err', String(p)); }
        } else if (e.kind === 'actions') {
          aiApplyActions(p);
        } else if (e.kind === 'done') {
          aiFinish();
        }
      });
    });
  }

  function aiFinish() {
    if (AI.timer) { clearInterval(AI.timer); AI.timer = null; }
    if (AI.wait) { AI.wait.className = 'ai-msg ai-sys'; AI.wait.textContent = '（没有返回内容）'; AI.wait = null; }
    AI.busy = false;
    setSendMode(false);
    aiNext();                   /* 排队接力：答完自动发下一条（用户 2026-10-15） */
  }

  /* ---------- AI 消息排队 + 中止 + 直接发送（用户 2026-10-15） ----------
   * 回答中输入 → 排队（「N 条排队」列表，可 × 移除）；答完/中止后接力下一条。
   * 行内「直接发送」= 插队立即发出（先中止当前回答）。发送钮回答中变「中止」。 */
  var AIQ = [];                       /* 排队消息 */
  var SEND_SVG = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 19.2V6"/><path d="M6.7 11.3 12 6l5.3 5.3"/></svg>';
  var STOP_SVG = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="7" y="7" width="10" height="10" rx="1.6"/></svg>';

  function setSendMode(busy) {
    var b = $('aiSend');
    if (!b) return;
    b.classList.toggle('stop', !!busy);
    b.disabled = false;                       /* 中止钮永远可点 */
    b.innerHTML = busy ? STOP_SVG : SEND_SVG;
    b.title = busy ? '中止' : '发送';
    b.setAttribute('aria-label', b.title);
  }

  function aiQueueRender() {
    var box = $('aiQueue'), list = $('aiQueueList'), cnt = $('aiQueueCount');
    if (!box || !list || !cnt) return;
    cnt.textContent = AIQ.length + ' 条排队';
    box.hidden = !AIQ.length;
    list.textContent = '';
    AIQ.forEach(function (t, idx) {
      var row = document.createElement('div');
      row.className = 'ai-qrow';
      var sp = document.createElement('span');
      sp.className = 'ai-qtext';
      sp.textContent = t;
      sp.title = t;
      var go = document.createElement('button');
      go.type = 'button'; go.className = 'ai-qsend'; go.textContent = '直接发送';
      go.addEventListener('click', function () { aiSendDirect(idx); });
      var x = document.createElement('button');
      x.type = 'button'; x.className = 'ai-qx'; x.textContent = '×';
      x.title = '移出队列';
      x.addEventListener('click', function () { AIQ.splice(idx, 1); aiQueueRender(); });
      row.appendChild(sp); row.appendChild(go); row.appendChild(x);
      list.appendChild(row);
    });
  }

  function aiEnqueue(text) {
    AIQ.push(text);
    aiQueueRender();
  }

  function aiAbort(done) {
    bpost('/api/assistant/stop', {});
    if (AI.wait) { AI.wait.className = 'ai-msg ai-sys'; AI.wait.textContent = '（已中止）'; AI.wait = null; }
    if (AI.timer) { clearInterval(AI.timer); AI.timer = null; }
    AI.busy = false;
    setSendMode(false);
    if (done) done();
  }

  function aiSendDirect(idx) {
    var t = AIQ.splice(idx, 1)[0];
    if (t == null) return;
    aiQueueRender();
    if (AI.busy) aiAbort(function () { aiPost(t); });
    else aiPost(t);
  }

  function aiNext() {
    if (AI.busy || !AIQ.length) return;
    var t = AIQ.shift();
    aiQueueRender();
    aiPost(t);
  }

  function aiPost(text) {
    aiMsg('user', text);
    aiChipsOut();               /* 开始对话 → 开场提示淡出消失（用户要求） */
    AI.busy = true;
    AI.since = 0;
    setSendMode(true);
    AI.wait = aiMsg('wait', '正在回答…');
    bpost('/api/assistant/send', { text: text }).then(function (r) {
      if (!r || r.ok === false) {
        if (AI.wait) {
          AI.wait.className = 'ai-msg ai-err';
          AI.wait.textContent = '发送失败：' + ((r && r.error) || '未知');
          AI.wait = null;
        }
        AI.busy = false;
        setSendMode(false);
        aiState();
        aiNext();
        return;
      }
      AI.timer = setInterval(aiPoll, 700);
      aiPoll();
    });
  }

  function aiSend() {
    var i = $('aiInput');
    if (!i) return;
    var text = (i.value || '').trim();
    if (!text) return;
    i.value = '';
    i.style.height = '';
    if (AI.busy) { aiEnqueue(text); return; }   /* 回答中 → 排队（用户 2026-10-15） */
    aiPost(text);
  }

  /* 推荐指令池：按「当前页 + 小页签」出（用户 2026-10-17），文案表在 ai_prompts.js；
     训练页额外把该页 config 里的语料 / 初始·基座权重体量取回来，拼成参数建议。 */
  var AI_TRAIN_PAGES = {
    train_pretrain: 1, train_continue: 1, train_full: 1, train_lora: 1,
    train_lora_merge: 1, train_prune: 1, train_quant: 1,
  };

  function aiPageCtx() {
    /* 页面键要用 state.page：插件页在 DOM 里统一挂在 .page[data-page="plug"] 下，
       读 dataset.page 永远只会拿到 "plug"（用户 2026-10-17：提示要跟着当前页走） */
    var page = state.page || 'home';
    var pageEl = document.querySelector('.page.active') || document.body;
    var P = window.AIPrompts;
    var sub = (P && P.activeSub(pageEl, page)) || '';
    var ovP;
    try { ovP = eai.overview(); } catch (e) { ovP = null; }
    ovP = (ovP && ovP.then ? ovP : Promise.resolve(ovP || {}));
    ovP = ovP.catch(function () { return {}; });
    var szP = Promise.resolve({});
    if (P && AI_TRAIN_PAGES[page]) {
      szP = bget('/api/' + page + '/config').then(function (d) {
        return P.pickSizes(page, d || {});
      }).catch(function () { return {}; });
    }
    return Promise.all([ovP, szP]).then(function (a) {
      var ov = a[0];
      if (!ov || ov.error) ov = {};
      return {
        page: page, sub: sub, ov: ov, sizes: a[1] || {},
        hour: new Date().getHours(),
        theme: document.documentElement.getAttribute('data-theme') || 'light',
      };
    });
  }

  function aiSuggestPool(ctx) {
    var P = window.AIPrompts;
    ctx = ctx || {};
    var rest = (P && P.forPage) ? P.forPage(ctx) : ['帮我看看文章样本够不够训练'];
    var theme = (P && P.themeChip) ? P.themeChip(ctx) : null;
    return { theme: theme, rest: rest };
  }

  function aiPick(arr, n) {                  /* 随机不重复取 n 条 */
    var a = arr.slice(), out = [];
    while (out.length < n && a.length) out.push(a.splice(Math.floor(Math.random() * a.length), 1)[0]);
    return out;
  }

  function aiChipEl(t) {
    var c = document.createElement('button');
    c.type = 'button';
    c.className = 'ai-chip';
    c.textContent = t;
    c.addEventListener('click', function () {
      var i = $('aiInput');
      if (!i) return;
      i.value = t;
      i.focus();
      aiFitInput();
    });
    return c;
  }

  /* 开场提示：按当前页取 3 条（夜间/白天且命中概率时第一条是主题）+「换一批」（点一下填进输入框） */
  function aiChips(ctx) {
    var box = $('aiMsgs');
    if (!box) return;
    var go = function (c) {
      var old = box.querySelector('.ai-chips');
      if (old && old.parentNode) old.parentNode.removeChild(old);
      var g = aiSuggestPool(c);
      var wrap = document.createElement('div');
      wrap.className = 'ai-chips';
      var more = document.createElement('button');
      more.type = 'button';
      more.className = 'ai-chips-more';
      more.textContent = moreLabel();
      more.addEventListener('click', function () { aiChipsRefresh(); });
      var head = g.theme ? [g.theme] : [];
      head.concat(aiPick(g.rest, 3 - head.length)).forEach(function (t) { wrap.appendChild(aiChipEl(t)); });
      wrap.appendChild(more);
      box.appendChild(wrap);
      box.scrollTop = box.scrollHeight;
    };
    if (ctx) { go(ctx); return; }
    aiPageCtx().then(function (c) {
      AI.ctx = c;
      go(c);
    });
  }

  /* 药丸文案（用户 2026-10-22）：示例问题按当前语言生成，按钮同理 */
  function moreLabel() {
    return (window.I18N && window.I18N.lang() === 'en') ? 'More' : '换一批';
  }

  /* 切语言后重画药丸：英文药丸是原生英文（不是翻译产物），restore 换不回中文 */
  function aiChipsRelang() {
    var w = document.querySelector('#aiMsgs .ai-chips');
    if (!w || w.classList.contains('out')) return;
    var more = w.querySelector('.ai-chips-more');
    if (more) more.textContent = moreLabel();
    var ctx = AI.ctx;
    if (!ctx) { aiChips(); return; }
    var g = aiSuggestPool(ctx);
    var head = g.theme ? [g.theme] : [];
    [].slice.call(w.querySelectorAll('.ai-chip')).forEach(function (c) { c.parentNode.removeChild(c); });
    head.concat(aiPick(g.rest, 3 - head.length)).forEach(function (t) { w.insertBefore(aiChipEl(t), more); });
  }
  if (window.I18N && window.I18N.on) {
    window.I18N.on(function () { aiChipsRelang(); });
  }

  /* 换一批：旧药丸先淡出，再换上新的一批（同一个容器，动画接着走）。
     每次重算上下文（页可能已换、体量可能已变），算不出来就用上一次的。 */
  function aiChipsRefresh() {
    var w = document.querySelector('#aiMsgs .ai-chips');
    if (!w || w.classList.contains('out')) return;
    var go = function (c) {
      if (!w.parentNode) return;
      var more = w.querySelector('.ai-chips-more');
      [].slice.call(w.querySelectorAll('.ai-chip')).forEach(function (c2) { c2.parentNode.removeChild(c2); });
      var g = aiSuggestPool(c);
      var head = g.theme ? [g.theme] : [];
      head.concat(aiPick(g.rest, 3 - head.length)).forEach(function (t) { w.insertBefore(aiChipEl(t), more); });
      w.classList.remove('swap');
    };
    w.classList.add('swap');
    aiPageCtx().then(function (c) {
      AI.ctx = c;
      setTimeout(function () { go(c); }, 150);
    });
  }

  /* 开始对话后把推荐药丸收掉：淡出 + 位移 + 收高度同时进行（一段式，200ms 内完成），260ms 后移除节点 */
  function aiChipsOut() {
    var w = document.querySelector('#aiMsgs .ai-chips');
    if (!w || w.classList.contains('out')) return;
    w.style.maxHeight = w.offsetHeight + 'px';     /* 先坐实起始高度 */
    w.style.overflow = 'hidden';
    void w.offsetHeight;                           /* 强制一次重排，保证起始值生效（比 rAF 更确定，不会同帧被合并） */
    w.style.maxHeight = '0px';
    w.classList.add('out');                        /* 从这一刻起 opacity/transform/padding/高度同时过渡 */
    setTimeout(function () { if (w.parentNode) w.parentNode.removeChild(w); }, 260);
  }

  function aiToggle(force) {
    AI.open = (typeof force === 'boolean') ? force : !AI.open;
    var p = $('aiPanel'), b = $('aiBtn');
    if (p) { p.hidden = false; p.classList.toggle('ai-on', AI.open); }
    if (b) b.setAttribute('aria-expanded', AI.open ? 'true' : 'false');
    if (!AI.open) return;
    /* 窗口一律回默认右上角展开（顶栏 AI 键 / ✕ 是唯一入口） */
    if (p) { p.style.left = ''; p.style.top = ''; p.style.right = ''; }
    aiState();
    var i = $('aiInput');
    if (i) i.focus();
    if (AI.booted) return;
    AI.booted = true;
    bget('/api/assistant/history').then(function (h) {
      var msgs = (h && h.messages) || [];
      if (!msgs.length) { aiChips(); return; }
      msgs.forEach(function (m) { aiMsg(m.role === 'user' ? 'user' : 'assistant', m.content); });
    });
  }

  function aiHistOpen(open) {
    var pop = $('aiHistPop'), btn = $('aiHist');
    if (!pop) return;
    if (open) {
      var head = document.querySelector('.ai-head');
      pop.style.top = ((head ? head.offsetHeight : 40) + 2) + 'px';   /* 上沿落在标题栏/对话区分界线的下方 */
    }
    pop.classList.toggle('on', !!open);        /* 用 class 走过渡，不用 hidden（display:none 会吃掉动画） */
    if (btn) btn.classList.toggle('on', !!open);
  }

  /* 标题超字处理照仓库页：右缘渐变 + 悬停向右滚（用户 2026-10-20） */
  function aiScrollText(text) {
    var A = window.EAIPages && window.EAIPages.api;
    if (A && A.scrollText) { try { return A.scrollText(String(text || ''), 'ai-hist-tt', ''); } catch (e) { /* 退回纯文本 */ } }
    return document.createTextNode(String(text || ''));
  }

  /* 打开某条面板历史：整段装进面板（不再跳到对话验证页，用户 2026-10-20） */
  function aiOpenConv(id) {
    bpost('/api/assistant/open', { id: id }).then(function (r) {
      if (!r || r.ok === false) { aiMsg('assistant', '打开历史失败：' + ((r && r.error) || '未知错误')); return; }
      var box = $('aiMsgs');
      if (box) box.innerHTML = '';
      AI.since = 0;
      var msgs = r.messages || [];
      msgs.forEach(function (m) { aiMsg(m.role === 'user' ? 'user' : 'assistant', m.content); });
      if (!msgs.length) aiChips();
    })['catch'](function () { /* 网络失败不弹，列表还在 */ });
  }

  function aiHistRender() {
    var list = $('aiHistList');
    if (!list) return;
    list.textContent = '';
    /* 面板自己的会话（source=assistant），不是对话验证页的历史（用户 2026-10-20：两边串了） */
    bget('/api/assistant/convs').then(function (d) {
      if (!d || d.ok === false || d.error) {
        list.appendChild(mkEl('div', 'ai-hist-empty', '历史读取失败'));
        return;
      }
      var items = d.items || [];
      if (!items.length) { list.appendChild(mkEl('div', 'ai-hist-empty', '暂无历史会话')); return; }
      items.forEach(function (it) {
        /* 一行 = [可点开的按钮] + [悬停出现的 ✕]；外面包一层因为 <button> 里不能再放 <button> */
        var wrap = document.createElement('div');
        wrap.className = 'ai-hist-item';
        var row = document.createElement('button');
        row.type = 'button';
        row.className = 'ai-hist-row' + (it.current ? ' on' : '');
        var t = mkEl('span', 't');
        t.appendChild(aiScrollText(it.title || '新会话'));
        row.appendChild(t);
        row.appendChild(mkEl('span', 'm', (it.count || 0) + ' 条 · ' + (it.updated || it.created || '')));
        row.addEventListener('click', function () {
          aiHistOpen(false);
          aiOpenConv(it.id);                    /* 面板里直接切过去 */
        });
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'ai-hist-del';
        del.title = '删除这条会话';
        del.setAttribute('aria-label', '删除这条会话');
        del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"'
                      + ' stroke-linecap="round" aria-hidden="true"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>';
        del.addEventListener('click', function (e) {
          e.stopPropagation();                  /* 别顺手把这条会话打开 */
          e.preventDefault();
          if (del.disabled) return;
          del.disabled = true;
          /* 面板会话就是工程 chats/<id>.json，走同一个删除接口（用户 2026-10-20：每条历史可删） */
          bpost('/api/chat/delete', { id: it.id }).then(function (r) {
            if (r && r.ok === false) { del.disabled = false; return; }
            if (it.current) {                   /* 删掉的正是当前这条：面板一起清空 */
              var box = $('aiMsgs');
              if (box) box.innerHTML = '';
              AI.since = 0;
              bpost('/api/assistant/clear', {}).then(function () { aiChips(); })['catch'](function () { aiChips(); });
            }
            aiHistRender();                     /* 删掉后重列（列表空了会显示「暂无历史会话」） */
          })['catch'](function () { del.disabled = false; });
        });
        wrap.appendChild(row);
        wrap.appendChild(del);
        list.appendChild(wrap);
      });
    });
  }

  function aiInit() {
    var b = $('aiBtn');
    if (!b) return;
    var p = $('aiPanel');          /* 拖动面板 / 拖动输入框高度都要用 */
    b.addEventListener('click', function () { aiToggle(); });
    var hb = $('aiHist');
    if (hb) hb.addEventListener('click', function (e) {
      e.stopPropagation();
      var pop = $('aiHistPop');
      var willOpen = !!(pop && !pop.classList.contains('on'));
      if (willOpen) aiHistRender();
      aiHistOpen(willOpen);
    });
    document.addEventListener('click', function (e) {
      var pop = $('aiHistPop');
      if (!pop || !pop.classList.contains('on')) return;
      if (!pop.contains(e.target) && e.target !== $('aiHist')) aiHistOpen(false);
    });
    var cb = $('aiClose');                      /* ✕ 收起按钮（空判断保底） */
    if (cb) cb.addEventListener('click', function () { aiToggle(false); });
    $('aiNew').addEventListener('click', function () {
      bpost('/api/assistant/clear', {}).then(function () {
        var box = $('aiMsgs');
        if (box) box.innerHTML = '';
        AI.since = 0;
        aiChips();
      });
    });
    $('aiSend').addEventListener('click', function () {
      if (AI.busy) aiAbort(aiNext);            /* 回答中点发送钮 = 中止（用户 2026-10-15） */
      else aiSend();
    });
    $('aiQueueHead').addEventListener('click', function () {
      var q = $('aiQueue');
      if (q) q.classList.toggle('collapsed');
    });
    var i = $('aiInput');
    i.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); aiSend(); }
    });
    i.addEventListener('input', aiFitInput);

    /* ---- 拖动面板：按住标题栏移动；关掉再打开回默认位置（不落盘） ---- */
    var head = document.querySelector('.ai-head');
    if (head) head.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || (e.target.closest && e.target.closest('button'))) return;
      var r = p.getBoundingClientRect();
      /* 锚右边（用户 2026-10-22）：面板始终以右缘为基准，缩小时左缘往右收、右缘不动，
         不被改成 left 锚定（那会让缩放变成「从右往左」）。 */
      var right0 = Math.max(4, Math.round(window.innerWidth - r.right));
      var dy = e.clientY - r.top;
      p.style.left = 'auto';
      p.style.right = right0 + 'px';
      p.style.top = Math.round(r.top) + 'px';
      p.classList.add('dragging');
      var move = function (ev) {
        var w = p.offsetWidth;
        var rt = right0 - (ev.clientX - e.clientX);
        rt = Math.min(Math.max(rt, 4), Math.max(4, window.innerWidth - w - 4));
        var y = Math.min(Math.max(ev.clientY - dy, 44), Math.max(44, window.innerHeight - 40));
        p.style.right = Math.round(rt) + 'px';
        p.style.top = Math.round(y) + 'px';
      };
      var up = function () {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.style.userSelect = '';
        p.classList.remove('dragging');
      };
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
      return false;
    });

    /* ---- 拖动发送栏高度：往上拖变高、往下拖变矮（夹在 32 ~ 6 行 130px） ---- */
    var grip = $('aiGrip');
    if (grip) grip.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      var h0 = AI.maxH, y0 = e.clientY;
      var move = function (ev) {
        var h = Math.round(Math.min(Math.max(h0 - (ev.clientY - y0), 32), 130));   /* 6 行封顶 */
        AI.minH = AI.maxH = h;
        aiFitInput();
      };
      var up = function () {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.style.userSelect = '';
      };
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
      return false;
    });

    /* ---- 窗口大小（用户 2026-10-15）：右下角手柄自由拖，界限 = 宽 380~950（默认~2.5 倍）、
       高 336~624（默认 70%~130%）；标题栏「大小切换」钮在默认/调整后尺寸间一键来回 ---- */
    var AIW = {};
    function clampInt(v, lo, hi) { return Math.round(Math.min(Math.max(v, lo), hi)); }
    function setSizeMode(customOn) {
      AIW.mode = customOn ? 'custom' : 'default';
      var sb = $('aiSize');
      if (sb) sb.classList.toggle('on', !!customOn);
    }
    function applySize(w, h, customOn) {
      p.style.width = w + 'px';
      p.style.height = h + 'px';
      /* 锚右：宽度变化只动左缘。若面板被拖到贴左，右侧距至少留 4px（宽度上限已按窗口夹过） */
      if (p.style.right && p.style.right !== 'auto') {
        var rt = parseFloat(p.style.right) || 0;
        if (window.innerWidth - rt - w < 4) p.style.right = Math.max(4, window.innerWidth - w - 4) + 'px';
      }
      setSizeMode(!!customOn);
    }
    (function () {
      var cs = window.getComputedStyle(p);
      var w0 = parseFloat(cs.width) || 380;
      var h0 = parseFloat(cs.height) || 480;
      AIW.defW = w0; AIW.defH = h0;
      AIW.minW = w0;                          /* 宽最小 = 当前默认宽 */
      AIW.maxW = Math.round(w0 * 2.5);        /* 宽最大 = 默认 2.5 倍 */
      AIW.minH = Math.round(h0 * 0.7);        /* 高最小 = 默认 70% */
      AIW.maxH = Math.round(h0 * 1.3);        /* 高最大 = 默认 130% */
      try {
        var saved = JSON.parse(localStorage.getItem('eaiAiWin') || 'null');
        if (saved && saved.w && saved.h) {
          AIW.custom = { w: clampInt(saved.w, AIW.minW, AIW.maxW), h: clampInt(saved.h, AIW.minH, AIW.maxH) };
          applySize(AIW.custom.w, AIW.custom.h, true);
        } else {
          setSizeMode(false);
        }
      } catch (e2) { setSizeMode(false); }
    })();

    var sb = $('aiSize');
    if (sb) sb.addEventListener('click', function () {
      if (!AIW.custom) return;                /* 还没拖过 = 没有「调整后」尺寸，点了无变化 */
      if (AIW.mode === 'custom') applySize(AIW.defW, AIW.defH, false);
      else applySize(AIW.custom.w, AIW.custom.h, true);
    });

    var rz = $('aiRz');
    if (rz) rz.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      var r = p.getBoundingClientRect();
      var x0 = e.clientX, y0 = e.clientY, w0 = r.width, h0 = r.height;
      p.classList.add('dragging');
      var move = function (ev) {
        /* 手柄在左下角（右缘固定）：往右拖 = 变窄（左边往右缩） */
        applySize(
          clampInt(w0 - (ev.clientX - x0), AIW.minW, AIW.maxW),
          clampInt(h0 + ev.clientY - y0, AIW.minH, AIW.maxH),
          true);
      };
      var up = function () {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.style.userSelect = '';
        p.classList.remove('dragging');
        AIW.custom = { w: p.offsetWidth, h: p.offsetHeight };
        try { localStorage.setItem('eaiAiWin', JSON.stringify(AIW.custom)); } catch (e2) {}
        setSizeMode(true);
      };
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
      return false;
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && AI.open) aiToggle(false);
    });

  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', aiInit);
  else aiInit();

  function withState(fn) {
    var p;
    try { p = eai.state(); } catch (e) { p = null; }
    return (p && p.then ? p : Promise.resolve(p || {})).then(fn || function () {});
  }
  function bridgeBase(st) {
    return (st && st.bridgePort) ? ('http://127.0.0.1:' + st.bridgePort) : null;
  }
  /* 桥请求：现取 bridgePort（不依赖模块级 state 是否被赋值）；桥没起来时返回 {ok:false} 而不抛。 */
  function bfetch(path, body) {
    return withState(function (st) {
      var b = bridgeBase(st);
      if (!b) return { ok: false, error: '桥未就绪（Python 桥没起来）' };
      /* 桥鉴权（用户 2026-10-23）：每个请求带 X-EAI-Token，bridge.py 不认就 403 */
      var hdr = { 'X-EAI-Token': (st && st.bridgeToken) || '' };
      var opt = (body === undefined) ? { headers: hdr } : {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-EAI-Token': hdr['X-EAI-Token'] },
        body: JSON.stringify(body || {})
      };
      return fetch(b + path, opt).then(function (r) { return r.json(); })
        .catch(function (e) { return { ok: false, error: String(e) }; });
    });
  }
  function bget(path) { return bfetch(path); }
  function bpost(path, body) { return bfetch(path, body); }
  function setSt(id, text, cls) {
    var el = $(id);
    if (!el) return;
    if (el.classList.contains('dot')) {     /* 圆点态：说明只进悬停提示，界面上不出现文字 */
      el.classList.remove('ok', 'err', 'warn');
      if (cls) el.classList.add(cls);
      el.title = text || '';
      return;
    }
    el.textContent = text || '';
    el.className = 'set-status' + (cls ? ' ' + cls : '');
  }
  function markDevice(v) {
    $$('#deviceSeg .seg-opt').forEach(function (b) {
      var on = b.dataset.value === v;
      b.classList.toggle('on', on);            /* .seg-opt.on 才是真正的选中态 */
      b.setAttribute('aria-checked', String(on));
    });
  }
  /* 段选右边：CPU / 核显 / GPU（CUDA 独显）/ 内存 —— 数据来自桥的 /api/settings/device */
  function fillSysInfo(n) {
    if (!$('sysInfo')) return;
    bget('/api/settings/device').then(function (d) {
      if (!d || !d.ok) {
        if ((n || 0) < 3) { setTimeout(function () { fillSysInfo((n || 0) + 1); }, 1500); }
        return;
      }
      var set = function (id, v) { var e = $(id); if (e) e.textContent = v || '—'; };
      set('siCpu', d.cpu);
      set('siIgpu', d.igpu || '无');
      set('siGpu', d.gpu || '无');
      set('siMem', d.mem_gb ? (d.mem_gb + ' GB') : '');
    }).catch(function () {
      if ((n || 0) < 3) { setTimeout(function () { fillSysInfo((n || 0) + 1); }, 1500); }
    });
  }
  function loadSettingsExtra(tryNo) {
    if (!$('deviceSeg')) return;               /* 设置页锚点：工程卡已移除，不能再拿 projName 判空（否则整页不加载） */
    loadProjectFoot();   /* 打开设置页就顺手校验一次连接状态：桥挂了时也要刷（上面失败分支会 return） */
    setSt('projStatus', '读取中…');
    bget('/api/settings/state').then(function (s) {
      if (!s || !s.ok) {
        /* 桥比窗口晚起 2~4 秒：拿不到就等一等重来，否则整页停在初始值（空 URL / — / 设备段全禁） */
        var n = tryNo || 0;
        if (n < 8) { setTimeout(function () { loadSettingsExtra(n + 1); }, 900); return; }
        setSt('projStatus', '桥未就绪，设置项暂不可用', 'err');
        return;
      }
      var api = s.api || {};                   /* 工程卡已从设置页移除，工程管理在侧栏页脚 */
      $('apiBase').value = api.base_url || '';
      /* 有密钥就显示固定 32 个点（不可复制）；点一下才清空进入输入态 */
      apiKeyLock(!!(api.assistant_key_set || api.all_have_key));
      /* 圆点现在表示"连通性"：灰=未测 / 绿=通 / 红=失败，说明收进 title */
      setSt('apiStatus', '未测试连通性 · 点「测试」验证（'
        + (api.profiles || []).length + ' 个模型配置'
        + (api.all_have_key ? '都已有密钥' : '部分缺密钥') + '）');
      dsAutoSet(s.auto_distill_dirs !== false);        /* 初值：默认开启 */
      var dsSw = $('dsAutoSwitch');
      if (dsSw) dsSw.onclick = toggleDsAuto;
      hostForceSet(!!s.host_force_complete);           /* 托管：强制训练完成，默认关 */
      var hfSw = $('hostForceSwitch');
      if (hfSw) hfSw.onclick = toggleHostForce;
      unfinishedSet(s.unfinished === true);            /* 未完成功能：默认关=隐藏托管/格式转换 */
      var ufSw = $('unfinishedSwitch');
      if (ufSw) ufSw.onclick = toggleUnfinished;
      buildEdition = String((s.app || {}).edition || '').toLowerCase();   /* xpu / cuda，先定版本再算可用性 */
      markDevice((s.device || {}).value || 'auto');
      fillSysInfo(0);                          /* 段选右边的本机硬件信息 */
      devRefresh();                            /* 设备段选可用性：桥刚起来那次 devInit 的探测可能被吞了，每次刷设置都补一次 */
      $('aboutName').textContent = (s.app || {}).name || '—';
      /* 版本号后面跟版本形态（用户 2026-10-23）：0.1.0 xpu */
      var _ver = (s.app || {}).version || '—';
      $('aboutVer').textContent = buildEdition ? (_ver + ' ' + buildEdition) : _ver;
      loadProjectFoot();                       /* 侧栏页脚同步工程名/路径 */
    }).catch(function (e) {
      console.warn('设置页取数失败：', e);
    });
  }
  /* ---------- APIKEY 栏：已保存=固定点数且不可复制；点一下清空变输入态 ---------- */
  var API_KEY_DOTS = 32;

  function apiKeyLock(hasKey) {
    var k = $('apiKey');
    if (!k) return;
    if (hasKey) {
      k.value = new Array(API_KEY_DOTS + 1).join('•');
      k.readOnly = true;
      k.dataset.locked = '1';
      k.placeholder = '';
      k.title = '已保存（点一下可输入新密钥）';
    } else {
      k.value = '';
      k.readOnly = false;
      k.dataset.locked = '';
      k.placeholder = '粘贴新密钥';
      k.title = '';
    }
  }

  /* 表单里这把 key 是否算"用户新输入的"（点数占位不算，避免把点当成密钥存进去） */
  function apiKeyValue() {
    var k = $('apiKey');
    if (!k || k.dataset.locked) return '';
    return (k.value || '').trim();
  }

  function initApiKeyField() {
    var k = $('apiKey');
    if (!k) return;
    var unlock = function () {
      if (!k.dataset.locked) return;
      k.value = '';
      k.readOnly = false;
      k.dataset.locked = '';
      k.placeholder = '输入新密钥';
      k.title = '';
    };
    k.addEventListener('mousedown', unlock);
    k.addEventListener('focus', unlock);
    /* 锁着时不允许复制/剪切（免得把"点"当值带走，也避免任何形式的回显） */
    ['copy', 'cut'].forEach(function (ev) {
      k.addEventListener(ev, function (e) {
        if (k.dataset.locked) e.preventDefault();
      });
    });
  }

  function initSettingsExtra() {
    if (!$('deviceSeg')) return;               /* 设置页锚点：不再用 projName 判断（工程卡已移除） */
    initApiKeyField();

    $('apiSave').addEventListener('click', function () {
      var base = $('apiBase').value.trim();
      var key = apiKeyValue();
      if (!base && !key) { setSt('apiStatus', '两项都为空，无需保存', 'err'); return; }
      setSt('apiStatus', '保存中…');
      bpost('/api/settings/api', { base_url: base, api_key: key }).then(function (r) {
        if (r && r.ok) {
          var n = ((r.changed || {}).api_key || []).length + ((r.changed || {}).base_url || []).length;
          if (key) apiKeyLock(true);          /* 存好了就切回固定点数、不可复制 */
          /* 不回填表单：回填会拿 profile 里的旧地址盖掉你刚填的，看起来像「URL 被改成了别的」 */
          if (base) $('apiBase').value = base;
          setSt('apiStatus', '✓ 已保存（' + n + ' 处变更）' + (r.backup ? ' · 已留备份' : ''), 'ok');
        } else {
          setSt('apiStatus', '✗ ' + ((r && r.error) || '保存失败'), 'err');
        }
      });
    });

    var apiTesting = false;
    $('apiTest').addEventListener('click', function () {
      /* 不动按钮样式（disabled 会把 .btn 变 55% 透明 → 一点就闪）：只用一个忙标志挡重复点击 */
      if (apiTesting) return;
      var base = $('apiBase').value.trim();
      var key = apiKeyValue();
      apiTesting = true;
      setSt('apiStatus', '测试中…（真发请求）');
      bpost('/api/settings/api/test', { base_url: base, api_key: key }).then(function (r) {
        apiTesting = false;
        if (r && r.ok) {
          setSt('apiStatus', '连通 ✓ ' + r.route + ' · HTTP ' + r.http + ' · ' + r.ms + 'ms'
            + (r.models ? ' · 模型 ' + r.models + ' 个' : '')
            + (r.has_key ? '' : ' · 未带密钥'), 'ok');
        } else {
          setSt('apiStatus', '失败 ✗ ' + ((r && (r.error || ('HTTP ' + r.http))) || '未知'), 'err');
        }
      }).catch(function (e) {
        apiTesting = false;
        setSt('apiStatus', '失败 ✗ ' + e, 'err');
      });
    });
  }
  initSettingsExtra();

  /* ---------- 侧栏页脚：工程块（状态用圆点颜色，不写文字描述） ---------- */
  var footTries = 0;
  function setDot(cls, tip) {
    var d = $('projFootDot');
    if (!d) return;
    d.className = 'proj-dot' + (cls ? ' ' + cls : '');   /* 灰(无类) / warn 黄 / ok 绿 / err 红 */
    d.title = tip || '连接状态';
  }
  function loadProjectFoot() {
    if (!$('projFootName')) return;
    bfetch('/api/settings/state').then(function (s) {
      if (!s || !s.ok) {
        if (footTries < 6) {
          // 桥比窗口后起来：黄点 + 重试，页面上不出现「连接中/未连接」这类文字
          footTries += 1;
          setDot('warn', '连接中：等待 Python 桥（第 ' + footTries + ' 次重试）');
          setTimeout(loadProjectFoot, 1500);
        } else {
          setDot('err', '未能连接 Python 桥：' + ((s && s.error) || '桥未就绪'));
        }
        return;
      }
      footTries = 0;
      var pj = s.project || {};
      var chosen = (s.project_chosen !== false);        /* 显式关掉工程后 chosen=false（用户 2026-10-17） */
      /* 2026-10-23（用户）：当前工程文件夹被删/被移走 → 页脚照实写出来，别悄悄显示成别的工程 */
      if (pj.missing) {
        $('projFootName').textContent = pj.name || '—';
        $('projFootPath').textContent = pj.root || '—';
        $('projFootPath').title = pj.root || '';
        var footM = document.querySelector('.side-foot');
        if (footM) footM.classList.add('no-proj');
        setDot('err', '当前工程文件夹不存在：' + (pj.root || ''));
        setNoProject(true);
        return;
      }
      $('projFootName').textContent = chosen ? (pj.name || '—') : '—';   /* 只显示工程名，不带前缀 */
      $('projFootPath').textContent = chosen ? (pj.root || '—') : '—';
      $('projFootPath').title = chosen ? (pj.root || '') : '';
      var foot = document.querySelector('.side-foot');
      if (foot) foot.classList.toggle('no-proj', !chosen);
      if (chosen) setDot('ok', '已连接 Python 桥 · 当前工程 ' + (pj.name || '—'));
      else setDot('', '未选择工程');
      /* 用户还没明确选过工程（recent_projects 为空）→ 内容变灰 + 弹出建/导入（用户 2026-10-17）。
         后端有 projects/default 兜底，所以不用判断 root 有没有值。 */
      setNoProject(s.project_chosen === false);
    });
  }
  /* ---------- 未选工程：内容变灰 + 弹窗（用户 2026-10-17） ----------
     只在 /api/settings/state 明确说没有工程时打开；桥没起来/读失败保持关闭。 */
  function setNoProject(on) {
    var c = $('content'), m = $('npMask');
    if (c) c.classList.toggle('no-project', !!on);
    if (m) m.hidden = !on;
    if (on) npStatus('');
  }
  function npStatus(text, isErr) {
    var s = $('npStatus');
    if (!s) return;
    s.textContent = text || '';
    s.classList.toggle('err', !!isErr);
  }
  /* 工程切换/新建成功后：把当前页重新挂一遍（之前没工程时页面是空的/报错的） */
  function refreshAfterProject() {
    if (state.page === 'home') { refreshOverview(); return; }
    if (state.page === 'settings') { refreshSettings(); return; }
    var pr = $('plugRoot');
    if (pr && window.EAIPages && state.page) {
      try { window.EAIPages.mount(state.page, pr); } catch (e) { /* 忽略：下次点导航再挂 */ }
    }
  }

  function pickFolder(title) {
    try {
      if (window.eai && eai.pickFolder) return eai.pickFolder({ title: title, buttonLabel: '选择' })
        .catch(function () { return null; });
    } catch (e) { /* 回落：没有原生选择器就直接用文本框 */ }
    return Promise.resolve(null);
  }
  function applyProject(api, folderTitle) {
    pickFolder(folderTitle).then(function (p) {
      if (!p) return;                                    /* 取消：状态点保持原色 */
      var verb = (api.indexOf('create') > 0) ? '创建' : '导入';
      setDot('warn', '正在' + verb + '工程…');
      npStatus('正在' + verb + '工程…');
      bpost(api, { path: p }).then(function (r) {
        if (r && r.ok) {
          setDot('ok', '已' + (api.indexOf('create') > 0 ? '新建并切换' : '切换') + '到 ' + p);
          npStatus('');
          loadProjectFoot();                           /* 先刷新页脚工程名 */
          /* 工程切换：让各页清掉上一个工程的模块级缓存（用户 2026-10-18） */
          try { if (window.__EAI_API && window.__EAI_API.emitProjectChange) window.__EAI_API.emitProjectChange(); } catch (e) {}
          setTimeout(refreshAfterProject, 80);         /* 再重挂当前页 */
        } else {
          setDot('err', (r && r.error) || '操作失败');    /* 红点，原因在悬停提示里 */
          npStatus((r && r.error) || '操作失败', true);   /* 弹窗里也要看得见原因 */
        }
      });
    });
  }
  function initProjectFoot() {
    if (!$('projFootName')) return;
    setDot('warn', '连接中…');
    $('projSwitchBtn').addEventListener('click', function () {
      applyProject('/api/settings/project', '选择工程文件夹');
    });
    $('projNewBtn').addEventListener('click', function () {
      applyProject('/api/settings/project/create', '选择新工程的文件夹');
    });
    /* 关闭当前工程（用户 2026-10-17）：鼠标移进工程块才出现 ×；关掉后内容变灰 + 弹建/导入 */
    var pcBtn = $('projCloseBtn');
    if (pcBtn) pcBtn.addEventListener('click', function () {
      setDot('warn', '正在关闭工程…');
      bpost('/api/settings/project/close', {}).then(function (r) {
        if (!r || r.ok === false) {
          setDot('err', (r && r.error) || '关闭失败');
          npStatus((r && r.error) || '关闭工程失败', true);
          return;
        }
        /* 关闭工程后页面信息不留残留（用户 2026-10-21）：训练页权重命名、Loss 表等工程信息
           全部清掉 —— 2026-10-17「底层留着当前页」的口径作废，直接整页重载，按无工程空态重建。 */
        loadProjectFoot();
        setTimeout(function () { location.reload(); }, 60);
      });
    });
    /* 未选工程弹窗（用户 2026-10-17）：创建 = 选一个空文件夹建新工程；导入 = 选已有工程文件夹 */
    var npC = $('npCreate'), npI = $('npImport');
    if (npC) npC.addEventListener('click', function () {
      applyProject('/api/settings/project/create', '选择新工程的文件夹');
    });
    if (npI) npI.addEventListener('click', function () {
      applyProject('/api/settings/project', '选择工程文件夹');
    });
    loadProjectFoot();
  }
  initProjectFoot();

  /* ---------- 训练设备段选：滑动块 + 未识别到设备的「滑过去→提示→滑回来」 ---------- */
  var devCur = 'auto';
  function devOpts() { return $$('#deviceSeg .seg-opt'); }
  function devMove(v, animate) {
    var t = $('deviceThumb');
    var el = devOpts().filter(function (b) { return b.dataset.value === v; })[0];
    if (!t || !el) return;
    if (animate === false) t.classList.add('seg-thumb-init');
    t.style.width = el.offsetWidth + 'px';
    t.style.transform = 'translateX(' + el.offsetLeft + 'px)';
    if (animate === false) {
      void t.offsetWidth;                          /* 强制回流：首次定位别被过渡成动画 */
      t.classList.remove('seg-thumb-init');
    }
  }
  function devMark(v, animate) {
    devCur = v;
    devOpts().forEach(function (b) {
      var on = b.dataset.value === v;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    });
    devMove(v, animate);
  }
  var devToastTimer = null;
  function devToast(text) {
    var el = $('devToast');
    if (!el) return;
    el.textContent = text;
    el.classList.add('show');
    if (devToastTimer) clearTimeout(devToastTimer);
    devToastTimer = setTimeout(function () {        /* 3 秒后消失（内联提示，不是弹窗） */
      el.classList.remove('show');
      devToastTimer = null;
    }, 3000);
  }
  var devProbed = false;                             /* 设备探测（要 import torch，2~4s）是否已返回 */
  /* 版本形态（用户 2026-10-23）：EAI 以后分 XPU 版 / CUDA 版两个安装包，
     本版本只认自己的加速器；点另一个就是「请切换 xxx 版本」，不写 config。 */
  var buildEdition = '';                             /* 'xpu' / 'cuda'，来自 /api/settings/state 的 app.edition */
  function devL(zh, en) {
    return (window.I18N && window.I18N.lang() === 'en') ? en : zh;
  }
  function devEditionOk(v) {
    if (v !== 'cuda' && v !== 'xpu') return true;
    if (!buildEdition) return true;                  /* 还没读到版本：先按硬件可用性判断 */
    return v === buildEdition;
  }
  function devEditionTip(v) {
    return devL('请切换 ' + v.toUpperCase() + ' 版本', 'Switch to the ' + v.toUpperCase() + ' build');
  }
  function devSetAvail(cuda, xpu, probed) {
    devProbed = probed;
    devOpts().forEach(function (b) {
      var v = b.dataset.value;
      /* 保守默认：探测没回来之前，cuda/xpu 一律按「不可选」处理，避免把本机没有的设备写进 config */
      var editionOk = devEditionOk(v);
      var ok = (v === 'auto' || v === 'cpu') ? true
             : (editionOk && (probed ? (v === 'cuda' ? cuda : xpu) : false));
      b.dataset.avail = ok ? '1' : '0';
      b.classList.toggle('disabled', !ok);
      b.setAttribute('aria-disabled', String(!ok));
      var what = (v === 'xpu') ? '支持 XPU 的核显' : (v + ' 设备');
      b.title = ok ? ''
        : (!editionOk ? devEditionTip(v)
                      : (probed ? ('本机未识别到' + what) : ('正在读取' + what + '信息')));
    });
  }
  var devRefreshNo = 0;
  function devRefresh() {
    if (!devOpts().length) return;
    devSetAvail(false, false, false);                /* 先按“未知”收住 */
    bget('/api/settings/device').then(function (d) {
      if (!d || !d.ok) {                             /* 桥还没起来：退避重试，别让段选一直灰着 */
        if (devRefreshNo < 6) { devRefreshNo += 1; setTimeout(devRefresh, 600); }
        return;
      }
      devRefreshNo = 0;
      if (d.known === false) {                       /* 本机还没探测过：四个设备都放开，真解析交给训练/对话 */
        devSetAvail(true, true, true);
        return;
      }
      devSetAvail(!!d.cuda, !!d.xpu, true);
    });
    bget('/api/settings/state').then(function (s) {
      var v = (s && s.ok && s.device && s.device.value) ? s.device.value : devCur;
      devMark(v, false);
    });
  }
  function devClick(v, b) {
    var prev = devCur;
    if (b.dataset.avail === '1') {                   /* 只有明确“可用”才允许写入 */
      devMark(v, true);
      bpost('/api/settings/device', { value: v }).then(function (r) {
        /* 成功不弹提示（段选高亮本身就是回执）；只有失败才用内联提示 */
        if (!r || !r.ok) { devToast('✗ ' + ((r && r.error) || '保存失败')); }
      });
      return;
    }
    /* 不可选：滑块先滑过去 → 提示（内联、3s 消失）→ 再滑回原来的选项（同一非线性缓动） */
    var editionMismatch = !devEditionOk(v);
    devMark(v, true);
    setTimeout(function () {
      var what = (v === 'xpu') ? '支持 XPU 的核显' : (v + ' 设备');
      devToast(editionMismatch ? devEditionTip(v)
        : (devProbed ? ('本机未识别到' + what + '，不可选；已保持 ' + prev)
                     : ('正在探测本机设备，' + v + ' 暂不可选；已保持 ' + prev)));
      devMark(prev, true);
    }, 320);
  }
  function devInit() {
    if (!devOpts().length) return;
    devSetAvail(false, false, false);                /* 开机就先收住，别等探测 */
    devOpts().forEach(function (b) {
      /* 捕获阶段拦下：initSettingsExtra 里那个旧监听没有可用性判断，
         会直接把 cuda/xpu 写进 config.json，必须让它不执行 */
      b.addEventListener('click', function (ev) {
        ev.stopImmediatePropagation();
        ev.preventDefault();
        devClick(b.dataset.value, b);
      }, true);
    });
    var gearBtn = $('actSettings') || $$('.act-foot .act-btn')[0];   /* 左下角「设置」图标：打开设置页时刷新可用性 + 定位滑块 */
    if (gearBtn) gearBtn.addEventListener('click', function () { setTimeout(devRefresh, 80); });
    /* 切语言后重算：可用性提示（title / 内联 toast）是拼出来的中文，词典翻不到（用户 2026-10-23） */
    if (window.I18N && window.I18N.on) window.I18N.on(function () { devRefresh(); });
    devRefresh();
  }
  devInit();

  /* ---------- 窗口控制 ---------- */
  function winCall(name) {
    try {
      var f = eai.win && eai.win[name];
      if (typeof f === 'function') f.call(eai.win);
    } catch (e) { /* mock / 无操作 */ }
  }
  function setMax(m) {
    root.classList.toggle('maximized', !!m);
    var b = $('winMax');
    if (b) b.classList.toggle('restore', !!m);
  }
  function readMax() {
    try {
      var p = eai.win && eai.win.isMax && eai.win.isMax();
      if (p && p.then) p.then(setMax).catch(function () {});
      else if (typeof p === 'boolean') setMax(p);
    } catch (e) { /* ignore */ }
  }
  function bindWindow() {
    $('winMin').addEventListener('click', function () { winCall('minimize'); });
    $('winMax').addEventListener('click', function () {
      winCall('toggleMax');
      setTimeout(readMax, 60);
    });
    $('winClose').addEventListener('click', function () { winCall('close'); });
    try {
      if (typeof eai.onMaxChange === 'function') {
        eai.onMaxChange(function (m) { setMax(m); });
      }
    } catch (e) { /* ignore */ }
    readMax();
  }

  /* ---------- 启动 ---------- */
  function init() {
      if (window.__STEPS) window.__STEPS.push('init');
      // 初始主题：URL ?theme 优先，其次宿主 state().theme
    var urlTheme = params.get('theme');
    if (urlTheme === 'light' || urlTheme === 'dark') {
      applyLocalTheme(urlTheme);
    } else {
      var p;
      try { p = eai.state(); } catch (e) { p = Promise.resolve(null); }
      settle(
        (p && p.then ? p : Promise.resolve(p)).then(function (st) {
          if (st && st.theme) applyLocalTheme(st.theme);
        }).catch(function () {})
      );
    }

    bindWindow();
    if (window.__STEPS) window.__STEPS.push('init.bindWindow');

    /* 设置页的开关初值提前到启动时取（用户 2026-10-22：进设置时核显加速会从关跳到开） */
    settle(syncGpuSwitchAtBoot());

    /* 未完成功能（用户 2026-10-21）：启动即按落盘值决定 托管/格式转换 显隐（默认关）。
       复用 loadSettingsExtra：桥晚起时自带重试，别让开关停在初值 */
    settle(loadSettingsExtra(0));

    /* 图标点击：跳到该分组「上次看的页」；本次会话没看过（含刚启动）就用默认页 */
    var GROUP_DEFAULT = { '工作台': 'home', '数据中心': 'dataset', '训练中心': 'train_pretrain',
                          '模型': 'chat', '回收站': 'recycle_dataset', '系统': 'settings' };
    var LAST = {};      /* 只存内存不落盘：软件关闭重开后自然回到上面这组默认页 */
    $$('.act-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var g = b.dataset.group;
        LAST[groupOf(state.page)] = state.page;   /* 离开前先记下原分组最后看的那页 */
        showPage(b.dataset.page || LAST[g] || GROUP_DEFAULT[g] || 'home');
        setGroup(g);
      });
    });
    if (window.__STEPS) window.__STEPS.push('init.acts');
    var gear = $('settingsBtn');                        /* 页脚设置按钮已移除；留着此分支不报错 */
    if (gear) gear.addEventListener('click', function () { if (typeof devRefresh === 'function') devRefresh(); });

    $$('#themeSeg .seg-opt').forEach(function (o) {
      o.addEventListener('click', function () { chooseTheme(o.dataset.value); });
    });
    if (window.__STEPS) window.__STEPS.push('init.gpu');
    $('gpuSwitch').addEventListener('click', toggleGpu);
    $('restartBtn').addEventListener('click', function () {
      try { settle(eai.restart()); } catch (e) { /* ignore */ }
    });

    if (window.__STEPS) window.__STEPS.push('init.setGroup');
    /* 回收站/设置钮与右侧工程区顶边对齐（用户 2026-10-21）：两钮同高时均分右侧块高度，
       .act-foot 钉在底部 → 总高相等即顶部边框齐平 */
    (function alignFoot() {
      var sideFoot = document.querySelector('.side-foot');
      var btns = document.querySelectorAll('.act-foot .act-btn');
      if (!sideFoot || !btns.length) return;
      var h = sideFoot.getBoundingClientRect().height / btns.length;
      btns.forEach(function (b) { b.style.height = h + 'px'; });
    })();
    setGroup(groupOf('home'));
    showPage('home');

    /* 设置页数据开机就预取：桥比窗口晚起 1~2s，这里正好用重试循环等它。
       等用户点开设置时字段/硬件行已经填好，页面是「一按就出来」而不是先空一下。 */
    loadSettingsExtra();

    // 预览辅助：?page=settings 直达某页（仅供截图自查，不影响正常使用）
    var want = params.get('page');
    if (want && LABEL[want] && want !== 'home') showPage(want);
  }

  if (window.__STEPS) window.__STEPS.push('tail:' + document.readyState);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
