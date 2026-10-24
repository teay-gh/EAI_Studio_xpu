/* ============================================================
   EAI_Studio 页面插件注册表（必须在 app.js 之前加载）
   ------------------------------------------------------------
   一个页面 = 三个文件（互不干扰，可多人并行）：
     renderer/pages/<key>.js    注册页面（本文件提供的 API）
     renderer/pages/<key>.css   该页样式（自动 <link>，可选）
     bridge_api/<key>.py        该页后端接口（bridge.py 自动发现，可选）
   <key> 用导航键：home / repo / article / response / dataset /
                   train_pretrain / train_continue / train_full / train_lora / chat
   ------------------------------------------------------------
   pages/<key>.js 的写法（照抄改）：

     EAIPages.register('article', {
       title: '语料蒸馏',                    // 页头大标题
       sub: '把原始语料蒸馏成问答样本',        // 页头副标题
       mock: {                              // 只给 ?mock=1 预览（Edge 截图）用
         '/api/article/list': { total: 0, items: [] },
       },
       mount(root, api) {                   // root 进来自动清空；同步/异步都行
         var hero = api.hero('语料蒸馏', '把原始语料蒸馏成问答样本');
         var card = api.card('文章列表', '共 0 条');
         root.appendChild(hero);
         root.appendChild(card);
         return api.get('/api/article/list').then(function (d) {
           card.querySelector('.card-sub').textContent = '共 ' + (d.total || 0) + ' 条';
           var list = api.el('div', 'stack');
           (d.items || []).forEach(function (it) {
             list.appendChild(api.el('div', 'card-sub', it.title || String(it)));
           });
           card.body.appendChild(list);
         });
       },
       unmount(root) {},                    // 可选：切走时清理定时器
     });

   可用的 api：
     api.hero(title, sub)          页头元素（.hero，20pt 粗标题 + 12pt 副标题）
     api.card(title, sub)          卡片元素（.card；card.body 是内容容器）
     api.el(tag, cls, text)        建元素
     api.esc(s)                    HTML 转义
     api.get(path)                 Promise：走 bridge（mock 模式下取本页 mock[path]）
     api.post(path, body)          Promise
     api.state                     { theme, bridgePort }
     api.base                      bridge 根地址（未就绪时 null）
     api.LABEL                     导航键 → 中文名
   样式复用 styles.css 里的类：.stack .card .card-head .card-title .card-sub
     .card-body .stats .flow .kv .muted .ok .warn .err .mono .btn .btn2 .field .seg
   ============================================================ */
(function () {
  'use strict';

  var registry = {};
  var projHooks = [];        /* 工程切换时要清缓存的页面回调（用户 2026-10-18） */
  var tried = {};
  var bridgePort = null;
  var bridgeToken = null;   /* 2026-10-23：桥鉴权 token（X-EAI-Token） */
  var MOCK = new URLSearchParams(location.search).get('mock') === '1';
  var currentKey = null;
  var PREV_KEY = null;      /* 上一个挂载过的页面键（unmount 用，2026-10-23） */

  function base() {
    return bridgePort ? 'http://127.0.0.1:' + bridgePort : null;
  }

  /* 桥鉴权（用户 2026-10-23）：bridge.py 启动时随机生成 token，随 PORT 握手上报给主进程，
     渲染层每个请求都要带 X-EAI-Token，不带/不对一律 403。 */
  function authHeaders(extra) {
    var h = { 'X-EAI-Token': bridgeToken || '' };
    Object.keys(extra || {}).forEach(function (k) { h[k] = extra[k]; });
    return h;
  }

  /* ---------- 「明细」大弹窗：日志 / 训练输出 放大看（用户 2026-10-18） ---------- */
  var DETAIL_TITLES = { '训练输出': 1, '运行日志': 1, '日志': 1 };
  var detail = { mask: null, title: null, pre: null, timer: null, src: null };

  /* 复制到剪贴板（用户 2026-10-18）：navigator.clipboard 在部分环境不可用，退回 execCommand */
  function copyText(text) {
    function fallback(t) {
      try {
        var ta = document.createElement('textarea');
        ta.value = t;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.style.top = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch (e) { return false; }
    }
    return new Promise(function (res) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { res(true); },
            function () { res(fallback(text)); });
          return;
        }
      } catch (e) { /* 落到 fallback */ }
      res(fallback(text));
    });
  }

  function detailText(card) {
    if (!card || !card.body) return '';
    var pre = card.body.querySelector('pre');
    if (pre && pre.textContent.trim()) return pre.textContent;
    var box = card.body.querySelector('[class*="log"]');
    var host = box || card.body;
    /* 逐行 span（.rs-log-line 之类）：一行一条，按行首 [HH:MM:SS] 从上到下排（用户 2026-10-19）。
       原来直接取 textContent，所有行黏成一坨、顺序也乱。 */
    var spans = [].slice.call(host.querySelectorAll('.rs-log-line'));
    if (spans.length) {
      var rows = spans.map(function (n, i) {
        var t = n.textContent;
        var m = /^\[(\d{2}:\d{2}:\d{2})\]/.exec(t);
        return { t: t, k: m ? m[1] : '', i: i, p: n.classList.contains('warn') || n.classList.contains('err') };
      });
      /* 有时间的按时间升序；没时间的保持原有相对顺序（放在它原来的位置附近） */
      rows.sort(function (a, b) {
        if (a.k && b.k && a.k !== b.k) return a.k < b.k ? -1 : 1;
        return a.i - b.i;
      });
      return rows.map(function (o) { return o.t; }).join('\n');
    }
    var txt = (host.textContent || '');
    return txt.trim();
  }

  function ensureDetail() {
    if (detail.mask && detail.mask.isConnected) return;
    var mask = api.el('div', 'detail-mask');
    var card = api.el('div', 'detail-card');
    var head = api.el('div', 'detail-head');
    var title = api.el('span', 'detail-title', '');
    var x = api.el('button', 'detail-x', '');
    x.type = 'button';
    x.setAttribute('aria-label', '关闭');
    x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
    /* 一键复制（用户 2026-10-18）：只用图标，点完变勾号 1.2 秒 */
    var copy = api.el('button', 'detail-copy', '');
    copy.type = 'button';
    copy.title = '复制全部';
    copy.setAttribute('aria-label', '复制全部');
    copy.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round" stroke-linejoin="round">'
      + '<rect x="9.5" y="9.5" width="10" height="10" rx="2"/>'
      + '<path d="M6.5 14.5H5.2A1.7 1.7 0 0 1 3.5 12.8V5.2A1.7 1.7 0 0 1 5.2 3.5h7.6a1.7 1.7 0 0 1 1.7 1.7v1.3"/></svg>';
    copy.addEventListener('click', function () {
      copyText(detail.pre ? detail.pre.textContent : '').then(function (ok) {
        copy.classList.toggle('done', !!ok);
        copy.title = ok ? '已复制' : '复制失败';
        setTimeout(function () { copy.classList.remove('done'); copy.title = '复制全部'; }, 1200);
      });
    });
    var pre = api.el('pre', 'detail-pre', '');
    head.appendChild(title);
    head.appendChild(copy);
    head.appendChild(x);
    card.appendChild(head);
    card.appendChild(pre);
    mask.appendChild(card);
    document.body.appendChild(mask);
    detail.mask = mask; detail.title = title; detail.pre = pre;
    x.addEventListener('click', closeDetail);
    mask.addEventListener('click', function (e) { if (e.target === mask) closeDetail(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDetail(); });
  }

  /* 明细内容指纹（用户 2026-10-19）：只比行数 + 末行，别每次都把整段日志重排一遍。
     原来 500ms 一次 detailText()+textContent 赋值，日志一多（几十万字）整机就卡。 */
  function detailSig(card) {
    if (!card || !card.body) return '';
    var host = card.body.querySelector('[class*="log"]');
    var spans = host ? host.querySelectorAll('.rs-log-line') : null;
    if (spans && spans.length) {
      var last = spans[spans.length - 1].textContent || '';
      return spans.length + '|' + last.length + '|' + last.slice(-24);
    }
    var pre = card.body.querySelector('pre');
    if (pre) return 'p|' + pre.textContent.length;
    return 't|' + String((host || card.body).textContent || '').length;
  }

  function openDetail(card, title) {
    ensureDetail();
    detail.src = card;
    detail.title.textContent = (title || '日志') + ' · 明细';
    detail.pre.textContent = detailText(card) || '暂无内容';
    detail.mask.classList.add('on');
    detail.pre.scrollTop = detail.pre.scrollHeight;
    if (detail.timer) clearInterval(detail.timer);
    var lastSig = detailSig(card);
    detail.timer = setInterval(function () {          /* 弹窗开着就跟页面一起长（1s，变了才重建） */
      if (!detail.src) return;
      var sig = detailSig(detail.src);
      if (sig === lastSig) return;
      lastSig = sig;
      var t = detailText(detail.src) || '暂无内容';
      var atBottom = detail.pre.scrollHeight - detail.pre.scrollTop - detail.pre.clientHeight < 40;
      detail.pre.textContent = t;
      if (atBottom) detail.pre.scrollTop = detail.pre.scrollHeight;
    }, 1000);
  }

  function closeDetail() {
    if (!detail.mask) return;
    detail.mask.classList.remove('on');
    if (detail.timer) { clearInterval(detail.timer); detail.timer = null; }
  }

  /* ---------- 参数审查弹窗（用户 2026-10-18） ----------
     训练页「开始」前把后端判出来的问题摆出来：error 只能返回改，warn 可以仍然开始。 */
  var pc = { mask: null, title: null, sub: null, list: null, ok: null, cancel: null, resolve: null };

  function pcClose(val) {
    if (!pc.mask) return;
    pc.mask.classList.remove('on');
    var r = pc.resolve;
    pc.resolve = null;
    if (r) r(!!val);
  }

  function pcEnsure() {
    if (pc.mask && pc.mask.isConnected) return;
    var mask = api.el('div', 'pc-mask');
    var card = api.el('div', 'pc-card');
    var head = api.el('div', 'pc-head');
    var title = api.el('span', 'pc-title', '参数审查');
    var x = api.el('button', 'pc-x', '');
    x.type = 'button';
    x.setAttribute('aria-label', '关闭');
    x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
    head.appendChild(title);
    head.appendChild(x);
    var sub = api.el('p', 'pc-sub', '');
    var list = api.el('div', 'pc-list');
    var btns = api.el('div', 'pc-btns');
    var cancel = api.el('button', 'btn pc-cancel', '返回修改');
    cancel.type = 'button';
    var ok = api.el('button', 'btn pc-ok', '仍然开始');
    ok.type = 'button';
    btns.appendChild(cancel);
    btns.appendChild(ok);
    card.appendChild(head);
    card.appendChild(sub);
    card.appendChild(list);
    card.appendChild(btns);
    mask.appendChild(card);
    document.body.appendChild(mask);

    pc.mask = mask; pc.title = title; pc.sub = sub; pc.list = list; pc.ok = ok; pc.cancel = cancel;
    x.addEventListener('click', function () { pcClose(false); });
    cancel.addEventListener('click', function () { pcClose(false); });
    ok.addEventListener('click', function () { pcClose(true); });
    mask.addEventListener('click', function (e) { if (e.target === mask) pcClose(false); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && pc.mask.classList.contains('on')) pcClose(false);
    });
  }

  function pcOpen(label, issues, opts) {
    pcEnsure();
    /* 动作词（用户 2026-10-20）：流水线「添加步骤」时这里不该说「开始」 */
    var verb = (opts && opts.verb) || '开始';
    var errs = issues.filter(function (i) { return i.level === 'error'; });
    var warns = issues.filter(function (i) { return i.level !== 'error'; });
    /* 2026-10-23（用户）：标题只写「参数审查」，不带训练方式；下方小字不再说「改完再开始」 */
    pc.title.textContent = L('参数审查', 'Parameter check');
    void label;
    pc.sub.textContent = errs.length
      ? L('有 ' + errs.length + ' 项不可用' + (warns.length ? '、' + warns.length + ' 项有风险' : ''),
          errs.length + ' unavailable' + (warns.length ? ', ' + warns.length + ' at risk' : ''))
      : L('有 ' + warns.length + ' 项参数不常用，可以选择仍然' + verb + '。',
          warns.length + (warns.length > 1 ? ' parameters are' : ' parameter is') + ' unusual; you can still ' + (verb === '开始' ? 'start' : 'continue') + '.');
    pc.cancel.textContent = L('返回修改', 'Back to edit');
    pc.ok.textContent = L('仍然' + verb, verb === '开始' ? 'Start anyway' : 'Continue anyway');
    pc.list.innerHTML = '';
    issues.forEach(function (it) {
      var row = api.el('div', 'pc-item ' + (it.level === 'error' ? 'pc-item-err' : 'pc-item-warn'));
      /* 2026-10-23（用户）：去掉左侧「不可用 / 有风险」标签 —— 框的颜色已经说明了，
         把原因直接放进框里（标题行：字段名 + 当前值；下一行：原因）。 */
      var top = api.el('div', 'pc-item-top');
      top.appendChild(api.el('span', 'pc-field', it.label || it.field || ''));
      if (it.value) top.appendChild(api.el('span', 'pc-val', L('当前 ', 'Current ') + it.value));
      row.appendChild(top);
      row.appendChild(api.el('div', 'pc-msg', it.msg || ''));
      pc.list.appendChild(row);
    });
    pc.ok.style.display = errs.length ? 'none' : '';
    pc.mask.classList.add('on');
    return new Promise(function (resolve) { pc.resolve = resolve; });
  }

  function mockFor(path) {
    var def = registry[currentKey] || {};
    var m = def.mock || {};
    if (path in m) return m[path];
    for (var k in registry) {
      if (registry[k].mock && (path in registry[k].mock)) return registry[k].mock[path];
    }
    return { error: 'mock 未提供该接口', path: path };
  }

  function req(method, path, body) {
    if (MOCK) return Promise.resolve(mockFor(path));
    /* 先 ensureBridge 再取 base：bridgePort 是握手完才有的，取早了会缓存住 null，
       之后所有页面 api.get 都报「bridge 未就绪」（2026-10-14 实测踩过）。 */
    return ensureBridge().then(function () {
      var b = base();
      if (!b) return { error: 'bridge 未就绪（bridgePort 为空）' };
      var opt = { method: method, headers: authHeaders({ 'Content-Type': 'application/json' }) };
      if (body != null) opt.body = JSON.stringify(body);
      return fetch(b + path, opt).then(function (r) { return r.json(); })
        .then(function (d) {
          /* 没工程时后端统一抛「请先创建工程」（用户 2026-10-21）：提醒只由「还没有工程」
             空态出，页面里不许写状态行/弹窗（不能破坏现有 UI）→ 吞成空成功，页面按空态渲染。 */
          /* 2026-10-23：当前工程文件夹被删也按「没工程」处理，页面走空态（页脚红点会说明原因） */
          if (d && d.error && /请先创建工程|还没有工程|请先(新建|导入|创建)工程|当前工程文件夹不存在/.test(String(d.error))) {
            return { ok: true, __noProj: true };
          }
          return d;
        })
        .catch(function (e) { return { error: String(e) }; });
    });
  }

  /* 带数字的拼接文案（用户 2026-10-20）：词典按整段匹配，拼接串只能这里给中英两套 */
  function L(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }

  var api = {
    /* 调试/探针口径（同 window.__EAI_LABEL 先例）：页面外部可调 api 能力 */
    /* 段选滑块：给任意 .seg 补一个 .seg-thumb 并滑到当前选中项（缓动写在 styles.css）。
       animate=false / 首次定位 / 之前宽度还是 0（页面还隐藏着）→ 直接落位不做动画。 */
    segThumb: function (seg, animate) {
      if (!seg) return null;
      var th = seg.querySelector('.seg-thumb');
      var fresh = false;
      if (!th) {
        th = api.el('span', 'seg-thumb');
        th.setAttribute('aria-hidden', 'true');
        seg.insertBefore(th, seg.firstChild);
        fresh = true;
      }
      var on = seg.querySelector('.seg-opt.on') || seg.querySelector('.seg-opt[aria-checked="true"]');
      if (!on) return th;
      var noAnim = fresh || animate === false || !th.style.width || th.style.width === '0px';
      if (noAnim) th.classList.add('seg-thumb-init');
      th.style.width = on.offsetWidth + 'px';
      th.style.transform = 'translateX(' + on.offsetLeft + 'px)';
      if (th.offsetWidth > 0) seg.classList.add('seg-has-thumb');
      /* 按当前语言量过了才允许显示滑块（用户 2026-10-20）：切语言后旧宽度先别露出来 */
      try { seg.setAttribute('data-seg-fit', (window.I18N && window.I18N.lang()) || 'zh'); } catch (e) { }
      if (noAnim) {
        void th.offsetWidth;                       /* 强制回流：首次定位别被过渡成动画 */
        th.classList.remove('seg-thumb-init');
      }
      return th;
    },
    /* 长文本单行排布 + 悬停自动向右滚（用户 2026-10-18：数据集页的多字字段照仓库页） */
    scrollText: function (text, cls, mode) {
      var wrap = api.el('div', 'rp-scroll ' + (cls || ''));
      var inner = api.el('span', 'rp-scroll-in', text);
      wrap.appendChild(inner);
      wrap.__capCheck = function () {
        var w = inner.scrollWidth, bw = wrap.clientWidth, need;
        if (!bw) { setTimeout(wrap.__capCheck, 80); return; }
        if (mode === 'theme') {
          need = w > bw * 2 / 3;
        } else {
          if (!api.scrollText.__refW) {
            var cv = document.createElement('canvas').getContext('2d');
            var cs = getComputedStyle(inner);
            cv.font = (cs.fontWeight || '400') + ' ' + (cs.fontSize || '13px') + ' ' + (cs.fontFamily || 'sans-serif');
            api.scrollText.__refW = cv.measureText('articleZL_20261013_204232_articles.txt0000').width;
          }
          need = w > api.scrollText.__refW;
        }
        wrap.classList.toggle('rp-scroll-cap', need);
      };
      setTimeout(wrap.__capCheck, 0);
      wrap.addEventListener('mouseenter', function () {
        if (!wrap.classList.contains('rp-scroll-cap')) return;
        var over = inner.scrollWidth - wrap.clientWidth;
        if (over <= 4) return;
        inner.style.setProperty('--shift', over + 'px');
        inner.style.setProperty('--dur', Math.max(1, over / 36).toFixed(2) + 's');
        inner.classList.remove('rp-scrolling');
        void inner.offsetWidth;
        inner.classList.add('rp-scrolling');
        wrap.classList.add('rp-scroll-on');
      });
      wrap.addEventListener('mouseleave', function () {
        inner.classList.remove('rp-scrolling');
        wrap.classList.remove('rp-scroll-on');
      });
      return wrap;
    },
    /* ---------- 训练参数审查（用户 2026-10-18） ----------
       「开始训练」前先问后端 /api/train/check：参数明显不合理就弹窗。
       error = 不可用（只能返回修改）；warn = 有风险（可以选择仍然开始）。
       审查接口本身失败不挡训练（catch → true），避免后端没起来时按钮变哑巴。 */
    paramCheck: function (kind, params, label, opts) {
      /* lang 随请求带给后端（用户 2026-10-23）：参数名与原因文字由后端出，英文界面要出英文 */
      return api.post('/api/train/check', { kind: kind, params: params || {}, lang: L('zh', 'en') }).then(function (d) {
        var issues = (d && d.issues) || [];
        if (!issues.length) return true;
        return pcOpen(label || (d && d.kind_label) || '训练', issues, opts);
      })['catch'](function () { return true; });
    },
    /* 参数行的状态小球（用户 2026-10-18）：空=灰、正常=绿、有风险=黄、不可用=红。
       kind 可以是函数（压缩页按当前模式切 prune/quant）；rows = {后端字段: {input, dot}}。
       输入后 350ms 防抖再问后端，避免每敲一个字打一次接口。 */
    bindParamDots: function (kind, rows, getParams) {
      var timer = null;
      var lastMap = {};
      function paint(map) {
        if (map) lastMap = map;           /* paint() 不带参 = 按上次结论重涂（清空立刻变灰） */
        Object.keys(rows || {}).forEach(function (k) {
          var it = rows[k];
          if (!it || !it.dot) return;
          var v = String((it.input && it.input.value) || '').trim();
          var lv = (!v || v === '—') ? 'empty' : (lastMap[k] || 'ok');
          it.dot.dataset.level = lv;
          it.dot.title = lv === 'error' ? '不可用'
            : lv === 'warn' ? '有风险'
            : lv === 'ok' ? '正常' : '未填写';
        });
      }
      function refresh() {
        var k = (typeof kind === 'function') ? kind() : kind;
        if (!k) return;
        var params = {};
        try { params = (getParams && getParams()) || {}; } catch (e) { return; }   /* 页面还没建完时先跳过 */
        api.post('/api/train/check', { kind: k, params: params, lang: L('zh', 'en') }).then(function (d) {
          var map = {};
          ((d && d.issues) || []).forEach(function (i) {
            var f = i.field;
            if (!f) return;
            if (map[f] === 'error') return;
            map[f] = (i.level === 'error' || map[f] === 'warn') ? 'error' : 'warn';
          });
          paint(map);
        })['catch'](function () {});
      }
      Object.keys(rows || {}).forEach(function (k) {
        var inp = rows[k] && rows[k].input;
        if (!inp) return;
        inp.addEventListener('input', function () {
          paint();                         /* 先按已有结论重涂（清空立刻变灰） */
          if (timer) clearTimeout(timer);
          timer = setTimeout(refresh, 350);
        });
        inp.addEventListener('change', refresh);
      });
      setTimeout(refresh, 0);      /* 等 mount 把剩下的行/开关建完再取一次，否则会读到未初始化的变量 */
      return refresh;
    },
    /* 瓷片数值（用户 2026-10-18）：到三位数就不带小数，「269.0 KB」→「269KB」，避免越界 */
    compactNum: function (v) {
      var n = Number(v);
      if (!isFinite(n)) return '—';
      return n >= 100 ? String(Math.round(n)) : String(Math.round(n * 10) / 10);
    },
    /* 瓷片体积：紧凑单位、无空格（与训练页原本口径一致，只是加了三位数取整） */
    tileSize: function (bytes) {
      if (bytes == null) return '—';
      var b = Number(bytes);
      if (!isFinite(b)) return '—';
      var KB = 1024, MB = KB * 1024, GB = MB * 1024;
      if (b >= GB) return api.compactNum(b / GB) + 'GB';
      if (b >= MB) return api.compactNum(b / MB) + 'MB';
      if (b >= KB) return Math.round(b / KB) + 'KB';
      return Math.round(b) + 'B';
    },
    /* 瓷片数值自适应（用户 2026-10-18）：先缩字号到能放下（最小 12px），
       还放不下交给 CSS 省略号 —— 英文状态词（Not started）不再顶出卡片 */
    fitStats: function (root) {
      var list = (root || document).querySelectorAll('.stat-val');
      Array.prototype.forEach.call(list, function (el) {
        if (!el.__baseSize) {
          var fs = parseFloat(getComputedStyle(el).fontSize);
          el.__baseSize = fs > 0 ? fs : 20;
        }
        /* 体积统一成紧凑单位「269KB」（用户 2026-10-18：带空格会顶到框边） */
        var txt = el.textContent;
        var packed = txt.replace(/^(\d[\d.,]*)\s+(KB|MB|GB|B)$/i, '$1$2');
        if (packed !== txt) el.textContent = packed;
        el.style.fontSize = '';
        var size = el.__baseSize;
        while (size > 13 && el.scrollWidth > el.clientWidth + 2) {   /* 2px 容差：亚像素取整不算溢出 */
          size -= 1;
          el.style.fontSize = size + 'px';
        }
      });
    },
    get base() { return base(); },
    get state() {
      return {
        theme: document.documentElement.getAttribute('data-theme') || 'light',
        bridgePort: bridgePort,
      };
    },
    get LABEL() { return window.__EAI_LABEL || {}; },
    el: function (tag, cls, txt) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (txt != null) n.textContent = txt;
      return n;
    },
    esc: function (s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    },
    hero: function (title, sub) {
      var h = api.el('header', 'hero');
      h.appendChild(api.el('h1', 'hero-title', title || ''));
      if (sub) h.appendChild(api.el('p', 'hero-sub', sub));
      return h;
    },
    card: function (title, sub) {
      var c = api.el('section', 'card');
      var head = api.el('div', 'card-head');
      if (title) head.appendChild(api.el('h2', 'card-title', title));
      if (sub) head.appendChild(api.el('span', 'card-sub', sub));
      c.appendChild(head);
      c.body = api.el('div', 'card-body');
      c.appendChild(c.body);
      /* 日志 / 训练输出 这类长文本卡：卡头右上角挂「明细」，点开大弹窗看（用户 2026-10-18） */
      if (title && DETAIL_TITLES[title]) {
        var more = api.el('button', 'card-more', '明细 ›');
        more.type = 'button';
        more.title = '放大查看';
        more.addEventListener('click', function (e) {
          e.stopPropagation();
          openDetail(c, title);
        });
        head.appendChild(more);
      }
      return c;
    },
    detailModal: function (card, title) { openDetail(card, title || ''); },
    get: function (path) { return req('GET', path, null); },
    post: function (path, body) { return req('POST', path, body || {}); },

    /* 页内弹窗回执（用户 2026-10-16：拒收给出弹窗呈现理由）——
       原生 alert 在窗口被遮挡时看不见；页内弹窗跟应用一起显示。
       notice(title, lines[])：lines 每项一行（文件：理由）。 */
    notice: function (title, lines, opts) {
      var o = opts || {};
      var mask = api.el('div', 'nt-mask');
      var box = api.el('div', 'nt-box' + (o.wide ? ' nt-wide' : ''));
      var head = api.el('div', 'nt-head');
      head.appendChild(api.el('div', 'nt-title', String(title == null ? '提示' : title)));
      var x = api.el('button', 'nt-x');
      x.type = 'button';
      x.setAttribute('aria-label', '关闭');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>';
      head.appendChild(x);
      box.appendChild(head);
      var body = api.el('div', 'nt-body');
      (lines || []).forEach(function (ln) {
        body.appendChild(api.el('div', 'nt-line', String(ln)));
      });
      if (!lines || !lines.length) body.appendChild(api.el('div', 'nt-line', ''));
      box.appendChild(body);
      var foot = api.el('div', 'nt-foot');
      var ok = api.el('button', 'nt-ok');
      ok.type = 'button';
      ok.textContent = o.okText || '确定';
      if (o.cancelText) {
        var cancel = api.el('button', 'nt-cancel');
        cancel.type = 'button';
        cancel.textContent = o.cancelText;
        cancel.addEventListener('click', function () {
          close();
          if (o.onCancel) o.onCancel();
        });
        foot.appendChild(cancel);
      }
      foot.appendChild(ok);
      box.appendChild(foot);
      function close() {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
      }
      x.addEventListener('click', function () { close(); if (o.onCancel) o.onCancel(); });
      ok.addEventListener('click', function () {
        close();
        if (o.onOk) o.onOk();
      });
      mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
      mask.appendChild(box);
      document.body.appendChild(mask);
      ok.focus();
      return mask;
    },

    /* 参数说明（用户 2026-10-16：左键点参数名 → 弹窗看说明）：
       help(标签元素, 标题, 说明文本)——没文本就不绑、不可点。 */
    help: function (el, title, text) {
      if (!el || !text) return el;
      el.classList.add('has-help');
      el.addEventListener('click', function (e) {
        if (e && e.stopPropagation) e.stopPropagation();   /* 勾选行整行可点切换，别连带切勾 */
        /* 用户 2026-10-17：说明写详细了 —— 字符串里的 \n 分段（每段一行），弹窗加宽 */
        var lines = Array.isArray(text)
          ? text.map(String)
          : String(text).split('\n').filter(function (x) { return x.trim() !== ''; });
        api.notice(String(title == null ? '' : title), lines, { wide: true });
      });
      return el;
    },

    /* 数据集条目排版（仓库「查看内容」与蒸馏页数据管理共用，用户 2026-10-16：
       蒸馏页查看对话同仓库格式）：JSONL 逐条 → 问答卡（问/答）或条目卡（主题·时间+正文）。
       datasetRecCards(text, host) → { allJson, recs }；样式 rp-ds-* 在 repo.css。 */
    datasetRecCards: function (text, host, opts) {
      var lines = String(text == null ? '' : text).split('\n');
      var recs = [];
      var allJson = true;
      var lost = 0;              /* 解析不了的行数（文件被 200KB 截断时会有半截尾行）*/
      lines.forEach(function (ln) {
        if (!ln.trim()) return;
        try {
          var o = JSON.parse(ln);
          if (o && typeof o === 'object') { recs.push(o); return; }
        } catch (e) { /* 非 JSON 行 */ }
        allJson = false;
        lost += 1;
      });
      function recCard(head, qText, aText, bodyText) {
        var card = api.el('div', 'rp-ds-rec');
        if (head) card.appendChild(api.el('div', 'rp-ds-rec-head', head));
        if (qText != null) {
          var qr = api.el('div', 'rp-ds-qa');
          qr.appendChild(api.el('span', 'rp-ds-lab rp-ds-q', '问'));
          qr.appendChild(api.el('div', 'rp-ds-qtext', String(qText)));
          card.appendChild(qr);
        }
        if (aText != null) {
          var ar = api.el('div', 'rp-ds-qa');
          ar.appendChild(api.el('span', 'rp-ds-lab rp-ds-a', '答'));
          ar.appendChild(api.el('div', 'rp-ds-atext', String(aText)));
          card.appendChild(ar);
        }
        if (bodyText != null) card.appendChild(api.el('div', 'rp-ds-qtext', String(bodyText)));
        return card;      /* 2026-10-23：改由调用方按批次插进 host（原来直接 append，分批时得再摘下来） */
      }
      /* 分批画（用户 2026-10-23）：原来 499 条 → 3,992 个节点、140ms，条数越多越卡。
         现在先画 batch 条，底部「显示更多」再追加，全部画完按钮自己消失。
         batch 默认 200；传 { batch: 0 } 可以退回「一次全画」。 */
      var showMore = !(opts && opts.batch === 0);
      var batch = Math.max(1, Math.round(Number(opts && opts.batch) || 200));
      var drawn = 0;
      var moreEl = null;
      function cardOf(r) {
        var meta = [];
        if (r.topic) meta.push(String(r.topic));
        if (r.theme) meta.push(String(r.theme));
        if (r.created_at) meta.push(String(r.created_at));
        var head = meta.join(' · ');
        if (r.question != null || r.answer != null) {
          return recCard(head, r.question != null ? r.question : null, r.answer != null ? r.answer : null, null);
        }
        return recCard(head, null, null, r.article != null ? r.article : (r.text != null ? r.text : ''));
      }
      function drawMore() {
        var left = recs.length - drawn;
        if (!showMore || left <= 0) {
          if (moreEl && moreEl.parentNode) moreEl.parentNode.removeChild(moreEl);
          moreEl = null;
          return;
        }
        if (!moreEl) {
          moreEl = api.el('div', 'rp-ds-more');
          host.appendChild(moreEl);
        }
        moreEl.textContent = '';
        var btn = api.el('button', 'rp-ds-more-btn', window.bi('显示更多（还有 ' + left + ' 条）', 'Show more (' + left + ' left)'));
        btn.addEventListener('click', function () {
          var end = Math.min(recs.length, drawn + batch);
          var frag = document.createDocumentFragment();
          for (var i = drawn; i < end; i++) frag.appendChild(cardOf(recs[i]));
          drawn = end;
          host.insertBefore(frag, moreEl);
          drawMore();
        });
        moreEl.appendChild(btn);
      }
      if (allJson && recs.length) {
        var end0 = Math.min(recs.length, showMore ? batch : recs.length);
        var frag0 = document.createDocumentFragment();
        for (var i0 = 0; i0 < end0; i0++) frag0.appendChild(cardOf(recs[i0]));
        drawn = end0;
        host.appendChild(frag0);
        drawMore();
      }
      return { allJson: allJson, recs: recs, lost: lost, drawn: drawn, batch: batch };
    },

    /* ---------- 密钥输入（与设置页 APIKEY 同一套口径） ----------
       已保存：填固定 32 个点（点数与真实长度无关，防反推）+ readOnly，锁定态禁止 copy/cut；
       点击/聚焦：清空并进入可输入态；提交前一律用 api.secretValue() 取值 —— 锁定态判空（= 不修改），
       绝不会把「点」当密钥发出去。 */
    secretDots: 32,
    secretLock: function (input, hasKey) {
      if (!input) return;
      if (hasKey) {
        /* 锁定态：值就是字面 32 个点（用 text 显示，password 会把它们再打一遍码）*/
        input.type = 'text';
        input.value = new Array(api.secretDots + 1).join('\u2022');
        input.readOnly = true;
        input.dataset.locked = '1';
        input.placeholder = '';
        input.title = '已保存（点一下可输入新密钥）';
      } else {
        /* 输入态：type=password —— 用户刚粘/刚敲的密钥也一律显成点，屏幕上不留明文 */
        input.type = 'password';
        input.setAttribute('autocomplete', 'new-password');
        input.value = '';
        input.readOnly = false;
        input.dataset.locked = '';
        input.placeholder = '粘贴新密钥';
        input.title = '';
      }
    },
    secretValue: function (input) {
      if (!input || input.dataset.locked) return '';
      return String(input.value || '').trim();
    },
    secretWire: function (input) {
      if (!input || input.dataset.secretWired) return input;
      input.dataset.secretWired = '1';
      var unlock = function () {
        if (!input.dataset.locked) return;
        input.value = '';
        input.readOnly = false;
        input.type = 'password';                       // 输入过程也打点
        input.setAttribute('autocomplete', 'new-password');
        input.dataset.locked = '';
        input.placeholder = '输入新密钥';
        input.title = '';
      };
      input.addEventListener('mousedown', unlock);
      input.addEventListener('focus', unlock);
      ['copy', 'cut'].forEach(function (ev) {
        input.addEventListener(ev, function (e) {
          if (input.dataset.locked) e.preventDefault();
        });
      });
      return input;
    },

    /* ---------- 状态小球（灰=未测 / 黄=测试中 / 绿=通 / 红=失败） ----------
       口径：状态不用文字，用颜色；具体原因放 title（悬停看）。 */
    dot: function (state, title) {
      var el = api.el('span', 'stat-dot' + (state ? ' ' + state : ''));
      if (title) el.title = String(title);
      return el;
    },
    dotSet: function (el, state, title) {
      if (!el) return el;
      el.className = 'stat-dot' + (state ? ' ' + state : '');
      el.title = title == null ? '' : String(title);
      return el;
    },

    /* ---------- 模型下拉（测试成功后替换 model 输入框） ----------
       自绘而不是原生 <select>：原生弹层没法做圆角、也不跟主题色。触发区沿用页面的输入井类
       （cls，如 'rs-input rs-mono'），弹层挂到 body 上做 fixed 定位（卡片有圆角裁剪，不能用绝对定位）。
       目标可以是 <input>（就地替换）或已建好的 .pick（只刷选项）；返回元素带 .value，
       保存/测试都读它，所以 ins.model 的用法不变。 */
    modelSelect: function (target, models, cls) {
      if (!target) return target;
      var cur = String(target.value == null ? '' : target.value).trim();
      var ids = (models || []).slice();
      /* 旧值不在测试返回的列表里 → 丢弃它（不再插到最前面），自动选中返回列表的第一个 */
      var keepCur = !!cur && ids.indexOf(cur) >= 0;
      var isPick = !!(target.classList && target.classList.contains('pick'));
      var pick = isPick ? target : api.el('div', ('pick ' + (cls || '')).trim());
      var label, caret;
      if (isPick) {
        label = pick.querySelector('.pick-label');
        caret = pick.querySelector('.pick-caret');
      } else {
        pick.tabIndex = 0;
        label = api.el('span', 'pick-label');
        caret = api.el('span', 'pick-caret');
        caret.innerHTML = '<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true">' +
          '<path d="M5.4 7.8 10 12.4l4.6-4.6" fill="none" stroke="currentColor" stroke-width="1.7" ' +
          'stroke-linecap="round" stroke-linejoin="round"/></svg>';
        pick.appendChild(label);
        pick.appendChild(caret);
        if (target.parentNode) target.parentNode.replaceChild(pick, target);
      }
      var value = keepCur ? cur : (ids[0] || cur || '');
      pick.value = value;
      label.textContent = value;
      label.title = value;
      if (pick._pop && pick._pop.parentNode) pick._pop.parentNode.removeChild(pick._pop);
      var pop = api.el('div', 'pick-pop');
      ids.forEach(function (id) {
        var o = api.el('div', 'pick-opt' + (id === value ? ' sel' : ''), id);
        o.title = id;
        o.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        o.addEventListener('click', function (ev) {
          ev.stopPropagation();
          pick.value = id;
          label.textContent = id;
          label.title = id;
          /* 选中派发 change（同 api.pick；供外部监听筛选用） */
          try { pick.dispatchEvent(new Event('change')); } catch (e) { /* noop */ }
          Array.prototype.forEach.call(pop.children, function (c) {
            c.classList.toggle('sel', c === o);
          });
          closePick(pick);
        });
        pop.appendChild(o);
      });
      pick._pop = pop;
      if (!pick._wired) {
        pick._wired = 1;
        pick.addEventListener('click', function (ev) {
          if (ev.target && ev.target.closest && ev.target.closest('.pick-pop')) return;
          if (pick.classList.contains('pick-open')) closePick(pick);
          else openPick(pick);
        });
        pick.addEventListener('keydown', function (ev) {
          if (ev.key === 'Escape') closePick(pick);
          else if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openPick(pick); }
        });
      }
      return pick;
    },

    /* 通用自绘下拉：把 <input> 换成 .pick（label 可与 value 不同，便于中文显示）。
       options: [[value, label], ...] 或 [value, ...]。
       返回元素带 value 访问器 —— 直接 el.value = 'batch' 会同步更新显示文字（回填就靠它）。
       为什么不用原生 <select>：弹层由系统绘制，选中项是方角高亮、会盖住弹层圆角，且样式不可控。 */
    pick: function (target, options, cls) {
      if (!target) return target;
      var opts = (options || []).map(function (o) {
        if (Array.isArray(o)) return { value: String(o[0]), label: String(o.length > 1 ? o[1] : o[0]) };
        return { value: String(o), label: String(o) };
      });
      var isPick = !!(target.classList && target.classList.contains('pick'));
      var pick = isPick ? target : api.el('div', ('pick ' + (cls || '')).trim());
      var label, caret;
      if (isPick) {
        label = pick.querySelector('.pick-label');
        caret = pick.querySelector('.pick-caret');
      } else {
        pick.tabIndex = 0;
        label = api.el('span', 'pick-label');
        caret = api.el('span', 'pick-caret');
        caret.innerHTML = '<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true">' +
          '<path d="M5.4 7.8 10 12.4l4.6-4.6" fill="none" stroke="currentColor" stroke-width="1.7" ' +
          'stroke-linecap="round" stroke-linejoin="round"/></svg>';
        pick.appendChild(label);
        pick.appendChild(caret);
        if (target.parentNode) target.parentNode.replaceChild(pick, target);
      }
      var init = String(target.value == null ? '' : target.value).trim();
      var state = { value: '' };
      if (pick._pop && pick._pop.parentNode) pick._pop.parentNode.removeChild(pick._pop);
      var pop = api.el('div', 'pick-pop');
      function show(v) {
        var hit = null;
        opts.forEach(function (o) { if (o.value === v) hit = o; });
        if (!hit) hit = opts[0] || { value: '', label: '' };
        state.value = hit.value;
        label.textContent = hit.label;
        label.title = hit.label;
        Array.prototype.forEach.call(pop.children, function (c, i) {
          var isSel = !!(opts[i] && opts[i].value === state.value);
          c.classList.toggle('sel', isSel);
          /* pick._hideSel：列表里不重复列「当前已选」项（用户 2026-10-15，仓库类型筛选用） */
          c.style.display = (pick._hideSel && isSel) ? 'none' : '';
        });
      }
      opts.forEach(function (o) {
        /* 弹层选项双语（用户 2026-10-22）：选项是建好后挂在 pick 上、打开才进 DOM，
           i18n 的 walk 扫不到它们 —— 建的时候按当前语言写，并记住原文，切语言时统一重写。 */
        var el = api.el('div', 'pick-opt', api._tr ? api._tr(o.label) : o.label);
        el.__zh = o.label;
        el.title = el.textContent;
        el.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        el.addEventListener('click', function (ev) {
          ev.stopPropagation();
          show(o.value);
          closePick(pick);
          /* 选中派发 change（程序赋值 pk.value = x 不发；只此用户点选路径发） */
          try { pick.dispatchEvent(new Event('change')); } catch (e) { /* noop */ }
        });
        pop.appendChild(el);
      });
      pick._pop = pop;
      Object.defineProperty(pick, 'value', {
        get: function () { return state.value; },
        set: function (v) { show(String(v == null ? '' : v)); },
        configurable: true
      });
      if (!pick._wired) {
        pick._wired = 1;
        pick.addEventListener('click', function (ev) {
          if (ev.target && ev.target.closest && ev.target.closest('.pick-pop')) return;
          if (pick.classList.contains('pick-open')) closePick(pick);
          else openPick(pick);
        });
        pick.addEventListener('keydown', function (ev) {
          if (ev.key === 'Escape') closePick(pick);
          else if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openPick(pick); }
        });
      }
      show(init);
      return pick;
    },

    /* 开关（单控件，如「隔离并保存被拒样本」）：返回 { el, value, set(v) }。
       本体就是 styles.css 里的 .switch + .knob（含非线性动效），点击自己切换。 */
    toggle: function (on) {
      var sw = api.el('button', 'switch' + (on ? ' on' : ''));
      sw.setAttribute('type', 'button');
      sw.setAttribute('role', 'switch');
      sw.setAttribute('aria-checked', on ? 'true' : 'false');
      sw.appendChild(api.el('span', 'knob'));
      var t = {
        el: sw,
        value: !!on,
        set: function (v) {
          var on2 = !!v;
          t.value = on2;
          sw.classList.toggle('on', on2);
          sw.setAttribute('aria-checked', on2 ? 'true' : 'false');
        }
      };
      sw.addEventListener('click', function (ev) { ev.stopPropagation(); t.set(!t.value); });
      return t;
    },

    /* 两档滑块（如「被拒样本：隔离 | 丢弃」）。
       options: [[value, label], ...]；返回元素带 value 访问器（读写都会同步滑块位置与高亮），
       点哪一档就切到哪一档。样式在 styles.css 的 .slider2*。 */
    slider: function (options, current, cls) {
      var opts = (options || []).map(function (o) {
        if (Array.isArray(o)) return { value: String(o[0]), label: String(o.length > 1 ? o[1] : o[0]) };
        return { value: String(o), label: String(o) };
      });
      var wrap = api.el('div', ('slider2 ' + (cls || '')).trim());
      var thumb = api.el('span', 'slider2-thumb');
      wrap.appendChild(thumb);
      var els = opts.map(function (o, i) {
        var el = api.el('span', 'slider2-opt' + (o.value === current ? ' on' : ''), o.label);
        el.title = '';
        el.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        el.addEventListener('click', function (ev) { ev.stopPropagation(); apply(o.value); });
        wrap.appendChild(el);
        return el;
      });
      var state = { value: (opts[0] || { value: '' }).value };
      function measure() {
        /* 只切 data-idx 与高亮类：滑块位置/宽度由 CSS 算（两档等宽，translateX(100%)），
           这样面板隐藏时构造也正确 —— 量 offsetWidth 在 hidden 面板里恒为 0。 */
        var i = 0;
        opts.forEach(function (o, k) { if (o.value === state.value) i = k; });
        wrap.dataset.idx = String(i);
        els.forEach(function (el, k) { el.classList.toggle('on', k === i); });
      }
      function apply(v) {
        var hit = null;
        opts.forEach(function (o) { if (o.value === String(v)) hit = o; });
        state.value = (hit || opts[0] || { value: '' }).value;
        measure();
      }
      Object.defineProperty(wrap, 'value', {
        get: function () { return state.value; },
        set: function (v) { apply(v); },
        configurable: true
      });
      apply(String(current == null ? '' : current));   /* 初值非法则落回首档 */
      return wrap;
    },
  };

  /* 弹层：挂 body + fixed 定位，避免被卡片圆角/overflow 裁掉 */
  function positionPick(pick, pop) {
    var r = pick.getBoundingClientRect();
    var h = pop.offsetHeight;
    var fitsBelow = r.bottom + h + 8 <= window.innerHeight;
    var fitsAbove = r.top - h - 8 >= 0;
    var below = fitsBelow || !fitsAbove;
    /* 与输入井相连：不留缝、接缝那侧去圆角/去边线（另一侧圆角保留） */
    pop.classList.toggle('pop-below', below);
    pop.classList.toggle('pop-above', !below);
    pick.classList.toggle('pick-below', below);
    pick.classList.toggle('pick-above', !below);
    pop.style.left = Math.round(r.left) + 'px';
    pop.style.width = Math.round(r.width) + 'px';
    pop.style.top = Math.round(below ? r.bottom : Math.max(8, r.top - h)) + 'px';
  }
  function openPick(pick) {
    var pop = pick._pop;
    if (!pop) return;
    if (pop._outTimer) { clearTimeout(pop._outTimer); pop._outTimer = null; }   /* 正在收起→取消 */
    pop.classList.remove('pop-out');
    if (pop.parentNode) pop.parentNode.removeChild(pop);   /* 重挂让入场动画重播 */
    document.body.appendChild(pop);
    pick.classList.add('pick-open');
    pick._outside = function (ev) {
      if (!pick.contains(ev.target) && !pop.contains(ev.target)) closePick(pick);
    };
    pick._repos = function () { positionPick(pick, pop); };
    document.addEventListener('mousedown', pick._outside, true);
    window.addEventListener('resize', pick._repos, true);
    positionPick(pick, pop);
    var sel = pop.querySelector('.pick-opt.sel');
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
  }
  function closePick(pick) {
    if (!pick) return;
    var pop = pick._pop;
    pick.classList.remove('pick-open');
    if (pick._outside) document.removeEventListener('mousedown', pick._outside, true);
    if (pick._repos) window.removeEventListener('resize', pick._repos, true);
    if (!pop || !pop.parentNode) return;
    /* 收起过渡：播完退场动画再摘节点；期间重新展开会被 openPick 取消 */
    if (pop._outTimer) { clearTimeout(pop._outTimer); pop._outTimer = null; }
    pop.classList.add('pop-out');
    pop._outTimer = setTimeout(function () {
      pop._outTimer = null;
      pop.classList.remove('pop-out');
      if (pop.parentNode) pop.parentNode.removeChild(pop);
    }, 130);
  }

  function ensureBridge() {
    if (bridgePort || MOCK || !window.eai || !window.eai.state) return Promise.resolve();
    return Promise.resolve(window.eai.state()).then(function (st) {
      bridgePort = (st && st.bridgePort) || null;
      bridgeToken = (st && st.bridgeToken) || null;   /* 2026-10-23：桥鉴权 token */
    }).catch(function () {});
  }

  function loadScript(src) {
    return new Promise(function (res) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = function () { res(true); };
      s.onerror = function () { res(false); };
      document.head.appendChild(s);
    });
  }

  /* 返回 Promise：**样式应用之后**才算装好。
     原来只 append <link> 不等待 —— 首次进某页时那一帧是裸 DOM（无类样式、卡片方角、输入框满宽），
     用户看到的就是「先闪一下没样式的页面」。等 onload（本地文件、命中缓存，几毫秒）就没了。
     800ms 只是保险丝：万一 onload 不触发也不会把页面卡住。 */
  function loadCss(href) {
    return new Promise(function (res) {
      var l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = href;
      var done = false;
      var fin = function () { if (!done) { done = true; res(true); } };
      l.onload = fin;
      l.onerror = fin;
      document.head.appendChild(l);
      setTimeout(fin, 800);
    });
  }

  /* 页面键 → 模块文件名（默认同名；例外：回收站拆两页共用 pages/recycle.js） */
  var MODULE_FILE = { recycle_dataset: 'recycle', recycle_weight: 'recycle',
    train_prune: 'train_compress', train_quant: 'train_compress' };

  function ensureModule(key) {
    if (registry[key]) return Promise.resolve(true);
    if (tried[key]) return Promise.resolve(false);
    tried[key] = true;
    var file = MODULE_FILE[key] || key;
    return loadScript('pages/' + file + '.js').then(function (ok) {
      if (!ok) return false;
      return loadCss('pages/' + file + '.css').then(function () { return !!registry[key]; });
    });
  }

  /* 输入弹窗（数据集合并命名用，2026-10-15）：Promise<string|null>（取消=null）。
     自绘——原生 prompt 不可用且样式不受控；标题/标签裸文案，无教程式说明。 */
  api.prompt = function (opts) {
    return new Promise(function (resolve) {
      var o = opts || {};
      var mask = api.el('div', 'ask-mask');
      var box = api.el('div', 'ask-box');
      box.appendChild(api.el('div', 'ask-title', o.title || ''));
      var row = api.el('div', 'ask-row');
      row.appendChild(api.el('span', 'ask-label', o.label || ''));
      var input = api.el('input', 'ask-input');
      input.type = 'text';
      input.value = o.value || '';
      if (o.placeholder) input.placeholder = o.placeholder;
      row.appendChild(input);
      box.appendChild(row);
      var btns = api.el('div', 'ask-btns');
      var cancel = api.el('button', 'btn ask-btn', o.cancelText || '取消');
      var ok = api.el('button', 'btn btn-accent ask-btn ask-ok', o.okText || '确定');
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
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
        else if (e.key === 'Enter' && document.activeElement === input) { e.preventDefault(); ok.click(); }
      }
      ok.addEventListener('click', function () { close((input.value || '').trim()); });
      cancel.addEventListener('click', function () { close(null); });
      mask.addEventListener('mousedown', function (e) { if (e.target === mask) close(null); });
      document.addEventListener('keydown', onKey, true);
      document.body.appendChild(mask);
      input.focus();
      input.select();
    });
  };

  /* 导出数据集（数据管理）：先弹原生「另存为」让用户选地址+文件名，再让桥把本板块的多个
     样本文件合并成一个文件写过去。用户口径：数据管理只能导出、不能导入；每个板块只有一种格式，
     所以导出内容与该板块文件的数据一致（逐行原样拼）。成功不写提示，只在失败时写状态行。 */
  /* 横条数值就地输入（用户 2026-10-20）：默认外观不动，鼠标移到数字上提示可改，
     点一下换成输入框（当前值选中），回车/失焦生效，Esc 放弃。生效时按 min/max 夹紧、
     按 step 吸附，再派发 input+change 事件 —— 各页原本的 paint / 自动保存逻辑照旧跑。
     用法：api.rangeEdit(valEl, inputEl) */
  /* 权重锁查询（用户 2026-10-20）：训练页选完初始权重后问一句，
     锁上的权重不许勾「覆盖初始权重」，问不到就当没锁（不挡正常训练） */
  api.isLockedPath = function (path) {
    if (!path) return Promise.resolve(false);
    return api.post('/api/repo/lock_state', { paths: [path] }).then(function (d) {
      return !!(d && d.locked && d.locked[path]);
    })['catch'](function () { return false; });
  };

  api.rangeEdit = function (valEl, inputEl) {
    if (!valEl || !inputEl) return;
    valEl.addEventListener('click', function () {
      if (inputEl.disabled) return;                       /* 横条锁着时数字也不可改 */
      var box = valEl.parentNode;
      if (!box || box.querySelector('.range-edit')) return;
      var ed = document.createElement('input');
      ed.type = 'text';
      ed.inputMode = 'decimal';
      ed.className = 'range-edit';
      ed.value = String(inputEl.value);                    /* 原始数值（不带 % / 个） */
      valEl.style.display = 'none';
      box.appendChild(ed);
      ed.focus();
      ed.select();
      var done = false;
      function finish(ok) {
        if (done) return;
        done = true;
        var t = String(ed.value || '').trim();
        if (ok && t) {
          var v = Number(t.replace(/[^\d.\-]/g, ''));
          if (isFinite(v)) {
            var lo = Number(inputEl.min), hi = Number(inputEl.max), st = Number(inputEl.step) || 0;
            v = Math.min(hi, Math.max(lo, v));
            if (st > 0) v = lo + Math.round((v - lo) / st) * st;
            v = Math.round(v * 1000) / 1000;
            inputEl.value = String(v);
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
            inputEl.dispatchEvent(new Event('change', { bubbles: true }));   /* 页面据此重画 + 自动保存 */
          }
        }
        if (ed.parentNode) ed.parentNode.removeChild(ed);
        valEl.style.display = '';
      }
      ed.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      });
      ed.addEventListener('blur', function () { finish(true); });
      ed.addEventListener('click', function (e) { e.stopPropagation(); });
    });
  };

  /* 数据管理表格（用户 2026-10-21）：表格限高到「下方内容框压到窗口底、与边框留距」，
     超出的行滚动显示 + 上下渐变（参考导出模型 .ex-listwrap/.ex-fade）。
     wrap=表格外框（内部会包一层滚动区 + 两条渐变）、preview=下方内容框（隐藏/为空时不计）。
     返回 { fit }：每次渲染/切视图/预览开合后调用；窗口尺寸变化自动重算。 */
  api.dmLayout = function (wrap, preview, opts) {
    opts = opts || {};
    var ht = opts.heightTarget || preview;      /* 高度拖调作用的元素 */
    var scroll = api.el('div', 'dm-scroll');
    while (wrap.firstChild) scroll.appendChild(wrap.firstChild);
    wrap.classList.add('dm-tablewrap');
    wrap.appendChild(scroll);
    var ft = api.el('div', 'dm-fade dm-fade-top');
    var fb = api.el('div', 'dm-fade dm-fade-bot');
    wrap.appendChild(ft);
    wrap.appendChild(fb);
    function upd() {
      /* 渐隐随窗口缩放（用户 2026-10-21）：窗口越小渐隐越淡（带高 10~22px 按可视高 8% 折算） */
      var H = Math.max(10, Math.min(22, Math.round(scroll.clientHeight * 0.08)));
      ft.style.height = H + 'px';
      fb.style.height = H + 'px';
      var t = scroll.scrollTop;
      var b = scroll.scrollHeight - scroll.clientHeight - t;
      ft.style.opacity = String(Math.min(1, Math.max(0, t / H)));
      fb.style.opacity = String(Math.min(1, Math.max(0, b / H)));
    }
    scroll.addEventListener('scroll', upd);

    /* 下方内容区高度可拖调（用户 2026-10-21）：表与内容框之间的握把，上限由 fit 保底表头+2 行。
       同一内容框只建一条握把（文件/逐条两套表共用）；高度按页签键记忆。 */
    if (ht && !ht.__dmGrip) {
      var grip = api.el('div', 'dm-grip');
      var host = preview && preview.parentNode;
      if (host) host.insertBefore(grip, preview);
      ht.__dmGrip = grip;
      /* 握把上下间距对称（用户 2026-10-21）：内容框↔握把 = 握把↔上框。
         上框=握把上方最近的可见框（响应/语料是表格外框，网页整理是数据卡底边）。 */
      grip.__align = function () {
        var gr = grip.getBoundingClientRect();
        var above = grip.previousElementSibling;
        if (!above && grip.parentNode) above = grip.parentNode.previousElementSibling;
        while (above && above.offsetParent === null && above.previousElementSibling) above = above.previousElementSibling;
        if (!above || above.offsetParent === null) return false;
        var pr = preview.getBoundingClientRect();
        var gapAbove = gr.top - above.getBoundingClientRect().bottom;
        var gapBelow = pr.top - gr.bottom;
        if (gapAbove > 0 && gapAbove <= 64 && gapBelow > 0 && Math.abs(gapBelow - gapAbove) > 0.5) {
          preview.style.marginTop = '0px';
          grip.style.marginBottom = Math.round(gapAbove) + 'px';
          return true;
        }
        return false;
      };
      var drag = null;
      function gripMove(ev) {
        if (!drag) return;
        ht.style.maxHeight = 'none';
        ht.style.height = Math.max(80, Math.round(drag.h - (ev.clientY - drag.y))) + 'px';
        api.dmRefit();
      }
      function gripUp() {
        drag = null;
        document.removeEventListener('mousemove', gripMove);
        document.removeEventListener('mouseup', gripUp);
        document.body.classList.remove('dm-grip-drag');
        ht.__dmDragged = true;   /* 用户自己拖过：fit 按拖出来的高度来（用户 2026-10-20） */
        try { if (opts.key) localStorage.setItem('eaiDmH:' + opts.key, String(ht.offsetHeight)); } catch (e) { /* 隐私模式等 */ }
        api.dmRefit();
      }
      grip.addEventListener('mousedown', function (ev) {
        ev.preventDefault();
        if (preview && preview.hidden) preview.hidden = false;
        drag = { y: ev.clientY, h: ht.offsetHeight };
        document.addEventListener('mousemove', gripMove);
        document.addEventListener('mouseup', gripUp);
        document.body.classList.add('dm-grip-drag');
      });
      try {
        var savedH = parseFloat(localStorage.getItem('eaiDmH:' + (opts.key || '')));
        /* 记忆值只当初始高度，不当「用户拖过」——否则新开页面表格会按旧比例铺满，
           看起来和语料蒸馏空态不一致（用户 2026-10-20：内容框下边框钉底、表格按内容高）。 */
        if (savedH > 0) { ht.style.maxHeight = 'none'; ht.style.height = Math.round(savedH) + 'px'; }
      } catch (e) { /* ignore */ }
    }
    /* 下方内容区上下渐隐（用户 2026-10-21）：和表一样滚动渐隐，但更淡（带高 14px、峰值透明度 0.55）。
       渐隐底色取内容框实际底色（各页不同：输入底/卡底）。 */
    if (ht && !ht.__dmPvFade && ht.parentNode) {
      var pvWrap = api.el('div', 'dm-pvwrap');
      ht.parentNode.insertBefore(pvWrap, ht);
      pvWrap.appendChild(ht);
      var bgEl = ht, bg = '';
      while (bgEl) {
        bg = getComputedStyle(bgEl).backgroundColor || '';
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') break;
        bgEl = bgEl.parentElement;
      }
      var bgNums = (bg.match(/\d+/g) || []).slice(0, 3);
      var fadeTo = bgNums.length === 3 ? 'rgba(' + bgNums.join(',') + ',0)' : 'transparent';
      var pft = api.el('div', 'dm-fade dm-fade-top dm-fade-soft');
      var pfb = api.el('div', 'dm-fade dm-fade-bot dm-fade-soft');
      pft.style.background = 'linear-gradient(to bottom, ' + (bg || 'var(--CARD)') + ' 15%, ' + fadeTo + ')';
      pfb.style.background = 'linear-gradient(to top, ' + (bg || 'var(--CARD)') + ' 15%, ' + fadeTo + ')';
      pvWrap.appendChild(pft);
      pvWrap.appendChild(pfb);
      var pvUpd = function () {
        var H = 14;
        var t = ht.scrollTop;
        var b = ht.scrollHeight - ht.clientHeight - t;
        pft.style.opacity = String(Math.min(1, Math.max(0, t / H)) * 0.55);
        pfb.style.opacity = String(Math.min(1, Math.max(0, b / H)) * 0.55);
      };
      ht.addEventListener('scroll', pvUpd);
      ht.__dmPvFade = { upd: pvUpd };
    }
    /* 统一重算入口：拖调/尺寸变化后所有表格都重算（同页两套表共用一个内容框） */
    api.__dmFits = api.__dmFits || [];
    api.__dmFits.push(fit);
    if (!api.dmRefit) {
      api.dmRefit = function () {
        (api.__dmFits || []).forEach(function (f) { try { f(); } catch (e) { /* 单个失效不拖垮其余 */ } });
      };
    }
    function fit(retry) {
      if (!wrap.parentNode) return;            /* 已卸载 */
      if (!wrap.offsetParent) {               /* 还没显示出来（页签刚建好/视图切换中）：等一拍再算 */
        if ((retry || 0) < 12) setTimeout(function () { fit((retry || 0) + 1); }, 150);
        return;
      }
      wrap.style.height = 'auto';
      if (ht.__dmGrip && !ht.__dmGrip.parentNode && preview && preview.parentNode) {
        preview.parentNode.insertBefore(ht.__dmGrip, preview);   /* 宿主晚挂载的兜底补插（2026-10-21） */
      }
      if (ht.__dmGrip && ht.__dmGrip.__align) ht.__dmGrip.__align();   /* 握把间距对称（用户 2026-10-21） */
      if (ht.__dmPvFade && ht.__dmPvFade.upd) ht.__dmPvFade.upd();     /* 内容区渐隐状态跟内容刷新（用户 2026-10-21） */
      var wrapBox = wrap.getBoundingClientRect();
      var pv = preview && !preview.hidden && preview.offsetHeight ? preview : null;
      var card = wrap.closest ? wrap.closest('.card') : null;
      /* 底距（用户 2026-10-21 二次口径）：最底下的边框（卡片下边框）到 EAI 窗口边 = 横栏到卡片上边框的间距，
         严格一致（今天早前「至少大于横条空隙 12px」的口径作废）。 */
      var topGap = 36;
      if (card) {
        var ctop = card.getBoundingClientRect().top;
        var segs = [].slice.call(document.querySelectorAll('.seg'));
        for (var si = 0; si < segs.length; si++) {
          var sb = segs[si].getBoundingClientRect().bottom;
          if (segs[si].offsetParent && sb <= ctop + 2 && ctop - sb < 200) { topGap = Math.round(ctop - sb); break; }
        }
      }
      var BOTTOM = topGap;
      var headEl = scroll.querySelector('thead');
      var rowEl = scroll.querySelector('tbody tr');
      /* 还没有数据行时按「空态行」的高度（56）估：用 54 估出来的是 151，
         数据一到位变成 155，握把会当场跳 4px（用户 2026-10-20 实测）。 */
      /* 表格最小高 = 表头 + 2 行；取一个跨页统一的保底（116 = 40 + 2×34 + 8）。
         各页行高差 1px、页首空态行高也不同，不兜底的话握把能拖到的最高高度三页会差几像素
         （用户 2026-10-22：网页整理的最高高度和另两页不一致）。 */
      var MIN_TABLE_H = 116;
      var minWrap = Math.max(MIN_TABLE_H,
        (headEl ? headEl.offsetHeight : 40) + 2 * (rowEl ? rowEl.offsetHeight : 56) + 8);
      /* 内容框下边框钉在底部（用户 2026-10-20）：先按表格自身内容定高（保底表头 + 2 行，
         上限给内容框留 120px），剩下的高度全部给内容框 ——
         这样内容框的下边框永远在窗口底部往上 BOTTOM 处，不随上面行数/空态而上下漂。
         旧算法用「卡片下边框 - 内容框底」反推可用高度，自己绕自己：空态时表格缩到 155px、
         内容框停在 120px，下面空出两百多像素。 */
      /* 卡片下内边距也算进去（用户 2026-10-20）：内容框下边框要和卡片外框留出这段距离，
         不能贴着外框。card 是 flex 子项，高度由布局定死，扣掉这段内容框才会缩进去。 */
      var cardPb = card ? (parseFloat(getComputedStyle(card).paddingBottom) || 0) : 0;
      var bottomTarget = Math.round(window.innerHeight - BOTTOM - cardPb);
      var gapNow = pv ? Math.max(0, pv.getBoundingClientRect().top - wrapBox.bottom) : 12;
      var avail = Math.max(minWrap + 120, Math.round(bottomTarget - wrapBox.top - gapNow));
      var pvMin = 120;
      var dragged = !!ht.__dmDragged;

      wrap.style.height = 'auto';
      var contentH = wrap.offsetHeight;                 /* 表头 + 全部行（含边框） */
      var wrapH;
      if (dragged) {
        /* 用户拖过握把：尊重拖出来的内容框高度，表格吃剩下的（不够就保底） */
        wrapH = Math.max(minWrap, avail - Math.round(ht.offsetHeight));
      } else {
        wrapH = Math.max(minWrap, Math.min(contentH, avail - pvMin));
      }
      wrap.style.height = wrapH + 'px';

      if (pv) {
        var pvTop = pv.getBoundingClientRect().top;
        var wantPv = Math.max(pvMin, Math.round(bottomTarget - pvTop));
        ht.style.maxHeight = 'none';
        if (Math.abs(wantPv - pv.offsetHeight) > 1) ht.style.height = wantPv + 'px';
      }
      upd();
    }
    window.addEventListener('resize', fit);
    fit();
    return { fit: fit };
  };

  /* 单按钮警告弹窗（用户 2026-10-21）：重复率警告这类只要「知道了」，
     原生 confirm 带个「取消」不合适。样式沿用参数审查弹窗（pc-*）。 */
  api.alertBox = function (opts) {
    var o = opts || {};
    return new Promise(function (resolve) {
      var mask = api.el('div', 'pc-mask');
      var card = api.el('div', 'pc-card');
      var head = api.el('div', 'pc-head');
      head.appendChild(api.el('span', 'pc-title', o.title || '提示'));
      var sub = api.el('p', 'pc-sub', o.message || '');
      var btns = api.el('div', 'pc-btns');
      var ok = api.el('button', 'btn pc-ok', o.okText || '知道了');
      ok.type = 'button';
      btns.appendChild(ok);
      card.appendChild(head);
      card.appendChild(sub);
      card.appendChild(btns);
      mask.appendChild(card);
      document.body.appendChild(mask);
      function close() {
        mask.classList.remove('on');
        setTimeout(function () { if (mask.parentNode) mask.parentNode.removeChild(mask); }, 200);
        resolve(true);
      }
      ok.addEventListener('click', close);
      mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
      requestAnimationFrame(function () { mask.classList.add('on'); });
    });
  };

  /* 应用内自绘确认框（用户 2026-10-21：EAI 内所有弹窗都用自己的弹窗，不再用系统原生框）。
     api.confirm({title, message, detail, buttons}) → Promise<boolean>；
     版式与回收站「彻底删除」弹窗一致：标题 / 主句 / 次要行（detail）/ 右侧按钮，无分割线。
     buttons[0]=取消、[1]=确定；只传一项（如 ['知道了']）= 单按钮提示框，点它即 true。 */
  api.confirm = function (opts) {
    var o = opts || {};
    var btns = (o.buttons && o.buttons.length) ? o.buttons.slice() : ['取消', '确定'];
    return new Promise(function (resolve) {
      var mask = api.el('div', 'nt-mask');
      var box = api.el('div', 'nt-box');
      var head = api.el('div', 'nt-head');
      head.appendChild(api.el('div', 'nt-title', String(o.title || '请确认')));
      box.appendChild(head);
      var body = api.el('div', 'nt-body');
      body.appendChild(api.el('div', 'nt-line', String(o.message || '')));
      if (o.detail) body.appendChild(api.el('div', 'nt-sub', String(o.detail)));
      box.appendChild(body);
      var foot = api.el('div', 'nt-foot');
      function done(v) {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        resolve(v);
      }
      if (btns.length >= 2) {
        var cancel = api.el('button', 'nt-cancel');
        cancel.type = 'button';
        cancel.textContent = String(btns[0]);
        cancel.addEventListener('click', function () { done(false); });
        foot.appendChild(cancel);
        var ok = api.el('button', 'nt-ok');
        ok.type = 'button';
        ok.textContent = String(btns[1]);
        ok.addEventListener('click', function () { done(true); });
        foot.appendChild(ok);
      } else {
        var only = api.el('button', 'nt-ok');
        only.type = 'button';
        only.textContent = String(btns[0]);
        only.addEventListener('click', function () { done(true); });
        foot.appendChild(only);
      }
      box.appendChild(foot);
      mask.appendChild(box);
      document.body.appendChild(mask);
      mask.addEventListener('click', function (e) { if (e.target === mask) done(false); });
    });
  };

  /* 仓库权重选择弹窗（训练页「初始权重」用，用户 2026-10-15）：
     外部权重必须先添加到仓库（仓库页「+」卡）——训练页不提供添加/浏览文件，弹窗只列仓库现有权重。
     2026-10-17（用户）：改成大弹窗 + 仓库式卡片排布 + 搜索框（搜权重名称 / 备注）。
     api.weightPicker({ title, current, onPick(w) })；
     w = { name, path, size, mtime, title, method_label, remark, hist }
     2026-10-17（用户）：加 accept 过滤 —— 'lora' 只列 *.lora.pt（LoRA 适配器），
     'base' 只列非 *.lora.pt（基座/普通权重），不传则列全部。 */
  api.weightPicker = function (opts) {
    var o = opts || {};
    function fmtBytes(n) {
      n = Number(n) || 0;
      if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
      if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
      if (n >= 1024) return Math.round(n / 1024) + ' KB';
      return n + ' B';
    }
    function fmtTime(sec) {
      if (!sec) return '—';
      var d = new Date(sec * 1000);
      var p = function (x) { return (x < 10 ? '0' : '') + x; };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
        p(d.getHours()) + ':' + p(d.getMinutes());
    }

    var all = [];   /* 仓库权重（已按 accept 过滤），搜索只在前端再过滤（不重新请求） */

    /* 后缀口径（同对话页 chat.js 的两个 picker）：适配器 = *.lora.pt，基座 = 其余 .pt */
    function isLora(w) { return /\.lora\.pt$/i.test(String((w && w.name) || '')); }
    function accept(w) {
      var a = o.accept;
      if (!a) return true;
      if (typeof a === 'function') return !!a(w);
      if (a === 'lora') return isLora(w);
      if (a === 'base') return !isLora(w);
      return true;
    }

    /* 路径比较：config 里存的是 D:/… 正斜杠，仓库扫盘给的是 D:\… 反斜杠（用户 2026-10-17 实测对不上→当前权重不高亮） */
    function samePath(a, b) {
      var norm = function (v) {
        var t = String(v || '').split(String.fromCharCode(92)).join('/');   /* 反斜杠 → 正斜杠 */
        while (t.length > 1 && t.charAt(t.length - 1) === '/') t = t.slice(0, -1);
        return t.toLowerCase();
      };
      return !!a && !!b && norm(a) === norm(b);
    }

    /* 只允许同时开一个（重复点行/换页残留的旧弹窗先清掉） */
    [].slice.call(document.querySelectorAll('.wt-mask')).forEach(function (m) {
      if (m.parentNode) m.parentNode.removeChild(m);
    });

    var mask = api.el('div', 'wt-mask');
    function close() {
      if (mask.parentNode) mask.parentNode.removeChild(mask);
      document.removeEventListener('keydown', onKey);
    }
    function onKey(ev) { if (ev.key === 'Escape') close(); }
    mask.addEventListener('click', function (ev) {
      if (ev.target === mask) close();
    });

    var modal = api.el('div', 'wt-modal');
    var head = api.el('div', 'wt-head');
    head.appendChild(api.el('span', 'wt-title', o.title || '选择权重'));

    /* 搜索框（用户 2026-10-17）：权重名称 / 备注；空格分词，全部命中才算匹配 */
    var searchbox = api.el('div', 'wt-searchbox');
    searchbox.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" ' +
      'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true">' +
      '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.2 4.2"/></svg>';
    var inp = document.createElement('input');
    inp.type = 'search';
    inp.className = 'wt-search';
    inp.placeholder = '搜索权重名称 / 备注';
    searchbox.appendChild(inp);
    head.appendChild(searchbox);

    var count = api.el('span', 'wt-count', '');
    head.appendChild(count);

    /* 右上角的额外按钮（用户 2026-10-20：流水线的「导入权重」弹窗要一个「不导入」） */
    if (o.extra && o.extra.text) {
      var extraBtn = api.el('button', 'wt-extra', String(o.extra.text));
      extraBtn.type = 'button';
      extraBtn.title = o.extra.title || '';
      extraBtn.addEventListener('click', function () {
        close();
        if (typeof o.extra.onClick === 'function') o.extra.onClick();
      });
      head.appendChild(extraBtn);
    }

    var x = api.el('button', 'wt-close');
    x.type = 'button';
    x.title = '关闭';
    x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M6 6l12 12M18 6L6 18"/></svg>';
    x.addEventListener('click', close);
    head.appendChild(x);
    modal.appendChild(head);

    var body = api.el('div', 'wt-body');
    body.appendChild(api.el('div', 'wt-empty', '读取中…'));
    modal.appendChild(body);
    mask.appendChild(modal);
    document.body.appendChild(mask);
    document.addEventListener('keydown', onKey);
    try { inp.focus(); } catch (e) {}

    /* 训练方式标签：最近 3 次训练（最左=上一次），无历史回退单个 method_label —— 同仓库页权重卡 */
    function tagsOf(w) {
      var tags = [];
      var hist3 = (w.hist || []).slice(-3).reverse();
      hist3.forEach(function (h) { if (h && h.m) tags.push(String(h.m)); });
      if (!tags.length && w.method_label) tags.push(String(w.method_label));
      return tags;
    }
    function crow(k, v) {
      var r = api.el('div', 'wt-c-row');
      r.appendChild(api.el('span', 'wt-c-k', k));
      var vv = api.el('span', 'wt-c-v', v);
      vv.title = String(v || '');
      r.appendChild(vv);
      return r;
    }
    /* 锁标（用户 2026-10-22）：被锁的权重/文件夹在选择器里也标出来，
       和仓库页同一套语义（文件夹锁向下继承，锁住 = EAI 内不能删除，不影响加载） */
    function lockBadge(owner) {
      var b = api.el('span', 'rp-lock on');   /* 与仓库页同一套外观（用户 2026-10-22） */
      b.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
      b.title = owner ? ('随文件夹「' + owner + '」锁定：文件夹不解锁就不能单独解锁')
                      : '已锁定：EAI 内不能删除';
      return b;
    }
    var LOCK_CACHE = {};   /* 文件名 → {locked, lock_folder}，从 /api/repo/summary 带下来 */
    function card(w) {
      var c = api.el('button', 'wt-card' + (samePath(w.path, o.current) ? ' on' : ''));
      c.type = 'button';
      c.title = w.path || '';
      var head = api.el('div', 'wt-c-head');
      head.appendChild(api.el('div', 'wt-c-name', w.name || '（未命名）'));
      var lk = LOCK_CACHE[w.name];
      if (w.locked || (lk && lk.locked)) head.appendChild(lockBadge((w.lock_folder || (lk && lk.lock_folder) || '')));
      c.appendChild(head);
      var tg = api.el('div', 'wt-c-tags');
      tagsOf(w).forEach(function (t) { tg.appendChild(api.el('span', 'wt-c-tag', t)); });
      c.appendChild(tg);
      c.appendChild(crow('大小', fmtBytes(w.size)));
      c.appendChild(crow('更新时间', fmtTime(w.mtime)));
      c.appendChild(crow('备注', w.remark || '—'));
      c.addEventListener('click', function () {
        close();
        if (o.onPick) o.onPick(w);
      });
      return c;
    }
    function hay(w) {
      var hs = (w.hist || []).map(function (h) { return (h && h.m) ? h.m : ''; }).join(' ');
      return [w.name, w.remark, w.method_label, w.title, hs].join(' ').toLowerCase();
    }
    function words(s) { return String(s || '').toLowerCase().split(/\s+/).filter(Boolean); }

    function render() {
      body.textContent = '';
      var q = words(inp.value);
      /* 选择器不出文件夹卡（用户 2026-10-22）：所有权重一起列，
         顺序在加载时已排好 = 单独文件在前、文件夹里的在后（组内按仓库置顶/更新时间） */
      var hit = all.filter(function (w) {
        if (!q.length) return true;
        var h = hay(w);
        return q.every(function (k) { return h.indexOf(k) >= 0; });
      });
      count.textContent = L('共' + all.length + '个可用权重', all.length + ' weights available');   /* 用户 2026-10-17：显示总数，不再显示「命中 / 全部」 */
      if (!all.length) {
        /* 空态按类型分开说，别让用户以为仓库空了（用户 2026-10-17） */
        var msg = o.accept === 'lora'
          ? '仓库里还没有 LoRA 适配器。'
          : (o.accept === 'base' ? '仓库里还没有可用作基座的权重。' : '仓库里还没有权重。');
        body.appendChild(api.el('div', 'wt-empty', msg));
        return;
      }
      if (!hit.length) {
        var qq = String(inp.value).trim();
        body.appendChild(api.el('div', 'wt-empty',
          L('没有匹配「' + qq + '」的权重。', 'No weights match "' + qq + '".')));
        return;
      }
      var grid = api.el('div', 'wt-grid');
      hit.forEach(function (w) { grid.appendChild(card(w)); });
      body.appendChild(grid);
    }

    var renderTimer = 0;
    inp.addEventListener('input', function () {
      clearTimeout(renderTimer);
      renderTimer = setTimeout(render, 90);   /* 输入就过滤，90ms 防抖 */
    });
    inp.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter') return;
      var first = body.querySelector('.wt-card');
      if (first) first.click();               /* Enter = 选第一个命中项 */
    });

    api.get('/api/repo/summary').then(function (d) {
      all = ((d && !d.error) ? (d.weights || []) : []).filter(accept);   /* 用户 2026-10-17：按类型过滤 */
      /* 排序（用户 2026-10-22）：单独文件在前、文件夹里的在后；
         每段内先按仓库置顶（置顶先后），其余保持接口给的更新时间顺序 */
      all.sort(function (a, b) {
        var fa = String(a.vfolder || '') ? 1 : 0, fb = String(b.vfolder || '') ? 1 : 0;
        if (fa !== fb) return fa - fb;
        var pa = a.pinned ? 0 : 1, pb = b.pinned ? 0 : 1;
        if (pa !== pb) return pa - pb;
        return (a.pin_rank || 9999) - (b.pin_rank || 9999);
      });
      ((d && !d.error) ? (d.weights || []) : []).forEach(function (w) {
        LOCK_CACHE[w.name] = { locked: !!w.locked, lock_folder: w.lock_folder || '' };
      });
      render();
    })["catch"](function () {
      body.textContent = '';
      body.appendChild(api.el('div', 'wt-empty', '权重读取失败。'));
    });
  };

  /* 模型权重命名行（用户 2026-10-15）：训练页参数卡顶部命名输出权重，
     输入即审查是否与仓库权重同名（同名红字 + start 前拦）。 */
  api.weightName = function (opts) {
    /* 2026-10-15 终版（用户口径）：在参数卡内、参数上方 —— 标题与「参数」卡标题同格式，
       下方同款分隔条由各页自己补；输入框在左、提示在右（√ 绿 / 重复 红）。 */
    var o = opts || {};
    var wrap = api.el('div', 'wn-wrap');
    wrap.appendChild(api.el('div', 'wn-title', '权重命名'));   /* 原「模型权重命名」，用户 2026-10-16 定稿 */
    var row = api.el('div', 'wn-row');
    var inp = document.createElement('input');
    inp.type = 'text';
    inp.className = (o.inputClass || 'tp-input') + ' wn-input';
    inp.placeholder = '输出权重名';
    row.appendChild(inp);
    var hint = api.el('div', 'wn-hint');
    var hintDot = api.el('span', 'wn-dot');
    hintDot.setAttribute('aria-hidden', 'true');
    var hintText = api.el('span', 'wn-text');
    hint.appendChild(hintDot);
    hint.appendChild(hintText);
    row.appendChild(hint);
    wrap.appendChild(row);
    /* 初始=未判断 → 灰球（用户 2026-10-21）：别一上来闪「未命名」红字，判完再切对应状态 */
    setHint('pending', '');

    var st = { dup: false, name: '', file: '' };
    function stem() { return String(inp.value || '').trim().replace(/\.pt$/i, ''); }
    function fileName() { return (stem() || o.fallback || 'eproai') + '.pt'; }
    /* 合格标：绿点（用户 2026-10-16：对勾改绿点）；
       绿点/红字常驻叠放、切状态只换 class → CSS opacity 交叉淡入淡出（用户 2026-10-16：切页不再闪） */
    function setHint(kind, text) {
      hint.className = 'wn-hint' + (kind ? ' ' + kind : '');
      hintText.textContent = (kind === 'ok') ? '' : (text || '');
    }
    function audit() {
      if (!stem()) {                 /* 命名框空着就不判重名，改显示「未命名」（用户 2026-10-17） */
        st.dup = false; st.name = ''; st.file = '';
        hint.title = '';
        setHint('bad', '未命名');
        return Promise.resolve(st);
      }
      var file = fileName();
      st.file = file;
      /* 一份输入可能落到多个文件名上（用户 2026-10-22：LoRA 页填 HIS_LORA，
         真正落盘是 HIS_LORA.lora.pt —— 只比 HIS_LORA.pt 会漏掉仓库里的 HIS_LORA.lora.pt，
         等导出那一刻才撞名）。o.checkNames 返回该输入的全部候选文件名，命中任一即重复。 */
      var cands = [file];
      if (typeof o.checkNames === 'function') {
        try {
          var extra = o.checkNames(stem()) || [];
          for (var k = 0; k < extra.length; k++) {
            var en = String(extra[k] || '');
            if (en && cands.map(function (x) { return x.toLowerCase(); }).indexOf(en.toLowerCase()) < 0) cands.push(en);
          }
        } catch (e) {}
      }
      var lowers = cands.map(function (x) { return x.toLowerCase(); });
      var want = file;               /* 判完回写前先确认输入没被改（旧一次判重别盖新结果） */
      setHint('pending', '');        /* 判重中 → 灰球（用户 2026-10-21），判完再切 */
      return api.get('/api/repo/summary').then(function (d) {
        if (fileName() !== want) return st;   /* 名字已经改过了，这次结果作废 */
        var ws = (d && !d.error) ? (d.weights || []) : [];
        for (var i = 0; i < ws.length; i++) {
          var n = String(ws[i].name || '');
          if (lowers.indexOf(n.toLowerCase()) >= 0) {
            st.dup = true; st.name = n;
            hint.title = '与仓库权重「' + n + '」同名';
            setHint('bad', '重复');       /* 重复红字（用户 2026-10-15）；细节进 title 提示 */
            return st;
          }
        }
        st.dup = false; st.name = '';
        hint.title = '';
        setHint('ok', '√');               /* 合格打 √（用户 2026-10-15） */
        return st;
      })['catch'](function () {
        st.dup = false;
        setHint('pending', '');      /* 没判成 = 未判断，灰球待着 */
        return st;
      });
    }
    var t = null;
    inp.addEventListener('input', function () {
      if (t) clearTimeout(t);
      /* 一动手旧判断就作数不得 → 立刻回灰球，判完再给结果（用户 2026-10-21） */
      if (stem()) setHint('pending', '');
      t = setTimeout(function () { t = null; audit(); }, 400);
    });
    return { wrap: wrap, row: row, input: inp, hint: hint,
             audit: audit, stem: stem, fileName: fileName, state: st };
  };

  /* 状态瓷片自适应（用户 2026-10-18）：内容变化 / 语言切换 / 窗口缩放都重量一次字号 */
  (function () {
    var pending = null;
    function refit() {
      if (pending) return;
      pending = setTimeout(function () {
        pending = null;
        try { api.fitStats(); } catch (e) {}
      }, 80);
    }
    function boot() {
      if (typeof MutationObserver !== 'undefined') {
        new MutationObserver(refit).observe(document.body, { childList: true, subtree: true, characterData: true });
      }
      window.addEventListener('resize', refit);
      refit();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
  })();

  window.__EAI_API = api;   /* 调试钩子（同 __EAI_LABEL 口径） */

  api.exportDataset = function (opts) {
    var o = opts || {};
    var statusEl = o.status || null;
    function fail(msg) {
      if (statusEl) { statusEl.textContent = msg; statusEl.classList.add('a-err'); }
    }
    if (statusEl) { statusEl.textContent = ''; statusEl.classList.remove('a-err', 'a-warn'); }
    if (!window.eai || !window.eai.saveFile) { fail('导出失败：当前外壳没有「另存为」对话框'); return; }
    var d = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var ts = '' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + '_' +
      pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
    var ext = String(o.ext || 'jsonl').replace(/^\./, '');
    var name = (o.defaultName || '数据集') + '_' + ts + '.' + ext;
    Promise.resolve(window.eai.saveFile({
      title: o.title || '导出数据集',
      defaultPath: name,
      buttonLabel: '保存',
      filters: [{ name: o.filterName || '数据集', extensions: [ext] }],
    })).then(function (r) {
      if (!r || r.canceled || !r.path) return;          /* 用户取消 → 什么都不做 */
      /* o.body：数据管理带 targets（只导出选中的行，用户 2026-10-14） */
      return api.post(o.route, Object.assign({ path: r.path }, o.body || {})).then(function (res) {
        if (!res || res.ok === false) {
          fail('导出失败：' + ((res && res.error) || '未知错误'));
        }
        /* 成功不写任何提示（与「保存配置」同口径）；文件已经写到用户选的位置 */
      });
    }).catch(function (e) {
      fail('导出失败：' + ((e && e.message) ? e.message : e));
    });
  };

  /* 下拉弹层选项的双语支持（用户 2026-10-22）：api.pick 建选项时按当前语言写文字、
     并把中文原文留在 el.__zh 上；语言一换，把所有已存在的 .pick-opt 按 __zh 重写一遍，
     省得每处 pick 自己重绘（仓库类型筛选/训练方式筛选等只在挂载时建一次）。 */
  api._tr = function (zh) {
    return (window.I18N && window.I18N.t) ? window.I18N.t(zh) : zh;
  };
  (function () {
    var hooked = false;
    function hookPickI18N() {
      if (hooked || !window.I18N || !window.I18N.on) return;
      hooked = true;
      window.I18N.on(function () {
        /* 弹层平时是挂空的（打开才 append 到 body），所以从每个 pick 的 _pop 里找选项 */
        Array.prototype.forEach.call(document.querySelectorAll('.pick'), function (pk) {
          var pop = pk._pop;
          if (!pop) return;
          Array.prototype.forEach.call(pop.querySelectorAll('.pick-opt'), function (o) {
            if (o.__zh == null) return;
            o.textContent = api._tr(o.__zh);
            o.title = o.textContent;
          });
        });
        Array.prototype.forEach.call(document.querySelectorAll('.pick-opt'), function (o) {
          if (o.__zh == null) return;
          o.textContent = api._tr(o.__zh);
          o.title = o.textContent;
        });
      });
    }
    hookPickI18N();
    /* I18N 可能比本文件晚加载：挂载时再试一次 */
    document.addEventListener('DOMContentLoaded', hookPickI18N);
    setTimeout(hookPickI18N, 0);
  })();

  window.EAIPages = {
    api: api,
    register: function (key, def) { if (key && def && def.mount) registry[key] = def; },
    /* 工程切换（用户 2026-10-18）：新建/切换工程后，页面要丢掉自己缓存的上一个工程数据 */
    onProjectChange: function (fn) { if (typeof fn === 'function') projHooks.push(fn); },
    emitProjectChange: function () { projHooks.forEach(function (fn) { try { fn(); } catch (e) {} }); },
    has: function (key) { return !!registry[key]; },
    keys: function () { return Object.keys(registry); },
    /* 返回 Promise<boolean>：false = 该页还没迁（调用方回退到空态卡）。
       注意：只要模块已注册，页面内部渲染/取数失败**不算**「没迁」——
       否则一个 fetch 报错就会把已迁页面踢回空态卡。 */
    mount: function (key, container) {
      if (!container) return Promise.resolve(false);
      return ensureModule(key).then(function (okModule) {
        if (!okModule || !registry[key]) return false;
        return ensureBridge().then(function () {
          currentKey = key;
          var page = registry[key];
          /* 先把数据取回来，**再**换页 —— 这样新页出现的第一帧内容就是全的。
             原来先换页再取数：页面自己那次本地 HTTP 往返（蒸馏页实测 24~40ms）里
             输入框/提示词都是空的，用户看到「切过去先空一下、字才出来」。
             页面用 prefetch(api) 声明要预取什么（它把结果交给 mount 同步消费，见 HANDOFF）。
             取数失败或慢都不挡切换：400ms 保险丝一到就照旧换页（最差回到旧行为）。 */
          var pre = (page && typeof page.prefetch === 'function')
            ? Promise.resolve().then(function () { return page.prefetch(api); })["catch"](function () {})
            : Promise.resolve();
          return Promise.race([pre, new Promise(function (res) { setTimeout(res, page.prefetchTimeout || 400); })]).then(function () {
          /* 原子换页（用户 2026-10-21：导航切页别闪）：新页先在隐藏暂存盒里建好，
             同一帧整体换掉旧内容 —— 旧内容在新页就绪前一直挂着，中间没有空白帧。
             （原来先清空再异步重建：ensureBridge + prefetch + 建 DOM 的几十~几百毫秒里
             页面是空的 = 每次切页都闪一下。）暂存盒 position:absolute + visibility:hidden：
             不占布局但参与测量（display:none 量出来全是 0，分段滑块/表格限高会算错）。 */
          var stage = document.createElement('div');
          stage.style.cssText = 'position:absolute;left:0;top:0;width:' +
            Math.max(0, container.clientWidth || 0) + 'px;visibility:hidden;pointer-events:none;';
          if (!container.style.position) container.style.position = 'relative';
          container.appendChild(stage);
          /* 2026-10-23：切页时要调**上一个页**的 unmount。原来这里用的是新页面自己的
             __mounted（`page` 是 registry[key]），跨页切换时永远不成立 —— 旧页的定时器、
             文档级监听全都没被清掉（对话页「离开 2 分钟自动退出」就是被这个卡住的）。 */
          var prevPage = (PREV_KEY && PREV_KEY !== key) ? registry[PREV_KEY] : null;
          if (!prevPage && page.__mounted) prevPage = page;        /* 同一页重挂：沿用原行为 */
          var prevRoot = prevPage && prevPage.__mounted ? prevPage.__root : null;
          if (prevPage && prevPage.unmount) {
            try { prevPage.unmount(prevRoot || container); } catch (e) {}
          }
          PREV_KEY = key;
          page.__root = container;
          page.__mounted = true;
          function swap() {
            var kids = Array.prototype.slice.call(stage.childNodes);
            kids.forEach(function (n) { container.appendChild(n); });   /* 新内容就位 */
            Array.prototype.slice.call(container.childNodes).forEach(function (n) {
              if (kids.indexOf(n) < 0) container.removeChild(n);        /* 旧内容/暂存盒同一帧拿掉 */
            });
            return true;
          }
          return Promise.resolve(page.mount(stage, api)).then(swap)
            .catch(function (e) {
              console.error('[EAIPages] ' + key + ' 渲染出错：', e);
              return swap();                       /* 已注册 = 算成功，错误由页面自己显示 */
            });
          });
        });
      }).catch(function (e) {
        console.error('[EAIPages] 装载 ' + key + ' 失败：', e);
        return !!registry[key];
      });
    },
  };
})();
