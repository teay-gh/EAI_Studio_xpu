/* ============================================================
   仓库（工作台分组）· 渲染层模块
   ------------------------------------------------------------
   数据集与模型权重总览，卡片排布；一页两视图（数据集 / 模型权重），
   用段选按钮切换显示（用户 2026-10-14）。

   2026-10-15 扩充：
   · 权重逐文件出卡（同类型可多个），没权重的训练类型不出卡
   · 数据集卡补 文件夹/训练方式/主题；权重卡补 来源
   · 两视图各一条筛选栏：类型/训练方式/文本查找；权重另有日期筛选
   · 网格最后固定一张「+」空白卡：点开原生选文件，拷进工程

   数据来源：bridge_api/repo.py
     GET  /api/repo/summary → { datasets: [...], weights: [...], counts, project }
     POST /api/repo/add_dataset {paths} / /api/repo/add_weight {paths}

   取数交接（治页面打开闪烁，同 dataset.js）：换页前 prefetch 取回 summary
   放 HANDOFF，mount 的 load() 同步消费，首帧即满。
   ============================================================ */
(function () {
  'use strict';

/* 「+」空白卡兜底高：每视图（datasets/weights）记上次实测的数据卡高 —— 空态一张卡都
   量不到时用它兜底，空页的「+」卡与有数据/有权重的页同高（用户 2026-10-16） */
var RP_ADD_H = {};

/* 仓库页挂在共享容器 #plugRoot 上：容器换页时只清 DOM、监听器不会自己消失，
   所以右键监听要能被 unmount 摘掉（用户 2026-10-20：切到回收站右击空白处还会弹出仓库的导入菜单） */
var CTX_ROOT = null;
/* 2026-10-23（用户）：删除确认弹窗文案跟语言走（删除 = 移入回收站） */
function RL(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }
var CTX_HANDLER = null;
/* 批量选择底栏（.rp-selbar）挂在 document.body 上、position:fixed，页面 DOM 被清掉它也还在。
   切页时必须自己摘掉，否则会浮在回收站等别的页面上（用户 2026-10-23） */
var SELBAR_CLEANUP = null;

EAIPages.register('repo', {
  title: '仓库',
  sub: '数据集与模型权重总览',

  unmount: function () {
    if (CTX_ROOT && CTX_HANDLER) {
      try { CTX_ROOT.removeEventListener('contextmenu', CTX_HANDLER); } catch (e) {}
    }
    CTX_ROOT = null;
    CTX_HANDLER = null;
    /* 底栏不跟着页面 DOM 走，这里手动清（用户 2026-10-23：切到回收站还浮着一条「已选 1 项」） */
    if (SELBAR_CLEANUP) { try { SELBAR_CLEANUP(); } catch (e) {} SELBAR_CLEANUP = null; }
  },

  /* 只给 ?mock=1 用，正常跑一律走真实桥接口 */
  mock: {
    '/api/repo/summary': {
      ok: true, project: '（预览）EAI_GC', dataset_count: 2, weight_count: 3,
      datasets: [
        { name: '（预览）articleZL_20261013_204232_articles.txt', folder: 'txt',
          kind: '纯文本', kind_key: 'plain', tab: ['pretrain'], chars: 240601,
          size_bytes: 240534, lines: 120, ok: true, fmt_ok: true, fmt_msg: '',
          themes: ['光合作用', '恒星的生命周期'], use: 'pretrain' },
        { name: '（预览）mimoZL_20261011_175932.jsonl', folder: 'response',
          kind: '对话', kind_key: 'chat', tab: ['sft'], chars: 18400,
          size_bytes: 20200, lines: 3, ok: true, fmt_ok: true, fmt_msg: '',
          themes: ['机器学习基础'], use: 'sft' },
      ],
      weights: [
        { name: 'eproai_pretrain.pt', path: '（预览）', size: 6710886, mtime: 1760000000,
          method: 'pretrain', method_label: '预训练', title: '预训练权重', source: 'config.train' },
        { name: 'eproai_continue.pt', path: '（预览）', size: 7200000, mtime: 1760000600,
          method: 'continue', method_label: '继续预训练', title: '继续预训练权重', source: 'config.train' },
        { name: 'eproai_old.pt', path: '（预览）', size: 1048576, mtime: 1750000000,
          method: 'other', method_label: '其他', title: '其他权重', source: '工程 checkpoints/' },
      ],
      scan_error: '', weights_error: '',
    },
  },

  prefetch: function (api) {
    return fetchSummary(api);
  },

  mount(root, api) {
    root.appendChild(api.hero('仓库'));

    /* 切语言后重定宽（用户 2026-10-22）：英文候选词比中文长，中文量出来的宽度会截字；
       词典翻完（I18N.fire 在 walk 之后）再按英文重量一遍，字和框都刚好。 */
    if (window.I18N && window.I18N.on) {
      window.I18N.on(function () { try { fitAllUsePicks(); } catch (e) {} });
    }

    /* ---------- 取数交接（同 dataset.js） ---------- */
    var DATA = null;   /* summary 缓存；段选来回切不重复渲染 */

    /* ---------- 小工具 ---------- */
    function fmtSize(n) {
      n = Number(n) || 0;
      if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
      if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
      if (n >= 1024) return Math.round(n / 1024) + ' KB';
      return n + ' B';
    }
    function fmtNum(n) {
      n = Number(n) || 0;
      return n.toLocaleString('en-US');
    }
    function fmtTime(sec) {
      if (!sec) return '—';
      var d = new Date(sec * 1000);
      var p = function (x) { return (x < 10 ? '0' : '') + x; };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
        ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }
    function fmtDay(sec) {
      if (!sec) return '';
      var d = new Date(sec * 1000);
      var p = function (x) { return (x < 10 ? '0' : '') + x; };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
    }
    function row(k, v, mono) {
      var r = api.el('div', 'rp-row');
      r.appendChild(api.el('span', 'rp-k', k));
      r.appendChild(api.el('span', 'rp-v' + (mono ? ' rp-mono' : ''), String(v)));
      return r;
    }
    function dot(cls) { return api.el('span', 'rp-dot ' + cls); }

    var USE_LABEL = { pretrain: '预训练', continue: '继续预训练', sft: '指令微调', lora: 'LoRA 微调', '': '未标注' };

    /* 标注选项按数据类型收口（用户 2026-10-16）：纯文本→预训练/继续预训练，对话→指令微调/LoRA 微调，
       其他（JSONL/未知）四种全开；一个数据集可标两次（两个下拉同一行）。老数据越界值照常显示可改。 */
    var USE_OPTS_ALL = [
      ['pretrain', '预训练'], ['continue', '继续预训练'], ['sft', '指令微调'],
      ['lora', 'LoRA 微调'], ['', '未标注'],
    ];
    function useOptionsFor(f, cur, other) {
      var k = f && f.kind_key;
      var opts = (k === 'plain')
        ? [['pretrain', '预训练'], ['continue', '继续预训练'], ['', '未标注']]
        : (k === 'chat')
          ? [['sft', '指令微调'], ['lora', 'LoRA 微调'], ['', '未标注']]
          : USE_OPTS_ALL.slice();
      if (cur && !opts.some(function (o) { return o[0] === cur; })) {
        opts.splice(opts.length - 1, 0, [cur, USE_LABEL[cur] || cur]);   /* 越界旧值可看可改、不能新加 */
      }
      if (other) {
        opts = opts.filter(function (o) { return o[0] !== other || o[0] === cur; });   /* 一个选项不能标两次（用户 2026-10-16）：另一槽已用的值剔除 */
      }
      return opts;
    }

    /* 当前语言下的文案（用户 2026-10-22）：量宽度要用翻译后的字，英文比中文长 */
    function TR(zh) { return (window.I18N && window.I18N.t) ? window.I18N.t(zh) : zh; }

    /* 训练方式下拉定宽（用户 2026-10-22）：框宽按「该类型全部标注项」里最长那条量死 ——
       选值一换、另一槽换值、切语言都重跑，框宽不跟着文字涨缩。
       用 canvas 量字、内衬/箭头/间距取实测值：**不改标签文字**。原来靠「临时改字再量」，
       会把 i18n 记着的中文文本节点顶掉，切回中文时 restore() 找不回那一段（实测切回中文后
       下拉里还是 Pretrain / Continue Pretrain）。 */
    function fitUsePick(pick) {
      var lb = pick.querySelector('.pick-label');
      var care = pick.querySelector('.pick-caret');
      var opts = pick.__opts || [];
      if (!lb || !opts.length) return;
      var cs = getComputedStyle(lb);
      var cv = fitUsePick._cv || (fitUsePick._cv = document.createElement('canvas'));
      var ctx = cv.getContext('2d');
      ctx.font = (cs.fontStyle || 'normal') + ' ' + (cs.fontWeight || '400') + ' ' +
        (cs.fontSize || '11px') + ' ' + (cs.fontFamily || 'sans-serif');
      var maxw = 0;
      opts.map(function (o) { return TR(o[1]); }).concat([lb.textContent]).forEach(function (t) {
        maxw = Math.max(maxw, ctx.measureText(t).width);
      });
      var cp = getComputedStyle(pick);
      var extra = (parseFloat(cp.paddingLeft) || 0) + (parseFloat(cp.paddingRight) || 0) +
        (parseFloat(cp.borderLeftWidth) || 0) + (parseFloat(cp.borderRightWidth) || 0) +
        (parseFloat(cp.gap) || 0) + (care ? care.getBoundingClientRect().width : 0);
      var w = Math.ceil(maxw + extra);
      /* 英文词更长，两个框 + 类型标签会顶出卡片右缘（实测卡片 329 宽、内宽 301，
         三个控件要 315 → 右间距 -1、标签压到外框上）。这里按卡片内宽算一个上限：
         右侧留白至少等于左侧（卡片内衬），放不下就让标签截断成「Continue Pre…」。 */
      var cap = 0;
      var card = pick.closest ? pick.closest('.rp-card') : null;
      if (card) {
        var csC = getComputedStyle(card);
        var padL = parseFloat(csC.paddingLeft) || 0;
        var padR = parseFloat(csC.paddingRight) || 0;
        var row = card.querySelector('.rp-tags');
        var csR = row ? getComputedStyle(row) : null;
        var gap = csR ? (parseFloat(csR.columnGap || csR.gap) || 8) : 8;
        var tag = row ? row.querySelector('.rp-tag') : null;
        var tagW = tag ? tag.getBoundingClientRect().width : 0;
        var picks = row ? row.querySelectorAll('.rp-use-pick').length : 1;
        var inner = card.clientWidth - padL - padR;
        var room = inner - padL - gap * picks - tagW;   /* 右侧留白 = padL（不小于左侧） */
        if (picks > 0 && room > 40) cap = Math.floor(room / picks);
      }
      if (w <= 0) return;
      var use = cap > 0 && w > cap ? cap : w;
      pick.style.minWidth = use + 'px';
      pick.style.maxWidth = cap > 0 ? cap + 'px' : '';
      pick.classList.toggle('pick-ell', cap > 0 && w > cap);
    }
    function fitAllUsePicks() {
      /* 不能从 root 里找（用户 2026-10-22）：换页用的是「暂存盒」，页面建好后子节点被搬到
         容器里、暂存盒本身空掉，root.querySelectorAll 之后就是空集；按页面类名从文档里找，
         没挂载时自然为空。 */
      Array.prototype.forEach.call(document.querySelectorAll('.rp-view .rp-use-pick'), fitUsePick);
    }

    /* ---------- 段选：数据集 / 模型权重 ---------- */
    var TABS = [['datasets', '数据集'], ['weights', '模型权重']];
    var segWrap = api.el('div', 'rp-segwrap');
    var seg = api.el('div', 'seg rp-seg');
    seg.setAttribute('role', 'tablist');
    var segOpts = {};
    TABS.forEach(function (t) {
      var b = api.el('button', 'seg-opt', t[1]);
      b.type = 'button';
      b.dataset.value = t[0];
      b.setAttribute('role', 'tab');
      b.addEventListener('click', function () { pickTab(t[0]); });
      segOpts[t[0]] = b;
      seg.appendChild(b);
    });
    segWrap.appendChild(seg);
    root.appendChild(segWrap);

    /* ---------- 筛选态（两视图各一套） ---------- */
    var F = {
      ds: { q: '', kind: '', use: '' },
      w: { q: '', method: '', d1: '', d2: '' },
    };

    /* 格式示例（用户 2026-10-15：从数据集页挪到仓库页）：一屏看两套格式（预训练纯文本 / 微调问答） */
    var SAMPLE_KINDS = [
      { label: '预训练数据集', hint: '格式要求：UTF-8纯文本形式的json/txt文件。', samples: [
        '山间的清晨来得慢。雾气先漫过溪谷，再爬上石阶，最后停在半山的松林里；溪水很浅，能看见底下被磨圆的碎石，' +
        '风穿过针叶时，露水就一颗一颗抖落下来，砸在水面上，散成细小的圈。守林人五点半起床，先去柴房添一把干枝，' +
        '再拎着铜壶到泉边接水，火苗舔着壶底，白汽漫过窗棂。他说这山里的日子不看钟，看光：光从东坡移到西坡，' +
        '一天就过去了；等松针上的水汽散尽，鸟声也稀疏下来，就该下山买菜了。\n' +
        '\n' +
        '院子里的桂花开了。\n' +
        '他把旧书摊在石桌上。\n' +
        '风一吹，纸页翻过去两页。\n' +
        '狗在台阶下打盹。\n' +
        '天快黑了，该收衣裳了。'
      ] },
      { label: '微调数据集', hint: '格式要求：问答形式的json/jsonl/txt文件，详细格式查看示例。', samples: [
        { name: '标准问答', code:
          '{"question": "地球为什么会有四季？", "answer": "因为地轴相对公转轨道面有约 23.4° 的倾角。"}' },
        { name: '一问多答', code:
          '{"question": "什么是光合作用？", "answers": ["绿色植物利用光能合成有机物。", "同时释放氧气。"]}' },
        { name: 'HC3 风格', code:
          '{"question": "天空为什么是蓝的？", "human_answers": ["瑞利散射。"], "chatgpt_answers": ["短波散射更强。"]}' },
        { name: '短字段 q/a、prompt/completion', code:
          '{"q": "水的沸点？", "a": "标准大气压下 100 摄氏度。"}\n' +
          '{"prompt": "写一句祝福", "completion": "愿你岁岁平安。"}' },
        { name: 'Alpaca 指令格式', code:
          '{"instruction": "把下面的话翻译成英文", "input": "今天天气很好", "output": "The weather is nice today."}' },
        { name: 'messages / conversations 多轮', code:
          '{"messages": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好，有什么可以帮你？"}]}\n' +
          '{"conversations": [{"from": "human", "value": "为什么天空是蓝的"}, {"from": "gpt", "value": "阳光被大气分子散射，蓝光最容易被散射出来。"}]}' },
        { name: '扁平多轮 role/content', code:
          '{"role": "user", "content": "什么是黑洞"}\n' +
          '{"role": "assistant", "content": "黑洞是引力强到连光都无法逃逸的天体。"}' },
        { name: '分隔符纯文本', code:
          '\\x01什么是重力？\\x02物体之间相互吸引的力。\\x03' },
      ] },
    ];
    function showSample() {
      /* 示例跟随类型筛选（用户 2026-10-15）：选中对话→只要对话例；纯文本→只要纯文本例；全部→两套 */
      var only = F.ds.kind;   /* ''=全部 | 'plain' | 'chat' */
      var mask = api.el('div', 'rp-mask');
      var box = api.el('div', 'rp-modal');
      var head = api.el('div', 'rp-modal-head');
      head.appendChild(api.el('div', 'rp-modal-title', '数据集格式示例'));
      var x = api.el('button', 'rp-modal-x');
      x.type = 'button';
      x.setAttribute('aria-label', '关闭示例');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>';
      head.appendChild(x);
      box.appendChild(head);
      var body = api.el('div', 'rp-modal-body');
      SAMPLE_KINDS.forEach(function (d) {
        /* 按类型筛选只留对应一节（用户 2026-10-15）：纯文本→预训练节、对话→微调节、全部→两节 */
        if (only === 'plain' && d.label.indexOf('预训练') < 0) return;
        if (only === 'chat' && d.label.indexOf('微调') < 0) return;
        body.appendChild(api.el('div', 'rp-modal-section', d.label));
        body.appendChild(api.el('div', 'rp-modal-hint', d.hint || ''));
        (d.samples || []).forEach(function (blk) {
          var item = api.el('div', 'rp-modal-item');
          var name = typeof blk === 'string' ? '' : (blk.name || '');
          var code = typeof blk === 'string' ? blk : (blk.code || '');
          if (name) item.appendChild(api.el('div', 'rp-modal-name', name));
          item.appendChild(api.el('pre', 'rp-modal-pre', code));
          body.appendChild(item);
        });
      });
      box.appendChild(body);

      function onKey(e) { if (e.key === 'Escape') close(); }
      function close() {
        document.removeEventListener('keydown', onKey);
        if (mask.parentNode) mask.parentNode.removeChild(mask);
      }
      x.onclick = close;
      mask.onclick = function (e) { if (e.target === mask) close(); };
      document.addEventListener('keydown', onKey);
      mask.appendChild(box);
      document.body.appendChild(mask);
    }

    function barPick(options, get, set, cls) {
      /* cls：筛选下拉定宽用（rp-pick-narrow/rp-pick-wide，用户 2026-10-15 加宽 30%） */
      var pk = api.pick(api.el('input'), options, ('rp-sel ' + (cls || '')).trim());
      pk._hideSel = true;              /* 列表不重复列「当前已选」项（用户 2026-10-15，仓库筛选下拉统一） */
      pk.value = get();                /* 置 _hideSel 后再 show()：初始态也隐掉已选项 */
      pk.addEventListener('change', function () { set(pk.value); });
      return pk;
    }
    function barSearch(ph, set) {
      var inp = api.el('input', 'rp-search');
      inp.type = 'text';
      inp.placeholder = ph;
      inp.addEventListener('input', function () { set(inp.value.trim()); });
      return inp;
    }
    /* 日期框（用户 2026-10-22）：Chromium 自带的日期弹层是按浏览器 UI 语言画的（英文界面里
       照样是「2026年10月 / 清除 / 今天」），选中日那圈黄框是系统焦点环，上下翻月箭头也偏大，
       这三样都改不动 —— 干脆自绘：文案跟 I18N 走、箭头按我们的尺寸画、焦点样式自己定。 */
    function barDate(set) {
      var L = function (zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; };
      var MONTHS_EN = ['January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November', 'December'];
      var WEEK_ZH = ['一', '二', '三', '四', '五', '六', '日'];
      var WEEK_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

      var wrap = api.el('span', 'rp-datewrap');
      var inp = api.el('input', 'rp-date');
      inp.type = 'text';
      inp.readOnly = true;
      inp.placeholder = L('年/月/日', 'YYYY/MM/DD');
      var btn = api.el('button', 'rp-date-btn');
      btn.type = 'button';
      btn.title = L('选择日期', 'Pick a date');
      btn.innerHTML = '<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true">' +
        '<rect x="3.3" y="4.7" width="13.4" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
        '<path d="M3.3 8.3h13.4M6.9 3.1v3M13.1 3.1v3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
        '</svg>';
      wrap.appendChild(inp);
      wrap.appendChild(btn);

      var pop = null, view = null;   /* view = 当前显示月份的第一天 */

      function isoOf(d) {
        var p = function (n) { return (n < 10 ? '0' : '') + n; };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
      }
      function display(iso) { return iso ? String(iso).replace(/-/g, '/') : ''; }
      function apply(iso) {
        inp.dataset.iso = iso || '';
        inp.value = display(iso);
        set(iso || '');
      }
      function todayIso() {
        var d = new Date();
        return isoOf(new Date(d.getFullYear(), d.getMonth(), d.getDate()));
      }

      function closePop() {
        if (!pop) return;
        if (pop.parentNode) pop.parentNode.removeChild(pop);
        pop = null;
        document.removeEventListener('mousedown', onOutside, true);
        document.removeEventListener('keydown', onKey, true);
        wrap.classList.remove('rp-date-open');
      }
      function onOutside(ev) {
        if (!wrap.contains(ev.target)) closePop();
      }
      function onKey(ev) { if (ev.key === 'Escape') { closePop(); ev.stopPropagation(); } }

      function paint() {
        if (!pop) return;
        var y = view.getFullYear(), m = view.getMonth();
        pop.innerHTML = '';

        var head = api.el('div', 'rp-cal-head');
        var prev = api.el('button', 'rp-cal-nav');
        prev.type = 'button';
        prev.innerHTML = '<svg viewBox="0 0 20 20" width="11" height="11" aria-hidden="true"><path d="M12.4 4.8 7.2 10l5.2 5.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        prev.addEventListener('click', function (ev) { ev.stopPropagation(); view = new Date(y, m - 1, 1); paint(); });
        var next = api.el('button', 'rp-cal-nav');
        next.type = 'button';
        next.innerHTML = '<svg viewBox="0 0 20 20" width="11" height="11" aria-hidden="true"><path d="M7.6 4.8 12.8 10l-5.2 5.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        next.addEventListener('click', function (ev) { ev.stopPropagation(); view = new Date(y, m + 1, 1); paint(); });
        var ttl = api.el('span', 'rp-cal-title', L(y + '年' + (m + 1) + '月', MONTHS_EN[m] + ' ' + y));
        head.appendChild(ttl);
        head.appendChild(prev);
        head.appendChild(next);
        pop.appendChild(head);

        var wk = api.el('div', 'rp-cal-week');
        (window.I18N && window.I18N.lang() === 'en' ? WEEK_EN : WEEK_ZH).forEach(function (w) {
          wk.appendChild(api.el('span', '', w));
        });
        pop.appendChild(wk);

        var grid = api.el('div', 'rp-cal-grid');
        var first = new Date(y, m, 1);
        var lead = (first.getDay() + 6) % 7;            /* 周一开头 */
        var cur = inp.dataset.iso || '';
        var tIso = todayIso();
        for (var i = 0; i < 42; i++) {
          var d = new Date(y, m, 1 - lead + i);
          var iso = isoOf(d);
          var cls = 'rp-cal-day';
          if (d.getMonth() !== m) cls += ' out';
          if (iso === cur) cls += ' sel';
          if (iso === tIso) cls += ' today';
          var b = api.el('button', cls, String(d.getDate()));
          b.type = 'button';
          (function (iso) {
            b.addEventListener('click', function (ev) {
              ev.stopPropagation();
              apply(iso);
              closePop();
            });
          })(iso);
          grid.appendChild(b);
        }
        pop.appendChild(grid);

        var foot = api.el('div', 'rp-cal-foot');
        var clear = api.el('button', 'rp-cal-act', L('清除', 'Clear'));
        clear.type = 'button';
        clear.addEventListener('click', function (ev) { ev.stopPropagation(); apply(''); closePop(); });
        var now = api.el('button', 'rp-cal-act', L('今天', 'Today'));
        now.type = 'button';
        now.addEventListener('click', function (ev) { ev.stopPropagation(); apply(todayIso()); closePop(); });
        foot.appendChild(clear);
        foot.appendChild(now);
        pop.appendChild(foot);
      }

      function openPop() {
        if (pop) { closePop(); return; }
        var base = inp.dataset.iso ? new Date(inp.dataset.iso + 'T00:00:00') : new Date();
        view = new Date(base.getFullYear(), base.getMonth(), 1);
        pop = api.el('div', 'rp-cal');
        wrap.appendChild(pop);
        wrap.classList.add('rp-date-open');
        paint();
        setTimeout(function () {
          document.addEventListener('mousedown', onOutside, true);
          document.addEventListener('keydown', onKey, true);
        }, 0);
      }

      btn.addEventListener('click', function (ev) { ev.stopPropagation(); openPop(); });
      inp.addEventListener('click', function () { openPop(); });
      inp.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openPop(); }
      });
      /* 切语言：占位字跟当前语言（弹层里是现画的，下次打开就是新语言） */
      if (window.I18N && window.I18N.on) {
        window.I18N.on(function () {
          inp.placeholder = L('年/月/日', 'YYYY/MM/DD');
          btn.title = L('选择日期', 'Pick a date');
          closePop();
        });
      }
      return wrap;
    }

    /* 数据集筛选栏：文本查找（主题与文件夹名称）/ 类型 / 训练方式 —— 排布按图一（用户 2026-10-15）：
       整组钉行尾右侧、搜索在前、筛选在后 */
    var barDs = api.el('div', 'rp-bar');
    /* 格式示例按钮（用户 2026-10-15：格式示例放到仓库页）：靠左，筛选整组钉右 */
    var sampleBtn = api.el('button', 'rp-sample-btn', '示例');
    sampleBtn.type = 'button';
    sampleBtn.title = '数据集格式示例';
    sampleBtn.onclick = function () { showSample(); };
    barDs.appendChild(barSearch('搜索', function (v) { F.ds.q = v; renderCur(); }));
    /* 类型筛选（用户 2026-10-15）：选项排序 全部>纯文本>对话、「其他」直接删掉；
       打开时列表不重复列「当前已选」项（_hideSel） */
    var kindPick = barPick(
      [['', '全部'], ['plain', '文本'], ['chat', '对话']],
      function () { return F.ds.kind; },
      function (v) { F.ds.kind = (v === '' || v == null) ? '' : v; renderCur(); }, 'rp-pick-narrow');
    barDs.appendChild(kindPick);
    barDs.appendChild(barPick(
      [['', '全部'], ['pretrain', '预训练'], ['continue', '继续预训练'],
       ['sft', '指令微调'], ['lora', 'LoRA 微调'], ['none', '未标注']],
      function () { return F.ds.use; },
      function (v) { F.ds.use = (v == null) ? '' : v; renderCur(); }, 'rp-pick-wide'));
    /* 「示例」钉在筛选栏最右（用户 2026-10-20：空白卡删了，示例按钮回到栏里） */
    barDs.appendChild(sampleBtn);

    /* 权重筛选栏：文本查找（权重名称）/ 训练方式 / 日期筛选 */
    var barW = api.el('div', 'rp-bar');
    barW.appendChild(barSearch('搜索', function (v) { F.w.q = v; renderCur(); }));
    barW.appendChild(barPick(
      [['', '全部'], ['pretrain', '预训练'], ['continue', '继续预训练'],
       ['sft', '指令微调'], ['lora', 'LoRA 微调'], ['merge', 'LoRA 合并'], ['other', '其他']],
      function () { return F.w.method; },
      function (v) { F.w.method = (v == null) ? '' : v; renderCur(); }, 'rp-pick-wide'));
    barW.appendChild(api.el('span', 'rp-dl', '起'));
    barW.appendChild(barDate(function (v) { F.w.d1 = v || ''; renderCur(); }));
    barW.appendChild(api.el('span', 'rp-dl', '止'));
    barW.appendChild(barDate(function (v) { F.w.d2 = v || ''; renderCur(); }));

    var panels = api.el('div', 'rp-panels');
    root.appendChild(panels);
    var pDs = api.el('div', 'rp-view');
    var pW = api.el('div', 'rp-view');
    pDs.dataset.tab = 'datasets';
    pW.dataset.tab = 'weights';
    pW.hidden = true;
    var gridDs = api.el('div', 'rp-grid');
    var gridW = api.el('div', 'rp-grid');
    pDs.appendChild(barDs);
    pDs.appendChild(gridDs);
    pW.appendChild(barW);
    pW.appendChild(gridW);
    panels.appendChild(pDs);
    panels.appendChild(pW);

    /* ---------- 文件夹（用户 2026-10-20：权重/数据集的文件夹系统） ---------- */
    /* OPEN = 各视图当前打开的文件夹（''=根）；卡片双击打开，「‹ 返回」回根。
       面包屑并进筛选栏（用户 2026-10-20）：文件夹名 | ‹ 返回 | 搜索栏…（返回在搜索左、名字在返回左） */
    var OPEN = { datasets: '', weights: '' };
    var crumbDs = api.el('div', 'rp-crumb');
    var crumbW = api.el('div', 'rp-crumb');
    barDs.insertBefore(crumbDs, barDs.firstChild);
    barW.insertBefore(crumbW, barW.firstChild);
    /* 「返回」单独一只按钮、固定钉在搜索栏左边（用户 2026-10-20：不要跟着文件夹名跑到行首）；
       只有进到文件夹里才显形 */
    var backBtns = {
      datasets: api.el('button', 'rp-crumb-back', '返回'),
      weights: api.el('button', 'rp-crumb-back', '返回'),
    };
    Object.keys(backBtns).forEach(function (tab) {
      var b = backBtns[tab];
      b.type = 'button';
      b.hidden = true;
      b.addEventListener('click', function () { OPEN[tab] = ''; renderCur(); });
      var bar = (tab === 'datasets') ? barDs : barW;
      var crumb = (tab === 'datasets') ? crumbDs : crumbW;
      bar.insertBefore(b, crumb.nextSibling);
    });
    /* 文件夹名左边缘对齐卡片左边框（用户 2026-10-20）：筛选栏和网格在不同宽度下
       可能各自有内缩，量出来差多少就补多少，比写死 padding 稳。 */
    function alignCrumb(tab) {
      var view = (tab === 'weights') ? pW : pDs;
      if (!view || view.hidden) return;
      var crumb = (tab === 'weights') ? crumbW : crumbDs;
      var grid = (tab === 'weights') ? gridW : gridDs;
      if (!crumb || crumb.hidden || !grid) return;
      crumb.style.marginLeft = '0px';
      var cl = crumb.getBoundingClientRect().left;
      var gl = grid.getBoundingClientRect().left;
      var d = Math.round((gl - cl) * 10) / 10;
      if (Math.abs(d) >= 0.5) crumb.style.marginLeft = d + 'px';
    }
    function alignCrumbAll() { alignCrumb('datasets'); alignCrumb('weights'); }
    window.addEventListener('resize', function () { alignCrumbAll(); });

    function renderCrumb(crumb, tab) {
      crumb.textContent = '';
      var fn = OPEN[tab] || '';
      crumb.hidden = !fn;
      backBtns[tab].hidden = !fn;
      if (!fn) return;
      crumb.appendChild(api.el('span', 'rp-crumb-now', fn));   /* 只剩文件夹名，钉行首 */
    }

    /* 卡片进场动画（用户 2026-10-17：与数据集页同款）：每个视图首次露出来时播一次；
       切页签、首次 mount 各一次，搜索/筛选造成的重渲染不重播。 */
    var entered = { datasets: false, weights: false };
    function playEnter(grid, key) {
      if (entered[key] || !grid) return;
      var cards = [].slice.call(grid.children);
      if (!cards.length) return;
      cards.forEach(function (c, i) { c.style.setProperty('--rp-i', Math.min(i, 8)); });
      /* .enter 留在网格上，等动画真正结束（animationend）再摘：预取时网格是 display:none，
         动画不会跑，但类先挂好 → 页面一露出来就跟着首帧起播（不会先满不透明再补动画）；
         隐藏期间永远不跑，类就一直留着，等真正可见时再播。 */
      grid.classList.add('enter');
      entered[key] = true;
      grid.addEventListener('animationend', function onEnd(e) {
        if (e.target.parentNode !== grid) return;
        grid.classList.remove('enter');
        grid.removeEventListener('animationend', onEnd);
      });
    }

    /* 删除请求体（用户 2026-10-18）：模型权重不在 data/ 下，按名字附上真实路径，
       后端校验路径必须在本工程内才删。数据集照旧只传名字。
       文件夹（2026-10-20）：'folder:<名>' 键走 folders 通道（删文件夹=删里面的文件）。 */
    function delBody(names) {
      var body = { names: [] };
      var folders = [];
      (names || []).forEach(function (n) {
        var s = String(n || '');
        if (s.indexOf('folder:') === 0) folders.push(s.slice(7));
        else body.names.push(s);
      });
      if (folders.length) body.folders = folders;
      if (!body.names.length && !folders.length) body.names = (names || []).slice();
      var ws = (DATA && DATA.weights) || [];
      var map = {};
      for (var i = 0; i < ws.length; i++) {
        var w = ws[i] || {};
        if (w.name && w.path && body.names.indexOf(w.name) >= 0) map[w.name] = w.path;
      }
      if (Object.keys(map).length) body.paths = map;
      return body;
    }

    function pickTab(key) {
      entered[key] = false;                    /* 切过来 = 一次新的「出现」，重播一次 */
      TABS.forEach(function (t) {
        var on = t[0] === key;
        segOpts[t[0]].classList.toggle('on', on);
        segOpts[t[0]].setAttribute('aria-selected', on ? 'true' : 'false');
      });
      pDs.hidden = key !== 'datasets';
      pW.hidden = key !== 'weights';
      api.segThumb(seg, true);
      /* 切视图补量「+」卡高（用户 2026-10-16）：隐藏网格里卡片 offsetHeight=0 会被跳过，
         切出来后必须重测，否则「+」卡停在 CSS min-height 148 比普通卡高/矮不齐 */
      fitAllUsePicks();   /* 标注框定宽（用户 2026-10-22）：必须在卡片挂进 DOM 之后量 */
      sizeAddCards();
      playEnter(key === 'datasets' ? gridDs : gridW, key);
    }

    /* ---------- 卡片 ---------- */
    function clip50(s) { s = String(s == null ? '' : s); return s.length > 50 ? s.slice(0, 50) : s; }

    /* 超长文本：鼠标移上去自动向右滚（用户 2026-10-15），右缘由半透明渐入实体由 CSS mask 负责 */
    function scrollText(text, cls, mode) {
      var wrap = api.el('div', 'rp-scroll ' + (cls || ''));
      var inner = api.el('span', 'rp-scroll-in', text);
      wrap.appendChild(inner);
      /* 渐隐判定按像素长度（用户 2026-10-16）：名称=「articleZL_20261013_204232_articles.txt」+4字符 的像素长；
         主题=不超过框宽 2/3 不渐隐。只有判定需要时才上渐隐（.rp-scroll-cap） */
      wrap.__capCheck = function () {
        var w = inner.scrollWidth, bw = wrap.clientWidth, need;
        if (!bw) { setTimeout(wrap.__capCheck, 80); return; }   /* 未入布局先重试，别误判 */
        if (mode === 'theme') {
          need = w > bw * 2 / 3;
        } else {
          if (!scrollText.__refW) {
            var cv = document.createElement('canvas').getContext('2d');
            var cs2 = getComputedStyle(inner);
            cv.font = (cs2.fontWeight || '400') + ' ' + (cs2.fontSize || '13px') + ' ' + (cs2.fontFamily || 'sans-serif');
            scrollText.__refW = cv.measureText('articleZL_20261013_204232_articles.txt0000').width;
          }
          need = w > scrollText.__refW;
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
        void inner.offsetWidth;          /* 重启动画 */
        inner.classList.add('rp-scrolling');
        wrap.classList.add('rp-scroll-on');   /* 滚动时左缘也渐隐（用户 2026-10-15） */
      });
      wrap.addEventListener('mouseleave', function () {
        inner.classList.remove('rp-scrolling');
        wrap.classList.remove('rp-scroll-on');
      });
      return wrap;
    }

    /* 锁标记（用户 2026-10-20）：锁上的权重/数据集在 EAI 内不许删，卡头右侧挂个小锁 */
    function lockBadge(on, owner) {
      var b = api.el('span', 'rp-lock' + (on ? ' on' : ''));
      b.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
      /* owner = 锁住它的文件夹名（文件夹锁向下继承，用户 2026-10-20） */
      b.title = owner ? '随文件夹「' + owner + '」锁定：文件夹不解锁就不能单独解锁'
                      : '已锁定：EAI 内不能删除';
      return b;
    }

    function dataCard(f) {
      var c = api.el('div', 'card rp-card');
      var head = api.el('div', 'rp-head');
      /* 格式合不合格由卡片左上角圆球颜色表示（用户 2026-10-15）：绿=合格、红=不合格 */
      head.appendChild(dot((f.ok && f.fmt_ok) ? 'ok' : 'err'));
      /* 名称超 50 字只取前 50；悬停自动向右滚 + 右缘渐隐（用户 2026-10-15） */
      var title = scrollText(clip50(f.name || '（未命名）'), 'rp-title', 'name');
      title.title = f.path || '';
      head.appendChild(title);
      if (f.locked) head.appendChild(lockBadge(true, f.lock_folder));
      c.appendChild(head);
      /* 训练方式（下拉可改）+ 数据类型：标题下方、小字上方一行，两框等高（用户 2026-10-15） */
      var tags = api.el('div', 'rp-tags');
      /* 两个训练方式标注下拉（用户 2026-10-16：与数据类型标签同一行、一个数据集可标两次）；
         选项按数据类型收口（useOptionsFor） */
      function usePickFor(slot) {
        var cur = (slot === 2 ? (f.use2 || '') : (f.use || ''));
        var other = (slot === 2 ? (f.use || '') : (f.use2 || ''));
        var pick = api.pick(api.el('input'), useOptionsFor(f, cur, other), 'rp-use-pick');
        pick._hideSel = 1;   /* 弹出选项不列「当前已选」（用户 2026-10-16）；closed 态照常显示当前值 */
        /* 定宽（用户 2026-10-22）：框宽按「该数据类型全部标注项」里最长那条量死，
           选值一换、另一槽换值、切语言都只重跑量化、不跟着文字涨缩。
           全量选项（不是 filtered）才是稳定基准 —— 原来按弹层克隆量，弹层会剔除
           「当前已选」和「另一槽已用」，于是选什么宽什么。 */
        pick.classList.add('pick-fit');
        pick.__opts = useOptionsFor(f, '', '');
        pick.value = cur;
        pick.addEventListener('change', function () {
          var send = pick.value;
          var otherNow = (slot === 2 ? (f.use || '') : (f.use2 || ''));
          if (send && send === otherNow) { pick.value = cur; return; }   /* 一个选项不能标两次：兜底拦（选项里已剔除） */
          api.post('/api/repo/dataset_use', { name: f.name, use: send, slot: slot }).then(function (d) {
            if (!d || d.error || d.ok === false) {
              api.notice('训练方式保存失败', [String((d && d.error) || '未知错误')]);   /* 自绘弹窗（用户 2026-10-21：不用系统原生框） */
              pick.value = (slot === 2 ? (f.use2 || '') : (f.use || ''));
              return;
            }
            if (slot === 2) { f.use2 = (d.use2 != null ? d.use2 : send); }
            else { f.use = (d.use != null ? d.use : send); }
            renderCur();   /* 按训练方式筛的命中可能变 */
          });
        });
        return pick;
      }
      tags.appendChild(usePickFor(1));
      tags.appendChild(usePickFor(2));
      /* 数据类型标签（用户 2026-10-22）：纯文本改叫「文本」，中英都改（Text） */
      tags.appendChild(api.el('div', 'rp-tag' +
        (f.kind_key === 'chat' ? ' rp-tag-chat' : (f.kind_key === 'plain' ? ' rp-tag-plain' : '')),
        (f.kind_key === 'plain') ? '文本' : (f.kind || '—')));
      c.appendChild(tags);
      /* 路径 = 到文件夹那一层级（用户 2026-10-16）：不再只显文件夹名 */
      var dirPath = f.path ? String(f.path).replace(/[\\/][^\\/]*$/, '') : (f.folder || '—');
      var dirRow = api.el('div', 'rp-row');
      dirRow.appendChild(api.el('span', 'rp-k', '路径'));
      dirRow.appendChild(scrollText(dirPath, 'rp-v rp-scroll-v', ''));
      dirRow.title = dirPath;
      var themeRow = api.el('div', 'rp-row');
      themeRow.appendChild(api.el('span', 'rp-k', '主题'));
      themeRow.appendChild(scrollText((f.themes && f.themes.length) ? clip50(f.themes.join('、')) : '—', 'rp-v rp-scroll-v', 'theme'));
      c.appendChild(themeRow);
      c.appendChild(row('体量', fmtSize(f.size_bytes)));
      /* 备注显示在体量下面（用户 2026-10-16） */
      c.appendChild(row('备注', f.remark || '—'));
      c.appendChild(dirRow);   /* 路径放最后一行（用户 2026-10-16） */
      /* 字符/行数不显示；格式=左上角圆球颜色（用户 2026-10-15） */
      bindMenu(c, f, false);
      bindCheck(c, f);
      return c;
    }

    /* 右键卡片 → 大弹窗看完整数据集（用户 2026-10-15）；弹窗层级低于 AI 窗口 */
    function showDataset(f) {
      var mask = api.el('div', 'rp-mask rp-ds-mask');
      var box = api.el('div', 'rp-modal rp-ds-modal');
      var head = api.el('div', 'rp-modal-head');
      head.appendChild(api.el('div', 'rp-modal-title', f.name || '（未命名）'));
      var x = api.el('button', 'rp-modal-x');
      x.type = 'button';
      x.setAttribute('aria-label', '关闭数据集');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>';
      head.appendChild(x);
      box.appendChild(head);
      var wrap = api.el('div', 'rp-ds-wrap');
      var body = api.el('div', 'rp-ds-body');
      var pre = api.el('pre', 'rp-ds-pre', '正在读取数据集…');
      body.appendChild(pre);
      /* 顶/底滚动渐隐（用户 2026-10-15）：约一行高、按距边距离显隐 */
      var fadeTop = api.el('div', 'rp-ds-fade rp-ds-fade-top');
      var fadeBot = api.el('div', 'rp-ds-fade rp-ds-fade-bot');
      wrap.appendChild(body);
      wrap.appendChild(fadeTop);
      wrap.appendChild(fadeBot);
      box.appendChild(wrap);
      (function () {
        var H = 22;
        function upd() {
          var t = body.scrollTop;
          var b = body.scrollHeight - body.clientHeight - body.scrollTop;
          fadeTop.style.opacity = Math.min(1, Math.max(0, t / H));
          fadeBot.style.opacity = Math.min(1, Math.max(0, b / H));
        }
        body.addEventListener('scroll', upd);
        setTimeout(upd, 60);
      })();
      mask.appendChild(box);
      function close() { if (mask.parentNode) mask.parentNode.removeChild(mask); }
      x.addEventListener('click', close);
      mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) close(); });
      document.addEventListener('keydown', function onk(e) {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onk); }
      });
      document.body.appendChild(mask);
      api.get('/api/repo/dataset_text?name=' + encodeURIComponent(f.name || '')).then(function (d) {
        if (!d || d.ok === false || d.text == null) {
          pre.textContent = '数据集读取失败：' + ((d && d.error) || '未知错误');
          return;
        }
        /* 按条目形态排版：走共享渲染器 api.datasetRecCards（用户 2026-10-16：蒸馏页查看对话同仓库格式） */
        var parsed = api.datasetRecCards(d.text, body);
        var recs = parsed.recs;
        /* 被 200KB 截断时末尾可能剩半截行：只要丢的行不超过 1 行，仍然按条目排版
           （用户 2026-10-20：以前会整体退回原文，一问一答的格式就没了） */
        if (recs.length && (parsed.allJson || parsed.lost <= 1)) {
          pre.style.display = 'none';
        } else {
          /* 纯文本：直接原文显示（用户 2026-10-16：原生 txt 没记主题/日期，删掉小头与分割线） */
          pre.textContent = d.text + (d.truncated ? '\n\n…（文件过大，已截断显示）' : '');
        }
        if (d.truncated) body.appendChild(api.el('div', 'rp-ds-note', '…（文件过大，已截断显示）'));
        if (!recs.length && !(d.records && d.records.length) && !String(d.text || '').trim()) {
          /* 只有真没内容才算空（用户 2026-10-16：无 .meta 侧车的原生 txt 正文曾被这守卫覆盖成「（空数据集）」） */
          pre.style.display = '';
          pre.textContent = '（空数据集）';
        }
      });
    }

    /* 训练方式标签分色（用户 2026-10-16）：一种训练方式一种颜色，一眼分清（双类名压基础 .rp-tag） */
    var TAG_CLS = {
      '预训练': 'rp-tag-pretrain', '继续预训练': 'rp-tag-continue',
      '指令微调': 'rp-tag-sft', 'LoRA 微调': 'rp-tag-lora',
      'LoRA 合并': 'rp-tag-merge',
      '模型裁切': 'rp-tag-prune', '量化': 'rp-tag-quant',
    };
    function methodTag(label) {
      var text = String(label || '其他');
      return api.el('span', 'rp-tag ' + (TAG_CLS[text] || ''), text);
    }

    function weightCard(w) {
      var c = api.el('div', 'card rp-card');
      var head = api.el('div', 'rp-head');
      head.appendChild(dot('ok'));   /* 能出卡的都是真实存在的权重 */
      /* 大标题=文件名（用户 2026-10-16）；训练方式进「上一次训练」行 */
      var title = api.el('div', 'rp-title', w.name || '（未命名）');
      title.title = w.path || '';
      head.appendChild(title);
      if (w.locked) head.appendChild(lockBadge(true, w.lock_folder));
      c.appendChild(head);
      /* 训练方式标签（用户 2026-10-16）：不带文本、从左起排，与数据集卡标签行同款同位；
         最近 3 次、最左=上一次，无历史回退单个 method_label 标签 */
      var tTags = api.el('div', 'rp-tags');
      var hist3 = (w.hist || []).slice(-3).reverse();
      if (hist3.length) {
        hist3.forEach(function (h) {
          var tag = methodTag(h.m);
          if (h.t) tag.title = fmtTime(h.t);
          tTags.appendChild(tag);
        });
      } else {
        tTags.appendChild(methodTag(w.method_label));
      }
      c.appendChild(tTags);
      c.appendChild(row('大小', fmtSize(w.size)));
      c.appendChild(row('更新时间', fmtTime(w.mtime)));
      c.appendChild(row('备注', w.remark || '—'));
      /* 来源改路径、放最后一行（用户 2026-10-16）：与数据卡同款到文件夹层级 */
      var dirPath = w.path ? String(w.path).slice(0, Math.max(w.path.lastIndexOf('\\'), w.path.lastIndexOf('/'))) : (w.source || '—');
      var dirRow = api.el('div', 'rp-row');
      dirRow.appendChild(api.el('span', 'rp-k', '路径'));
      dirRow.appendChild(scrollText(dirPath, 'rp-v rp-scroll-v', ''));
      dirRow.title = dirPath;
      c.appendChild(dirRow);
      bindMenu(c, w, true);
      bindCheck(c, w);
      return c;
    }

    /* ---------- 右键菜单（用户 2026-10-16：右键=菜单栏） ---------- */
    function closeMenu() {
      var m = document.querySelector('.rp-menu');
      if (m && m.parentNode) m.parentNode.removeChild(m);
      document.removeEventListener('mousedown', onMenuAway, true);
      document.removeEventListener('keydown', onMenuKey, true);
    }
    function onMenuAway(ev) {
      var m = document.querySelector('.rp-menu');
      if (m && !m.contains(ev.target)) closeMenu();
    }
    function onMenuKey(ev) { if (ev.key === 'Escape') closeMenu(); }
    function showMenu(x, y, items) {
      closeMenu();
      var m = api.el('div', 'rp-menu');
      items.forEach(function (it) {
        if (it === '-') { m.appendChild(api.el('div', 'rp-menu-sep')); return; }
        var mi = api.el('button', 'rp-menu-item' + (it.danger ? ' danger' : ''));
        mi.type = 'button';
        mi.textContent = it.label;
        if (it.disabled) mi.disabled = true;   /* 置灰项点不动（被锁的删除/批量删除，用户 2026-10-20） */
        mi.addEventListener('click', function () { closeMenu(); it.run(); });
        m.appendChild(mi);
      });
      document.body.appendChild(m);
      var r = m.getBoundingClientRect();
      var px = Math.min(x, window.innerWidth - r.width - 8);
      var py = Math.min(y, window.innerHeight - r.height - 8);
      m.style.left = Math.max(8, px) + 'px';
      m.style.top = Math.max(8, py) + 'px';
      setTimeout(function () {
        document.addEventListener('mousedown', onMenuAway, true);
        document.addEventListener('keydown', onMenuKey, true);
      }, 0);
    }
    /* ---------- 锁定 / 复制（用户 2026-10-20） ---------- */

    function toggleLock(f) {
      var want = !f.locked;
      var run = function () {
        api.post('/api/repo/lock', { name: f.name, locked: want }).then(function (d) {
          if (!d || d.error || d.ok === false) {
            api.notice('操作失败', [String((d && d.error) || '未知错误')]);
            return;
          }
          f.locked = want;
          renderCur();       /* 切换类操作不弹提示，卡片上的小锁就是反馈 */
        });
      };
      /* 解锁要二次确认（用户 2026-10-21）：解锁后此文件可被删除；锁定不打扰 */
      if (!want) {
        api.notice('解锁', ['确定解锁「' + f.name + '」？解锁后可被删除。'], {
          okText: '解锁', cancelText: '取消', onOk: run,
        });
        return;
      }
      run();
    }

    function promptCopy(f, isWeight) {
      var base = String(f.name || '');
      var dot = base.lastIndexOf('.');
      var suf = RL('_副本', '_copy');      /* 2026-10-23：英文界面用 _copy */
      var def = dot > 0 ? (base.slice(0, dot) + suf + base.slice(dot)) : (base + suf);
      api.prompt({
        title: (isWeight ? '复制权重' : '复制数据集'),
        label: '新文件名',
        value: def,
        okText: '复制',
        cancelText: '取消',
      }).then(function (nm) {
        if (nm === null) return;
        var name = String(nm || '').trim();
        if (!name) {
          api.notice('复制失败', ['文件名不能为空']);
          return;
        }
        api.post('/api/repo/copy', { path: f.path, new_name: name }).then(function (d) {
          if (!d || d.error || d.ok === false) {
            api.notice('复制失败', [String((d && (d.error || d.reason)) || '未知错误')]);
            return;
          }
          load(true);      /* 复制完刷新一页，新卡片按更新时间排在最前 */
        });
      });
    }

    /* ---------- 文件夹：新建 / 移动（用户 2026-10-20） ---------- */
    function promptNewFolder(moveName) {
      api.prompt({
        title: '新建文件夹',
        label: '文件夹名',
        value: '',
        okText: '创建',
        cancelText: '取消',
      }).then(function (nm) {
        if (nm === null) return;
        var name = String(nm || '').trim();
        if (!name) { api.notice('创建失败', ['文件夹名不能为空']); return; }
        /* kind = 从哪个页签建的（用户 2026-10-22）：空文件夹只在那个页签出现 */
        api.post('/api/repo/folder_create', { name: name, kind: curTab() }).then(function (d) {
          if (!d || d.error || d.ok === false) {
            api.notice('创建失败', [String((d && d.error) || '未知错误')]);
            return;
          }
          if (!moveName) { load(true); return; }
          api.post('/api/repo/item_folder', { names: [moveName], folder: name }).then(function (d2) {
            if (!d2 || d2.error || d2.ok === false) {
              api.notice('移动失败', [String((d2 && d2.error) || '未知错误')]);
              return;
            }
            load(true);
          });
        });
      });
    }

    /* 可移动到的文件夹（用户 2026-10-22）：数据集与权重隔离 ——
       数据集只能进「没有权重」的文件夹，权重只能进「没有数据集」的文件夹；
       空的自建文件夹两边都出现；已经在里面的那个不列 */
    function moveTargets(isWeight) {
      var cur = OPEN[curTab()] || '';
      return ((DATA && DATA.folders) || []).filter(function (fd) {
        if (fd.name === cur) return false;
        var nd = fd.n_ds || 0, nw = fd.n_wt || 0;
        return isWeight ? (nd === 0) : (nw === 0);
      });
    }
    function moveNamesTo(names, folder) {
      api.post('/api/repo/item_folder', { names: names, folder: folder }).then(function (d) {
        if (!d || d.error || d.ok === false) { api.notice('移动失败', [String((d && d.error) || '未知错误')]); return; }
        load(true);
      });
    }
    /* 移动到文件夹：只列同类文件夹；没有「根目录」项、也不在这里新建文件夹（用户 2026-10-22） */
    function promptMove(f, isWeight, x, y) {
      var folders = moveTargets(isWeight);
      if (!folders.length) { api.notice(RL('移动到文件夹', 'Move to folder'), [RL('没有可用的文件夹。', 'No folders available.')]); return; }
      showMenu(x, y, folders.map(function (fd) {
        return {
          label: (f.vfolder === fd.name ? '√ ' : '') + fd.name,
          run: function () { moveNamesTo([f.name], fd.name); },
        };
      }));
    }
    /* 批量移动到文件夹（用户 2026-10-22）：选中的一起挪，只列同类文件夹 */
    function promptBatchMove(names) {
      var isWeight = curTab() === 'weights';
      var folders = moveTargets(isWeight);
      if (!folders.length) { api.notice(RL('移动所选', 'Move selected'), [RL('没有可用的文件夹。', 'No folders available.')]); return; }
      var bar = document.querySelector('.rp-selbar .nt-ok');
      var r = bar ? bar.getBoundingClientRect() : { left: 200, bottom: 200 };
      showMenu(Math.round(r.left), Math.round(r.bottom + 6), folders.map(function (fd) {
        return { label: fd.name, run: function () {
          moveNamesTo(names, fd.name);
          exitSelMode(false);
        } };
      }));
    }

    function bindMenu(card, f, isWeight) {
      /* 双击手势（用户 2026-10-21）：数据集文件卡=查看内容；权重卡=查看详细信息。
         卡上的下拉/勾选框等控件不触发；批量选择中双击不打开。 */
      card.addEventListener('dblclick', function (ev) {
        ev.preventDefault();
        if (SEL_MODE) return;
        var t = ev.target;
        if (t && t.closest && t.closest('.pick, .rp-check, input, button, select, textarea')) return;
        if (isWeight) showDetails(f, true);
        else showDataset(f);
      });
      card.addEventListener('contextmenu', function (ev) {
        ev.preventDefault();
        if (SEL_MODE) return;   /* 批量选择中右键不弹菜单 */
        /* 菜单分组（用户 2026-10-16）：置顶、备注 / 分隔线 / 删除、批量删除 / 分隔线 / 查看… */
        var inFolder = !!OPEN[curTab()];
        var items = [
          { label: f.pinned ? '取消置顶' : '置顶', run: function () {
            api.post('/api/repo/mark', { name: f.name, pinned: !f.pinned }).then(function (d) {
              if (!d || d.error || d.ok === false) { api.notice('操作失败', [String((d && d.error) || '未知错误')]); return; }
              f.pinned = !f.pinned;
              renderCur();
            });
          } },
          { label: '备注', run: function () { promptRemark(f); } },
          '-',
          /* 锁（用户 2026-10-20）：锁上后 EAI 内不许删；右键可解锁 / 复制一份 */
          /* 随文件夹锁定的成员（用户 2026-10-20）：解锁项置灰 —— 文件夹不解锁，里面的文件不许解锁 */
          { label: f.lock_folder ? '解锁（随文件夹锁定）' : (f.locked ? '解锁' : '锁定'),
            disabled: !!f.lock_folder,
            run: function () { toggleLock(f); } },
          { label: '复制', run: function () { promptCopy(f, isWeight); } },
          /* 移动分组上方那条分割线（用户 2026-10-22） */
          '-',
        ];
        /* 移动分组（用户 2026-10-22）：在根目录 = 移动到文件夹 / 批量移动到文件夹；
           已经在某个文件夹里 = 移出文件夹 / 批量移出（不列别的文件夹，也不能在这里新建文件夹） */
        if (inFolder) {
          items.push(
            { label: '移出文件夹', run: function () { moveNamesTo([f.name], ''); } },
            { label: '批量移出', run: function () { enterSelMode(f, 'move'); } });
        } else {
          items.push(
            { label: '移动到文件夹', run: function () { promptMove(f, isWeight, ev.clientX, ev.clientY); } },
            { label: '批量移动到文件夹', run: function () { enterSelMode(f, 'move'); } });
        }
        items.push(
          '-',
          /* 被锁的把「删除/批量删除」直接置灰（用户 2026-10-20）：
             不标「删除（已锁定）」、点了也不弹「已锁定」 */
          { label: '删除', danger: true, disabled: !!f.locked, run: function () {
            api.notice(RL('删除文件：', 'Delete file:'),
              [RL('确定将「' + f.name + '」移入回收站', 'Move “' + f.name + '” to the Recycle Bin')],
              { okText: '删除', cancelText: '取消', onOk: function () {
                api.post('/api/repo/delete_files', delBody([f.name])).then(function (d) {
                  if (!d || d.error || d.ok === false) { api.notice('删除失败', [String((d && d.error) || ((d && d.errors) || []).join('；') || '未知错误')]); return; }
                  load(true);
                });
              } });
          } },
          { label: '批量删除', danger: true, disabled: !!f.locked, run: function () { enterSelMode(f); } },
          '-');
        if (!isWeight) items.push({ label: '查看内容', run: function () { showDataset(f); } });
        items.push({ label: '查看详细信息', run: function () { showDetails(f, isWeight); } });
        showMenu(ev.clientX, ev.clientY, items);
      });
    }

    /* ---------- 文件夹卡与文件夹菜单（用户 2026-10-20） ----------
       卡片大小/布局与权重卡一致（同 .rp-card、同四行）；双击打开；
       右键菜单 = 单文件菜单把「查看内容/查看详细信息」换成「打开文件夹」。 */
    function promptFolderCopy(fd) {
      api.prompt({
        title: '复制文件夹',
        label: '新文件夹名',
        value: (fd.name || '') + RL('_副本', '_copy'),   /* 2026-10-23：英文界面用 _copy */
        okText: '复制',
        cancelText: '取消',
      }).then(function (nm) {
        if (nm === null) return;
        var name = String(nm || '').trim();
        if (!name) { api.notice('复制失败', ['文件夹名不能为空']); return; }
        api.post('/api/repo/folder_copy', { name: fd.name, new_name: name }).then(function (d) {
          if (!d || d.error || d.ok === false) {
            api.notice('复制失败', [String((d && (d.error || d.reason)) || '未知错误')]);
            return;
          }
          load(true);
        });
      });
    }

    /* 重命名文件夹（用户 2026-10-22）：后端做重名审查（权重/数据集共用名字空间、不能与文件同名） */
    function promptFolderRename(fd, tab) {
      api.prompt({
        title: '重命名文件夹',
        label: '新文件夹名',
        value: fd.name || '',
        okText: '重命名',
        cancelText: '取消',
      }).then(function (nm) {
        if (nm === null) return;
        var name = String(nm || '').trim();
        if (!name) { api.notice('重命名失败', ['文件夹名不能为空']); return; }
        if (name === fd.name) return;
        api.post('/api/repo/folder_rename', { name: fd.name, new_name: name }).then(function (d) {
          if (!d || d.error || d.ok === false) {
            api.notice('重命名失败', [String((d && d.error) || '未知错误')]);
            return;
          }
          if (OPEN[tab] === fd.name) OPEN[tab] = name;   /* 正开着这个文件夹时跟着改 */
          load(true);
        });
      });
    }

    function openFolder(fd, tab) {
      OPEN[tab] = fd.name || '';
      renderCur();
    }

    function bindFolderMenu(card, fd, tab) {
      card.addEventListener('contextmenu', function (ev) {
        ev.preventDefault();
        if (SEL_MODE) return;
        showMenu(ev.clientX, ev.clientY, [
          { label: fd.pinned ? '取消置顶' : '置顶', run: function () {
            api.post('/api/repo/mark', { name: fd.name, pinned: !fd.pinned }).then(function (d) {
              if (!d || d.error || d.ok === false) { api.notice('操作失败', [String((d && d.error) || '未知错误')]); return; }
              load(true);
            });
          } },
          { label: '备注', run: function () { promptRemark(fd); } },
          '-',
          { label: fd.locked ? '解锁' : '锁定', run: function () { toggleLock(fd); } },
          { label: '复制', run: function () { promptFolderCopy(fd); } },
          { label: '重命名', run: function () { promptFolderRename(fd, tab); } },
          '-',
          /* 被锁的把「删除/批量删除」直接置灰（用户 2026-10-20）：同单文件菜单 */
          { label: '删除', danger: true, disabled: !!fd.locked, run: function () {
            api.notice(RL('删除文件夹：', 'Delete folder:'),
              [RL('确定将「' + fd.name + '」移入回收站（里面的文件一起移入）',
                  'Move “' + fd.name + '” to the Recycle Bin (its files go with it)')],
              { okText: '删除', cancelText: '取消', onOk: function () {
                api.post('/api/repo/folder_delete', { name: fd.name }).then(function (d) {
                  if (!d || d.error || d.ok === false) {
                    api.notice('删除失败', [String((d && d.error) || ((d && d.errors) || []).join('；') || '未知错误')]);
                    return;
                  }
                  load(true);
                });
              } });
          } },
          { label: '批量删除', danger: true, disabled: !!fd.locked, run: function () { enterSelMode({ name: 'folder:' + fd.name, locked: !!fd.locked }); } },
          '-',
          /* 单文件的「查看内容 / 查看详细信息」在文件夹上就是「打开文件夹」（用户 2026-10-20） */
          { label: '打开文件夹', run: function () { openFolder(fd, tab); } },
        ]);
      });
    }

    function folderCard(fd, tab) {
      var c = api.el('div', 'card rp-card rp-folder');
      /* 文件夹卡（用户 2026-10-20 第二轮）：不再画内框，就是普通卡片 + 标题行下面一条通栏分割线；
         标题行（图标 + 名称 + 锁）的高度/位置与文件卡标题行一致，分割线右端贴到卡片右边框。 */
      var head = api.el('div', 'rp-fhead');
      var ico = api.el('span', 'rp-fico');
      ico.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.2h7A1.5 1.5 0 0 1 19 9.7v7.8A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"/></svg>';
      head.appendChild(ico);
      head.appendChild(api.el('div', 'rp-title', fd.name || '（未命名）'));
      if (fd.locked) head.appendChild(lockBadge(true));
      c.appendChild(head);
      var tTags = api.el('div', 'rp-tags');
      tTags.appendChild(api.el('span', 'rp-tag rp-tag-folder', '文件夹'));
      var nTab = tab === 'datasets' ? fd.n_ds : fd.n_wt;
      tTags.appendChild(api.el('span', 'rp-tag rp-tag-count',
        (typeof nTab === 'number' ? nTab : (fd.count || 0)) + ' 个'));
      c.appendChild(tTags);
      c.appendChild(row('大小', fmtSize(fd.size)));
      c.appendChild(row('更新时间', fmtTime(fd.mtime)));
      c.appendChild(row('备注', fd.remark || '—'));
      /* 第四行「来源」= 自建 / 预训练 / LoRA 微调…（用户 2026-10-20，与文件卡的「路径」行对齐） */
      c.appendChild(row('来源', fd.origin || '自建'));
      c.addEventListener('dblclick', function (ev) {
        ev.preventDefault();
        openFolder(fd, tab);
      });
      bindFolderMenu(c, fd, tab);
      bindCheck(c, { name: 'folder:' + fd.name, locked: !!fd.locked });
      return c;
    }

    /* ---------- 备注（≤15 中文字或 ≤20 英文字，用户 2026-10-16） ---------- */
    function remarkUnits(t) {
      var u = 0;
      for (var i = 0; i < t.length; i++) u += t.charCodeAt(i) > 0x2E7F ? 1 : 0.75;
      return u;
    }
    function promptRemark(f) {
      var mask = api.el('div', 'rp-mask');
      var box = api.el('div', 'rp-modal rp-rm-modal');
      var head = api.el('div', 'rp-modal-head');
      head.appendChild(api.el('div', 'rp-modal-title', RL('备注', 'Note') + ' · ' + (f.name || '')));
      var x = api.el('button', 'rp-modal-x');
      x.type = 'button';
      x.setAttribute('aria-label', '关闭');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>';
      head.appendChild(x);
      box.appendChild(head);
      var body = api.el('div', 'rp-rm-body');
      var input = api.el('input', 'rp-rm-input');
      input.type = 'text';
      input.value = f.remark || '';
      body.appendChild(input);
      var hint = api.el('div', 'rp-rm-hint', '');
      body.appendChild(hint);
      box.appendChild(body);
      var foot = api.el('div', 'rp-rm-foot');
      var cancel = api.el('button', 'nt-cancel');
      cancel.type = 'button';
      cancel.textContent = '取消';
      var save = api.el('button', 'nt-ok');
      save.type = 'button';
      save.textContent = '保存';
      foot.appendChild(cancel);
      foot.appendChild(save);
      box.appendChild(foot);
      function close() {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        document.removeEventListener('keydown', onk, true);
      }
      function onk(ev) { if (ev.key === 'Escape') close(); }
      function upd() {
        var u = remarkUnits(input.value);
        hint.textContent = u > 15 ? '过长：不超过 15 个中文字或 20 个英文字' : (input.value ? '' : '');
        hint.classList.toggle('err', u > 15);
      }
      input.addEventListener('input', upd);
      upd();
      x.addEventListener('click', close);
      cancel.addEventListener('click', close);
      save.addEventListener('click', function () {
        var rm = input.value.trim();
        if (remarkUnits(rm) > 15) { upd(); return; }
        api.post('/api/repo/mark', { name: f.name, remark: rm }).then(function (d) {
          if (!d || d.error || d.ok === false) { api.notice('备注保存失败', [String((d && d.error) || '未知错误')]); return; }
          f.remark = rm;
          close();
          renderCur();
        });
      });
      mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) close(); });
      document.addEventListener('keydown', onk, true);
      mask.appendChild(box);
      document.body.appendChild(mask);
      input.focus();
    }

    /* ---------- 查看详细信息（用户 2026-10-16） ---------- */
    function showDetails(f, isWeight) {
      var mask = api.el('div', 'rp-mask');
      var box = api.el('div', 'rp-modal rp-det-modal');
      var head = api.el('div', 'rp-modal-head');
      head.appendChild(api.el('div', 'rp-modal-title', '详细信息'));
      var x = api.el('button', 'rp-modal-x');
      x.type = 'button';
      x.setAttribute('aria-label', '关闭');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>';
      head.appendChild(x);
      box.appendChild(head);
      var body = api.el('div', 'rp-det-body');
      /* 字符数/条数以 K 计（用户 2026-10-16） */
      function fmtK(n) { var v = n / 1000; return (v < 0.01 ? v.toFixed(3) : v.toFixed(2)) + 'K'; }
      function dirOf(p) { return p ? String(p).replace(/[\\/][^\\/]+$/, '') : ''; }
      function line(k, v) {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', k));
        r.appendChild(api.el('div', 'rp-det-v', String(v == null || v === '' ? '—' : v)));
        body.appendChild(r);
      }
      /* 可编辑行（名称/主题/备注）：Enter 或失焦提交，失败回退旧值 */
      function editLine(k, val, commit) {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', k));
        var v = api.el('div', 'rp-det-v');
        var input = api.el('input', 'rp-det-input');
        input.type = 'text';
        input.value = val || '';
        var old = input.value;
        input.addEventListener('blur', function () {
          var nv = input.value;
          if (nv === old) return;
          commit(nv, function (err) {
            api.notice('保存失败', [String(err || '未知错误')]);
            input.value = old;
          }, function (nval) { old = (nval != null ? nval : nv); input.value = old; load(true); });
        });
        input.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        });
        v.appendChild(input);
        r.appendChild(v);
        body.appendChild(r);
      }
      /* 路径=到文件夹层级 + 浏览（资源管理器定位到文件存储位置） */
      function pathLine(path) {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', '路径'));
        var v = api.el('div', 'rp-det-v rp-det-path');
        v.appendChild(api.el('span', 'rp-det-pathtext', dirOf(path)));
        var btn = api.el('button', 'rp-det-btn');
        btn.type = 'button';
        btn.textContent = '浏览';
        btn.addEventListener('click', function () {
          api.post('/api/repo/reveal', { path: path }).then(function (d) {
            if (!d || d.error || d.ok === false) api.notice('打开失败', [String((d && d.error) || '未知错误')]);
          });
        });
        v.appendChild(btn);
        r.appendChild(v);
        body.appendChild(r);
      }
      function pickLine(k, cur, opts, onSave) {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', k));
        var v = api.el('div', 'rp-det-v');
        var pk = api.pick(api.el('input'), opts, 'rp-det-pick');
        pk._hideSel = true;
        pk.value = cur || '';
        pk.addEventListener('change', function () {
          var nv = pk.value;
          onSave(nv, function (err) {
            api.notice('保存失败', [String(err || '未知错误')]);
            pk.value = cur;
          }, function () { load(true); });
        });
        v.appendChild(pk);
        r.appendChild(v);
        body.appendChild(r);
      }
      function toggleLine(k, on, onSave) {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', k));
        var v = api.el('div', 'rp-det-v');
        var t = api.toggle(!!on);
        t.el.addEventListener('click', function () {
          onSave(t.value, function (err) {
            api.notice('保存失败', [String(err || '未知错误')]);
            t.set(!!on);
          }, function () { load(true); });
        });
        v.appendChild(t.el);
        r.appendChild(v);
        body.appendChild(r);
      }

      /* Loss 曲线（用户 2026-10-20）：仓库「详细信息」里直接看这个权重那次训练的完整曲线。
         数据和训练页曲线同一口径（读 logs/<权重名>_loss.csv），保持简单：只画 train/val 两条线。 */
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
      function lossBlock() {
        var r = api.el('div', 'rp-det-row rp-det-lossrow');
        r.appendChild(api.el('div', 'rp-det-k', 'Loss 曲线'));
        var v = api.el('div', 'rp-det-v');
        var chart = api.el('div', 'rp-loss-chart');
        chart.appendChild(api.el('div', 'rp-loss-empty', '读取中…'));
        v.appendChild(chart);
        r.appendChild(v);
        body.appendChild(r);
        var q = '/api/repo/weight_loss?name=' + encodeURIComponent(String(f.name || ''))
          + '&path=' + encodeURIComponent(String(f.path || ''))
          + '&method=' + encodeURIComponent(String(f.method || ''));
        api.get(q).then(function (d) {
          chart.textContent = '';
          if (!d || d.error || d.ok === false) {
            chart.appendChild(api.el('div', 'rp-loss-empty', '读取失败：' + String((d && d.error) || '未知错误')));
            return;
          }
          var pts = (d.points || []).filter(function (pt) {
            return pt && isFinite(pt.step) && isFinite(pt.train) && isFinite(pt.val);
          });
          if (pts.length < 2) {
            chart.appendChild(api.el('div', 'rp-loss-empty', d.note || '这条权重还没有 Loss 曲线'));
            return;
          }
          chart.appendChild(drawLoss(pts));
          if (d.rows) chart.title = '共 ' + d.rows + ' 个点';
        }, function () {
          chart.textContent = '';
          chart.appendChild(api.el('div', 'rp-loss-empty', '读取失败'));
        });
      }

      /* 行序（用户 2026-10-16）：可编辑（名称/训练方式/主题/备注）放上面 → 只读信息 → 置顶仍最底 */
      /* 名称行（用户 2026-10-16）：隐扩展名（改名不能改格式）、右侧「重复」红字/绿点与训练页同款 */
      function nameLine() {
        var r = api.el('div', 'rp-det-row');
        r.appendChild(api.el('div', 'rp-det-k', '名称'));
        var v = api.el('div', 'rp-det-v rp-det-name');
        var input = api.el('input', 'rp-det-input');
        input.type = 'text';
        var origName = String(f.name || f.title || '');
        var ext = (origName.match(/\.[^.]+$/) || [''])[0];
        function baseOf(n) {
          var s = String(n == null ? '' : n);
          return ext && s.toLowerCase().slice(-ext.length) === ext.toLowerCase() ? s.slice(0, -ext.length) : s;
        }
        input.value = baseOf(origName);
        var hint = api.el('div', 'wn-hint');
        var hintDot = api.el('span', 'wn-dot');
        hintDot.setAttribute('aria-hidden', 'true');
        var hintText = api.el('span', 'wn-text');
        hint.appendChild(hintDot);
        hint.appendChild(hintText);
        v.appendChild(input);
        v.appendChild(hint);
        r.appendChild(v);
        body.appendChild(r);
        /* 绿点/红字常驻叠放、切状态只换 class → CSS opacity 交叉淡入淡出（用户 2026-10-16：不再闪） */
        function setHint(kind, text) {
          hint.className = 'wn-hint' + (kind ? ' ' + kind : '');
          hintText.textContent = (kind === 'ok') ? '' : (text || '');
        }
        function audit() {
          var typed = String(input.value || '').trim();
          if (!typed) { setHint('', ''); return; }
          var full = baseOf(typed) + ext;   /* 与后端同口径：补原扩展再比 */
          api.get('/api/repo/summary').then(function (d) {
            var list = isWeight ? (d.weights || []) : (d.datasets || []);
            for (var i = 0; i < list.length; i++) {
              var n = String(list[i].name || '');
              if (n === origName) continue;
              /* 按路径豁免自己（2026-10-21）：改名后名字已变，名字豁免失效会把自己判成「重复」 */
              if (list[i].path && f.path && String(list[i].path) === String(f.path)) continue;
              if (n.toLowerCase() === full.toLowerCase()) {
                hint.title = '与仓库' + (isWeight ? '权重' : '数据集') + '「' + n + '」同名';
                setHint('bad', '重复');
                return;
              }
            }
            hint.title = '';
            setHint('ok', '√');
          })['catch'](function () { setHint('', ''); });
        }
        var old = input.value;
        var t = null;
        input.addEventListener('input', function () {
          if (t) clearTimeout(t);
          t = setTimeout(function () { t = null; audit(); }, 400);
        });
        input.addEventListener('blur', function () {
          var nv = input.value;
          if (nv === old) return;
          api.post('/api/repo/rename', { name: f.name, new_name: nv, path: f.path }).then(function (d) {
            if (!d || d.error || d.ok === false) {
              api.notice('保存失败', [String((d && d.error) || '未知错误')]);
              input.value = old;
              return;
            }
            f.name = d.name;
            f.path = d.path || f.path;    /* 新路径（真改名会换路径，2026-10-21） */
            origName = d.name;            /* 自查豁免跟着新名走：否则改完立刻把自己判成「重复」 */
            old = baseOf(d.name);
            input.value = old;
            load(true);
            audit();
          });
        });
        input.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        });
        audit();
      }
      nameLine();
      if (!isWeight) {
        /* 训练方式两槽（用户 2026-10-16：一数据集可标两次）；选项按数据类型收口 */
        pickLine('训练方式 1', f.use || '', useOptionsFor(f, f.use || '', f.use2 || ''), function (nv, fail, ok) {
          if (nv && nv === (f.use2 || '')) { fail('同一选项不能标两次'); return; }
          api.post('/api/repo/dataset_use', { name: f.name, use: nv, slot: 1 }).then(function (d) {
            if (!d || d.error || d.ok === false) { fail((d && d.error) || '未知错误'); return; }
            f.use = (d.use != null ? d.use : nv);
            ok();
          });
        });
        pickLine('训练方式 2', f.use2 || '', useOptionsFor(f, f.use2 || '', f.use || ''), function (nv, fail, ok) {
          if (nv && nv === (f.use || '')) { fail('同一选项不能标两次'); return; }
          api.post('/api/repo/dataset_use', { name: f.name, use: nv, slot: 2 }).then(function (d) {
            if (!d || d.error || d.ok === false) { fail((d && d.error) || '未知错误'); return; }
            f.use2 = (d.use2 != null ? d.use2 : nv);
            ok();
          });
        });
        editLine('主题', (f.themes || []).join('、'), function (nv, fail, ok) {
          api.post('/api/repo/mark', { name: f.name, theme: nv }).then(function (d) {
            if (!d || d.error || d.ok === false) { fail((d && d.error) || '未知错误'); return; }
            ok();
          });
        });
      }
      editLine('备注', f.remark || '', function (nv, fail, ok) {
        if (remarkUnits(nv) > 15) { fail('备注过长：不超过 15 个中文字或 20 个英文字'); return; }
        api.post('/api/repo/mark', { name: f.name, remark: nv }).then(function (d) {
          if (!d || d.error || d.ok === false) { fail((d && d.error) || '未知错误'); return; }
          ok();
        });
      });
      pathLine(f.path || '');
      if (!isWeight) {
        /* 数据类型：纯文本一律显示「文本」（用户 2026-10-22 定名）；来源在它下面一行 */
        line('数据类型', f.kind_key === 'plain' ? '文本' : (f.kind || ''));
        line('来源', f.origin || '外部导入');
        line('体量', fmtSize(f.size_bytes));
        line('字符数', typeof f.chars === 'number' ? fmtK(f.chars) : '—');
        if (f.kind_key === 'chat') line('条数', typeof f.lines === 'number' ? fmtK(f.lines) : '—');
        line('格式审查', (f.ok && f.fmt_ok) ? '合格' : ('不合格：' + (f.fmt_msg || '')));
      } else {
        line('大小', fmtSize(f.size));
        line('更新时间', fmtTime(f.mtime));
        /* 训练方式溯源（用户 2026-10-16）：多种/多次经历，最近 6 次分行显示（新的在上）；来源行删除 */
        var hist = (f.hist || []).slice().reverse();
        if (hist.length) {
          line('训练方式', hist.map(function (h) {
            /* 2026-10-23（用户）：只有一条训练记录时不带时间 —— 和上面「更新时间」完全重复；
               多条时才带，用来区分是哪一次训的 */
            var m = String(h.m || '其他');
            return (hist.length > 1 && h.t) ? (m + ' · ' + fmtTime(h.t)) : m;
          }).join('\n'));
        } else {
          line('训练方式', f.method_label || '—');
        }
        /* 初始权重（用户 2026-10-23）：这条权重是从哪个权重训出来的 —— 训练溯源里最近一条的
           来源权重；预训练没有来源、外部导入的也没有 → 显示「-」。置顶不在这里了（右键菜单里有）。 */
        var fromName = hist.length ? String(hist[0].from || '') : '';
        line('初始权重', fromName ? fromName.replace(/^.*[\\/]/, '') : '-');
      }
      if (isWeight) lossBlock();          /* Loss 曲线（用户 2026-10-20） */
      box.appendChild(body);
      function close() {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        document.removeEventListener('keydown', onk, true);
      }
      function onk(ev) { if (ev.key === 'Escape') close(); }
      x.addEventListener('click', close);
      mask.addEventListener('mousedown', function (ev) { if (ev.target === mask) close(); });
      document.addEventListener('keydown', onk, true);
      mask.appendChild(box);
      document.body.appendChild(mask);
    }

    /* ---------- 批量删除（用户 2026-10-16） ---------- */
    var SEL_MODE = false;
    var SEL = {};                       /* name -> true */
    function selCount() { var n = 0; for (var k in SEL) if (SEL[k]) n++; return n; }
    function bindCheck(card, f) {
      card.__f = f;
      if (!SEL_MODE) return;
      if (f.locked) {
        /* 被锁的不参与批量选择（用户 2026-10-20）：不出勾选框、点不动，
           右上角维持卡头的小锁图标 */
        return;
      }
      card.classList.add('rp-sel-on');
      var box = api.el('div', 'rp-check' + (SEL[f.name] ? ' on' : ''));
      box.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
        'stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
      card.appendChild(box);
      card.addEventListener('click', function () {
        SEL[f.name] = !SEL[f.name];
        box.classList.toggle('on', !!SEL[f.name]);
        updSelbar();
      });
    }
    function updSelbar() {
      var bar = document.querySelector('.rp-selbar');
      if (!bar) return;
      /* 计数随语言走（用户 2026-10-23：英文模式下这条底栏还是中文） */
      bar.querySelector('.rp-selinfo').textContent =
        RL('已选 ' + selCount() + ' 项', selCount() + ' selected');
    }
    var SEL_ACT = 'delete';             /* 批量选择要做什么：delete = 批量删除；move = 批量移动（用户 2026-10-22） */
    function enterSelMode(f, act) {
      SEL_MODE = true;
      SEL_ACT = act || 'delete';
      SEL = {};
      if (f && f.name && !f.locked) SEL[f.name] = true;   /* 被锁的连入口都不预选（2026-10-20） */
      renderCur();
      var bar = api.el('div', 'rp-selbar');
      bar.appendChild(api.el('span', 'rp-selinfo',
        RL('已选 ' + selCount() + ' 项', selCount() + ' selected')));
      var all = api.el('button', 'nt-cancel');
      all.type = 'button';
      all.textContent = '全选';
      all.addEventListener('click', function () {
        /* 全选跳过被锁的（用户 2026-10-20：被锁的不可被选中） */
        (DATA && DATA.datasets ? DATA.datasets : []).forEach(function (d) { if (!d.locked) SEL[d.name] = true; });
        (DATA && DATA.weights ? DATA.weights : []).forEach(function (w) { if (!w.locked) SEL[w.name] = true; });
        (DATA && DATA.folders ? DATA.folders : []).forEach(function (fd) { if (!fd.locked) SEL['folder:' + fd.name] = true; });
        renderSelChecks();
        updSelbar();
      });
      var del = api.el('button', 'nt-ok');
      del.type = 'button';
      del.textContent = SEL_ACT === 'move' ? '移动到文件夹' : '删除所选';
      del.addEventListener('click', function () {
        var names = [];
        for (var k in SEL) if (SEL[k]) names.push(k);
        if (!names.length) {
          api.notice(SEL_ACT === 'move' ? RL('移动所选', 'Move selected') : RL('批量删除', 'Bulk delete'),
            [RL('未选择任何文件。', 'Nothing is selected.')]);
          return;
        }
        if (SEL_ACT === 'move') { promptBatchMove(names); return; }
        api.notice(RL('删除所选文件：', 'Delete selected files:'),
          [RL('确定将所选的 ' + names.length + ' 个文件移入回收站',
              'Move the ' + names.length + ' selected ' + enPlural(names.length, 'file', 'files') +
              ' to the Recycle Bin')],
          { okText: '删除', cancelText: '取消', onOk: function () {
            api.post('/api/repo/delete_files', delBody(names)).then(function (d) {
              if (!d || d.error || d.ok === false) {
                api.notice('删除失败', (d && d.errors) ? d.errors.map(String) : [String((d && d.error) || '未知错误')]);
              }
              exitSelMode(true);
            });
          } });
      });
      var quit = api.el('button', 'nt-cancel');
      quit.type = 'button';
      quit.textContent = '取消';
      quit.addEventListener('click', function () { exitSelMode(true); });
      bar.appendChild(all);
      bar.appendChild(del);
      bar.appendChild(quit);
      document.body.appendChild(bar);
      /* 底栏相对「页面内容区」居中（用户 2026-10-16）：.content 在侧栏右侧，left:50% 会对整个应用居中而偏左 */
      (function () {
        function place() {
          var host = document.querySelector('.content') || document.body;
          var r = host.getBoundingClientRect();
          bar.style.left = Math.round(r.left + r.width / 2) + 'px';
        }
        place();
        window.addEventListener('resize', place);
        bar.__onResize = place;
      })();
    }
    function renderSelChecks() {
      document.querySelectorAll('.rp-check').forEach(function (box) {
        var card = box.parentNode;
        var nm = card && card.__f && card.__f.name;
        box.classList.toggle('on', !!(nm && SEL[nm]));
      });
    }
    /* 切页兜底清理：只摘底栏和监听，不碰页面 DOM、不触发重排（用户 2026-10-23） */
    SELBAR_CLEANUP = function () {
      var bar = document.querySelector('.rp-selbar');
      if (bar) {
        if (bar.__onResize) window.removeEventListener('resize', bar.__onResize);
        if (bar.parentNode) bar.parentNode.removeChild(bar);
      }
      SEL_MODE = false;
      SEL = {};
    };

    function exitSelMode(refresh) {
      SEL_ACT = 'delete';
      SEL_MODE = false;
      SEL = {};
      var bar = document.querySelector('.rp-selbar');
      if (bar && bar.parentNode) {
        if (bar.__onResize) window.removeEventListener('resize', bar.__onResize);
        bar.parentNode.removeChild(bar);
      }
      if (refresh) load(true); else renderCur();
    }

    /* 「+」空白卡：中间一个圈、圈里一个加号；点开原生选文件往工程里加 */
    /* 「+」卡与数据卡等高（用户 2026-10-16）：按同网格数据卡实高定高 */
    function sizeAddCards() {
      document.querySelectorAll('.rp-grid').forEach(function (g) {
        var add = g.querySelector('.rp-add');
        if (!add) return;
        var h = 0;
        Array.prototype.forEach.call(g.querySelectorAll('.rp-card'), function (c) {
          if (!c.classList.contains('rp-add')) h = Math.max(h, c.offsetHeight);
        });
        /* 空态（一张数据卡都没有、量不到高）：用该视图上次实测的卡高兜底，
           首次 178 —— 空页的「+」卡与有数据/有权重的页同高（用户 2026-10-16） */
        var view = (g.parentNode && g.parentNode.dataset) ? g.parentNode.dataset.tab : '';
        if (h > 0) RP_ADD_H[view] = h; else h = RP_ADD_H[view] || 178;
        add.style.minHeight = '0';
        add.style.height = h + 'px';
      });
    }

    /* ---------- 筛选命中 ---------- */
    function dsHit(f) {
      if (F.ds.kind && String(f.kind_key || '') !== F.ds.kind) return false;
      if (F.ds.use) {
        /* 两个标注槽任一命中（用户 2026-10-16：一数据集可标两次） */
        var u = String(f.use || '');
        var u2 = String(f.use2 || '');
        if (F.ds.use === 'none') { if (u || u2) return false; }
        else if (u !== F.ds.use && u2 !== F.ds.use) return false;
      }
      var q = F.ds.q.toLowerCase();
      if (!q) return true;
      /* 文本查找：主题 + 文件夹名称（用户 2026-10-15 口径） */
      var hay = [(f.folder || ''), ((f.themes || []).join(' '))].join('\n').toLowerCase();
      return hay.indexOf(q) >= 0;
    }
    /* 权重「训练方式」筛选跟卡片标签同一口径（用户 2026-10-20）：
       训练历史里的训练方式（中文）优先，没历史才退回 method_label。
       原来只比「按文件名猜的码」，自命名权重（TCX.pt 这类不含英文关键字）一律猜成 other ——
       筛「预训练」空、筛「其他」全出（实测复现）。 */
    var W_NAMED_LABEL = {
      pretrain: '预训练', continue: '继续预训练', sft: '指令微调', lora: 'LoRA 微调',
      merge: 'LoRA 合并',
    };
    function wLabels(w) {
      var ms = (w.hist || []).map(function (h) { return String(h.m || '').trim(); }).filter(Boolean);
      return ms.length ? ms : [String(w.method_label || '其他')];
    }
    function wHit(w) {
      if (F.w.method) {
        var labs = wLabels(w);
        var want = W_NAMED_LABEL[F.w.method] || '';
        if (want) {
          if (labs.indexOf(want) < 0) return false;
        } else {
          /* 「其他」= 不带四种已知训练方式标签的（其他/量化/模型裁切/未标注都归这） */
          var named = ['预训练', '继续预训练', '指令微调', 'LoRA 微调', 'LoRA 合并'];
          if (labs.some(function (x) { return named.indexOf(x) >= 0; })) return false;
        }
      }
      var day = fmtDay(w.mtime);
      if (F.w.d1 && day && day < F.w.d1) return false;
      if (F.w.d2 && day && day > F.w.d2) return false;
      var q = F.w.q.toLowerCase();
      if (!q) return true;
      return String(w.name || '').toLowerCase().indexOf(q) >= 0;
    }

    function renderCur() { if (DATA) render(DATA); }

    /* ---------- 渲染 ---------- */
    function render(d) {
      DATA = d;
      /* 重渲染要保住滚动位置（用户 2026-10-20：点「锁定/解锁」视图会「滑到顶上」——
         清空网格那一下滚动容器高度塌掉，scrollTop 被夹回 0）。改前实测 scrollTop 190 → 0。 */
      var scHost = (function (el) {
        while (el && el !== document.documentElement) {
          if (el.scrollHeight > el.clientHeight && el.clientHeight > 0 &&
              /auto|scroll/.test(getComputedStyle(el).overflowY)) return el;
          el = el.parentNode;
        }
        return document.scrollingElement || document.documentElement;
      })(gridDs);
      var scTop = scHost.scrollTop;
      /* 置顶排前（用户 2026-10-16），置顶组内按置顶先后（pin_rank 越小越前） */
      function pinFirst(a, b) {
        var pa = a.pinned ? 0 : 1, pb = b.pinned ? 0 : 1;
        if (pa !== pb) return pa - pb;
        return (a.pin_rank || 9999) - (b.pin_rank || 9999);
      }
      var dssAll = (d.datasets || []).filter(dsHit);
      var wsAll = (d.weights || []).filter(wHit);
      /* 文件夹视图（用户 2026-10-20）：根视图只显示「没进文件夹的项 + 文件夹卡」；
         双击打开文件夹后只显示里面的成员（筛选照常生效） */
      function inOpen(f, tab) {
        var fn = f.vfolder || '';
        return OPEN[tab] ? (fn === OPEN[tab]) : !fn;
      }
      var dss = dssAll.filter(function (f) { return inOpen(f, 'datasets'); }).sort(pinFirst);
      var ws = wsAll.filter(function (w) { return inOpen(w, 'weights'); }).sort(pinFirst);
      /* 文件夹卡：手工建的（空也出卡）/ 名字命中搜索 / 至少一个成员过了筛 */
      function folderHit(fd, tab) {
        if (OPEN[tab]) return false;
        /* 类型隔离（用户 2026-10-22）：数据集页签只出「含数据集」的文件夹，权重页签只出「含权重」的；
           空文件夹按建立时所属的页签出现（老记录没有 kind 的，两边都出现，免得建完看不到） */
        var mine = tab === 'datasets' ? (fd.n_ds || 0) : (fd.n_wt || 0);
        var other = tab === 'datasets' ? (fd.n_wt || 0) : (fd.n_ds || 0);
        var q = (tab === 'datasets' ? F.ds.q : F.w.q).toLowerCase();
        if (!mine) {
          if (mine + other > 0) return false;          /* 只有另一类的成员 → 本页签不出现 */
          /* 空文件夹（刚建的）：只在建立它的页签出现；搜索时按名字匹配 */
          if (fd.kind && fd.kind !== tab) return false;
          return !q || String(fd.name || '').toLowerCase().indexOf(q) >= 0;
        }
        if (q && String(fd.name || '').toLowerCase().indexOf(q) >= 0) return true;
        var items = (tab === 'datasets' ? (d.datasets || []) : (d.weights || [])).filter(function (it) {
          return (it.vfolder || '') === fd.name;
        });
        return items.some(tab === 'datasets' ? dsHit : wHit);
      }
      var fdsDs = (d.folders || []).filter(function (fd) { return folderHit(fd, 'datasets'); }).sort(pinFirst);
      var fdsW = (d.folders || []).filter(function (fd) { return folderHit(fd, 'weights'); }).sort(pinFirst);
      gridDs.innerHTML = '';
      gridW.innerHTML = '';
      renderCrumb(crumbDs, 'datasets');
      renderCrumb(crumbW, 'weights');
      /* 空白「+」卡删掉（用户 2026-10-20）：导入 / 新建一律走页面右键菜单 */
      /* 卡片顺序（用户 2026-10-20）：先文件、后文件夹 —— 文件夹排在网格最下面 */
      dss.forEach(function (f) { gridDs.appendChild(dataCard(f)); });
      ws.forEach(function (w) { gridW.appendChild(weightCard(w)); });
      fdsDs.forEach(function (fd) { gridDs.appendChild(folderCard(fd, 'datasets')); });
      fdsW.forEach(function (fd) { gridW.appendChild(folderCard(fd, 'weights')); });
      fitAllUsePicks();   /* 标注框定宽（用户 2026-10-22）：卡片刚挂进 DOM，这一刻量得最准 */
      sizeAddCards();
      scHost.scrollTop = scTop;      /* 滚动位置还原（用户 2026-10-20） */
      requestAnimationFrame(alignCrumbAll);   /* 布局落定后再对一次（用户 2026-10-20） */
      playEnter(gridDs, 'datasets');
      playEnter(gridW, 'weights');
    }

    /* ---------- 「+」添加：菜单两项（添加文件 / 添加文件夹，用户 2026-10-16） ---------- */
    function addReport(res) {
      var rej = (res && res.rejected) || [];
      var errs = (res && res.errors) || [];
      /* 拒收/失败用页内弹窗呈现理由（用户 2026-10-16：拒收给出弹窗呈现理由） */
      var lines = [];
      rej.forEach(function (r) { lines.push(r.name + '：' + (r.reason || '不合格')); });
      errs.forEach(function (e2) { lines.push(String(e2)); });
      if (!res || res.ok === false) {
        lines.push(String((res && res.error) || (lines.length ? '' : '未知错误')));
        api.notice('添加失败', lines.filter(Boolean));
      } else if (rej.length) {
        api.notice('未通过格式审查，未添加', lines);
      }
      load(true);      /* 添加后强制刷新（不吃缓存） */
    }
    function addFilesFlow(title, filters, route, tab) {
      if (!(window.eai && eai.pickFile)) {
        api.notice('添加失败', ['当前环境不支持选择文件']);
        return;
      }
      Promise.resolve(eai.pickFile({ title: title, buttonLabel: '添加', filters: filters }))
        .then(function (paths) {
          if (!paths || !paths.length) return;
          /* 在文件夹里导入 → 加完直接归到该文件夹（用户 2026-10-22） */
          return api.post(route, { paths: paths, folder: (OPEN[tab || curTab()] || '') }).then(addReport);
        }).catch(function (e) {
          api.notice('添加失败', [String((e && e.message) ? e.message : e)]);
        });
    }
    function addFolderFlow(title, route, tab) {
      if (!(window.eai && eai.pickFolder)) {
        api.notice('添加失败', ['当前环境不支持选择文件夹']);
        return;
      }
      Promise.resolve(eai.pickFolder({ title: title, buttonLabel: '添加' }))
        .then(function (dir) {
          if (!dir) return;
          var tk = tab || curTab();
          var body = { paths: [dir] };
          if (OPEN[tk]) {
            body.folder = OPEN[tk];            /* 文件夹内：全部文件放进当前文件夹 */
          } else {
            /* 根目录：按源目录名自动新建同名文件夹（后端遇到同名文件夹会拒绝整批） */
            body.auto_folder = String(dir).split(/[\\/]/).filter(Boolean).pop() || '';
          }
          return api.post(route, body).then(addReport);
        }).catch(function (e) {
          api.notice('添加失败', [String((e && e.message) ? e.message : e)]);
        });
    }
    /* 导入 / 新建菜单（用户 2026-10-20）：「+」卡点击与仓库页右键共用这一份。
       分组：导入文件 · 导入文件夹 ‖ 新建文件夹 ‖ 返回（只有进到文件夹里才有「返回」） */
    function importMenuItems(isWeight, tab) {
      var items = [];
      /* 返回排第一（用户 2026-10-20）：只在文件夹里出现，后面跟一条分割线 */
      if (OPEN[tab]) {
        items.push({ label: '返回', run: function () { OPEN[tab] = ''; renderCur(); } });
        items.push('-');
      }
      items.push(
        { label: '导入文件', run: function () {
          if (isWeight) {
            addFilesFlow('导入模型权重',
              [{ name: '模型权重', extensions: ['pt', 'bin', 'safetensors', 'ckpt'] }],
              '/api/repo/add_weight', tab);
          } else {
            addFilesFlow('导入数据集',
              [{ name: '数据集', extensions: ['txt', 'jsonl', 'json', 'md', 'csv'] }],
              '/api/repo/add_dataset', tab);
          }
        } },
        /* 导入文件夹（用户 2026-10-22）：在文件夹内 → 文件直接放进这个文件夹（不再套子文件夹）；
           在根目录 → 按源目录名自动新建同名文件夹，同名已存在则提示、不导入 */
        { label: '导入文件夹', run: function () {
          addFolderFlow(isWeight ? '导入权重文件夹' : '导入数据集文件夹',
            isWeight ? '/api/repo/add_weight' : '/api/repo/add_dataset', tab);
        } });
      /* 新建文件夹只在根目录出现（用户 2026-10-22：文件夹内不允许再新建） */
      if (!OPEN[tab]) items.push('-', { label: '新建文件夹', run: function () { promptNewFolder(); } });
      return items;
    }
    /* 当前显示的那个视图（用户 2026-10-22）：两个视图用 display 切换，
       [hidden] 属性始终是 false —— 原来按 :not([hidden]) 取会永远命中第一个
       （模型权重），于是在数据集页的文件夹里右键，菜单算出的是权重页的项、没有「返回」 */
    /* 当前页签（用户 2026-10-22）：按视图的 display 判定，供移动/导入判断数据集与权重 */
    function curTab() {
      var v = visibleView();
      return (v && v.dataset && v.dataset.tab === 'weights') ? 'weights' : 'datasets';
    }
    function visibleView() {
      var vs = [].slice.call(document.querySelectorAll('.rp-view'));
      for (var i = 0; i < vs.length; i++) {
        if (getComputedStyle(vs[i]).display !== 'none') return vs[i];
      }
      return null;
    }
    /* 仓库页右键菜单（用户 2026-10-20）：空白处（含「+」卡）右键 = 导入 / 新建 / 返回；
       数据卡、文件夹卡各自有右键菜单，交给它们自己处理 */
    if (CTX_ROOT && CTX_HANDLER && CTX_ROOT !== root) {
      try { CTX_ROOT.removeEventListener('contextmenu', CTX_HANDLER); } catch (e) {}
    }
    CTX_ROOT = root;
    CTX_HANDLER = function (ev) {
      /* 仓库自己的视图不在页面里 = 已经切到别的页（共享容器换页时监听器可能还挂着），
         这时不接管右键（用户 2026-10-20：回收站空白处曾弹出仓库的导入菜单） */
      if (!document.querySelector('.rp-view')) return;
      if (SEL_MODE) return;
      /* 以搜索栏为界限（用户 2026-10-21）：搜索栏及其上方不弹右键菜单，
         卡片区空白/「+」卡照旧（含「返回/导入/新建」） */
      var view0 = ev.target.closest ? ev.target.closest('.rp-view') : null;
      var bar = (view0 && view0.querySelector('.rp-bar')) ||
                (visibleView() ? visibleView().querySelector('.rp-bar') : null) ||
                document.querySelector('.rp-bar');
      if (bar && ev.clientY < bar.getBoundingClientRect().bottom) return;
      var card = ev.target.closest ? ev.target.closest('.rp-card') : null;
      if (card && !card.classList.contains('rp-add')) return;
      ev.preventDefault();
      /* 页签判定（用户 2026-10-20）：空白处右键时 target 可能落在面板/网格空隙，
         取不到 .rp-view 就退回「当前显示的那个视图」，否则文件夹里右键会缺「返回」 */
      var view = ev.target.closest ? ev.target.closest('.rp-view') : null;
      if (!view) view = visibleView();
      var tab = (view && view.dataset.tab === 'weights') ? 'weights' : 'datasets';
      showMenu(ev.clientX, ev.clientY, importMenuItems(tab === 'weights', tab));
    };
    /* 监听挂 document（用户 2026-10-22：仓库空白处右键弹不出菜单）。
       换页走「暂存盒」：mount 拿到的是临时 div，换页后它被丢掉，挂在它上面的监听器随之消失
       （所以卡片自己的右键还在、空白处全哑）。挂 document 与换页无关；
       unmount 会摘掉，handler 里也有「.rp-view 不在就不接管」的兜底。 */
    CTX_ROOT = document;
    document.addEventListener('contextmenu', CTX_HANDLER);

    function load(force) {
      return fetchSummary(api, force).then(function (d) {
        if (!d || d.error) {
          if (DATA) return;                 /* 已有内容时别用报错把页面抹空 */
          gridDs.innerHTML = '';
          gridDs.appendChild(api.el('div', 'rp-empty rp-err',
            '仓库读取失败：' + ((d && d.error) || '未知错误')));
          return;
        }
        if (d !== DATA) render(d);          /* 同一份数据不重画（避免二次闪） */
      });
    }

    if (!window.__rpSizeBound) {
      window.__rpSizeBound = true;
      var rt = null;
      window.addEventListener('resize', function () {
        if (rt) clearTimeout(rt);
        rt = setTimeout(function () { if (typeof sizeAddCards === 'function') sizeAddCards(); }, 150);
      });
    }

    /* 首帧直出：模块级缓存的上次 summary 画缓存，再后台刷新（治「切仓库偶发闪屏」：
       summary 两轮扫描偶发超 prefetch 400ms 保险丝时，原流程首帧是空网格、卡片后到） */
    /* pickTab 必须先于 render（用户 2026-10-16：进页横栏文字闪）：render→sizeAddCards 读
       offsetHeight 会强制样式解析（此刻按钮还没 .on、文字定格灰），随后 .on 变白就走
       160ms 颜色过渡=文字淡入闪；先 pickTab 让 .on 在首次解析前就位，首帧直出白色 */
    pickTab('datasets');
    if (REPO_CACHE && !REPO_CACHE.error) render(REPO_CACHE);

    return load();
  },
});

/* ---------- summary 单飞 + 客户端缓存（治切仓库偶发闪屏） ----------
   prefetch 与 mount 复用同一趟请求（原先是两次全量扫描）；REPO_CACHE 让首帧同步直出。
   800ms 内的重复取数直接吃缓存（正好盖住 prefetch→mount 的间隙）；添加后 force 强制刷新。 */
var REPO_CACHE = null;
var REPO_CACHE_AT = 0;
var REPO_INFLIGHT = null;
function fetchSummary(api, force) {
  if (REPO_INFLIGHT) return REPO_INFLIGHT;
  if (!force && REPO_CACHE && (Date.now() - REPO_CACHE_AT) < 800) {
    return Promise.resolve(REPO_CACHE);
  }
  REPO_INFLIGHT = Promise.resolve(api.get('/api/repo/summary')).then(function (d) {
    if (d && !d.error) { REPO_CACHE = d; REPO_CACHE_AT = Date.now(); }
    return d;
  })["catch"](function (e) {
    return { error: (e && e.message) || String(e) };
  }).then(function (d) {
    REPO_INFLIGHT = null;
    return d;
  });
  return REPO_INFLIGHT;
}

/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
(window.__EAI_API || {}).onProjectChange && window.__EAI_API.onProjectChange(function () { REPO_CACHE = null; REPO_INFLIGHT = null; });

})();
