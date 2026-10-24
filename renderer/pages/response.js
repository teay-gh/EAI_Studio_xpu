/* ============================================================
   响应蒸馏页（导航键 response）· 渲染层模块
   ------------------------------------------------------------
   迁移自 ENGINE_DIR/pages/distill.py 的 DistillPage(kind="response")。
   信息结构 1:1 复刻：页头「响应蒸馏 / 出题 → 答题 → 审阅」，
   三个分段标签：模型配置 / 采集中心 / 数据管理（默认采集中心）。

   与语料蒸馏页的差异（Tk 版同一 DistillPage 的 kind 分支）：
     · 三张模型卡为 出题 Generator / 答题 Responder / 审阅 Reviewer；
     · 采集参数为 topics / count / concurrency / dataset_name，
       无 min_words / max_words；额外有「微调方式 sft|lora」「自动出题」（模式已删，固定流式）；
     · 输出提示为 sft.txt（\\x01问\\x02答\\x03）；
     · 数据表列为 ID / 问题 / 主题 / 评分，问题按 Tk 版截断到 50 字；
     · 数据管理工具栏无「导出 Markdown」（Tk 版仅 article 才有）。

   数据 / 动作来源：bridge_api/response.py（真后端）
     GET  /api/response/config                   模型卡（真实 config.json）
     GET  /api/response/collection               采集参数 + 输出目录
     GET  /api/response/list                       数据管理：真实样本列表
     GET  /api/response/collect/status?since=N     采集运行态 + 增量事件 + 工程统计
     POST /api/response/collect/start              起真采集线程（body 覆盖表单值）
     POST /api/response/collect/stop               停止采集
     POST /api/response/models/save                保存模型配置（只改 response.<role>）
     POST /api/response/models/test                真发一次最小请求测连通

   采集启动后每 1 秒轮询 status 的 since 游标：log 事件追加到日志区，
   progress 事件刷进度条与统计行；停止按钮只在运行中可用；错误原支照实显示。
   数据管理的导出 / 导入 / 删除 / 清空仍为 not_implemented（置灰）。
   ?mock=1 时仅用本模块 mock 字段做截图预览；?tab=config|data 直达标签页（截图用）。
   ============================================================ */
(function () {
  'use strict';

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
    span.className = 'rs-ic';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      (ICONS[name] || '') + '</svg>';
    return span;
  }

  /* ---------- 采集状态（模块级：mount 用 / unmount 清） ---------- */
  var collect = { timer: null, cursor: 0, running: false, lastStats: null };
  var collectEls = {};

  /* ---------- 三张模型卡（同步骨架用） ----------
     键/标题与 bridge_api/response.py::RESPONSE_MODELS 逐字一致。
     为什么在这里再放一份：进页第一帧就要把三张卡建出来（见 loadConfig）——
     否则会先画一个没有卡片的空壳、等 /api/response/config 回来（实测 24~40ms）才把三张卡插进去，
     那两三帧就是用户看到的「切蒸馏页会闪」。 */
  var CARD_DEFS = [['generator', '出题模型'], ['responder', '答题模型'], ['reviewer', '审阅模型']];

  /* 「填充示例」用的示例档案（用户 2026-10-17）：三张卡各一套能直接跑的值。
     api_key 不在其中 —— 留空即保留原值，绝不往密钥框写东西。 */
  var MODEL_DEFAULTS = null;   /* 后端下发的出厂默认（/api/response/config → defaults），「填充示例」用它 */
  /* 采集主题示例（用户 2026-10-20）：每点一次换一条，不再一次塞进多条 */
  /* 采集主题示例（用户 2026-10-20）：[中文, 英文] 一对，点一次换一条；
     全部是自然科学主题，不涉及任何国家的历史与文化。切界面语言不改已填内容，再点一次才换成对应语言。 */
  var TOPIC_SAMPLES = [
    ['光合作用的原理', 'How photosynthesis works'],
    ['牛顿三大定律', 'Newton\'s three laws of motion'],
    ['为什么天空是蓝的', 'Why the sky is blue'],
    ['疫苗的作用机制', 'How vaccines work'],
    ['宇宙大爆炸的证据', 'Evidence for the Big Bang'],
    ['地球年龄是怎么测出来的', 'How the age of the Earth was measured'],
    ['睡眠对记忆的影响', 'How sleep affects memory'],
    ['咖啡因如何提神', 'How caffeine keeps you awake'],
    ['潮汐是怎么形成的', 'What causes the tides'],
    ['极光是怎么产生的', 'How auroras form'],
    ['板块构造学说', 'The theory of plate tectonics'],
    ['蜜蜂与授粉', 'Bees and pollination'],
    ['太阳系的构成', 'The structure of the solar system'],
    ['元素周期表是怎么排列的', 'How the periodic table is organized'],
    ['闪电是怎么产生的', 'How lightning forms'],
    ['声音是怎么传播的', 'How sound travels'],
    ['光的折射与反射', 'Refraction and reflection of light'],
    ['人为什么会做梦', 'Why we dream'],
    ['抗生素如何杀菌', 'How antibiotics kill bacteria'],
    ['血液循环是怎么运作的', 'How blood circulation works'],
    ['原子的结构', 'The structure of the atom'],
    ['熵是什么', 'What entropy is'],
    ['引力波是怎么探测到的', 'How gravitational waves are detected'],
    ['恐龙灭绝的原因', 'Why the dinosaurs went extinct'],
  ];
  /* 带数字的文案整段匹配不上词典，按当前界面语言直接给（用户 2026-10-20） */
  function bi(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }

  function sampleLangIdx() { return (window.I18N && window.I18N.lang() === 'en') ? 1 : 0; }
  /* 重置用的英文系统提示词（用户 2026-10-20）：英文界面下点「重置」时提示词换成英文；
     数值项两种语言一致，base_url / model / api_key 一律不动。 */
  var PROMPTS_EN = {
    generator: 'You are an English question designer building distillation data for a language model. Questions ' +
      'must be specific, self-contained and sharply focused (concept, mechanism, comparison, cause and effect, ' +
      'or application), of medium-to-hard difficulty. Avoid yes/no questions, avoid open prompts that need ' +
      'follow-up questions, and never use context references such as "according to the passage". Output the ' +
      'question only: no numbering, no quotes, no explanation. All output must be in English.',
    responder: 'You are a rigorous, helpful English assistant. Answer directly: state the conclusion first, then ' +
      'the necessary explanation, steps or examples. Use precise terminology, keep the logic clear, stay on the ' +
      'question, do not restate it and do not open with pleasantries. Output the answer body only. ' +
      'All output must be in English.',
    reviewer: 'You are a strict English QA reviewer. Judge only major errors in the question-answer pair: ' +
      'answers that miss the question, factual mistakes, empty fabrication, clearly harmful content, serious ' +
      'incompleteness or self-contradiction. Style and level of detail do not count. Output only a JSON object, ' +
      'nothing else: {"pass": true or false, "score": integer 0-10, "major_errors": ["each major issue, one ' +
      'line"], "comment": "one-sentence summary"}'
  };

  var topicIdx = 0;


  /* 兜底示例档案：后端没给 defaults 时用 */
  /* 用户 2026-10-20：提示词不跟着切语言换 —— 只有点「重置」才换成当前语言的出厂提示词 */

  var EXAMPLES = {
    generator: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.9, max_tokens: 4096,
      system_prompt: '你是一名严谨的出题专家。请生成一个清晰、可独立回答的问题。问题要具体、有深度，' +
        '但避免过长的铺垫。只输出问题本身，不要解释，不要编号，不要引号。'
    },
    responder: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.7, max_tokens: 8192,
      system_prompt: '你是一个严谨、有帮助的中文助手。请直接回答问题，结构清晰、准确完整，' +
        '但避免冗长，正文尽量控制在 300-600 字。'
    },
    reviewer: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.1, max_tokens: 512,
      system_prompt: '你是一名严格的质检审核员。判断给定问答是否存在重大问题：答非所问、空洞胡编、明显有害等；' +
        '文笔或详略问题不算重大问题。只输出一个 JSON 对象，不要其它文字，格式：' +
        '{"pass":true或false,"score":0到1,"major_errors":["重大问题"],"comment":"一句话总评"}'
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
  var PREFETCH_URLS = ['/api/response/config', '/api/response/collection', '/api/response/list'];
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

  EAIPages.register('response', {
    title: '响应蒸馏',

    mock: {
      '/api/response/config': {
        project: '（预览）<项目目录>\\\\\\\\项目',
        project_source: '（预览）config.app.recent_projects[0]',
        config_path: '（预览）ENGINE_DIR\\\\\\\\config.json',
        config_exists: true,
        models: [
          { key: 'generator', label: '出题模型', tone: 'accent',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.9,
              max_tokens: 4096, system_prompt: '（预览 mock）系统提示词占位。' } },
          { key: 'responder', label: '答题模型', tone: 'ink3',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.7,
              max_tokens: 8192, system_prompt: '（预览 mock）系统提示词占位。' } },
          { key: 'reviewer', label: '审阅模型', tone: 'ink3',
            profile: { base_url: '（预览）https://example.invalid/v1', api_key_set: true,
              api_key_mask: '••••••••', model: '（预览）model', temperature: 0.1,
              max_tokens: 4096, system_prompt: '（预览 mock）系统提示词占位。' } }
        ]
      },
      '/api/response/collection': {
        project: '（预览）<项目目录>\\\\\\\\项目',
        project_source: '（预览）config.app.recent_projects[0]',
        output_dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response',
        output_dir_exists: true,
        config_output_dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response',
        example_topics: '光合作用的原理\n恒星的生命周期\n量子力学入门\n板块构造',
        fields: {
          topics: '（预览）主题 A\n（预览）主题 B', count: 50, concurrency: 2,
          dataset_name: 'response_set', discard_on_reject: true, save_rejected: false,
          mode: 'stream', finetune: 'sft', auto_topic: true
        }
      },
      '/api/response/list': {
        dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response',
        dir_exists: true,
        total: 2,
        sources: ['（预览）mimoZL.jsonl'],
        count_source: '（预览 mock）',
        items: [
          { id: 1,
            question: '（预览）这是一条用于自检表格截断与预览填充的超长问题示例问题示例问题示例',
            question_short: '（预览）这是一条用于自检表格截断与预览填充的超长问题示例问题示例问题示例',
            topic: '（预览）主题', score: 8, chars: 512,
            answer: '（预览 mock）答案占位：此内容仅用于 ?mock=1 截图，非真实样本。' },
          { id: 2, question: '（预览）示例问题二', question_short: '（预览）示例问题二',
            topic: '（预览）主题', score: 7, chars: 340,
            answer: '（预览 mock）答案占位：此内容仅用于 ?mock=1 截图，非真实样本。' }
        ]
      },
      '/api/response/models/save': {
        ok: true, config_path: '（预览）ENGINE_DIR\\\\\\\\config.json',
        saved_roles: ['generator', 'responder', 'reviewer']
      },
      '/api/response/models/test': {
        ok: true, role: 'generator', base_url: '（预览）https://example.invalid/v1',
        model: '（预览）model', reply: '在线（预览 mock）', note: '（预览 mock）真实联网测试成功。'
      },
      '/api/response/collect/start': {
        ok: true, task: 'collect:response',
        info: { count: 1, concurrency: 1, mode: 'stream',
          store_path: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response\\\\\\\\mimoZL_preview.jsonl',
          output_dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response' },
        note: '（预览 mock）已启动真实采集流水线。'
      },
      '/api/response/collect/stop': { ok: true, stopped: true, note: '（预览 mock）已请求停止' },
      '/api/response/collect/status?since=0': {
        ok: true, running: false, exists: false, events: [], next: 0, stats: {},
        project: '（预览）<项目目录>\\\\\\\\项目',
        output_dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response',
        output_dir_exists: true, samples: 2, files: []
      },
      '/api/response/collect/status': {
        ok: true, running: false, exists: false, events: [], next: 0, stats: {},
        project: '（预览）<项目目录>\\\\\\\\项目',
        output_dir: '（预览）<项目目录>\\\\\\\\项目\\\\\\\\data\\\\\\\\response',
        output_dir_exists: true, samples: 2, files: []
      },
      '/api/response/export': ni(),
      '/api/response/import': ni(),
      '/api/response/delete': ni(),
      '/api/response/clear': ni()
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
      /* 查看对话同仓库格式（用户 2026-10-16）：问答卡样式在 repo.css，按需挂一份 */
      if (!document.querySelector('link[data-repo-css]')) {
        var rcss = document.createElement('link');
        rcss.rel = 'stylesheet';
        rcss.href = 'pages/repo.css';
        rcss.setAttribute('data-repo-css', '1');
        document.head.appendChild(rcss);
      }
      var TABS = [['config', '模型配置'], ['collect', '采集中心'], ['data', '数据管理']];
      /* 本次启动内记住用户最后停留的页签。
         挂在 window 上：页面模块可能被重新装载，闭包变量会丢；window 在整个应用进程内稳定。
         不落盘 —— 重启软件后回到「模型配置」。 */
      var LAST_TAB_KEY = 'eaiLastTab:response';
      function getLastTab() { return (window.__eaiLastTab || {})[LAST_TAB_KEY] || ''; }
      function setLastTab(k) { (window.__eaiLastTab = window.__eaiLastTab || {})[LAST_TAB_KEY] = k; }

      /* ---------- 通用小工具 ---------- */
      function button(label, variant, iconName, onClick, muted) {
        var b = api.el('button', 'btn rs-btn ' + variant);
        b.type = 'button';
        if (iconName) b.appendChild(iconEl(iconName));
        b.appendChild(api.el('span', 'rs-btn-label', label));
        if (muted) b.classList.add('rs-btn-muted');
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
        el.classList.toggle('rs-warn', !!(res && (res.not_implemented || res.error)));
        el.classList.toggle('rs-err', !!(res && res.error));
      }
      function act(path, body, statusEl, label) {
        statusEl.textContent = label + ' …';
        api.post(path, body).then(function (res) { showStatus(statusEl, res, label); });
      }
      function field(parent, label, opts) {
        opts = opts || {};
        var box = api.el('div', 'rs-field' + (opts.span2 ? ' rs-span2' : ''));
        var lab = api.el('div', 'rs-flabel', label);
        if (opts.labelTitle) lab.title = String(opts.labelTitle);
        box.appendChild(lab);
        var inp;
        if (opts.select) {
          /* 自绘下拉：原生 <select> 的弹层是系统绘制的，选中项方角高亮会盖住弹层圆角 */
          var holder = api.el('input', 'rs-input');
          holder.type = 'text';
          holder.readOnly = true;
          holder.value = opts.value == null
            ? String(((opts.options || [])[0] || [''])[0])
            : String(opts.value);
          inp = api.pick(holder, opts.options, 'rs-input');
        } else {
          inp = api.el(opts.textarea ? 'textarea' : 'input',
                       'rs-input' + (opts.mono ? ' rs-mono' : '') + (opts.textarea ? ' rs-textarea' : ''));
          if (opts.textarea) { inp.rows = opts.rows || 4; } else { inp.type = 'text'; }
          inp.value = opts.value == null ? '' : String(opts.value);
        }
        if (opts.readonly) inp.readOnly = true;
        if (opts.secret != null) { api.secretWire(inp); api.secretLock(inp, !!opts.secret); }
        else if (opts.placeholder != null) inp.placeholder = String(opts.placeholder);
        box.appendChild(inp);
        if (opts.note) box.appendChild(api.el('div', 'rs-note', opts.note));
        parent.appendChild(box);
        return inp;
      }
      function checkBox(parent, label, checked) {
        var wrap = api.el('label', 'rs-check rs-row');
        var inp = api.el('input', '');
        inp.type = 'checkbox';
        inp.checked = !!checked;
        wrap.appendChild(inp);
        wrap.appendChild(api.el('span', 'rs-check-label', label));
        parent.appendChild(wrap);
        return { wrap: wrap, input: inp };
      }
      function cardError(title, message) {
        var c = api.card(title, '读取失败');
        c.body.appendChild(api.el('p', 'rs-status rs-err', message || '未知错误'));
        return c;
      }

      /* ---------- 页头 + 分段标签 + 面板容器 ---------- */
      root.appendChild(api.hero('响应蒸馏'));

      var segWrap = api.el('div', 'rs-segwrap');
      var seg = api.el('div', 'seg rs-seg');
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

      var panels = api.el('div', 'rs-panels');
      root.appendChild(panels);
      var bodies = {};
      TABS.forEach(function (t) {
        var p = api.el('section', 'rs-panel');
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
      var modelInputs = {};
      var toneBars = {};        // key -> 卡头分隔条（状态条）
      var LOADED = {};          // 本轮 mount 里哪些面板已经取过数（挡 pickTab 的重复请求）

      function collectProfiles() {
        var body = {};
        Object.keys(modelInputs).forEach(function (key) {
          var ins = modelInputs[key];
          var p = {
            base_url: ins.base_url.value,
            model: ins.model.value,
            temperature: ins.temperature.value,
            max_tokens: ins.max_tokens.value,
            system_prompt: ins.system_prompt.value
          };
          var ak = api.secretValue(ins.api_key);   /* 锁定态（点数）算空 → 不下发 → 保留原值 */
          if (ak) p.api_key = ak;
          body[key] = p;
        });
        return body;
      }

      /* 卡头分隔条 = 该卡保存状态（用户 2026-10-14）：紫 = 已保存，灰 = 有修改未保存 */
      function setTone(key, saved) {
        var b = toneBars[key];
        if (b) b.classList.toggle('rs-tone-accent', !!saved);
      }

      function testModel(key, statusEl) {
        var ins = modelInputs[key];
        var base = ins.base_url.value;
        var ak = api.secretValue(ins.api_key);
        api.dotSet(statusEl, 'busy', '[' + key + '] 正在测试连通并读取模型列表 …');
        /* 第一跳：列模型（快、不耗 token）——成功就顺手把 model 变成下拉框 */
        api.post('/api/settings/models', { base_url: base, api_key: ak }).then(function (lst) {
          var ids = (lst && lst.ok && lst.models) || [];
          if (ids.length) {
            ins.model = api.modelSelect(ins.model, ids, 'rs-input');   /* 字体与 base_url 一致（不用等宽） */
            api.dotSet(statusEl, 'ok', '[' + key + '] 连通成功 · ' + ids.length +
              ' 个模型可选（' + (lst.ms || 0) + 'ms）→ model 已变成下拉框');
            return;
          }
          /* 密钥问题(401/403)、或压根连不上（没有 http 状态）→ 跳过 chat 兜底，别让用户干等 */
          var http = lst && lst.http;
          if (!http || http === 401 || http === 403) {
            api.dotSet(statusEl, 'err', '[' + key + '] 连通失败：' +
              (http ? 'HTTP ' + http + '（密钥无效或已过期，或与本卡地址不匹配）'
                    : String((lst && lst.error) || '连不上（地址/网络）')) +
              ' · 已跳过慢速兜底测试');
            return;
          }
          /* 第二跳：列不出来 → 退回最小 chat 请求，model 保持手填 */
          var prof = { base_url: base, model: ins.model.value };
          if (ak) prof.api_key = ak;
          api.post('/api/response/models/test', { role: key, profile: prof }).then(function (res) {
            if (!res || res.ok === false || res.error) {
              var why = (lst && (lst.error || lst.note)) ||
                (res && res.error) || '无响应';
              api.dotSet(statusEl, 'err', '[' + key + '] 连通失败：' + why +
                (/40[13]/.test(why) ? '（密钥无效或已过期，或与本卡地址不匹配）' : '') + ' · model 保持手填');
              return;
            }
            api.dotSet(statusEl, 'ok', '[' + key + '] 连通成功 · ' + (res.model || '') +
              ' → ' + (res.reply || '') + '；该服务不支持 /models，model 仍需手填');
          });
        });
      }

      /* 数据到了之后**就地填值**（不重建卡片 → 不闪屏）。只写 .value / 密钥锁态 / 色条，尺寸不变。 */

      function fillCard(key, m) {
        var ins = modelInputs[key];
        if (!ins) return;
        var prof = m.profile || {};
        ['base_url', 'model', 'temperature', 'max_tokens', 'system_prompt'].forEach(function (f) {
          var v = prof[f];
          ins[f].value = v == null ? '' : String(v);
        });
        api.secretLock(ins.api_key, !!prof.api_key_set);
        /* 色条 = 保存状态（用户 2026-10-14）：值从配置读来 = 已保存 → 紫 */
        setTone(key, true);
      }

      function loadConfig() {
        LOADED.config = true;
        var host = bodies.config;
        host.innerHTML = '';
        modelInputs = {};
        toneBars = {};
        var stack = api.el('div', 'stack');
        var grid = api.el('div', 'rs-grid3');
        var status = api.el('p', 'rs-status');

        stack.appendChild(grid);

        function modelCard(m) {
          var card = api.card(m.label, null);
          card.classList.add('rs-modelcard');
          /* 分隔条 = 该卡保存状态（用户 2026-10-14）：紫 = 已保存，灰 = 有修改未保存，
             点「保存配置」成功 → 变紫。连通态只看「测试」按钮旁的状态点。 */
          var bar = api.el('div', 'rs-tonebar');
          toneBars[m.key] = bar;
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
          ins.system_prompt.parentElement.classList.add('rs-field-prompt');
          setTone(m.key, true);   /* 刚建/刚读进来的值 = 已保存 */
          /* 任何字段被改动（input/change 冒泡，替换过的下拉框也盖得到）→ 该卡变灰 */
          card.addEventListener('input', function () { setTone(m.key, false); });
          card.addEventListener('change', function () { setTone(m.key, false); });
          var dot = api.dot('idle', '尚未测试');
          /* 连通性测试按钮放进 api_key 右侧的新一行（label 留在行外面，仍是上下排列）*/
          var keyRow = api.el('div', 'rs-keyrow');
          ins.api_key.parentElement.insertBefore(keyRow, ins.api_key);
          keyRow.appendChild(ins.api_key);
          keyRow.appendChild(dot);
          keyRow.appendChild(button('测试', 'rs-btn-soft', '', function () {
            testModel(m.key, dot);
          }));
          modelInputs[m.key] = ins;
          return card;
        }

        var bar = api.el('div', 'rs-row rs-bar');
        bar.appendChild(button('保存配置', 'rs-btn-accent', 'save', function () {
          status.classList.remove('rs-warn', 'rs-err');
          status.textContent = '正在保存模型配置 …';
          api.post('/api/response/models/save', collectProfiles()).then(function (res) {
            if (!res || res.ok === false || res.error) {
              status.classList.add('rs-err');
              status.textContent = '保存失败：' + ((res && (res.error || res.reason)) || '未知错误');
              return;
            }
            status.textContent = '';        /* 保存成功不弹提示（用户口径：只在失败时写状态行） */
            /* 不再 loadConfig()：整块重建会闪屏。就地刷新即可（把刚提交的密钥框锁回点数态）。 */
            Object.keys(modelInputs).forEach(function (k) {
              var ins = modelInputs[k];
              if (!ins || !ins.api_key) return;
              var hadKey = ins.api_key.dataset.locked === '1' || !!String(ins.api_key.value || '').trim();
              api.secretLock(ins.api_key, hadKey);
            });
            /* 保存成功 → 色条全部变紫（保存状态：已保存） */
            Object.keys(toneBars).forEach(function (k) { setTone(k, true); });
          });
        }));
        /* 重置（用户 2026-10-20：原「填充示例」改成重置）：只恢复 temperature / max_tokens /
           系统提示词，模型配置（base_url / model / api_key）一律不动。 */
        bar.appendChild(button('重置', 'rs-btn-ghost', 'refresh', function () {
          var SRC = (MODEL_DEFAULTS && Object.keys(MODEL_DEFAULTS).length) ? MODEL_DEFAULTS : EXAMPLES;
          Object.keys(SRC).forEach(function (k) {
            var ins = modelInputs[k];
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
            setTone(k, false);
          });
          status.textContent = '';
        }));
        bar.appendChild(status);        /* 状态行并入按钮行：页脚恒一行高，两页签按钮同高 */
        stack.appendChild(bar);
        host.appendChild(stack);

        /* 先把三张卡**同步**建好（空值、紫条=已保存）——进页第一帧就是完整版面；
           具体数值等 /api/response/config 回来再就地填（fillCard：只改 .value，不改尺寸）。 */
        CARD_DEFS.forEach(function (d) {
          grid.appendChild(modelCard({ key: d[0], label: d[1], profile: {} }));
        });

        status.textContent = '正在读取 config.json …';
        cget(api, '/api/response/config').then(function (d) {
          if (d && d.error) { grid.appendChild(cardError('模型配置', d.error)); status.textContent = ''; return; }
          MODEL_DEFAULTS = (d && d.defaults) || null;
          (d.models || []).forEach(function (m) {
            if (modelInputs[m.key]) fillCard(m.key, m);
            else grid.appendChild(modelCard(m));   /* 后端多出来的角色才补卡（正常不会发生） */
          });
          /* 工程/配置路径不再写在页面上（要看的悬停 title）；只有配置真缺失时才报一句 */
          status.textContent = d.config_exists ? '' : '配置文件缺失：' + (d.config_path || 'config.json');
          status.title = '当前工程：' + (d.project || '—') +
            '（来源：' + (d.project_source || '—') + '） · 配置：' + (d.config_path || '—');
        });
      }

      /* ==================== 采集中心 ==================== */
      /* 运行日志：只留最近 50 条（用户 2026-10-19），并且整批一次渲染。
         原来每行都读 scrollHeight / 写 scrollTop（强制同步布局），条目一多就是 O(n²) → 页面卡。 */
      var RS_LOG_MAX = 50;
      var logPending = [];
      var logFlushTimer = null;

      function logLine(text, cls) {
        if (text == null || text === '') return;
        logPending.push({ t: String(text), c: cls || '' });
        if (!logFlushTimer) { logFlushTimer = setTimeout(flushLog, 0); }   /* 同一拍内的行合并成一次 DOM 操作 */
      }

      function flushLog() {
        logFlushTimer = null;
        var el = collectEls.log;
        var items = logPending;
        logPending = [];
        if (!el || !items.length) return;
        /* 本来贴在底部才跟随滚动；用户用滚轮翻上去看时不被新行拽回底部 */
        var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
        var frag = document.createDocumentFragment();
        items.forEach(function (p) {
          frag.appendChild(api.el('span', 'rs-log-line' + (p.c ? ' ' + p.c : ''), p.t));
        });
        el.appendChild(frag);
        while (el.childElementCount > RS_LOG_MAX) { el.removeChild(el.firstElementChild); }
        if (atBottom) el.scrollTop = el.scrollHeight;
      }

      function setRunningUI(running) {
        collect.running = !!running;
        if (collectEls.start) collectEls.start.disabled = collect.running;
        if (collectEls.stop) collectEls.stop.disabled = !collect.running;
      }

      function updateProgress(s) {
        if (!s) return;
        var total = Math.max(1, Number(s.total) || 0);
        var done = Number(s.done) || 0;
        var disc = Number(s.discarded) || 0;
        var fail = Number(s.failed) || 0;
        var inflight = Number(s.inflight) || 0;
        if (collectEls.fill) {
          collectEls.fill.style.width = Math.max(0, Math.min(1, done / total)) * 100 + '%';
        }
        if (collectEls.plabel) {
          collectEls.plabel.textContent = '完成 ' + done + '/' + total +
            ' · 丢弃 ' + disc + ' · 失败 ' + fail + ' · 进行中 ' + inflight +
            (s.dup_rate == null ? '' : ' · 重复率 ' + Math.round(Number(s.dup_rate) * 100) + '%');
        }
      }

      function handleEvent(e) {
        var k = e.kind, p = e.payload || {};
        if (k === 'log') {
          logLine(p.text || '');
        } else if (k === 'progress') {
          collect.lastStats = p;
          updateProgress(p);
        } else if (k === 'record') {
          var r = p.record || {};
          logLine('入库: ' + String(r.question || '').slice(0, 48));
        } else if (k === 'started') {
          logLine('采集开始：共 ' + p.total + ' 条（模式 ' + p.mode + '）');
        } else if (k === 'dup_warn') {
          api.alertBox({ title: '重复率警告',
            message: '当前重复率已达 ' + Math.round(Number(p.rate || 0) * 100) + '%（≥20%），收集到的内容重复偏高。' });
        } else if (k === 'dup_abort') {
          api.alertBox({ title: '重复率过高，已中止',
            message: '当前重复率已达 ' + Math.round(Number(p.rate || 0) * 100) + '%（≥40%），采集已中止，已收集的内容保留。' });
        } else if (k === 'finished') {
          var tot = Number((collect.lastStats && collect.lastStats.total) || p.total || 0);
          logLine('采集结束：入库 ' + (p.done || 0) + (tot ? '/' + tot : '') +
            ' · 丢弃 ' + (p.discarded || 0) + ' · 失败 ' + (p.failed || 0) + ' · 用时 ' +
            (p.elapsed != null ? Number(p.elapsed).toFixed(1) + 's' : '—'));
        } else if (k === 'error') {
          logLine('[错误] ' + (p.text || p.message || 'error'), 'warn');
        } else if (k === 'done') {
          logLine('采集任务结束。');
        } else {
          logLine('[' + k + '] ' + JSON.stringify(p));
        }
      }

      /* 用户 2026-10-19：数据量大时 1 秒一拍偶尔回不来，不能并发出第二拍——
         否则后回来的旧响应会把游标往回拖，日志/进度看着「不更新」。这里改成排队重入。 */
      function pollOnce() {
        if (collect.polling) { collect.again = true; return collect.pollPromise || Promise.resolve(); }
        collect.polling = true;
        collect.pollPromise = api.get('/api/response/collect/status?since=' + collect.cursor).then(function (d) {
          if (!d || d.error) { logLine('[状态] ' + ((d && d.error) || '未知错误'), 'warn'); return; }
          if (typeof d.next === 'number' && d.next < collect.cursor) {
            /* 服务端缓冲被重置（新开一轮 / 桥重启）：游标归零，清掉旧日志重新跟 */
            collect.cursor = 0;
            logPending = [];
            if (collectEls.log) collectEls.log.textContent = '';
            collect.again = true;
            return;
          }
          (d.events || []).forEach(handleEvent);
          if (typeof d.next === 'number') collect.cursor = d.next;
          if (d.stats && (d.stats.total || d.stats.done || d.stats.finished)) collect.lastStats = d.stats;
          updateProgress(collect.lastStats);
          if (d.running) { setRunningUI(true); checkDiscardGuard(); } else { finishRun(d); }
        })['catch'](function () {}).then(function () {
          collect.polling = false;
          collect.pollPromise = null;
          if (collect.again) { collect.again = false; pollOnce(); }
        });
        return collect.pollPromise;
      }

      /* ---------- 丢弃率保护（用户 2026-10-19） ----------
         进度到 30% 之后丢弃率达到 20% → 弹窗警告并停止蒸馏；30% 之前样本太少不判。
         判据做成纯函数，方便单测：进度按「已处理/计划」（done+discarded），丢弃率按
         「丢弃/已处理」，失败条不算进分母（失败本身由失败率管）。 */
      var RS_DISCARD_MIN_PROGRESS = 0.30;
      var RS_DISCARD_MAX_RATE = 0.20;
      var RS_DISCARD_MIN_SAMPLE = 5;      /* 至少 5 条再判，避免开头一两条就误伤 */

      function discardGuard(s) {
        if (!s) return null;
        var total = Number(s.total) || 0;
        var done = Number(s.done) || 0;
        var disc = Number(s.discarded) || 0;
        var processed = done + disc;
        if (!total || processed < RS_DISCARD_MIN_SAMPLE) return null;
        var progress = processed / total;
        if (progress < RS_DISCARD_MIN_PROGRESS) return null;
        var rate = disc / processed;
        if (rate < RS_DISCARD_MAX_RATE) return null;
        return { total: total, done: done, discarded: disc, processed: processed, progress: progress, rate: rate };
      }

      var guardDlg = { mask: null, sub: null, ok: null };
      function ensureGuardDialog() {
        if (guardDlg.mask && guardDlg.mask.isConnected) return;
        var mask = api.el('div', 'pc-mask');
        var card = api.el('div', 'pc-card');
        var head = api.el('div', 'pc-head');
        head.appendChild(api.el('span', 'pc-title', '丢弃率过高'));
        var x = api.el('button', 'pc-x', '');
        x.type = 'button';
        x.setAttribute('aria-label', '关闭');
        x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
          + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
        head.appendChild(x);
        var sub = api.el('p', 'pc-sub', '');
        var list = api.el('div', 'pc-list');
        var btns = api.el('div', 'pc-btns');
        var ok = api.el('button', 'btn pc-ok', '知道了');
        ok.type = 'button';
        btns.appendChild(ok);
        card.appendChild(head);
        card.appendChild(sub);
        card.appendChild(list);
        card.appendChild(btns);
        mask.appendChild(card);
        document.body.appendChild(mask);
        guardDlg.mask = mask; guardDlg.sub = sub; guardDlg.list = list; guardDlg.ok = ok;
        function close() { mask.classList.remove('on'); }
        x.addEventListener('click', close);
        ok.addEventListener('click', close);
        mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
      }

      function showDiscardDialog(info, stopped) {
        ensureGuardDialog();
        var pct = function (v) { return (v * 100).toFixed(1) + '%'; };
        guardDlg.sub.textContent = '已处理 ' + info.processed + '/' + info.total + ' 条（进度 ' + pct(info.progress) +
          '），丢弃 ' + info.discarded + ' 条，丢弃率 ' + pct(info.rate) + '。' +
          (stopped ? '本次蒸馏已自动停止。' : '');
        guardDlg.list.innerHTML = '';
        [['进度门槛', '30% 之前样本太少不判，30% 之后才看丢弃率'],
         ['丢弃率阈值', '达到 20% 即停止，避免继续烧接口'],
         ['常见原因', '审阅模型判太严、主题太偏、答题质量差']].forEach(function (kv) {
          var row = api.el('div', 'pc-item pc-item-warn');
          var top = api.el('div', 'pc-item-top');
          top.appendChild(api.el('span', 'pc-field', kv[0]));
          row.appendChild(top);
          row.appendChild(api.el('div', 'pc-msg', kv[1]));
          guardDlg.list.appendChild(row);
        });
        guardDlg.mask.classList.add('on');
      }

      function checkDiscardGuard() {
        if (!collect.running || collect.guarded) return;
        var info = discardGuard(collect.lastStats);
        if (!info) return;
        collect.guarded = true;
        logLine('[警告] 丢弃率 ' + (info.rate * 100).toFixed(1) + '%（丢弃 ' + info.discarded +
          ' / 已处理 ' + info.processed + '，进度 ' + (info.progress * 100).toFixed(1) + '%），已自动停止蒸馏。', 'warn');
        api.post('/api/response/collect/stop', {}).then(function () {}, function () {});
        showDiscardDialog(info, true);
      }

      /* 调试/探针口径（同 window.__EAI_LABEL 先例）：外部可验判据与弹窗 */
      window.__RS_GUARD = {
        check: discardGuard, show: showDiscardDialog,
        /* 直接喂一份统计走完整条链路（判据 → 写警告行 → 请求停止 → 弹窗），用于验证 */
        simulate: function (s) {
          collect.running = true; collect.lastStats = s; collect.guarded = false;
          checkDiscardGuard();
          return collect.guarded;
        }
      };

      function finishRun(d) {
        if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
        var wasRunning = collect.running;
        setRunningUI(false);
        if (!wasRunning) return;
        if (d && d.result) {
          logLine('本批结果：入库 ' + d.result.done + ' · 丢弃 ' + d.result.discarded +
            ' · 失败 ' + d.result.failed);
        }
        if (d) updateProgress(d.stats || collect.lastStats);
        if (collectEls.status) {
          collectEls.status.textContent = '';   /* 用户口径：按钮下方不留文字提示 */
          collectEls.status.classList.remove('rs-warn', 'rs-err');
        }
      }

      function startPolling() {
        if (collect.timer) clearInterval(collect.timer);
        collect.timer = setInterval(pollOnce, 1000);
      }

      function loadCollect() {
        LOADED.collect = true;
        var host = bodies.collect;
        host.innerHTML = '';
        if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
        collect.cursor = 0;
        collect.running = false;
        collect.lastStats = null;
        collect.polling = false;
        collect.again = false;
        collect.pollPromise = null;
        collect.guarded = false;
        collectEls = {};
        logPending = [];
        if (logFlushTimer) { clearTimeout(logFlushTimer); logFlushTimer = null; }
        /* 两栏：左 = 设置（采集参数 + 操作按钮），右 = 进度 + 运行日志（整页一屏，不出滚动条） */
        var stack = api.el('div', 'rs-collect');
        var colL = api.el('div', 'rs-collect-col');
        var colR = api.el('div', 'rs-collect-col');

        var paramCard = api.card('采集参数');
        var form = api.el('div', 'rs-grid2');
        /* 用户 2026-10-14：标签去掉「（每行一个）」；不填主题 = 随机主题（自动出题），
           提示只在框内无内容时出现（占位符），字体与其他文字有差别（斜体、小一号、灰） */
        var topicsEl = field(form, '主题', {
          textarea: true, rows: 5, span2: true,
          placeholder: '每行一个，\n不填入表示随机主题。'
        });
        var fCount = field(form, '采集数量', { value: '50' });
        var fConc = field(form, '并发数', { value: '2' });
        fConc.min = '1'; fConc.max = '10';   /* 并发上限 10（用户 2026-10-14） */
        fConc.addEventListener('change', function () {
          fConc.value = String(Math.min(10, Math.max(1, parseInt(fConc.value, 10) || 2)));
        });
        /* 字数区间（用户 2026-10-19：和语料蒸馏一样在采集中心调，管的是「答案」字数） */
        var fMin = field(form, '最少字数', { value: '20' });
        var fMax = field(form, '最多字数', { value: '200' });
        var fName = field(form, '数据集名称', { value: 'response_set' });
        /* 原「模式」位置（同一行第二列）：删掉模式选择（固定流式），
           换成选择这条数据用于指令微调还是 LoRA 微调（用户 2026-10-14） */
        var finetuneWrap = field(form, '微调方式', {
          select: true,
          options: [['sft', '指令微调'], ['lora', 'LoRA 微调']]
        });
        var outEl = field(form, '输出目录', {
          span2: true, readonly: true,
        });
        paramCard.body.appendChild(form);

        var checks = api.el('div', 'rs-row rs-checks');
        /* 被拒文本：开关。开 = 保存并隔离被拒文本（逐条留档），关 = 丢弃（不进数据集也不留档） */
        var rejRow = api.el('div', 'rs-check rs-row');
        rejRow.appendChild(api.el('span', 'rs-check-label', '保存并隔离被拒文本'));
        var rejSwitch = api.toggle(false);
        rejRow.appendChild(rejSwitch.el);
        checks.appendChild(rejRow);
        /* 「自动出题」不再做成勾选框：主题框不填 = 自动出题（模型自选方向），填了 = 严格围绕主题。
           见 payload 里的 auto_topic 推导。 */
        paramCard.body.appendChild(checks);
        colL.appendChild(paramCard);

        var logEl = api.el('div', 'rs-log');
        var status = api.el('p', 'rs-status');


        function gatherFields() {
          return {
            topics: topicsEl.value,
            count: fCount.value,
            concurrency: Math.min(10, Math.max(1, parseInt(fConc.value, 10) || 2)),
            min_words: fMin.value,
            max_words: fMax.value,
            dataset_name: fName.value,
            discard_on_reject: true,   /* 与滑块同义：不通过一律不进主数据集（隔离=另存留档 / 丢弃=直接丢） */
            save_rejected: !!rejSwitch.value,
            mode: 'stream',            /* 「模式」已删，固定流式（用户 2026-10-14） */
            finetune: finetuneWrap.value,
            /* 不填主题 = 自动出题（引擎侧会放宽为「主题不限，自行选择方向」）；
               填了主题 = 严格围绕主题（引擎侧若无主题会跳过该条，所以这样推导不会空跑）。 */
            auto_topic: !String(topicsEl.value || '').trim(),
            lang: (window.I18N && window.I18N.lang()) || 'zh'   /* 蒸馏数据语言跟界面（用户 2026-10-20） */
          };
        }

        /* 用户 2026-10-19：切页回来已填的东西要还在 —— 输入即存到本工程 ui_state（400ms 防抖） */
        var formSaveTimer = null;
        function saveFormSoon() {
          if (formSaveTimer) clearTimeout(formSaveTimer);
          formSaveTimer = setTimeout(function () {
            formSaveTimer = null;
            api.post('/api/response/collection', gatherFields()).then(function () {}, function () {});
          }, 400);
        }
        [topicsEl, fCount, fConc, fMin, fMax, fName].forEach(function (el) {
          el.addEventListener('input', saveFormSoon);
          el.addEventListener('change', saveFormSoon);
        });
        finetuneWrap.addEventListener('change', saveFormSoon);
        rejSwitch.el.addEventListener('click', saveFormSoon);
        rejSwitch.el.addEventListener('change', saveFormSoon);

        function onStart() {
          if (collect.running) return;
          logLine('正在启动采集 …');
          status.textContent = '';        /* 用户口径：按钮下方不留文字提示 */
          status.classList.remove('rs-warn', 'rs-err');
          collectEls.start.disabled = true;
          collect.cursor = 0;
          collect.guarded = false;
          api.post('/api/response/collect/start', gatherFields()).then(function (res) {
            if (!res || res.ok === false || res.error) {
              var msg = (res && (res.error || res.reason)) || '未知错误';
              status.classList.add('rs-err');
              status.textContent = '启动失败：' + msg;
              logLine('[错误] 启动失败：' + msg, 'warn');
              setRunningUI(false);
              return;
            }
            var inf = res.info || {};
            status.textContent = '';      /* 用户口径：按钮下方不留文字提示 */
            logLine('已启动：' + (res.task || 'collect:response') +
              (inf.count != null ? (' · ' + inf.count + ' 条 · 落盘 ' + (inf.store_path || '')) : ''));
            collect.lastStats = { total: inf.count, done: 0, discarded: 0, failed: 0, inflight: 0 };
            updateProgress(collect.lastStats);
            setRunningUI(true);
            startPolling();
            pollOnce();
          });
        }

        function onStop() {
          collectEls.stop.disabled = true;
          api.post('/api/response/collect/stop', {}).then(function (res) {
            logLine((res && res.note) ? ('停止请求：' + res.note) : '已请求停止。');
            if (!collect.timer) pollOnce();
          });
        }

        function onRefresh(btn) {
          if (btn) btn.disabled = true;
          if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
          var p = pollOnce();
          Promise.resolve(p).then(function () {
            if (collect.running) startPolling();     /* 重置节奏，别等上一拍 */
            if (btn) btn.disabled = false;
          }, function () { if (btn) btn.disabled = false; });
        }

        var actions = api.el('div', 'rs-row rs-actions');
        var startBtn = button('开始采集', 'rs-btn-accent', 'play', onStart);
        var stopBtn = button('停止', 'rs-btn-danger', 'stop', onStop);
        stopBtn.disabled = true;
        actions.appendChild(startBtn);
        actions.appendChild(stopBtn);
        /* 用户 2026-10-19：放在「停止」和「填充示例主题」之间；立刻重拉日志与进度，不等下一拍 */
        var refreshBtn = button('刷新', 'rs-btn-ghost', 'refresh', onRefresh);
        actions.appendChild(refreshBtn);
        actions.appendChild(button('填充示例主题', 'rs-btn-ghost', 'sample', function () {
          /* 每点一次换下一个主题（用户 2026-10-20）：以前是一次把后端给的 4 条全塞进来 */
          topicsEl.value = TOPIC_SAMPLES[topicIdx % TOPIC_SAMPLES.length][sampleLangIdx()];
          topicIdx += 1;
          status.textContent = '';        /* 用户口径：按钮下方不留文字提示 */
          status.classList.remove('rs-warn', 'rs-err');
        }));
        /* 按钮行 + 状态行是页脚：不再放在左栏卡片下，改由 CSS margin-top:auto 压到页签底部 */


        var progCard = api.card('进度');
        var track = api.el('div', 'rs-progress');
        var fill = api.el('div', 'rs-progress-fill');
        track.appendChild(fill);
        progCard.body.appendChild(track);
        var plabel = api.el('div', 'rs-progress-label', '尚未开始');
        progCard.body.appendChild(plabel);
        colR.appendChild(progCard);

        var logCard = api.card('运行日志');
        logCard.body.appendChild(logEl);
        colR.appendChild(logCard);

        stack.appendChild(colL);
        stack.appendChild(colR);
        host.appendChild(stack);
        actions.appendChild(status);    /* 状态行并入按钮行：页脚恒一行高，两页签按钮同高 */
        host.appendChild(actions);

        collectEls = { start: startBtn, stop: stopBtn, fill: fill, plabel: plabel,
                       log: logEl, status: status };
        setRunningUI(false);

        cget(api, '/api/response/collection').then(function (d) {
          if (d && d.error) {
            status.textContent = '采集参数读取失败：' + d.error;
            status.classList.add('rs-err');
            return;
          }
          var f = d.fields || {};
          topicsEl.value = f.topics == null ? '' : String(f.topics);
          fCount.value = f.count == null ? '' : String(f.count);
          fConc.value = f.concurrency == null ? '' : String(f.concurrency);
          fMin.value = f.min_words == null ? '' : String(f.min_words);
          fMax.value = f.max_words == null ? '' : String(f.max_words);
          fName.value = f.dataset_name == null ? '' : String(f.dataset_name);
          rejSwitch.set(!!f.save_rejected);
          finetuneWrap.value = (f.finetune === 'lora') ? 'lora' : 'sft';
          outEl.value = d.output_dir || '';
          /* 主题示例已改成前端本地列表（每次点换一条，用户 2026-10-20），不再取后端那组 */
          /* 用户要求：不显示「输出目录存在」这类正常态提示；这行只留给失败/异常写 */
          status.textContent = '';
          /* 若已有采集在跑（如页面刷新），自动续上轮询 */
          pollOnce().then(function () {
            if (collect.running && !collect.timer) collect.timer = setInterval(pollOnce, 1000);
          });
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
        DUI.status.classList.add('rs-err');
      }

      function dataOK() {
        if (!DUI) return;
        DUI.status.textContent = '';
        DUI.status.classList.remove('rs-err', 'rs-warn');
      }

      function dataHit(it) {
        var q = DATA.q.toLowerCase();
        if (!q) return true;
        /* 搜索范围四选一（用户 2026-10-15）：文件名/主题/问题/文本 */
        var s2 = DATA.scope || 'fname';
        var txt;
        if (s2 === 'theme') txt = it.topic || '';
        else if (s2 === 'q') txt = it.question || '';
        else if (s2 === 'text') txt = it.answer || '';
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

      /* 预览写入带序号：文件全文是异步取的，双击进逐条的「首条数据」会被晚到的整文件覆盖。
         渲染走共享问答卡（用户 2026-10-16：查看对话同仓库格式）；非 JSON 原文兜底 */
      function setPreview(text) {
        if (!DUI) return;
        DUI.pvSeq = (DUI.pvSeq || 0) + 1;
        var box = DUI.preview;
        box.innerHTML = '';
        var s = String(text == null ? '' : text);
        var parsed = api.datasetRecCards(s, box);
        /* 截断留下的半截尾行（lost<=1）不影响按条目排版（用户 2026-10-20） */
        if (!(parsed.recs.length && (parsed.allJson || parsed.lost <= 1))) {
          box.appendChild(api.el('pre', 'rp-ds-pre', s));
        }
      }
      function setPreviewQA(it) {
        setPreview(JSON.stringify({ question: (it && it.question) || '', answer: (it && it.answer) || '' }) + '\n');
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
        renderCur();
        if (view === 'files') {
          /* 返回数据集：内容自动换到第一条数据集（用户 2026-10-15），和进逐条视图换第一条同口径 */
          var f0 = shownFiles()[0] || DATA.files[0];
          if (f0) { DATA.fileOn = f0.name; showFileText(f0); highlightFileRow(f0.name); }
        }
        /* 进逐条视图：下方自动换到第一条数据（用户 2026-10-14），不用再点一下 */
        if (view === 'records') {
          var first = shownItems()[0] || scopedItems()[0];
          if (first) setPreviewQA(first);
        }
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
        cget(api, '/api/response/file_text?name=' + encodeURIComponent(f.name)).then(function (d) {
          if (seq !== DUI.pvSeq) return;   /* 双击进逐条/点了别的行已抢先更新，别盖掉 */
          if (d && d.ok !== false && d.text != null) {
            setPreview(d.text + (d.truncated ? bi('\n\n…（文件过大，已截断显示）', '\n\n… (file too large, truncated)') : ''));
          } else {
            setPreview(bi('文件读取失败：', 'Failed to read file: ') + ((d && d.error) || bi('未知错误', 'unknown error')));
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
          var tr0 = api.el('tr', 'rs-empty-row');   /* 空态行：不高亮（用户 2026-10-20） */
          var td0 = api.el('td', 'rs-empty');
          td0.colSpan = 5;   /* 文件表 5 列（勾选 + 文件名/条数/体量/日期）：写 6 会让表头只铺半截（用户 2026-10-20） */
          td0.textContent = DATA.files.length ? '没有匹配的文件。'
            : '暂无产物';   /* 空页面统一这一句（用户 2026-10-20）：英译 No output yet */
          tr0.appendChild(td0);
          DUI.ftbody.appendChild(tr0);
        } else {
          fs.forEach(function (f) {
            var tr = api.el('tr');
            var wrap = api.el('label', 'rs-check rs-pick');
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

      /* 造一行（原 forEach 里的逻辑原样搬进来） */
      function buildRecRow(it) {
        var tr = api.el('tr');
        var wrap = api.el('label', 'rs-check rs-pick');
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
        tr.appendChild(api.el('td', 'col-q', it.question_short || it.question || ''));
        tr.appendChild(api.el('td', 'col-topic', it.topic || ''));
        tr.addEventListener('click', function () {
          /* 单击=下方显示内容，且行保持高亮（用户 2026-10-21） */
          if (DATA.rowOn && DATA.rowOn.parentNode) DATA.rowOn.classList.remove('rs-row-on');
          tr.classList.add('rs-row-on');
          DATA.rowOn = tr;
          setPreviewQA(it);
        });
        return tr;
      }

      /* 追加一批行（from 起 ROW_BATCH 条），返回下一批的起点 */
      function appendRecRows(shown, from) {
        var end = Math.min(shown.length, from + ROW_BATCH);
        var frag = document.createDocumentFragment();
        for (var i = from; i < end; i++) frag.appendChild(buildRecRow(shown[i]));
        DUI.tbody.appendChild(frag);
        return end;
      }

      /* 底部「显示更多」行：点一次追加一批，全部画完就自己消失 */
      function addMoreRow(shown, from) {
        if (!DUI || from >= shown.length) return;
        var tr = api.el('tr', 'rs-more-row');
        var td = api.el('td', 'rs-more-cell');
        td.colSpan = 4;
        var btn = api.el('button', 'rs-more-btn', window.bi('显示更多（还有 ' + (shown.length - from) + ' 条）', 'Show more (' + (shown.length - from) + ' left)'));
        btn.addEventListener('click', function () {
          tr.remove();
          var next = appendRecRows(shown, from);
          addMoreRow(shown, next);
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
          var tr0 = api.el('tr', 'rs-empty-row');
          var td0 = api.el('td', 'rs-empty');
          td0.colSpan = 4;
          td0.textContent = scopedItems().length ? '没有匹配的样本。'
            : (DATA.file ? '该文件没有可读的逐条数据。'
              : '暂无产物');
          tr0.appendChild(td0);
          DUI.tbody.appendChild(tr0);
        } else {
          /* 分批画（用户 2026-10-23）：原来一次把全部记录建成 tr，499 条就 3.8k 节点、
             耗时 235ms，条数越多越卡。现在先画 200 行，底部「显示更多」再追加 200 行。 */
          var end = appendRecRows(shown, 0);
          addMoreRow(shown, end);
        }
        syncPick();
        if (DUI.fitDm) DUI.fitDm();   /* 表格限高重算（用户 2026-10-21） */
      }

      function fetchList() {
        dataOK();
        cget(api, '/api/response/list').then(function (d) {
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
          renderCur();
          if (d.dir_exists === false) {
            DUI.status.textContent = '输出目录不存在：' + (d.dir || '—');
            DUI.status.classList.add('rs-warn');
          } else {
            DUI.status.textContent = '';
            DUI.status.classList.remove('rs-warn', 'rs-err');
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
          return api.post('/api/response/merge', {
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
            api.post('/api/response/delete', { targets: targets }).then(function (res) {
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

        var toolbar = api.el('div', 'rs-toolbar');
        var status = api.el('p', 'rs-status');
        toolbar.appendChild(button('刷新', 'rs-btn-ghost', 'refresh', function () { fetchList(); }));
        var delBtn = button('删除所选', 'rs-btn-danger', 'trash', onDeleteSelected);
        toolbar.appendChild(delBtn);
        /* 数据管理只导出、不导入；导出只导出选中的数据（用户 2026-10-14）：
           没选中 → 按钮禁用、点了也不触发；选中集随请求带给桥（targets）。 */
        var exportBtn = button('导出', 'rs-btn-soft', 'down', function () {
          var targets;
          if (DATA.view === 'files') {
            targets = fileTargets(DATA.files.filter(function (f) { return DATA.fsel[f.name]; }));
          } else {
            targets = selectedTargets();
          }
          if (!targets.length) return;
          api.exportDataset({
            route: '/api/response/export', defaultName: '响应蒸馏数据集', status: status,
            body: { targets: targets },
          });
        });
        exportBtn.disabled = true;
        toolbar.appendChild(exportBtn);
        /* 数据集合并（用户 2026-10-15）：选中 ≥2 个文件合成一个新数据集（原文件保留）；
           逐条视图无文件概念，不足两个选中 → 禁用 */
        var mergeBtn = button('合并', 'rs-btn-soft', 'merge', onMergeSelected);
        mergeBtn.disabled = true;
        toolbar.appendChild(mergeBtn);
        /* 文件视图（默认）↔ 逐条数据（双击文件进入）：返回=逐条→数据集（并换到第一条数据集）；
           数据集视图里置灰禁用（用户 2026-10-15） */
        var backBtn = button('返回', 'rs-btn-ghost', '', function () {
          if (DATA.view === 'records') setView('files');
        });
        backBtn.hidden = false;
        backBtn.disabled = true;
        toolbar.appendChild(backBtn);

        /* 搜索 + 主题筛选：钉在行尾右侧（用户 2026-10-15） */
        var rightWrap = api.el('span', 'rs-row rs-right');
        var search = api.el('input', 'rs-search');
        search.type = 'text';
        search.placeholder = '搜索';
        search.value = DATA.q;
        search.addEventListener('input', function () {
          DATA.q = search.value.trim();
          renderCur();
        });
        /* 搜索范围四选一（用户 2026-10-15）：搜索栏左边；主题筛选已删（同日「删掉主题筛选功能」） */
        var scopePick = api.pick(api.el('input'),
          [['fname', '文件名'], ['theme', '主题'], ['q', '问题'], ['text', '文本']], 'rs-scope');
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
        var filesWrap = api.el('div', 'rs-tablewrap');
        var ftable = api.el('table', 'rs-table');
        var fthead = api.el('thead');
        var fhtr = api.el('tr');
        var fAllBox = api.el('input', '');
        fAllBox.type = 'checkbox';
        var fAllWrap = api.el('label', 'rs-check');
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

        /* 逐条视图的表（双击文件后显示）：选/ID/问题/主题 */
        var tableWrap = api.el('div', 'rs-tablewrap');
        var table = api.el('table', 'rs-table');
        var thead = api.el('thead');
        var htr = api.el('tr');
        var allBox = api.el('input', '');
        allBox.type = 'checkbox';
        var allWrap = api.el('label', 'rs-check');
        allWrap.appendChild(allBox);
        var thAll = api.el('th', 'col-pick');
        thAll.appendChild(allWrap);
        htr.appendChild(thAll);
        [['col-id', 'ID'], ['col-q', '问题'], ['col-topic', '主题']]
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

        /* 预览区=格式化问答卡容器（用户 2026-10-16：查看对话同仓库格式，不再裸 JSON） */
        var pvTa = field(card.body, '', { textarea: true, rows: 8, readonly: true });
        var preview = api.el('div', 'rs-input rs-textarea rs-preview rs-pv-box');
        pvTa.parentNode.replaceChild(preview, pvTa);
        /* 用户口径：不要「预览（点击某行查看）」这类说明小字 → 空标签行整行拿掉 */
        var pLab = preview.parentNode && preview.parentNode.querySelector('.rs-flabel');
        if (pLab) pLab.remove();
        /* 表格限高 + 滚动上下渐变（用户 2026-10-21）：数据集/单条数据两套表各包一层，
           下方内容框压到窗口底（与边框留距），超出的行滚动 */
        var dmFitF = api.dmLayout(filesWrap, preview, { key: 'response' });
        var dmFitR = api.dmLayout(tableWrap, preview, { key: 'response' });
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
      if (typeof collect !== 'undefined' && collect && collect.timer) {
        clearInterval(collect.timer);
        collect.timer = null;
      }
    }
  });
})();
