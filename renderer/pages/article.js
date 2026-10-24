/* ============================================================
   语料蒸馏页（导航键 article）· 渲染层模块
   ------------------------------------------------------------
   迁移自 ENGINE_DIR/pages/distill.py 的 DistillPage(kind="article")。
   信息结构 1:1 复刻：页头「语料蒸馏 / 题目 → 写作 → 审阅」，
   三个分段标签：模型配置 / 采集中心 / 数据管理（默认采集中心）。

   数据来源：bridge_api/article.py。
     * 模型配置：GET /api/article/config（读 config.json#article）
                 POST /api/article/models/save（真写 config.json，不传 api_key 保留原值）
                 POST /api/article/models/test（真发一次最小请求，成功/失败原文返回）
     * 采集中心：POST /api/article/collect/start（真起宿主线程作业跑 core.article_collector）
                 POST /api/article/collect/stop（真停：Collector.stop）
                 GET  /api/article/collect/status?since=N（增量事件 + 运行态 + 输出目录统计）
                 开始后每 1s 轮询 status 的 since 游标：log 事件追日志区、progress 事件刷进度条与统计行
     * 数据管理：GET /api/article/list；导出 / 删除 / 导入 / 清空本轮仍未实现（接口回
                 {not_implemented, reason}，按钮置灰并照实显示原因）。
   ?mock=1 时仅用本模块 mock 字段做截图预览。
   ============================================================ */
(function () {
  'use strict';

  /* 采集轮询定时器（模块级：unmount 时清理，避免切页后仍在刷） */
  var pollTimer = null;

  /* ---------- 受信任的内联 SVG 图标（无外链 / 无位图） ---------- */
  var ICONS = {
    save:   '<path d="M5.2 12.8 9.4 17 18.8 7.2"/>',
    sample: '<path d="M7 4.8h7.4L18 8.4v10.8H7Z"/><path d="M9.6 12h6M9.6 15.2h4.2"/>',
    reload: '<path d="M20 11a8 8 0 1 0-2.35 5.65"/><path d="M20 4.8V11h-6.2"/>',
    play:   '<path d="M8.6 5.6v12.8L19 12Z" fill="currentColor" stroke="none"/>',   /* 与训练页 ▶ 一致：实心三角 */
    /* 暂停（两竖条）：形状与训练页一致，但整体缩到和左边 ▶ 三角一样的视觉大小（用户 2026-10-17）——
       三角占 x 8.6~19 / y 5.6~18.4（高 12.8），这里两根条高 11.2、间距 4，视觉体量对齐 */
    stop:   '<path d="M10 6.4v11.2"/><path d="M14 6.4v11.2"/>',
    refresh:'<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',   /* 与预训练页同款（用户 2026-10-20） */
    trash:  '<path d="M5 7h14"/><path d="M9.4 7V5.2h5.2V7"/><path d="M6.6 7l.9 12.2h9L17.4 7"/><path d="M10.3 10.4v6M13.7 10.4v6"/>',
    down:   '<path d="M12 4.6v10.4"/><path d="M7.8 11 12 15.2 16.2 11"/><path d="M5 19.4h14"/>',
    up:     '<path d="M12 19.4V9"/><path d="M7.8 13.2 12 9l4.2 4.2"/><path d="M5 4.6h14"/>',
    tag:    '<path d="M4.8 11.2 11.2 4.8h8.2v8.2l-6.4 6.4Z"/><circle cx="15.8" cy="8.4" r="1.25"/>',
    merge:  '<path d="M4.8 5.2v2.2a4.6 4.6 0 0 0 4.6 4.6h9.8"/><path d="M4.8 18.8v-2.2a4.6 4.6 0 0 1 4.6-4.6"/><path d="M16.2 9l3 3-3 3"/>',
    warn:   '<path d="M12 4.8 20.6 19.4H3.4Z"/><path d="M12 10.2v4.4M12 17v.2"/>'
  };

  function iconEl(name) {
    var span = document.createElement('span');
    span.className = 'a-ic';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      (ICONS[name] || '') + '</svg>';
    return span;
  }

  /* ---------- 三张模型卡（同步骨架用） ----------
     键/标题与 bridge_api/article.py::ARTICLE_MODELS 逐字一致。
     为什么在这里再放一份：进页第一帧就要把三张卡建出来（见 loadConfig）——
     否则会先画一个没有卡片的空壳、等 /api/article/config 回来（实测 24~40ms）才把三张卡插进去，
     那两三帧就是用户看到的「切蒸馏页会闪」。 */
  var CARD_DEFS = [['titler', '题目模型'], ['writer', '写作模型'], ['reviewer', '审阅模型']];

  var MODEL_DEFAULTS = null;   /* 后端下发的出厂默认（/api/article/config → defaults），「填充示例」用它 */
  /* 采集主题示例（用户 2026-10-20）：每点一次换一条，不再一次塞进多条；
     第一条与占位提示一致，第二次起才真的“变”。 */
  /* 采集主题示例（用户 2026-10-20）：[中文, 英文] 一对，点一次换一条；
     全部是自然科学主题，不涉及任何国家的历史与文化。切界面语言不改已填内容，再点一次才换成对应语言。 */
  var THEME_SAMPLES = [
    ['光合作用', 'Photosynthesis'],
    ['板块构造', 'Plate tectonics'],
    ['天文学入门', 'Introduction to astronomy'],
    ['海洋生物', 'Marine life'],
    ['气候与天气', 'Climate and weather'],
    ['免疫与疫苗', 'Immunity and vaccines'],
    ['地震与地球内部', 'Earthquakes and Earth\'s interior'],
    ['恒星的生命周期', 'The life cycle of stars'],
    ['量子力学入门', 'Introduction to quantum mechanics'],
    ['遗传与基因', 'Heredity and genes'],
    ['细胞的结构与功能', 'Cell structure and function'],
    ['神经科学入门', 'Introduction to neuroscience'],
    ['人工智能基础', 'Fundamentals of artificial intelligence'],
    ['半导体与芯片', 'Semiconductors and chips'],
    ['材料科学入门', 'Introduction to materials science'],
    ['新能源技术', 'New energy technologies'],
    ['核聚变研究', 'Nuclear fusion research'],
    ['航天推进技术', 'Spacecraft propulsion'],
    ['大气层结构', 'Structure of the atmosphere'],
    ['矿物与岩石', 'Minerals and rocks'],
    ['植物的蒸腾作用', 'Transpiration in plants'],
    ['昆虫的变态发育', 'Metamorphosis in insects'],
    ['人体骨骼系统', 'The human skeletal system'],
    ['微生物与发酵', 'Microorganisms and fermentation'],
  ];
  /* 重置用的英文系统提示词（用户 2026-10-20）：英文界面下点「重置」时提示词也换成英文，
     数值项（temperature / max_tokens）两种语言一致；base_url / model / api_key 一律不动。 */
  var PROMPTS_EN = {
    titler: 'You are an English topic editor preparing pretraining corpora for a language model. Given a theme, ' +
      'draft a title that locks onto one concrete subject and names its scope (field, range or period) so it ' +
      'can carry an informative long-form article. No questions, no numbering, no quotation marks, no clickbait. ' +
      'Output the title only. All output must be in English.',
    writer: 'You are an English science writer producing pretraining corpora for a language model. Write a ' +
      'complete article for the given title: open by stating the subject; develop the body in 2-4 sections, each ' +
      'with one clear claim backed by facts, mechanisms, data or examples; close by summing up and noting ' +
      'significance or common misconceptions. Be factually accurate, use precise terminology, keep the logic ' +
      'coherent, and avoid filler, cliches and bullet lists. Do not repeat the title and add no commentary. ' +
      'All output must be in English.',
    reviewer: 'You are a strict English corpus reviewer. Judge only major errors: factual mistakes, broken ' +
      'logic, serious off-topic drift, fabricated citations or data, obvious misinformation, or content that is ' +
      'entirely empty filler. Style preferences and level of detail do not count. Output only a JSON object, ' +
      'nothing else: {"pass": true or false, "score": integer 0-10, "major_errors": ["each major error, one ' +
      'line"], "comment": "one-sentence summary"}'
  };

  /* 用户 2026-10-20：提示词不跟着切语言换 —— 只有点「重置」才换成当前语言的出厂提示词 */

  var themeIdx = 0;
  /* 带数字的文案整段匹配不上词典，按当前界面语言直接给（用户 2026-10-20） */
  function bi(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }

  function sampleLangIdx() { return (window.I18N && window.I18N.lang() === 'en') ? 1 : 0; }


  /* 兜底示例档案：后端没给 defaults 时用（api_key 不在其中） */
  var EXAMPLES = {
    titler: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
      temperature: 0.8, max_tokens: 256,
      system_prompt: '你是一名中文题目设计专家。请根据给定主题生成一个具体、有信息量的写作题目，' +
        '只输出题目本身，不要解释，不要编号。'
    },
    writer: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
      temperature: 0.7, max_tokens: 4096,
      system_prompt: '你是一名中文写作助手。请围绕题目写一篇结构清晰、内容具体的文章，正文控制在 600-900 字，' +
        '不要写标题，不要用列表。'
    },
    reviewer: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
      temperature: 0.1, max_tokens: 512,
      system_prompt: '你是一名严格的写作质检员。判断文章是否切题、是否存在事实错误或空洞套话。' +
        '只输出一个 JSON 对象：{"pass":true或false,"score":0到1,"major_errors":[],"comment":"一句话总评"}'
    }
  };

  /* ---------- 取数交接（治「切页时里面的字要等一会儿才出来」） ----------
     切页时 _registry 会先调 prefetch()，把本页要用的接口先取回来放进 HANDOFF；
     随后 mount 里的取数**同步拿走**（微任务内跑完）→ 新页出现的第一帧字就是满的。
     原来先换页再取数：那次本地 HTTP 往返（实测 24~40ms）里输入框/提示词都是空的，
     就是用户看到的「里面的字也会闪」。
     一次性交接（**拿走即删**）：mount 用掉就不再留着 —— 后续任何刷新（删样本后的列表、
     点「重新加载」）都真请求，不会拿旧数据糊弄。 */
  var HANDOFF = {};
  var PREFETCH_URLS = ['/api/article/config', '/api/article/collection', '/api/article/list'];
  function takeOnce(url) {
    if (!Object.prototype.hasOwnProperty.call(HANDOFF, url)) return null;
    var d = HANDOFF[url];
    delete HANDOFF[url];
    return d;
  }
  /* 页面统一取数入口：预取过就同步给（Promise.resolve → 微任务，同一帧内跑完），否则真请求 */
  function cget(api, url) {
    var h = takeOnce(url);
    return h ? Promise.resolve(h) : api.get(url);
  }

  /* ---------- mock（仅 ?mock=1 预览用） ---------- */
  var MOCK_REASON = '迁移期只读：该动作会写工程 / 联网 / 跑采集，本轮不实现。';
  var ni = function () { return { not_implemented: true, reason: MOCK_REASON }; };
  var MOCK_STATUS = {
    ok: true, exists: false, running: false, events: [], next: 0, total_events: 0,
    elapsed: null, error: '',
    project: '（预览）<工程目录>',
    project_source: '（预览）config.app.recent_projects[0]',
    output_dir: '（预览）<工程目录>\\data\\article',
    output_dir_exists: true,
    output: { dir: '（预览）<工程目录>\\data\\article', dir_exists: true,
      files: [], total: 2, main_files: 1, rejected_files: 0, rejected_records: 0 }
  };

  EAIPages.register('article', {
    title: '语料蒸馏',

    mock: {
      '/api/article/config': {
        project: '（预览）<工程目录>',
        project_source: '（预览）config.app.recent_projects[0]',
        config_path: '（预览）ENGINE_DIR\\config.json',
        config_exists: true,
        models: [
          { key: 'titler', label: '题目模型', tone: 'accent',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.9,
              max_tokens: 4096, system_prompt: '（预览 mock）系统提示词占位。' } },
          { key: 'writer', label: '写作模型', tone: 'ink3',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.8,
              max_tokens: 65536, system_prompt: '（预览 mock）系统提示词占位。' } },
          { key: 'reviewer', label: '审阅模型', tone: 'ink3',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.2,
              max_tokens: 4096, system_prompt: '（预览 mock）系统提示词占位。' } }
        ]
      },
      '/api/article/collection': {
        project: '（预览）<工程目录>',
        project_source: '（预览）config.app.recent_projects[0]',
        output_dir: '（预览）<工程目录>\\data\\article',
        output_dir_exists: true,
        config_output_dir: '（预览）<工程目录>\\data\\article',
        example_themes: '光合作用\n恒星的生命周期\n量子力学入门\n板块构造',
        fields: {
          themes: '光合作用\n恒星的生命周期', count: 20, concurrency: 2,
          min_words: 50, max_words: 500, dataset_name: 'articleZL',
          discard_on_reject: true, save_rejected: false,
          mode: 'stream', stage: 'pretrain'
        }
      },
      '/api/article/list': {
        dir: '（预览）<工程目录>\\data\\article',
        dir_exists: true,
        total: 2,
        sources: ['（预览）articleZL.jsonl'],
        count_source: '（预览 mock）',
        items: [
          { id: 1, title: '（预览）这是一条用于自检表格截断与预览填充的超长标题示例',
            title_short: '（预览）这是一条用于自检表格截断与预览填充的超长标题示例',
            theme: '（预览）主题', score: 8, chars: 512,
            article: '（预览 mock）正文占位：此内容仅用于 ?mock=1 截图，非真实样本。' },
          { id: 2, title: '（预览）示例标题二', title_short: '（预览）示例标题二',
            theme: '（预览）主题', score: 7, chars: 340,
            article: '（预览 mock）正文占位：此内容仅用于 ?mock=1 截图，非真实样本。' }
        ]
      },
      '/api/article/models/save': { ok: true, config_path: '（预览）ENGINE_DIR\\config.json',
        config_exists: true, saved: {} },
      '/api/article/models/test': { ok: true, role: 'titler', model: '（预览）model',
        base_url: '（预览）https://example.invalid/v1', max_tokens: 16, reply: '在线' },
      '/api/article/collect/start': { ok: true, job: 'collect:article',
        output_dir: '（预览）<工程目录>\\data\\article',
        dataset_path: '（预览）<工程目录>\\data\\article\\articleZL.jsonl',
        count: 1, concurrency: 1, min_words: 50, max_words: 500,
        models: { titler: '（预览）model', writer: '（预览）model', reviewer: '（预览）model' } },
      '/api/article/collect/stop': { ok: true, stopped: true, note: '已请求停止' },
      '/api/article/collect/status?since=0': MOCK_STATUS,
      '/api/article/collect/status': MOCK_STATUS,
      '/api/article/export': ni(),
      '/api/article/import': ni(),
      '/api/article/delete': ni(),
      '/api/article/clear': ni()
    },

    /* 换页之前先把本页的接口取回来（见文件头 HANDOFF 说明）：
       _registry 会 await 它再清空旧页 → 新页出现的第一帧字就是满的。
       失败/慢都不挡切换（_registry 有 400ms 保险丝，超时就照旧换页）。 */
    prefetch: function (api) {
      HANDOFF = {};
      return Promise.all(PREFETCH_URLS.map(function (u) {
        return api.get(u).then(function (d) { if (d && !d.error) HANDOFF[u] = d; });
      }));
    },

    mount: function (root, api) {
      var TABS = [['config', '模型配置'], ['collect', '采集中心'], ['data', '数据管理']];
      /* 本次启动内记住用户最后停留的页签。
         挂在 window 上：页面模块可能被重新装载，闭包变量会丢；window 在整个应用进程内稳定。
         不落盘 —— 重启软件后回到「模型配置」。 */
      var LAST_TAB_KEY = 'eaiLastTab:article';
      function getLastTab() { return (window.__eaiLastTab || {})[LAST_TAB_KEY] || ''; }
      function setLastTab(k) { (window.__eaiLastTab = window.__eaiLastTab || {})[LAST_TAB_KEY] = k; }

      /* ---------- 通用小工具 ---------- */
      function button(label, variant, iconName, onClick, muted) {
        var b = api.el('button', 'btn a-btn ' + variant);
        b.type = 'button';
        if (iconName) b.appendChild(iconEl(iconName));
        b.appendChild(api.el('span', 'a-btn-label', label));
        if (muted) b.classList.add('a-btn-muted');
        b.addEventListener('click', function () { onClick(b); });
        return b;
      }
      function explain(res, label) {
        if (!res) return label + '：无响应';
        if (res.not_implemented) return label + '：暂未实现 —— ' + (res.reason || '');
        if (res.error) return label + '：' + res.error;
        return label + '：完成';
      }
      function showStatus(el, res, label) {
        el.textContent = explain(res, label);
        el.classList.toggle('a-warn', !!(res && (res.not_implemented || res.error)));
        el.classList.toggle('a-err', !!(res && res.error));
      }
      function act(path, body, statusEl, label) {
        statusEl.textContent = label + ' …';
        api.post(path, body).then(function (res) { showStatus(statusEl, res, label); });
      }
      function field(parent, label, opts) {
        opts = opts || {};
        var box = api.el('div', 'a-field' + (opts.span2 ? ' a-span2' : ''));
        var lab = api.el('div', 'a-flabel', label);
        if (opts.labelTitle) lab.title = String(opts.labelTitle);
        box.appendChild(lab);
        var inp;
        if (opts.select) {
          /* 自绘下拉（同响应页 2026-10-14）：原生 <select> 的弹层是系统绘制的，选中项方角高亮会盖住弹层圆角 */
          var holder = api.el('input', 'a-input');
          holder.type = 'text';
          holder.readOnly = true;
          holder.value = opts.value == null
            ? String(((opts.options || [])[0] || [''])[0])
            : String(opts.value);
          inp = api.pick(holder, opts.options, 'a-input');
        } else {
          inp = api.el(opts.textarea ? 'textarea' : 'input',
                       'a-input' + (opts.mono ? ' a-mono' : '') + (opts.textarea ? ' a-textarea' : ''));
          if (opts.textarea) { inp.rows = opts.rows || 4; }
          else { inp.type = opts.type || 'text'; }
          inp.value = opts.value == null ? '' : String(opts.value);
        }
        if (opts.readonly) inp.readOnly = true;
        if (opts.secret != null) { api.secretWire(inp); api.secretLock(inp, !!opts.secret); }
        else if (opts.placeholder != null) inp.placeholder = String(opts.placeholder);
        box.appendChild(inp);
        if (opts.note) box.appendChild(api.el('div', 'a-note', opts.note));
        parent.appendChild(box);
        return inp;
      }
      function checkBox(parent, label, checked) {
        var wrap = api.el('label', 'a-check a-row');
        var inp = api.el('input', '');
        inp.type = 'checkbox';
        inp.checked = !!checked;
        wrap.appendChild(inp);
        wrap.appendChild(api.el('span', 'a-check-label', label));
        parent.appendChild(wrap);
        return { wrap: wrap, input: inp };
      }
      function cardError(title, message) {
        var c = api.card(title, '读取失败');
        c.body.appendChild(api.el('p', 'a-status a-err', message || '未知错误'));
        return c;
      }

      /* ---------- 页头 + 分段标签 + 面板容器 ---------- */
      root.appendChild(api.hero('语料蒸馏'));

      var segWrap = api.el('div', 'a-segwrap');
      var seg = api.el('div', 'seg a-seg');
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

      var panels = api.el('div', 'a-panels');
      root.appendChild(panels);
      var bodies = {};
      TABS.forEach(function (t) {
        var p = api.el('section', 'a-panel');
        p.dataset.tab = t[0];
        p.hidden = true;
        panels.appendChild(p);
        bodies[t[0]] = p;
      });

      function pickTab(key) {
        TABS.forEach(function (t) {
          var on = t[0] === key;
          segOpts[t[0]].classList.toggle('on', on);
          segOpts[t[0]].setAttribute('aria-selected', on ? 'true' : 'false');
          bodies[t[0]].hidden = !on;
        });
        api.segThumb(seg, true);              /* 顶栏页签滑块：与训练设备/数据集页同一条非线性缓动 */
        /* 本轮已经取过就不再取：mount 里三个面板都先取过一遍（切页签秒开），
           这里再调一次就是重复请求 —— 第二次回来的值会再盖一次（值相同看不出来，
           不同就是一次「字又变了」的闪），直接挡掉。要手动刷新用各面板自己的按钮。 */
        if (key === 'config' && !LOADED.config) loadConfig();
        if (key === 'data' && !LOADED.data) loadData();
        /* 面板是在隐藏状态下建好的：显示了再算一次表格限高 / 内容框高度，
           否则内容框停在 120px、下边框离窗口底还有一大截（用户 2026-10-20）。 */
        if (key === 'data') {
          /* 先同步算一次再让浏览器画（用户 2026-10-20）：放到 rAF 里会先画一帧旧排版，
             中间那条可拖动的握把就会「跳一下」。 */
          if (api.dmRefit) api.dmRefit();
          requestAnimationFrame(function () { if (api.dmRefit) api.dmRefit(); });
        }
        setLastTab(key);
      }

      /* ==================== 模型配置 ==================== */
      /* 模型卡的状态容器：挂 **mount 作用域**（不是 loadConfig 里）——fillCard 定义在外面，
         放里面会 ReferenceError（实测「cardInputs is not defined」，整段 .then 直接中断、卡片永远空白）。 */
      var cardInputs = {};   // role -> { base_url, api_key, model, temperature, max_tokens, system_prompt }
      var toneSetters = {};  // role -> setTone（色条 = 保存状态）
      var LOADED = {};       // 本轮 mount 里哪些面板已经取过数（挡 pickTab 的重复请求）

      /* 数据到了之后**就地填值**（不重建卡片 → 不闪屏）。只写 .value / 密钥锁态 / 色条，尺寸不变。 */
      function fillCard(key, m) {
        var ins = cardInputs[key];
        if (!ins) return;
        var prof = m.profile || {};
        ['base_url', 'model', 'temperature', 'max_tokens', 'system_prompt'].forEach(function (f) {
          var v = prof[f];
          ins[f].value = v == null ? '' : String(v);
        });
        api.secretLock(ins.api_key, !!prof.api_key_set);
        /* 色条 = 保存状态（用户 2026-10-14）：值从配置读来 = 已保存 → 紫 */
        var setTone = toneSetters[key];
        if (setTone) setTone(true);
      }

      function loadConfig() {
        LOADED.config = true;
        var host = bodies.config;
        host.innerHTML = '';
        var stack = api.el('div', 'stack');
        var grid = api.el('div', 'a-grid3');
        var status = api.el('p', 'a-status');

        stack.appendChild(grid);
        cardInputs = {};       /* 重新加载就重来一遍（容器本身挂在 mount 作用域） */
        toneSetters = {};

        function modelCard(m) {
          var card = api.card(m.label, null);
          card.classList.add('a-modelcard');
          /* 分隔条 = 该卡保存状态（用户 2026-10-14）：紫 = 已保存，灰 = 有修改未保存，
             点「保存配置」成功 → 变紫。连通态只看「测试」按钮旁的状态点。 */
          var bar = api.el('div', 'a-tonebar');
          card.body.insertBefore(bar, card.body.firstChild);
          var prof = m.profile || {};
          var ins = {};
          ins.base_url = field(card.body, 'base_url', { value: prof.base_url });
          ins.api_key = field(card.body, 'api_key', {
            value: '', mono: true, secret: !!prof.api_key_set,
            labelTitle: '留空即保留原值；已保存时显示为点，点一下可输入新密钥'
          });
          ins.model = field(card.body, 'model', { value: prof.model });
          ins.temperature = field(card.body, 'temperature', { value: prof.temperature });
          ins.max_tokens = field(card.body, 'max_tokens', { value: prof.max_tokens });
          ins.system_prompt = field(card.body, '系统提示词', {
            value: prof.system_prompt, textarea: true, rows: 5
          });
          /* 标签贴着上一个输入框（间距 0）显得太靠上 —— 单独给这个字段加一点上边距 */
          ins.system_prompt.parentElement.classList.add('a-field-prompt');
          function setTone(saved) {
            bar.classList.toggle('a-tone-accent', !!saved);
          }
          setTone(true);   /* 刚建/刚读进来的值 = 已保存 */
          /* 任何字段被改动（input/change 冒泡，替换过的下拉框也盖得到）→ 该卡变灰 */
          card.addEventListener('input', function () { setTone(false); });
          card.addEventListener('change', function () { setTone(false); });
          cardInputs[m.key] = ins;
          toneSetters[m.key] = setTone;          /* 按 key 存，供 fillCard / 保存成功后就地刷色条 */

          var dot = api.dot('idle', '尚未测试');
          var testBtn = button('测试', 'a-btn-soft', '', function () {
            var base = ins.base_url.value;
            var ak0 = api.secretValue(ins.api_key);
            api.dotSet(dot, 'busy', '[' + m.key + '] 正在测试连通并读取模型列表 …');
            testBtn.disabled = true;
            /* 第一跳：列模型（快、不耗 token）——成功就顺手把 model 变成下拉框 */
            api.post('/api/settings/models', { base_url: base, api_key: ak0 }).then(function (lst) {
              var ids = (lst && lst.ok && lst.models) || [];
              if (ids.length) {
                ins.model = api.modelSelect(ins.model, ids, 'a-input');   /* 字体与 base_url 一致（不用等宽） */
                testBtn.disabled = false;
                api.dotSet(dot, 'ok', '[' + m.key + '] 连通成功 · ' + ids.length +
                  ' 个模型可选（' + (lst.ms || 0) + 'ms）→ model 已变成下拉框');
                return;
              }
              /* 密钥问题(401/403)、或压根连不上（没有 http 状态：DNS/超时/拒绝）→ 跳过 chat 兜底：
                 兜底只对「服务答了但不给列表」才有意义，否则只是多等十几秒 */
              var http = lst && lst.http;
              if (!http || http === 401 || http === 403) {
                testBtn.disabled = false;
                api.dotSet(dot, 'err', '[' + m.key + '] 连通失败：' +
                  (http ? 'HTTP ' + http + '（密钥无效或已过期，或与本卡地址不匹配）'
                        : String((lst && lst.error) || '连不上（地址/网络）')) +
                  ' · 已跳过慢速兜底测试');
                return;
              }
              /* 第二跳：列不出来（服务不支持 /models）→ 退回最小 chat 请求，model 保持手填 */
              var body = { role: m.key, base_url: base, model: ins.model.value };
              if (ak0) body.api_key = ak0;
              api.post('/api/article/models/test', body).then(function (res) {
                testBtn.disabled = false;
                if (res && res.ok) {
                  api.dotSet(dot, 'ok', '[' + m.key + '] 连通成功 · ' + (res.model || '') +
                    '（max_tokens=' + (res.max_tokens || 16) + '）→ ' + (res.reply || '（空）') +
                    '；该服务不支持 /models，model 仍需手填');
                } else {
                  var why = (lst && (lst.error || lst.note)) ||
                    (res && (res.error || res.reason)) || '未知错误';
                  api.dotSet(dot, 'err', '[' + m.key + '] 连通失败：' + why +
                    (/40[13]/.test(why) ? '（密钥无效或已过期，或与本卡地址不匹配）' : '') + ' · model 保持手填');
                }
              });
            });
          });
          /* 连通性测试按钮放进 api_key 右侧的新一行（label 留在行外面，仍是上下排列）*/
          var keyRow = api.el('div', 'a-keyrow');
          ins.api_key.parentElement.insertBefore(keyRow, ins.api_key);
          keyRow.appendChild(ins.api_key);
          keyRow.appendChild(dot);
          keyRow.appendChild(testBtn);
          return card;
        }

        var bar2 = api.el('div', 'a-row a-bar');
        bar2.appendChild(button('保存配置', 'a-btn-accent', 'save', function () {
          var models = {};
          Object.keys(cardInputs).forEach(function (role) {
            var ins = cardInputs[role];
            var p = {
              base_url: ins.base_url.value,
              model: ins.model.value,
              temperature: ins.temperature.value,
              max_tokens: ins.max_tokens.value,
              system_prompt: ins.system_prompt.value
            };
            var ak = api.secretValue(ins.api_key);   // 锁定态（点数）算空 → 不传 → 保留原值
            if (ak) p.api_key = ak;
            models[role] = p;
          });
          status.textContent = '保存配置 …';
          status.classList.remove('a-warn', 'a-err');
          api.post('/api/article/models/save', { models: models }).then(function (res) {
            if (res && res.ok) {
              status.textContent = '';      /* 保存成功不弹提示（用户口径：只在失败时写状态行） */
              status.classList.remove('a-warn', 'a-err');
              /* 不再 loadConfig()：整块重建会闪屏。就地刷新即可（把刚提交的密钥框锁回点数态）。 */
              Object.keys(cardInputs).forEach(function (role) {
                var ins = cardInputs[role];
                if (!ins || !ins.api_key) return;
                var hadKey = ins.api_key.dataset.locked === '1' || !!String(ins.api_key.value || '').trim();
                api.secretLock(ins.api_key, hadKey);
              });
              /* 保存成功 → 色条全部变紫（保存状态：已保存） */
              Object.keys(toneSetters).forEach(function (role) {
                try { toneSetters[role](true); } catch (e) { /* 单卡失败不影响其它 */ }
              });
            } else {
              status.textContent = '保存失败：' + ((res && (res.error || res.reason)) || '未知错误');
              status.classList.add('a-err');
            }
          });
        }));
        /* 重置（用户 2026-10-20：原「填充示例」改成重置）：只把 temperature / max_tokens /
           系统提示词 恢复成后端下发的出厂默认，base_url / model / api_key 一律不动 ——
           换服务商或换模型名之后点一下不会把地址和密钥清掉。 */
        bar2.appendChild(button('重置', 'a-btn-ghost', 'refresh', function () {
          var SRC = (MODEL_DEFAULTS && Object.keys(MODEL_DEFAULTS).length) ? MODEL_DEFAULTS : EXAMPLES;
          Object.keys(SRC).forEach(function (k) {
            var ins = cardInputs[k];
            if (!ins) return;
            var ex = SRC[k];
            var en = !!(window.I18N && window.I18N.lang() === 'en');
            ['temperature', 'max_tokens', 'system_prompt'].forEach(function (f) {
              if (!ins[f] || ins[f].value === undefined) return;
              /* 英文界面：系统提示词用英文版（用户 2026-10-20） */
              var v = ex[f];
              if (f === 'system_prompt') {
                v = en ? (ex.system_prompt_en || PROMPTS_EN[k] || ex.system_prompt) : ex.system_prompt;
              }
              ins[f].value = String(v == null ? '' : v);
            });
            if (toneSetters[k]) toneSetters[k](false);
          });
          status.textContent = '';
        }));
        bar2.appendChild(status);        /* 状态行并入按钮行：页脚恒一行高，两页签按钮同高 */
        stack.appendChild(bar2);
        host.appendChild(stack);

        /* 先把三张卡**同步**建好（空值、紫条=已保存）——进页第一帧就是完整版面；
           具体数值等 /api/article/config 回来再就地填（fillCard：只改 .value，不改尺寸）。 */
        CARD_DEFS.forEach(function (d) {
          grid.appendChild(modelCard({ key: d[0], label: d[1], profile: {} }));
        });

        status.textContent = '正在读取 config.json …';
        cget(api, '/api/article/config').then(function (d) {
          if (d && d.error) { grid.appendChild(cardError('模型配置', d.error)); status.textContent = ''; return; }
          MODEL_DEFAULTS = (d && d.defaults) || null;
          (d.models || []).forEach(function (m) {
            if (cardInputs[m.key]) fillCard(m.key, m);
            else grid.appendChild(modelCard(m));   /* 后端多出来的角色才补卡（正常不会发生） */
          });
          /* 工程/配置路径不再写在页面上（要看的悬停 title）；只有配置真缺失时才报一句 */
          status.textContent = d.config_exists ? '' : '配置文件缺失：' + (d.config_path || 'config.json');
          status.title = '当前工程：' + (d.project || '—') +
            '（来源：' + (d.project_source || '—') + '） · 配置：' + (d.config_path || '—');
        });
      }

      /* ==================== 采集中心 ==================== */
      var running = false;
      var cursor = 0;

      function loadCollect() {
        LOADED.collect = true;
        var host = bodies.collect;
        host.innerHTML = '';

        /* 两栏：左 = 设置（采集参数 + 操作按钮），右 = 进度 + 运行日志（整页一屏，不出滚动条） */
        var stack = api.el('div', 'a-collect');
        var colL = api.el('div', 'a-collect-col');
        var colR = api.el('div', 'a-collect-col');

        var paramCard = api.card('采集参数');
        var form = api.el('div', 'a-grid2');
        /* 用户 2026-10-14：标签去掉「（每行一个）」；不填主题 = 随机主题（后端自由主题），
           提示只在框内无内容时出现（占位符），字体与其他文字有差别（斜体、小一号、灰） */
        var themesEl = field(form, '主题', {
          textarea: true, rows: 5, span2: true,
          placeholder: '每行一个，\n不填入表示随机主题。'
        });
        var fCount = field(form, '采集数量', { value: '20' });
        var fConc = field(form, '并发数', { value: '2' });
        fConc.min = '1'; fConc.max = '5';   /* 并发上限 5（用户 2026-10-14） */
        fConc.addEventListener('change', function () {
          fConc.value = String(Math.min(5, Math.max(1, parseInt(fConc.value, 10) || 1)));
        });
        var fMin = field(form, '最少字数', { value: '200' });
        var fMax = field(form, '最多字数', { value: '1000' });
        /* 数据集名称右侧同响应页样式（用户 2026-10-14）：选择这条纯文本数据
           用于预训练还是继续预训练。采集器本体即流式流水线（无批量路径），口径固定流式。 */
        var fName = field(form, '数据集名称', { value: 'article_set' });
        var stageWrap = field(form, '训练方式', {
          select: true,
          options: [['pretrain', '预训练'], ['continue', '继续预训练']]
        });
        var outEl = field(form, '输出目录', {
          span2: true, readonly: true,
        });
        paramCard.body.appendChild(form);

        var checks = api.el('div', 'a-row a-checks');
        /* 被拒文本：开关。开 = 保存并隔离被拒文本（逐条留档），关 = 丢弃（不进数据集也不留档） */
        var rejRow = api.el('div', 'a-check a-row');
        rejRow.appendChild(api.el('span', 'a-check-label', '保存并隔离被拒文本'));
        var rejSwitch = api.toggle(false);
        rejRow.appendChild(rejSwitch.el);
        checks.appendChild(rejRow);
        paramCard.body.appendChild(checks);
        colL.appendChild(paramCard);

        /* 用户 2026-10-19：切页回来已填的东西要还在 —— 表单输入即存到本工程 ui_state，
           重挂时由 /api/article/collection 回填（原来采集页不回写 config，一切页就回默认值）。 */
        function currentForm() {
          return {
            themes: themesEl.value,
            count: fCount.value,
            concurrency: Math.min(5, Math.max(1, parseInt(fConc.value, 10) || 1)),
            min_words: fMin.value,
            max_words: fMax.value,
            dataset_name: fName.value,
            discard_on_reject: true,   /* 与滑块同义：不通过一律不进主数据集 */
            save_rejected: !!rejSwitch.value,
            stage: stageWrap.value,
            mode: 'stream'
          };
        }

        var formSaveTimer = null;
        function saveFormSoon() {
          if (formSaveTimer) clearTimeout(formSaveTimer);
          formSaveTimer = setTimeout(function () {
            formSaveTimer = null;
            api.post('/api/article/collection', currentForm()).then(function () {}, function () {});
          }, 400);
        }
        [themesEl, fCount, fConc, fMin, fMax, fName].forEach(function (el) {
          el.addEventListener('input', saveFormSoon);
          el.addEventListener('change', saveFormSoon);
        });
        stageWrap.addEventListener('change', saveFormSoon);
        rejSwitch.el.addEventListener('click', saveFormSoon);
        rejSwitch.el.addEventListener('change', saveFormSoon);

        var logEl = api.el('div', 'a-log');
        var status = api.el('p', 'a-status');


        function logLine(text, cls) {
          /* 本来贴在底部才跟随滚动；用户用滚轮翻上去看时不被新行拽回底部 */
          var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 8;
          var line = api.el('span', 'a-log-line' + (cls ? ' ' + cls : ''), text);
          logEl.appendChild(line);
          if (atBottom) logEl.scrollTop = logEl.scrollHeight;
        }

        var actions = api.el('div', 'a-row a-actions');
        var startBtn = button('开始采集', 'a-btn-accent', 'play', onStart);
        var stopBtn = button('停止', 'a-btn-danger', 'stop', onStop);
        actions.appendChild(startBtn);
        actions.appendChild(stopBtn);
        /* 用户 2026-10-20：同响应蒸馏，「刷新」放「停止」和「填充示例主题」之间；
           立刻重拉日志与进度，不等下一拍 */
        var refreshBtn = button('刷新', 'a-btn-ghost', 'refresh', onRefresh);
        actions.appendChild(refreshBtn);
        actions.appendChild(button('填充示例主题', 'a-btn-ghost', 'sample', function () {
          /* 每点一次换下一个主题（用户 2026-10-20）：以前是一次把后端给的 4 条全塞进来 */
          themesEl.value = THEME_SAMPLES[themeIdx % THEME_SAMPLES.length][sampleLangIdx()];
          themeIdx += 1;
          status.textContent = '';        /* 用户口径：按钮下方不留文字提示 */
          status.classList.remove('a-warn', 'a-err');
        }));
        /* 按钮行 + 状态行是页脚：不再放在左栏卡片下，改由 CSS margin-top:auto 压到页签底部 */

        var progCard = api.card('进度');
        var track = api.el('div', 'a-progress');
        var fill = api.el('div', 'a-progress-fill');
        track.appendChild(fill);
        progCard.body.appendChild(track);
        var progressLabel = api.el('div', 'a-progress-label', '尚未开始');
        progCard.body.appendChild(progressLabel);
        colR.appendChild(progCard);

        var logCard = api.card('运行日志');
        logCard.body.appendChild(logEl);
        colR.appendChild(logCard);

        stack.appendChild(colL);
        stack.appendChild(colR);
        host.appendChild(stack);
        actions.appendChild(status);    /* 状态行并入按钮行：页脚恒一行高，两页签按钮同高 */
        host.appendChild(actions);

        /* ---------- 运行态 UI ---------- */
        function setRunning(on) {
          running = !!on;
          startBtn.disabled = running;
          stopBtn.disabled = !running;
          root.classList.toggle('a-running', running);
        }
        setRunning(false);

        function applyProgress(p) {
          var total = Math.max(1, Number(p && p.total) || 1);
          var done = Number((p && p.done) || 0);
          var discarded = Number((p && p.discarded) || 0);
          var failed = Number((p && p.failed) || 0);
          var inflight = Number((p && p.inflight) || 0);
          var finished = (p && p.finished != null)
            ? Number(p.finished) : (done + discarded + failed);
          fill.style.width = Math.min(100, Math.round(finished / total * 100)) + '%';
          progressLabel.textContent = '完成 ' + done + '/' + total + ' · 丢弃 ' + discarded +
            ' · 失败 ' + failed + ' · 进行中 ' + inflight +
            (p.dup_rate == null ? '' : ' · 重复率 ' + Math.round(Number(p.dup_rate) * 100) + '%');
        }

        function handleEvents(events) {
          (events || []).forEach(function (ev) {
            var kind = ev && ev.kind;
            var pl = (ev && ev.payload) || {};
            if (kind === 'log') {
              logLine(pl.text || '', null);
            } else if (kind === 'started') {
              logLine('采集开始，计划 ' + (pl.total || 0) + ' 条。', null);
            } else if (kind === 'progress') {
              applyProgress(pl);
            } else if (kind === 'record') {
              var r = pl.record || {};
              logLine('入库：' + (r.title || ''), null);
            } else if (kind === 'dup_warn') {
              api.alertBox({ title: '重复率警告',
                message: '当前重复率已达 ' + Math.round(Number(pl.rate || 0) * 100) + '%（≥20%），收集到的内容重复偏高。' });
            } else if (kind === 'dup_abort') {
              api.alertBox({ title: '重复率过高，已中止',
                message: '当前重复率已达 ' + Math.round(Number(pl.rate || 0) * 100) + '%（≥40%），采集已中止，已收集的内容保留。' });
            } else if (kind === 'finished') {
              var el = Number(pl.elapsed || 0);
              logLine('采集结束：入库 ' + (pl.done || 0) + ' · 丢弃 ' + (pl.discarded || 0) +
                ' · 失败 ' + (pl.failed || 0) + ' · 用时 ' +
                (isFinite(el) ? el.toFixed(1) : '0.0') + 's', null);
            } else if (kind === 'error') {
              logLine('错误：' + (pl.text || pl.message || '未知错误'), 'warn');
            } else if (kind === 'done') {
              /* 由 pollOnce 统一收尾 */
            } else if (pl.text || pl.message) {
              logLine(String(pl.text || pl.message), null);
            }
          });
        }

        function pollOnce() {
          return api.get('/api/article/collect/status?since=' + cursor).then(function (d) {
            if (!d || d.error || d.ok === false) {
              logLine('[状态错误] ' + ((d && d.error) || '未知错误'), 'warn');
              return;
            }
            if (d.exists === false) { setRunning(false); return; }
            handleEvents(d.events || []);
            if (typeof d.next === 'number') cursor = d.next;
            if (d.running) {
              setRunning(true);
            } else if (running) {
              finishRun(d);
            } else {
              setRunning(false);
            }
          });
        }

        function startPolling() {
          if (pollTimer) clearInterval(pollTimer);
          pollTimer = setInterval(pollOnce, 1000);
          pollOnce();
        }

        function finishRun(d) {
          if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
          setRunning(false);
          var out = (d && d.output) || {};
          status.classList.remove('a-err');
          status.textContent = '';        /* 用户口径：按钮下方不留文字提示（输出目录/条数看右侧日志） */
          logLine('采集结束。', null);
          loadData();
        }

        /* ---------- 动作 ---------- */
        function onStart() {
          if (running) return;
          var body = currentForm();   /* 与输入即存同一份口径（用户 2026-10-19） */
          body.lang = (window.I18N && window.I18N.lang()) || 'zh';   /* 蒸馏数据语言跟界面（用户 2026-10-20） */
          logEl.innerHTML = '';
          progressLabel.textContent = '正在启动…';
          fill.style.width = '0%';
          status.classList.remove('a-err', 'a-warn');
          status.textContent = '';        /* 用户口径：按钮下方不留文字提示 */
          startBtn.disabled = true;
          stopBtn.disabled = true;
          api.post('/api/article/collect/start', body).then(function (res) {
            if (!res || res.ok === false || res.error) {
              status.textContent = '启动失败：' + ((res && (res.error || res.reason)) || '未知错误');
              status.classList.add('a-err');
              setRunning(false);
              return;
            }
            status.textContent = '';      /* 用户口径：按钮下方不留文字提示 */
            logLine('[宿主] 已启动 ' + (res.job || 'collect:article') +
              ' · 主题 ' + String(res.themes || '').replace(/\n/g, ' / ') +
              ' · 数量 ' + res.count + ' · 并发 ' + res.concurrency +
              ' · 模型 ' + JSON.stringify(res.models || {}), null);
            cursor = 0;
            setRunning(true);
            startPolling();
          });
        }

        function onStop() {
          stopBtn.disabled = true;
          api.post('/api/article/collect/stop', {}).then(function (res) {
            logLine('停止请求：' + ((res && (res.note || res.error)) || ''), 'warn');
            if (!pollTimer) pollOnce();
          });
        }

        /* 刷新（用户 2026-10-20：同响应蒸馏）：立刻重拉日志与进度，不等下一拍 */
        function onRefresh(btn) {
          if (btn) btn.disabled = true;
          if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
          var p = pollOnce();
          Promise.resolve(p).then(function () {
            if (running) startPolling();     /* 重置节奏，别等上一拍 */
            if (btn) btn.disabled = false;
          }, function () { if (btn) btn.disabled = false; });
        }

        cget(api, '/api/article/collection').then(function (d) {
          if (d && d.error) {
            status.textContent = '采集参数读取失败：' + d.error;
            status.classList.add('a-err');
            return;
          }
          var f = d.fields || {};
          themesEl.value = f.themes == null ? '' : String(f.themes);
          fCount.value = f.count == null ? '' : String(f.count);
          fConc.value = f.concurrency == null ? '' : String(f.concurrency);
          fMin.value = f.min_words == null ? '' : String(f.min_words);
          fMax.value = f.max_words == null ? '' : String(f.max_words);
          fName.value = f.dataset_name == null ? '' : String(f.dataset_name);
          rejSwitch.set(!!f.save_rejected);
          stageWrap.value = (f.stage === 'continue') ? 'continue' : 'pretrain';
          outEl.value = d.output_dir || '';
          /* 主题示例已改成前端本地列表（每次点换一条，用户 2026-10-20），不再取后端那组 */
          /* 用户要求：不显示「输出目录存在」这类正常态提示；这行只留给失败/异常写 */
          status.textContent = '';
          /* 若已有采集在跑（例如切页回来），同步运行态并补上缓冲事件 */
          pollOnce();
        });
      }

      /* ==================== 数据管理 ==================== */
      /* 选择态按「行内容指纹」记（it.key）：删除后行号会移动、筛选会重排渲染，
         用指纹才不会张冠李戴。DATA 放模块作用域，切页签回来还在。 */
      var DATA = {
        items: [], sel: {}, q: '', themes: [],   /* themes：主题多选（用户 2026-10-14），空 = 全部 */
        files: [], fsel: {},                     /* 文件视图：产物文件清单 + 选中（键=文件名） */
        view: 'files', file: null,               /* view: files=文件列表（默认） | records=逐条数据 */
        lastFile: null,                          /* 上一次双击进去的文件：文件视图里的「返回」退它 */
      };
      var DUI = null;

      function dataFail(msg) {
        if (!DUI) return;
        DUI.status.textContent = msg;
        DUI.status.classList.add('a-err');
      }

      function dataOK() {
        if (!DUI) return;
        DUI.status.textContent = '';
        DUI.status.classList.remove('a-err', 'a-warn');
      }

      function dataHit(it) {
        var q = DATA.q.toLowerCase();
        if (!q) return true;
        /* 搜索范围四选一（用户 2026-10-15）：文件名/主题/问题/文本 */
        var s2 = DATA.scope || 'fname';
        var txt;
        if (s2 === 'theme') txt = it.theme || '';
        else if (s2 === 'q') txt = it.title || '';
        else if (s2 === 'text') txt = it.article || '';
        else {
          var mf = null;
          DATA.files.forEach(function (f) { if (f.meta === it.src) mf = f; });
          txt = String((mf && mf.name) || it.src || '');
        }
        return txt.toLowerCase().indexOf(q) >= 0;
      }

      /* 逐条数据的当前范围：双击进了某个文件，就只看该文件的内容 */
      function scopedItems() {
        return DATA.items.filter(function (it) {
          return DATA.file ? it.src === DATA.file.meta : true;
        });
      }
      function shownItems() { return scopedItems().filter(dataHit); }
      function fileHit(f) {
        var q = DATA.q.toLowerCase();
        if (!q) return true;
        /* 文件视图按搜索范围命中（用户 2026-10-15）：文件名 → 比文件名；其余 → 比文件内逐条 */
        if ((DATA.scope || 'fname') === 'fname') return (f.name || '').toLowerCase().indexOf(q) >= 0;
        return DATA.items.some(function (it) { return it.src === f.meta && dataHit(it); });
      }
      function shownFiles() { return DATA.files.filter(fileHit); }
      function renderCur() { if (DATA.view === 'files') renderFiles(); else renderRows(); }

      /* 预览写入带序号：文件全文是异步取的，双击进逐条的「首条数据」会被晚到的整文件覆盖 */
      function setPreview(text) {
        if (!DUI) return;
        DUI.pvSeq = (DUI.pvSeq || 0) + 1;
        DUI.preview.value = text;
      }

      function syncPick() {
        if (!DUI) return;
        /* 文件视图的全选态 */
        var fs = shownFiles();
        var nF = 0;
        fs.forEach(function (f) { if (DATA.fsel[f.name]) nF++; });
        DUI.fAllBox.checked = fs.length > 0 && nF >= fs.length;
        DUI.fAllBox.indeterminate = nF > 0 && nF < fs.length;
        DUI.fAllBox.disabled = fs.length === 0;   /* 没产物时全选框不该像能勾（用户 2026-10-20） */
        /* 逐条视图的全选态 */
        var shown = shownItems();
        var n = 0;
        shown.forEach(function (it) { if (DATA.sel[it.key]) n++; });
        DUI.allBox.checked = shown.length > 0 && n >= shown.length;
        DUI.allBox.indeterminate = n > 0 && n < shown.length;
        DUI.allBox.disabled = shown.length === 0;
        if (DATA.view === 'files') {
          DUI.delBtn.disabled = nF === 0;
          /* 没选中不能触发导出（用户 2026-10-14）；文件视图导出 = 选中文件里的逐条数据 */
          DUI.exportBtn.disabled = !fs.some(function (f) { return DATA.fsel[f.name] && f.count > 0; });
          DUI.mergeBtn.disabled = nF < 2;   /* 合并要选中 ≥2 个文件 */
        } else {
          DUI.delBtn.disabled = n === 0;
          DUI.exportBtn.disabled = n === 0;
          DUI.mergeBtn.disabled = true;     /* 逐条视图无文件概念 */
        }
      }

      /* 视图切换：files=文件列表（默认）/ records=某文件的逐条数据（双击进入） */
      function setView(view, file) {
        DATA.view = view;
        DATA.file = file || null;
        if (view === 'records' && file) DATA.lastFile = file;   /* 记住来路：文件视图的「返回」退它 */
        if (DUI) {
          DUI.filesWrap.hidden = view !== 'files';
          DUI.recWrap.hidden = view !== 'records';
          /* 用户 2026-10-15：数据集视图里「返回」置灰禁用（与旁边未勾选时的导出/合并一样），逐条视图才可点 */
          DUI.backBtn.hidden = false;
          DUI.backBtn.disabled = view !== 'records';
        }
        if (view === 'files') {
          renderFiles();
          /* 返回数据集：内容自动换到第一条数据集（用户 2026-10-15），和进逐条视图换第一条同口径 */
          var f0 = shownFiles()[0] || DATA.files[0];
          if (f0) { DATA.fileOn = f0.name; showFileText(f0); highlightFileRow(f0.name); }
          return;
        }
        renderRows();
        /* 进逐条视图：下方自动换到第一条数据（用户 2026-10-14），不用再点一下 */
        var first = shownItems()[0] || scopedItems()[0];
        if (first) setPreview('# ' + (first.title || '') + '\n\n' + (first.article || ''));
      }

      function fmtSize(n) {
        n = Number(n) || 0;
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1024 / 1024).toFixed(1) + ' MB';
      }
      function fmtDate(mt) {
        var d = new Date((Number(mt) || 0) * 1000);
        if (!d.getTime()) return '';
        var p = function (x) { return (x < 10 ? '0' : '') + x; };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
      }

      /* 单击整个文件 → 下方栏显示整个数据（异步取，带序号防被后到的动作盖掉） */
      function showFileText(f) {
        /* 内容框显示的是哪个文件 → 表格里那一行保持高亮（用户 2026-10-22）：
           从这里统一记，自动换第一条 / 点行 / 筛选后重画都能对上 */
        DATA.fileOn = (f && f.name) ? String(f.name) : '';
        highlightFileRow(DATA.fileOn);
        var seq = (DUI.pvSeq || 0) + 1;
        DUI.pvSeq = seq;
        cget(api, '/api/article/file_text?name=' + encodeURIComponent(f.name)).then(function (d) {
          if (seq !== DUI.pvSeq) return;   /* 双击进逐条/点了别的行已抢先更新，别盖掉 */
          if (d && d.ok !== false && d.text != null) {
            DUI.preview.value = d.text + (d.truncated ? bi('\n\n…（文件过大，已截断显示）', '\n\n… (file too large, truncated)') : '');
          } else {
            DUI.preview.value = bi('文件读取失败：', 'Failed to read file: ') + ((d && d.error) || bi('未知错误', 'unknown error'));
          }
        });
      }

      /* 内容框当前显示的文件在表格里保持高亮（用户 2026-10-22，照网页整理） */
      function highlightFileRow(name) {
        if (!DUI || !DUI.ftbody) return;
        [].forEach.call(DUI.ftbody.querySelectorAll('tr'), function (tr) {
          var td = tr.querySelector('.col-fname');
          tr.classList.toggle('rs-row-on', !!name && !!td && td.textContent === name);
        });
      }

      function renderFiles() {
        if (!DUI) return;
        var fs = shownFiles();
        DUI.ftbody.innerHTML = '';
        if (!fs.length) {
          var tr0 = api.el('tr', 'a-empty-row');   /* 空态行：不高亮（用户 2026-10-20） */
          var td0 = api.el('td', 'a-empty');
          td0.colSpan = 5;   /* 文件表 5 列（勾选 + 文件名/条数/体量/日期）：写 6 会让表头只铺半截（用户 2026-10-20） */
          td0.textContent = DATA.files.length ? '没有匹配的文件。'
            : '暂无产物';   /* 空页面统一这一句（用户 2026-10-20）：英译 No output yet */
          tr0.appendChild(td0);
          DUI.ftbody.appendChild(tr0);
        } else {
          fs.forEach(function (f) {
            var tr = api.el('tr');
            var wrap = api.el('label', 'a-check a-pick');
            var box = api.el('input', '');
            box.type = 'checkbox';
            box.checked = !!DATA.fsel[f.name];
            box.addEventListener('change', function () {
              if (box.checked) DATA.fsel[f.name] = 1;
              else delete DATA.fsel[f.name];
              syncPick();
            });
            wrap.appendChild(box);
            var tdPick = api.el('td', 'col-pick');
            tdPick.appendChild(wrap);
            tr.appendChild(tdPick);
            tr.appendChild(api.el('td', 'col-fname', f.name));
            /* 启用状态不在此页设置（用户 2026-10-15）：列与开关整块下线 */
            tr.appendChild(api.el('td', 'col-count', String(f.count == null ? 0 : f.count)));
            tr.appendChild(api.el('td', 'col-size', fmtSize(f.size)));
            tr.appendChild(api.el('td', 'col-date', fmtDate(f.mtime)));
            /* 当前内容框显示的是哪个文件（用户 2026-10-22）：行保持高亮，和网页整理一致 */
            if (DATA.fileOn === f.name) tr.classList.add('rs-row-on');
            tr.addEventListener('click', function () {
              DATA.fileOn = f.name;
              [].forEach.call(DUI.ftbody.querySelectorAll('tr.rs-row-on'), function (x) {
                if (x !== tr) x.classList.remove('rs-row-on');
              });
              tr.classList.add('rs-row-on');
              showFileText(f);
            });
            tr.addEventListener('dblclick', function () {
              /* 双击文件 → 只显示该文件的逐条数据 */
              setView('records', f);
            });
            DUI.ftbody.appendChild(tr);
          });
        }
        /* 内容框里没东西（刚进页 / 筛掉了当前文件）→ 自动显示第一条并高亮，
           和网页整理一致（用户 2026-10-22） */
        (function () {
          var names = fs.map(function (x) { return String(x.name || ''); });
          if (!names.length) { DATA.fileOn = ''; return; }
          if (names.indexOf(String(DATA.fileOn || '')) < 0) showFileText(fs[0]);
        })();
        highlightFileRow(DATA.fileOn);   /* 重画后高亮跟着内容框走（用户 2026-10-22） */
        syncPick();
        if (DUI.fitDm) DUI.fitDm();   /* 表格限高重算（用户 2026-10-21） */
      }

      var ROW_BATCH = 200;   /* 每批行数（用户 2026-10-23） */

      function buildArtRow(it) {
        var tr = api.el('tr');
        var wrap = api.el('label', 'a-check a-pick');
        var box = api.el('input', '');
        box.type = 'checkbox';
        box.checked = !!DATA.sel[it.key];
        box.addEventListener('change', function () {
          if (box.checked) DATA.sel[it.key] = { src: it.src, line: it.line, key: it.key };
          else delete DATA.sel[it.key];
          syncPick();
        });
        wrap.appendChild(box);
        var tdPick = api.el('td', 'col-pick');
        tdPick.appendChild(wrap);
        tr.appendChild(tdPick);
        tr.appendChild(api.el('td', 'col-id', String(it.id == null ? '' : it.id)));
        tr.appendChild(api.el('td', 'col-title', it.title_short || it.title || ''));
        tr.appendChild(api.el('td', 'col-theme', it.theme || ''));
        tr.addEventListener('click', function () {
          /* 单击=下方显示内容，且行保持高亮（用户 2026-10-21） */
          if (DATA.rowOn && DATA.rowOn.parentNode) DATA.rowOn.classList.remove('rs-row-on');
          tr.classList.add('rs-row-on');
          DATA.rowOn = tr;
          setPreview('# ' + (it.title || '') + '\n\n' + (it.article || ''));
        });
        return tr;
      }

      function appendArtRows(shown, from) {
        var end = Math.min(shown.length, from + ROW_BATCH);
        var frag = document.createDocumentFragment();
        for (var i = from; i < end; i++) frag.appendChild(buildArtRow(shown[i]));
        DUI.tbody.appendChild(frag);
        return end;
      }

      function addArtMoreRow(shown, from) {
        if (!DUI || from >= shown.length) return;
        var tr = api.el('tr', 'rs-more-row');
        var td = api.el('td', 'rs-more-cell');
        td.colSpan = 4;
        var btn = api.el('button', 'rs-more-btn', window.bi('显示更多（还有 ' + (shown.length - from) + ' 条）', 'Show more (' + (shown.length - from) + ' left)'));
        btn.addEventListener('click', function () {
          tr.remove();
          var next = appendArtRows(shown, from);
          addArtMoreRow(shown, next);
          syncPick();
          if (DUI.fitDm) DUI.fitDm();
        });
        td.appendChild(btn);
        tr.appendChild(td);
        DUI.tbody.appendChild(tr);
      }

      function renderRows() {
        if (!DUI) return;
        var shown = shownItems();
        DUI.tbody.innerHTML = '';
        if (!shown.length) {
          var tr0 = api.el('tr', 'a-empty-row');
          var td0 = api.el('td', 'a-empty');
          td0.colSpan = 4;
          td0.textContent = scopedItems().length ? '没有匹配的样本。'
            : (DATA.file ? '该文件没有可读的逐条数据。'
              : '暂无产物');
          tr0.appendChild(td0);
          DUI.tbody.appendChild(tr0);
        } else {
          /* 分批画（用户 2026-10-23）：原来一次把全部样本建成 tr（312 条实测 231ms），
             现在先画 200 行，底部「显示更多」再追加 200 行。 */
          var end = appendArtRows(shown, 0);
          addArtMoreRow(shown, end);
        }
        syncPick();
        if (DUI.fitDm) DUI.fitDm();   /* 表格限高重算（用户 2026-10-21） */
      }

      function fetchList() {
        dataOK();
        cget(api, '/api/article/list').then(function (d) {
          if (d && d.error) { dataFail('样本列表读取失败：' + d.error); return; }
          var items = d.items || [];
          DATA.items = items;
          /* 文件清单：条数 = 该文件的逐条记录数（按 src 与文件 meta 对位） */
          DATA.files = (d.files || []).map(function (f) {
            var n = 0;
            items.forEach(function (it) { if (it.src === f.meta) n++; });
            f.count = n;
            return f;
          });
          /* 已经不在列表里的选择项清掉（比如刚删过） */
          var live = {};
          items.forEach(function (it) { live[it.key] = 1; });
          Object.keys(DATA.sel).forEach(function (k) { if (!live[k]) delete DATA.sel[k]; });
          var liveF = {};
          DATA.files.forEach(function (f) { liveF[f.name] = 1; });
          Object.keys(DATA.fsel).forEach(function (k) { if (!liveF[k]) delete DATA.fsel[k]; });
          /* 当前正看着的文件没了 → 退回文件视图；来路文件没了也别留着退 */
          if (DATA.lastFile && !DATA.files.some(function (f) { return f.name === DATA.lastFile.name; })) {
            DATA.lastFile = null;
          }
          if (DATA.file && !DATA.files.some(function (f) { return f.name === DATA.file.name; })) {
            DATA.file = null;
            if (DUI) setView('files');
            else DATA.view = 'files';
          }
          /* 主题筛选已删（用户 2026-10-15）：不再维护主题清单 */
          if (DATA.view === 'files') renderFiles(); else renderRows();
          if (d.dir_exists === false) {
            DUI.status.textContent = '输出目录不存在：' + (d.dir || '—');
            DUI.status.classList.add('a-warn');
          } else {
            DUI.status.textContent = '';
            DUI.status.classList.remove('a-warn', 'a-err');
          }
        });
      }

      function selectedTargets() {
        return Object.keys(DATA.sel).map(function (k) { return DATA.sel[k]; });
      }

      /* 文件视图的导出目标 = 选中文件里的全部逐条数据（沿用逐条 targets 口径） */
      function fileTargets(files) {
        var t = [];
        files.forEach(function (f) {
          DATA.items.forEach(function (it) {
            if (it.src === f.meta) t.push({ src: it.src, line: it.line, key: it.key });
          });
        });
        return t;
      }

      function deleteSelectedFiles() {
        var files = DATA.files.filter(function (f) { return DATA.fsel[f.name]; });
        if (!files.length) return;
        /* 自己的弹窗（用户 2026-10-16：蒸馏页删除确认不用原生框） */
        api.notice(bi('删除所选文件：', 'Delete selected files:'),
          [bi('确定将所选的 ' + files.length + ' 个文件移入回收站',
              'Move the ' + files.length + ' selected ' + enPlural(files.length, 'file', 'files') +
              ' to the Recycle Bin')],
          { okText: '删除', cancelText: '取消', onOk: function () {
            Promise.all(files.map(function (f) {
              var jobs = [api.post('/api/recycle/trash_file', { path: f.path, kind: 'dataset' })];
              if (f.meta_path) jobs.push(api.post('/api/recycle/trash_file', { path: f.meta_path, kind: 'dataset' }));
              return Promise.all(jobs);
            })).then(function () {
              DATA.fsel = {};
              fetchList();
            })['catch'](function (e) {
              dataFail('删除失败：' + ((e && e.message) ? e.message : e));
            });
          } });
      }

      function mergeStamp() {
        var d = new Date();
        function p(n) { return (n < 10 ? '0' : '') + n; }
        return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
      }
      /* 数据集合并（用户 2026-10-15）：选中文件 → 弹命名框 → 合成新数据集（原文件保留） */
      function onMergeSelected() {
        if (DATA.view !== 'files') return;
        var files = DATA.files.filter(function (f) { return DATA.fsel[f.name]; });
        if (files.length < 2) return;
        api.prompt({
          title: '合并数据集',
          label: '名称',
          value: 'merged_' + mergeStamp(),      /* 2026-10-23（用户）：默认名固定英文，不随语言变 */
          okText: '合并',
        }).then(function (name) {
          if (!name) return null;
          return api.post('/api/article/merge', {
            names: files.map(function (f) { return f.name; }),
            name: name,
          }).then(function (res) {
            if (!res || res.ok === false) {
              dataFail('合并失败：' + ((res && res.error) || '未知错误'));
              return;
            }
            DATA.fsel = {};
            fetchList();
          });
        })['catch'](function (e) {
          dataFail('合并失败：' + ((e && e.message) ? e.message : e));
        });
      }
      function onDeleteSelected() {
        if (DATA.view === 'files') { deleteSelectedFiles(); return; }
        var targets = selectedTargets();
        if (!targets.length) return;
        /* 自己的弹窗（用户 2026-10-16：蒸馏页删除确认不用原生框） */
        api.notice(bi('删除所选样本：', 'Delete selected records:'),
          [bi('确定将所选的 ' + targets.length + ' 条样本移入回收站',
              'Move the ' + targets.length + ' selected ' + enPlural(targets.length, 'record', 'records') +
              ' to the Recycle Bin')],
          { okText: '删除', cancelText: '取消', onOk: function () {
            api.post('/api/article/delete', { targets: targets }).then(function (res) {
              if (!res || res.ok === false) {
                dataFail('删除失败：' + ((res && res.error) || '未知错误'));
                return;
              }
              targets.forEach(function (t) { delete DATA.sel[t.key]; });
              fetchList();
            })['catch'](function (e) {
              dataFail('删除失败：' + ((e && e.message) ? e.message : e));
            });
          } });
      }

      function loadData() {
        LOADED.data = true;
        var host = bodies.data;
        host.innerHTML = '';
        var stack = api.el('div', 'stack');

        var card = api.card('');   /* 用户口径：卡片不写「样本列表」标题 */
        var cardHead = card.querySelector('.card-head');
        if (cardHead) cardHead.remove();      /* 空卡头整块拿掉，工具行顶在卡内最上 */
        card.body.style.marginTop = '0px';    /* 按钮+数据区上移 14px（用户 2026-10-21：body 顶距只用于与卡头分隔） */

        var toolbar = api.el('div', 'a-toolbar');
        var status = api.el('p', 'a-status');
        toolbar.appendChild(button('刷新', 'a-btn-ghost', 'refresh', function () { fetchList(); }));
        var delBtn = button('删除所选', 'a-btn-danger', 'trash', onDeleteSelected);
        toolbar.appendChild(delBtn);
        /* 数据管理只导出、不导入；导出只导出选中的数据（用户 2026-10-14）：
           没选中 → 按钮禁用、点了也不触发；选中集随请求带给桥（targets）。 */
        var exportBtn = button('导出', 'a-btn-soft', 'down', function () {
          var targets;
          if (DATA.view === 'files') {
            targets = fileTargets(DATA.files.filter(function (f) { return DATA.fsel[f.name]; }));
          } else {
            targets = selectedTargets();
          }
          if (!targets.length) return;
          api.exportDataset({
            route: '/api/article/export', defaultName: '语料蒸馏数据集', status: status,
            body: { targets: targets },
          });
        });
        exportBtn.disabled = true;
        toolbar.appendChild(exportBtn);
        /* 数据集合并（用户 2026-10-15）：选中 ≥2 个文件合成一个新数据集（原文件保留）；
           逐条视图无文件概念，不足两个选中 → 禁用 */
        var mergeBtn = button('合并', 'a-btn-soft', 'merge', onMergeSelected);
        mergeBtn.disabled = true;
        toolbar.appendChild(mergeBtn);
        /* 文件视图（默认）↔ 逐条数据（双击文件进入）：返回=逐条→数据集（并换到第一条数据集）；
           数据集视图里置灰禁用（用户 2026-10-15） */
        var backBtn = button('返回', 'a-btn-ghost', '', function () {
          if (DATA.view === 'records') setView('files');
        });
        backBtn.hidden = false;
        backBtn.disabled = true;
        toolbar.appendChild(backBtn);

        /* 搜索 + 主题筛选：钉在行尾右侧（用户 2026-10-15） */
        var rightWrap = api.el('span', 'a-row a-right');
        var search = api.el('input', 'a-search');
        search.type = 'text';
        search.placeholder = '搜索';
        search.value = DATA.q;
        search.addEventListener('input', function () {
          DATA.q = search.value.trim();
          renderCur();
        });
        /* 搜索范围四选一（用户 2026-10-15）：搜索栏左边；主题筛选已删（同日「删掉主题筛选功能」） */
        var scopePick = api.pick(api.el('input'),
          [['fname', '文件名'], ['theme', '主题'], ['q', '问题'], ['text', '文本']], 'a-scope');
        scopePick.value = DATA.scope || 'fname';
        scopePick.addEventListener('change', function () {
          DATA.scope = scopePick.value;
          renderCur();
        });
        rightWrap.appendChild(scopePick);
        rightWrap.appendChild(search);
        toolbar.appendChild(rightWrap);
        card.body.appendChild(toolbar);

        /* 文件视图的表（默认显示）：选/文件名/条数/体量/日期 */
        var filesWrap = api.el('div', 'a-tablewrap');
        var ftable = api.el('table', 'a-table');
        var fthead = api.el('thead');
        var fhtr = api.el('tr');
        var fAllBox = api.el('input', '');
        fAllBox.type = 'checkbox';
        var fAllWrap = api.el('label', 'a-check');
        fAllWrap.appendChild(fAllBox);
        var fthAll = api.el('th', 'col-pick');
        fthAll.appendChild(fAllWrap);
        fhtr.appendChild(fthAll);
        [['col-fname', '文件名'], ['col-count', '条数'], ['col-size', '体量'], ['col-date', '日期']]
          .forEach(function (h) {
            var th = api.el('th', h[0], h[1]);
            fhtr.appendChild(th);
          });
        fthead.appendChild(fhtr);
        ftable.appendChild(fthead);
        fAllBox.addEventListener('change', function () {
          shownFiles().forEach(function (f) {
            if (fAllBox.checked) DATA.fsel[f.name] = 1;
            else delete DATA.fsel[f.name];
          });
          renderFiles();
        });
        var ftbody = api.el('tbody');
        ftable.appendChild(ftbody);
        filesWrap.appendChild(ftable);
        card.body.appendChild(filesWrap);

        /* 逐条视图的表（双击文件后显示）：选/ID/标题/主题 */
        var tableWrap = api.el('div', 'a-tablewrap');
        var table = api.el('table', 'a-table');
        var thead = api.el('thead');
        var htr = api.el('tr');
        var allBox = api.el('input', '');
        allBox.type = 'checkbox';
        var allWrap = api.el('label', 'a-check');
        allWrap.appendChild(allBox);
        var thAll = api.el('th', 'col-pick');
        thAll.appendChild(allWrap);
        htr.appendChild(thAll);
        [['col-id', 'ID'], ['col-title', '标题'], ['col-theme', '主题']]
          .forEach(function (h) {
            var th = api.el('th', h[0], h[1]);
            htr.appendChild(th);
          });
        thead.appendChild(htr);
        table.appendChild(thead);
        /* 表头复选 = 全选（只作用于当前筛出来的这些行） */
        allBox.addEventListener('change', function () {
          shownItems().forEach(function (it) {
            if (allBox.checked) DATA.sel[it.key] = { src: it.src, line: it.line, key: it.key };
            else delete DATA.sel[it.key];
          });
          renderRows();
        });
        var tbody = api.el('tbody');
        table.appendChild(tbody);
        tableWrap.appendChild(table);
        card.body.appendChild(tableWrap);
        tableWrap.hidden = true;   /* 默认文件视图；双击文件才显示逐条表 */

        var preview = field(card.body, '', { textarea: true, rows: 8, readonly: true });
        preview.classList.add('a-preview');
        /* 用户口径：不要「预览（点击某行查看）」这类说明小字 → 空标签行整行拿掉 */
        var pLab = preview.parentNode && preview.parentNode.querySelector('.a-flabel');
        if (pLab) pLab.remove();
        /* 表格限高 + 滚动上下渐变（用户 2026-10-21）：数据集/单条数据两套表各包一层，
           下方内容框压到窗口底（与边框留距），超出的行滚动 */
        var dmFitF = api.dmLayout(filesWrap, preview, { key: 'article' });
        var dmFitR = api.dmLayout(tableWrap, preview, { key: 'article' });
        function fitDm() { dmFitF.fit(); dmFitR.fit(); }
        stack.appendChild(card);
        stack.appendChild(status);
        host.appendChild(stack);

        DUI = {
          status: status, tbody: tbody, delBtn: delBtn,
          allBox: allBox, preview: preview, exportBtn: exportBtn, mergeBtn: mergeBtn,
          ftbody: ftbody, fAllBox: fAllBox,
          filesWrap: filesWrap, recWrap: tableWrap, backBtn: backBtn,
          fitDm: fitDm,
        };
        var subEl = card.querySelector('.card-sub');
        if (subEl) subEl.remove();      /* 用户口径：卡片头下那行「共 n 条 · 来源 …」不要 */
        fetchList();
      }

      /* ---------- 默认进入「采集中心」（与 Tk 版一致） ----------
         ?tab=config|data 仅供截图预览直达某标签页，不影响正常使用。 */
      loadConfig();
      loadCollect();
      loadData();
      var previewTab = (new URLSearchParams(location.search).get('tab') || '').trim();
      var initial = (previewTab === 'config' || previewTab === 'data' || previewTab === 'collect')
        ? previewTab                       /* ?tab= 仅供截图预览直达 */
        : (getLastTab() || 'config');           /* 本次启动上次停留的页签；首次进「模型配置」 */
      pickTab(initial);
      return Promise.resolve();
    },

    unmount: function () {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
  });
})();
