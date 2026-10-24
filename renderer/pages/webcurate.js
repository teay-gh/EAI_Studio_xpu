/* ============================================================
   网页整理页（导航键 webcurate）
   ------------------------------------------------------------
   用户 2026-10-17「参考响应蒸馏的多页排布，UI 要求完全一致」：
   三个页签 + 同一套 rs-* 类名与间距（样式直接借 pages/response.css，不另写一份）。
     模型配置  规划 / 整理 / 审阅 三张模型卡 → config.json#article 的 titler / writer / reviewer
     采集中心  采集参数 + 进度 + 运行日志 + 页脚按钮（开始整理 / 停止 / 保存参数）
     数据管理  产物表（data/corpora/*.txt）+ 行内预览

   一条流水线三个 AI：
     规划 AI 决定「怎么查」——按主题 + 上一轮审阅反馈产出检索式
     整理 AI 抓网页 → 整理成干净纯文本（与主题无关就丢）
     审阅 AI 看重复率与覆盖度；重复率 > 阈值 → 通知规划 AI 换查法
   接口见 bridge_api/webcurate.py。
   ============================================================ */
(function () {
  'use strict';

  /* ---------- 受信任的内联 SVG 图标（与响应蒸馏同一套） ---------- */
  var ICONS = {
    save:   '<path d="M5.2 12.8 9.4 17 18.8 7.2"/>',
    sample: '<path d="M7 4.8h7.4L18 8.4v10.8H7Z"/><path d="M9.6 12h6M9.6 15.2h4.2"/>',
    reload: '<path d="M20 11a8 8 0 1 0-2.35 5.65"/><path d="M20 4.8V11h-6.2"/>',
    play:   '<path d="M8.6 5.6v12.8L19 12Z" fill="currentColor" stroke="none"/>',   /* 与训练页 ▶ 一致：实心三角 */
    /* 暂停（两竖条）：形状与训练页一致，但整体缩到和左边 ▶ 三角一样的视觉大小（用户 2026-10-17）——
       三角占 x 8.6~19 / y 5.6~18.4（高 12.8），这里两根条高 11.2、间距 4，视觉体量对齐 */
    stop:   '<path d="M10 6.4v11.2"/><path d="M14 6.4v11.2"/>',
    refresh:'<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',   /* 与预训练页同款（用户 2026-10-20） */
    /* 数据管理工具行（用户 2026-10-20：图标以另两个蒸馏页为准，原来这几条漏了 → 按钮上没图标） */
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

  /* 三张模型卡（与 bridge_api/webcurate.py::ROLE_TABLE 逐字一致） */
  var CARDS = [['planner', '规划模型'], ['extractor', '整理模型'], ['reviewer', '审阅模型']];

  var SAMPLE_SETS = [
    ['光合作用的原理', 'How photosynthesis works'],
    ['地球上水的循环', 'The water cycle on Earth'],
    ['恐龙灭绝的原因', 'Why the dinosaurs went extinct'],
    ['潮汐是怎么形成的', 'What causes the tides'],
    ['疫苗的工作原理', 'How vaccines work'],
    ['蜜蜂与授粉', 'Bees and pollination'],
    ['板块构造学说', 'The theory of plate tectonics'],
    ['极光是怎么产生的', 'How auroras form'],
    ['太阳系的构成', 'The structure of the solar system'],
    ['元素周期表是怎么排列的', 'How the periodic table is organized'],
    ['闪电是怎么产生的', 'How lightning forms'],
    ['光的折射与反射', 'Refraction and reflection of light'],
    ['抗生素如何杀菌', 'How antibiotics kill bacteria'],
    ['恒星的生命周期', 'The life cycle of stars'],
    ['火山喷发的原理', 'How volcanoes erupt'],
    ['人工智能基础', 'Fundamentals of artificial intelligence'],
    ['半导体与芯片', 'Semiconductors and chips'],
    ['人体免疫系统', 'The human immune system'],
    ['深海热泉生态', 'Life at deep-sea hydrothermal vents'],
    ['气候变化的机制', 'The mechanisms of climate change'],
  ];
  var ZH_ENGINE = 'https://cn.bing.com';           /* 中文模式 */
  var EN_ENGINE = 'https://www.google.com/';       /* 英文模式（用户 2026-10-20） */
  var sampleIdx = 0;
  function sampleLangIdx() { return (window.I18N && window.I18N.lang() === 'en') ? 1 : 0; }
  /* 2026-10-23（用户）：删除确认弹窗文案按语言走（删除 = 移入回收站） */
  function bi(zh, en) { return (window.I18N && window.I18N.lang() === 'en') ? en : zh; }
  /* 「填充示例」（模型配置）的英文提示词（用户 2026-10-20）：英文界面下点示例，提示词也是英文 */
  var PROMPTS_EN = {
    planner: 'You are an English search planner producing distillation corpora for a language model. Output one ' +
      'query per line, covering different sides of the topic (definition and background, mechanism or principle, ' +
      'data and cases, common misconceptions) without overlap. Keep each query short, ready to paste into a ' +
      'search engine: no URLs, no numbering, no explanation. All output must be in English.',
    extractor: 'You are an English text extractor preparing web corpora for a language model. Turn the fetched ' +
      'page body into clean, continuous English prose: keep only facts, mechanisms, data and conclusions ' +
      'relevant to the topic; drop navigation, ads, copyright lines, comments, related links, bylines and click ' +
      'prompts; keep facts and numbers unchanged, do not rewrite, add or comment; no headings markers, no ' +
      'bullet symbols, no explanatory text. Output the cleaned body only. All output must be in English.',
    reviewer: 'You are a strict English corpus reviewer. Judge only major problems in the cleaned page body: ' +
      'off-topic content, not an article (login, ad or error page), clearly truncated or broken text, heavy ' +
      'garbled characters, obviously wrong facts. Short length or sparse detail does not count. Output only a ' +
      'JSON object, nothing else: {"pass": true or false, "score": integer 0-10, "major_errors": ["each major ' +
      'issue, one line"], "comment": "one-sentence summary"}'
  };
  var MODEL_DEFAULTS = null;   /* 后端下发的模型卡出厂值（/api/webcurate/config → model_defaults） */

  /* 「填充示例」用的示例档案（用户 2026-10-17）：三张卡各一套能直接跑的值；api_key 不在其中。 */
  var EXAMPLES = {
    planner: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.7, max_tokens: 2048,
      system_prompt: '你是网页资料整理的规划助手。根据用户给的主题，给出 3-6 个检索关键词（每行一个），' +
        '只输出关键词本身，不要解释。'
    },
    extractor: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.3, max_tokens: 4096,
      system_prompt: '你是网页正文整理助手。把抓取到的网页正文整理成结构清晰的纯文本：保留事实与数字，' +
        '去掉导航、广告、版权等噪声，不要改写原意，不要加自己的评论。'
    },
    reviewer: {
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      temperature: 0.1, max_tokens: 512,
      system_prompt: '你是资料质检员。判断整理后的文本是否与主题相关、是否为完整正文（而不是登录页或广告页）。' +
        '只输出一个 JSON 对象：{"pass":true或false,"score":0到1,"major_errors":[],"comment":"一句话总评"}'
    }
  };

  /* 采集态（模块级：重挂时清定时器 / 让旧回调闭嘴） */
  var collect = { timer: null, cursor: 0, running: false, gen: 0 };

  /* 模型档案缓存（模块级，用户 2026-10-17：切页时文本框先空一拍再冒字=闪）。
     切回来时用上一次读到的档案直接建卡 → 首帧就是终态；/config 回来写的是同一份值，不再变。 */
  var PCACHE = {};
  /* 脏标记（用户 2026-10-21：切页回来填的字不能丢）：用户动过的表单，
     慢回来的 /config 不许覆盖；值本身随输入记进 PCACHE，重建时带回 */
  var PDIRTY = false;    /* 采集参数 */
  var MDIRTY = {};       /* 模型档案：role → true */

  EAIPages.register('webcurate', {
    title: '网页整理',
    sub: '规划 AI 定查法 → 检索整理成纯文本 → 审阅 AI 抓重复，重复率高就换查法',
    mock: {
      '/api/webcurate/config': {
        ok: true,
        params: {
          topic: '', engine_url: 'https://cn.bing.com/search?q={q}', out_name: '',
          queries_per_round: 4, pages_per_query: 3, max_rounds: 3,
          dup_threshold: 0.35, target_chars: 6000,
        },
        models: {}, models_full: [], engine_hint: '填首页地址即可（如 https://cn.bing.com）。',
      },
      '/api/webcurate/runs': { ok: true, items: [] },
      '/api/webcurate/status': { ok: true, exists: false, running: false, events: [], next: 0 },
    },

    mount: function (root, api) {
      /* 样式直接借响应蒸馏那一份：类名 rs-*，几何/字号/间距与它完全一致。
         已有同款 link 就不再插一份（2026-10-17：重复插表会多一次样式重算 → 字体重刷） */
      if (!document.querySelector('link[data-rs-css], link[href$="pages/response.css"]')) {
        var lk = document.createElement('link');
        lk.rel = 'stylesheet';
        lk.href = 'pages/response.css';
        lk.setAttribute('data-rs-css', '1');
        document.head.appendChild(lk);
      }
      if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
      collect.gen += 1;
      var GEN = collect.gen;

      var TABS = [['config', '模型配置'], ['collect', '采集中心'], ['data', '数据管理']];
      var LAST_TAB_KEY = 'eaiLastTab:webcurate';
      function getLastTab() { return (window.__eaiLastTab || {})[LAST_TAB_KEY] || ''; }
      function setLastTab(k) { (window.__eaiLastTab = window.__eaiLastTab || {})[LAST_TAB_KEY] = k; }

      /* ---------- 通用小工具（与响应蒸馏逐字一致） ---------- */
      function button(label, variant, iconName, onClick) {
        var b = api.el('button', 'btn rs-btn ' + variant);
        b.type = 'button';
        if (iconName) b.appendChild(iconEl(iconName));
        b.appendChild(api.el('span', 'rs-btn-label', label));
        b.addEventListener('click', function () { onClick(b); });
        return b;
      }
      function field(parent, label, opts) {
        opts = opts || {};
        var box = api.el('div', 'rs-field' + (opts.span2 ? ' rs-span2' : ''));
        var lab = api.el('div', 'rs-flabel', label);
        if (opts.labelTitle) lab.title = String(opts.labelTitle);
        box.appendChild(lab);
        var inp;
        if (opts.select) {
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

      /* ---------- 页头 + 分段标签 + 面板容器 ---------- */
      root.appendChild(api.hero('网页整理'));

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

      var LOADED = {};
      function pickTab(key) {
        TABS.forEach(function (t) {
          var on = t[0] === key;
          segOpts[t[0]].classList.toggle('on', on);
          segOpts[t[0]].setAttribute('aria-selected', on ? 'true' : 'false');
          bodies[t[0]].hidden = !on;
        });
        api.segThumb(seg, true);
        if (key === 'config' && !LOADED.config) loadConfig();
        if (key === 'collect' && !LOADED.collect) loadCollect();
        if (key === 'data') {
          if (!LOADED.data) loadData();
          else if (DATA.stale && DATA.reload) DATA.reload();   /* 刚跑完有新产物：进来先刷一遍 */
          /* 面板是隐藏时建好的：显示了再算一次（用户 2026-10-20），内容框下边框才贴得住窗口底 */
          if (api.dmRefit) api.dmRefit();   /* 同步算一次，别等下一帧（否则握把会跳一下） */
          requestAnimationFrame(function () { if (api.dmRefit) api.dmRefit(); });
        }
        setLastTab(key);
      }

      /* ==================== 模型配置 ==================== */
      var modelInputs = {};
      var toneBars = {};

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
          var ak = api.secretValue(ins.api_key);      /* 锁定态算空 → 不下发 → 保留原值 */
          if (ak) p.api_key = ak;
          body[key] = p;
        });
        return body;
      }

      function setTone(key, saved) {
        var b = toneBars[key];
        if (b) b.classList.toggle('rs-tone-accent', !!saved);
      }

      /* 模型列表缓存（用户 2026-10-20）：点过一次「测试」就记住这个 base_url 的可选模型，
         下次进来 model 直接是下拉框，不用再点测试、也不用凭记忆手打模型名。 */
      var MKEY = 'eai_model_list';
      function cacheGet(base) {
        try {
          var m = JSON.parse(localStorage.getItem(MKEY) || '{}');
          return m[String(base || '').trim()] || null;
        } catch (e) { return null; }
      }
      function cacheSet(base, ids) {
        try {
          var m = JSON.parse(localStorage.getItem(MKEY) || '{}');
          m[String(base || '').trim()] = ids;
          localStorage.setItem(MKEY, JSON.stringify(m));
        } catch (e) { /* 存不下就算了，不影响测试流程 */ }
      }

      /* 测试连通（用户 2026-10-20）：与另两个蒸馏页一致——先读 /models 把 model 变成下拉框，
         读不到再退回最小 chat 请求、model 仍手填。原来这里只发 chat 请求，所以 model 永远是手填框，
         用户填的 deepseek-chat 账号下早没了也发现不了。 */
      function testModel(key, statusEl) {
        var ins = modelInputs[key];
        var base = ins.base_url.value;
        var ak = api.secretValue(ins.api_key);
        api.dotSet(statusEl, 'busy', '[' + key + '] 正在测试连通并读取模型列表 …');
        api.post('/api/settings/models', { base_url: base, api_key: ak || '' }).then(function (lst) {
          if (GEN !== collect.gen) return;
          var ids = (lst && lst.ok && lst.models) || [];
          if (ids.length) {
            var before = String(ins.model.value || '').trim();
            cacheSet(base, ids);
            ins.model = api.modelSelect(ins.model, ids, 'rs-input');
            setTone(key, false);
            rememberCard(key);
            api.dotSet(statusEl, 'ok', '[' + key + '] 连通成功 · ' + ids.length + ' 个模型可选' +
              (before && ids.indexOf(before) < 0
                ? '（原值 ' + before + ' 不在列表里，已换成 ' + ids[0] + '）'
                : '') + ' → model 已变成下拉框，记得保存配置');
            return;
          }
          var http = lst && lst.http;
          if (!http || http === 401 || http === 403) {
            api.dotSet(statusEl, 'err', '[' + key + '] 连通失败：' +
              (http ? 'HTTP ' + http + '（密钥无效或已过期，或与本卡地址不匹配）'
                    : String((lst && lst.error) || '连不上（地址/网络）')) + ' · 已跳过慢速兜底测试');
            return;
          }
          var prof = { base_url: base, model: ins.model.value };
          if (ak) prof.api_key = ak;
          api.post('/api/webcurate/models/test', { role: key, profile: prof }).then(function (res) {
            if (GEN !== collect.gen) return;
            if (res && res.ok && res.ok !== false && !res.error) {
              api.dotSet(statusEl, 'ok', '[' + key + '] 连通成功 · ' + (res.model || '') +
                ' → ' + (res.reply || '') + '；该服务不支持 /models，model 仍需手填');
              return;
            }
            var why = (res && (res.error || res.reason)) || (lst && (lst.error || lst.note)) || '无响应';
            api.dotSet(statusEl, 'err', '[' + key + '] 连通失败：' + why +
              (/40[13]/.test(why) ? '（密钥无效或已过期，或与本卡地址不匹配）' : '') + ' · model 保持手填');
          });
        });
      }

      function fillCard(key, m) {
        var ins = modelInputs[key];
        if (!ins) return;
        var prof = m.profile || {};
        ['base_url', 'model', 'temperature', 'max_tokens', 'system_prompt'].forEach(function (f) {
          var v = prof[f];
          ins[f].value = v == null ? '' : String(v);
        });
        api.secretLock(ins.api_key, !!prof.api_key_set);
        setTone(key, true);
      }

      /* 切页回来填的字不能丢（用户 2026-10-21）：输入即时记进 PCACHE，重建时带回。
         锁定态=用户没动过新密钥（secretValue 算空）→ 保留原 api_key_set 语义 */
      function rememberCard(key) {
        var ins = modelInputs[key];
        if (!ins) return;
        MDIRTY[key] = true;
        var prev = PCACHE[key] || {};
        var p = {
          api_key_set: !!prev.api_key_set,
          base_url: String(ins.base_url.value || ''),
          model: String(ins.model.value || ''),
          temperature: String(ins.temperature.value || ''),
          max_tokens: String(ins.max_tokens.value || ''),
          system_prompt: String(ins.system_prompt.value || '')
        };
        var typed = api.secretValue(ins.api_key);
        if (typed) { p.api_key = typed; p.api_key_set = false; }
        else if (ins.api_key.dataset.locked === '1') p.api_key_set = true;
        PCACHE[key] = p;
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
          /* 卡头分隔条 = 保存状态：紫 = 已保存，灰 = 有改动未保存 */
          var bar = api.el('div', 'rs-tonebar');
          toneBars[m.key] = bar;
          card.body.insertBefore(bar, card.body.firstChild);
          var prof = m.profile || {};
          var ins = {};
          ins.base_url = field(card.body, 'base_url', { value: prof.base_url });
          ins.api_key = field(card.body, 'api_key', {
            value: '', mono: true, secret: !prof.api_key && !!prof.api_key_set,
            labelTitle: '留空即保留原值；已保存时显示为点，点一下可输入新密钥'
          });
          /* 切页回来把没保存的新密钥也带回来（2026-10-21）：password 态回填，屏上仍只显点 */
          if (prof.api_key) ins.api_key.value = String(prof.api_key);
          ins.model = field(card.body, 'model', { value: prof.model });
          /* 有上次测试留下的模型列表 → 一进来就是下拉框（用户 2026-10-20） */
          var cached = cacheGet(prof.base_url);
          if (cached && cached.length) {
            ins.model = api.modelSelect(ins.model, cached, 'rs-input');
            ins.model.addEventListener('change', function () { setTone(key, false); rememberCard(key); });
          }
          ins.temperature = field(card.body, 'temperature', { value: prof.temperature });
          ins.max_tokens = field(card.body, 'max_tokens', { value: prof.max_tokens });
          ins.system_prompt = field(card.body, '系统提示词', {
            value: prof.system_prompt, textarea: true, rows: 5
          });
          ins.system_prompt.parentElement.classList.add('rs-field-prompt');
          setTone(m.key, true);
          card.addEventListener('input', function () { setTone(m.key, false); rememberCard(m.key); });
          card.addEventListener('change', function () { setTone(m.key, false); rememberCard(m.key); });
          var dot = api.dot('idle', '尚未测试');
          /* 「测试」按钮与状态点放进 api_key 右侧的同一行 */
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
          api.post('/api/webcurate/models/save', collectProfiles()).then(function (res) {
            if (GEN !== collect.gen) return;
            if (!res || res.ok === false || res.error) {
              status.classList.add('rs-err');
              status.textContent = '保存失败：' + ((res && (res.error || res.reason)) || '未知错误');
              return;
            }
            status.textContent = '';                 /* 成功不弹提示（用户口径：只在失败时写状态行） */
            Object.keys(modelInputs).forEach(function (k) {
              var ins = modelInputs[k];
              if (!ins || !ins.api_key) return;
              var hadKey = ins.api_key.dataset.locked === '1' || !!String(ins.api_key.value || '').trim();
              api.secretLock(ins.api_key, hadKey);
            });
            Object.keys(toneBars).forEach(function (k) { setTone(k, true); });
            MDIRTY = {};   /* 已落盘：与服务器一致，允许 /config 重新接管 */
          });
        }));
        /* 重置（用户 2026-10-20：原「填充示例」改成重置）：只把 temperature / max_tokens / 系统提示词
           恢复成出厂值，base_url / model / api_key 一律不动；英文界面用英文定稿提示词。 */
        bar.appendChild(button('重置', 'rs-btn-ghost', 'refresh', function () {
          var SRC = (MODEL_DEFAULTS && Object.keys(MODEL_DEFAULTS).length) ? MODEL_DEFAULTS : EXAMPLES;
          var en = !!(window.I18N && window.I18N.lang() === 'en');
          Object.keys(SRC).forEach(function (k) {
            var ins = modelInputs[k];
            if (!ins) return;
            var ex = SRC[k] || {};
            ['temperature', 'max_tokens'].forEach(function (f) {
              if (ins[f] && ex[f] != null && ex[f] !== '') ins[f].value = String(ex[f]);
            });
            if (ins.system_prompt) {
              var sp = en ? (ex.system_prompt_en || PROMPTS_EN[k] || ex.system_prompt)
                          : (ex.system_prompt || PROMPTS_EN[k] || '');
              if (sp) ins.system_prompt.value = String(sp);
            }
            setTone(k, false);
            rememberCard(k);   /* 重置值也进缓存（2026-10-21）：切页回来不丢 */
          });
          status.textContent = '';
        }));
        bar.appendChild(status);
        stack.appendChild(bar);
        host.appendChild(stack);

        /* 先同步建卡（第一帧就是完整版面），取回数据再就地填值（不重建 → 不闪）；
           档案用模块级缓存 PCACHE 预填，切页回来首帧就有字（用户 2026-10-17） */
        CARDS.forEach(function (d) {
          grid.appendChild(modelCard({ key: d[0], label: d[1], profile: PCACHE[d[0]] || {} }));
        });
        api.get('/api/webcurate/config').then(function (d) {
          if (GEN !== collect.gen) return;
          if (d && d.error) {
            status.classList.add('rs-err');
            status.textContent = '读取配置失败：' + d.error;
            return;
          }
          MODEL_DEFAULTS = d.model_defaults || MODEL_DEFAULTS;
          (d.models_full || []).forEach(function (m) {
            if (!m || !m.role) return;
            if (MDIRTY[m.role]) return;   /* 用户已动手填（2026-10-21）：别让慢回来的 /config 冲掉 */
            PCACHE[m.role] = m.profile || {};   /* 缓存档案：下次切回首帧就有字 */
            if (modelInputs[m.role]) fillCard(m.role, m);
          });
          status.textContent = '';
        });
      }

      /* ==================== 采集中心 ==================== */
      var PTARGET = 20;   /* 采集篇数兜底（用户 2026-10-21：按篇采集） */

      function logLine(text, cls) {
        if (text == null || text === '') return;
        var el = collect.logEl;
        if (!el) return;
        var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
        el.appendChild(api.el('span', 'rs-log-line' + (cls ? ' ' + cls : ''), String(text)));
        if (atBottom) el.scrollTop = el.scrollHeight;
      }

      function setRunningUI(running) {
        collect.running = !!running;
        if (collect.startBtn) collect.startBtn.disabled = collect.running;
        if (collect.stopBtn) collect.stopBtn.disabled = !collect.running;
      }

      /* 进度条按「已收篇数 / 采集篇数」走（用户 2026-10-21：按篇采集） */
      function updateProgress(s) {
        if (!s) return;
        var pieces = Number(s.pieces != null ? s.pieces : s.paras) || 0;
        var target = Math.max(1, Number(s.pieces_target || PTARGET) || 1);
        var chars = Number(s.chars) || 0;
        if (collect.fill) {
          collect.fill.style.width = Math.max(0, Math.min(1, pieces / target)) * 100 + '%';
        }
        if (collect.plabel) {
          collect.plabel.textContent = '第 ' + Math.max(1, Number(s.round) || 0) + ' 轮 · 已收 ' + pieces + '/' + target +
            ' 篇 · ' + chars + ' 字 · 来源 ' + (Number(s.sources) || 0) +
            ' · 重复率 ' + (s.dup_rate == null ? '—' : Math.round(Number(s.dup_rate) * 100) + '%');
        }
      }

      function handleEvent(e) {
        var k = e.kind, p = e.payload || {};
        if (k === 'log') {
          logLine(p.text || '');
        } else if (k === 'progress') {
          updateProgress(p);
        } else if (k === 'dup_warn' || k === 'dup_abort') {
          /* 重复率提醒只弹一次（用户 2026-10-21）：用户关掉后本次运行不再提醒 */
          if (!collect.dupAlerted) {
            collect.dupAlerted = true;
            var w = Math.round(Number(p.warn != null ? p.warn : 20));
            var a = Math.round(Number(p.abort != null ? p.abort : 40));
            if (k === 'dup_warn') {
              api.alertBox({ title: '重复率警告',
                message: '当前重复率已达 ' + Math.round(Number(p.rate || 0) * 100) + '%（≥' + w + '%），收集到的内容重复偏高。' });
            } else {
              api.alertBox({ title: '重复率过高，已中止',
                message: '当前重复率已达 ' + Math.round(Number(p.rate || 0) * 100) + '%（≥' + a + '%），采集已中止，已收集的内容保留。' });
            }
          }
        } else if (k === 'result') {
          var nsrc = [].concat(p.sources || []).length;
          logLine('整理完成：' + (p.pieces != null ? p.pieces : p.paras) + ' 篇 · ' + (p.chars || 0) +
            ' 字 · 来源 ' + nsrc + ' · 重复率 ' +
            (p.dup_rate == null ? '—' : Math.round(Number(p.dup_rate) * 100) + '%'));
          updateProgress(p);
          DATA.stale = true;                     /* 有新产物了，数据管理下次进来重取 */
        } else if (k === 'error') {
          logLine('[错误] ' + (p.text || p.message || 'error'), 'warn');
        } else {
          logLine('[' + k + '] ' + JSON.stringify(p));
        }
      }

      function pollOnce() {
        return api.get('/api/webcurate/status?since=' + collect.cursor).then(function (d) {
          if (GEN !== collect.gen) return;
          if (!d || d.error) { logLine('[状态] ' + ((d && d.error) || '未知错误'), 'warn'); return; }
          (d.events || []).forEach(handleEvent);
          if (typeof d.next === 'number') collect.cursor = d.next;
          if (d.running) {
            setRunningUI(true);
          } else {
            setRunningUI(false);
            if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
          }
        });
      }

      function startPolling() {
        if (collect.timer) return;
        collect.timer = setInterval(function () {
          pollOnce().then(function () {
            if (!collect.running && collect.timer) {
              clearInterval(collect.timer);
              collect.timer = null;
            }
          });
        }, 1000);
      }

      function loadCollect() {
        LOADED.collect = true;
        var host = bodies.collect;
        host.innerHTML = '';
        if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
        collect.cursor = 0;
        collect.running = false;

        var stack = api.el('div', 'rs-collect');
        var colL = api.el('div', 'rs-collect-col');
        var colR = api.el('div', 'rs-collect-col');

        var paramCard = api.card('采集参数');
        var form = api.el('div', 'rs-grid2');
        var fTopic = field(form, '主题', {
          textarea: true, rows: 5, span2: true,
          placeholder: '要查什么？一句话或几个关键词，\n例如：光合作用的原理'
        });
        var fEngine = field(form, '搜索引擎网址', { span2: true });
        var fPieces = field(form, '采集篇数', {});
        var fConc = field(form, '并发数', {});
        var fName = field(form, '输出文件名', {});   /* 用户 2026-10-21：与「最大字数」交换位置（文件名左、最大字数右） */
        var fMax = field(form, '最大字数', {});
        /* 输出目录（用户 2026-10-21）：和蒸馏一样显示地址，网页采集数据单独放 data/webcurate */
        var fDir = field(form, '输出目录', { span2: true, readonly: true });
        paramCard.body.appendChild(form);
        /* 高级设置（用户 2026-10-21）：检索式等收进弹窗，主面板只留按篇采集的参数 */
        /* 高级设置（用户 2026-10-21）：齿轮图标=EAI 设置同款、靠右；
           行高 22 与蒸馏页底行一致 → 采集底栏 y 与蒸馏页对齐 */
        var advBtn = api.el('button', 'wc-adv-btn', '');
        advBtn.type = 'button';
        advBtn.title = '高级设置';
        advBtn.setAttribute('aria-label', '高级设置');
        advBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
          + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
          + '<path d="M9.80 2.45 A9.80 9.80 0 0 1 14.20 2.45 L14.43 5.33 A7.10 7.10 0 0 1 16.56 6.56 L19.17 5.32 A9.80 9.80 0 0 1 21.37 9.13 L18.99 10.77 A7.10 7.10 0 0 1 18.99 13.23 L21.37 14.87 A9.80 9.80 0 0 1 19.17 18.68 L16.56 17.44 A7.10 7.10 0 0 1 14.43 18.67 L14.20 21.55 A9.80 9.80 0 0 1 9.80 21.55 L9.57 18.67 A7.10 7.10 0 0 1 7.44 17.44 L4.83 18.68 A9.80 9.80 0 0 1 2.63 14.87 L5.01 13.23 A7.10 7.10 0 0 1 5.01 10.77 L2.63 9.13 A9.80 9.80 0 0 1 4.83 5.32 L7.44 6.56 A7.10 7.10 0 0 1 9.57 5.33 L9.80 2.45 Z"/>'
          + '<circle cx="12" cy="12" r="3.3"/></svg>';
        paramCard.body.appendChild(advBtn);
        colL.appendChild(paramCard);

        /* 高级设置的值（弹窗里改）：检索式三个参数 + 重复率上限（用户 2026-10-21） */
        var ADV = {
          queries_per_round: 4,
          pages_per_query: 3,
          max_rounds: 3,
          dup_limit: 40
        };

        function openAdv() {
          var mask = api.el('div', 'pc-mask');
          var card = api.el('div', 'pc-card');
          var head = api.el('div', 'pc-head');
          head.appendChild(api.el('span', 'pc-title', '高级设置'));
          var x = api.el('button', 'pc-x', '');
          x.type = 'button';
          x.setAttribute('aria-label', '关闭');
          x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
          head.appendChild(x);
          var body = api.el('div', 'rs-grid2 wc-adv');
          var aQueries = field(body, '每轮检索式', {});
          var aPages = field(body, '每条检索抓几篇', {});
          var aRounds = field(body, '最多几轮', {});
          var aDup = field(body, '重复率上限', {});
          aQueries.value = String(ADV.queries_per_round);
          aPages.value = String(ADV.pages_per_query);
          aRounds.value = String(ADV.max_rounds);
          aDup.value = String(ADV.dup_limit);
          var btns = api.el('div', 'pc-btns');
          var cancel = api.el('button', 'btn pc-cancel', '取消');
          cancel.type = 'button';
          var ok = api.el('button', 'btn pc-ok', '保存');
          ok.type = 'button';
          btns.appendChild(cancel);
          btns.appendChild(ok);
          card.appendChild(head);
          card.appendChild(body);
          card.appendChild(btns);
          mask.appendChild(card);
          document.body.appendChild(mask);
          function close() {
            mask.classList.remove('on');
            setTimeout(function () { if (mask.parentNode) mask.parentNode.removeChild(mask); }, 200);
          }
          x.addEventListener('click', close);
          cancel.addEventListener('click', close);
          ok.addEventListener('click', function () {
            ADV.queries_per_round = Math.round(num(aQueries, 4, 1, 12));
            ADV.pages_per_query = Math.round(num(aPages, 3, 1, 8));
            ADV.max_rounds = Math.round(num(aRounds, 3, 1, 6));
            ADV.dup_limit = Math.round(num(aDup, 40, 5, 95));   /* 重复率上限%（警告线取一半） */
            rememberParams();   /* 高级设置也算用户输入（2026-10-21）：切页回来不丢 */
            close();
          });
          mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
          requestAnimationFrame(function () { mask.classList.add('on'); });
        }
        advBtn.addEventListener('click', openAdv);

        /* 采集参数：先建框再等 /config 回来填 → 切页时文本框会先空一拍（用户 2026-10-17 说闪）。
           改成用模块级缓存 PCACHE.params 在首帧就填好；/config 回来写同一份值不再变。 */
        function fillParams(p) {
          p = p || {};
          fTopic.value = p.topic == null ? '' : String(p.topic);
          fPieces.value = p.pieces == null ? '20' : String(p.pieces);
          fConc.value = p.concurrency == null ? '3' : String(p.concurrency);
          fMax.value = p.max_chars == null ? '500' : String(p.max_chars);
          fName.value = String(p.out_name || 'web_set');   /* 空着也给默认文件名（用户 2026-10-20） */
          fEngine.value = p.engine_url == null ? 'https://cn.bing.com' : String(p.engine_url);
          ADV.queries_per_round = p.queries_per_round == null ? 4 : Number(p.queries_per_round) || 4;
          ADV.pages_per_query = p.pages_per_query == null ? 3 : Number(p.pages_per_query) || 3;
          ADV.max_rounds = p.max_rounds == null ? 3 : Number(p.max_rounds) || 3;
          ADV.dup_limit = p.dup_limit == null ? 40 : Number(p.dup_limit) || 40;
          PTARGET = Number(p.pieces) || PTARGET;
        }
        fillParams(PCACHE.params);
        if (PCACHE.output_dir) fDir.value = PCACHE.output_dir;

        /* 切页回来填的字不能丢（用户 2026-10-21）：输入即时记进 PCACHE.params，
           下次重建 fillParams 原样带回；置脏标记挡掉慢回来的 /config 覆写 */
        function rememberParams() {
          PDIRTY = true;
          PCACHE.params = {
            topic: String(fTopic.value || ''),
            out_name: String(fName.value || ''),
            pieces: String(fPieces.value || ''),
            concurrency: String(fConc.value || ''),
            max_chars: String(fMax.value || ''),
            engine_url: String(fEngine.value || ''),
            queries_per_round: ADV.queries_per_round,
            pages_per_query: ADV.pages_per_query,
            max_rounds: ADV.max_rounds,
            dup_limit: ADV.dup_limit
          };
        }
        form.addEventListener('input', rememberParams);
        form.addEventListener('change', rememberParams);

        var logEl = api.el('div', 'rs-log');
        var status = api.el('p', 'rs-status');

        function num(el, dflt, lo, hi) {
          var v = parseFloat(String(el.value || '').replace(/[^\d.-]/g, ''));
          if (!isFinite(v)) v = dflt;
          return Math.min(hi, Math.max(lo, v));
        }

        function gatherFields() {
          return {
            topic: String(fTopic.value || '').trim(),
            out_name: String(fName.value || '').trim(),
            lang: (window.I18N && window.I18N.lang()) || 'zh',   /* 整理出的语料语言跟界面（用户 2026-10-20） */
            pieces: Math.round(num(fPieces, 20, 1, 500)),
            concurrency: Math.round(num(fConc, 3, 1, 8)),
            max_chars: Math.round(num(fMax, 500, 200, 20000)),
            engine_url: String(fEngine.value || '').trim(),
            queries_per_round: Math.round(Math.min(12, Math.max(1, Number(ADV.queries_per_round) || 4))),
            pages_per_query: Math.round(Math.min(8, Math.max(1, Number(ADV.pages_per_query) || 3))),
            max_rounds: Math.round(Math.min(6, Math.max(1, Number(ADV.max_rounds) || 3))),
            dup_limit: Math.round(Math.min(95, Math.max(5, Number(ADV.dup_limit) || 40)))
          };
        }

        function onStart() {
          if (collect.running) return;
          var body = gatherFields();
          if (!body.topic) {
            status.classList.add('rs-err');
            status.textContent = '请先填主题';
            return;
          }
          status.textContent = '';
          status.classList.remove('rs-err', 'rs-warn');
          collect.startBtn.disabled = true;
          collect.cursor = 0;
          collect.dupAlerted = false;   /* 新一次运行重新允许一次重复率提醒（用户 2026-10-21） */
          PTARGET = body.pieces;
          logEl.innerHTML = '';
          logLine('正在启动整理 …');
          api.post('/api/webcurate/start', body).then(function (res) {
            if (GEN !== collect.gen) return;
            if (!res || res.ok === false || res.error) {
              var msg = (res && (res.error || res.reason)) || '未知错误';
              status.classList.add('rs-err');
              status.textContent = '启动失败：' + msg;
              logLine('[错误] 启动失败：' + msg, 'warn');
              setRunningUI(false);
              return;
            }
            logLine('已启动：' + (res.out_name || '') + ' → ' + (res.path || ''));
            setRunningUI(true);
            startPolling();
            pollOnce();
          });
        }

        function onStop() {
          collect.stopBtn.disabled = true;
          api.post('/api/webcurate/stop', {}).then(function (res) {
            if (GEN !== collect.gen) return;
            logLine((res && res.note) ? ('停止请求：' + res.note) : '已请求停止。');
            if (!collect.timer) pollOnce();
          });
        }

        /* 刷新（用户 2026-10-21，参考蒸馏页）：立刻重拉日志与进度，不等下一拍 */
        function onRefresh(btn) {
          if (btn) btn.disabled = true;
          if (collect.timer) { clearInterval(collect.timer); collect.timer = null; }
          var p = pollOnce();
          Promise.resolve(p).then(function () {
            if (collect.running) startPolling();   /* 重置节奏，别等上一拍 */
            if (btn) btn.disabled = false;
          }, function () { if (btn) btn.disabled = false; });
        }

        function onSave() {
          api.post('/api/webcurate/config/save', gatherFields()).then(function (res) {
            if (GEN !== collect.gen) return;
            if (!res || res.ok === false || res.error) {
              status.classList.add('rs-err');
              status.textContent = '保存失败：' + ((res && res.error) || '未知错误');
              return;
            }
            PDIRTY = false;   /* 已落盘：与服务器一致 */
            status.classList.remove('rs-err', 'rs-warn');
            status.textContent = '';
          });
        }

        var actions = api.el('div', 'rs-row rs-actions');
        var startBtn = button('开始整理', 'rs-btn-accent', 'play', onStart);
        var stopBtn = button('停止', 'rs-btn-danger', 'stop', onStop);
        stopBtn.disabled = true;
        actions.appendChild(startBtn);
        actions.appendChild(stopBtn);
        /* 刷新（用户 2026-10-21，参考蒸馏页）：放「停止」和「填充示例」之间 */
        actions.appendChild(button('刷新', 'rs-btn-ghost', 'refresh', onRefresh));
        /* 填充示例（用户 2026-10-18 / 2026-10-20）：只填「主题」和「搜索引擎网址」，
           其余参数一律不动 —— 输出名/阈值/轮次这些是用户自己调过的，不该被示例覆盖。
           点一次换下一条主题（循环），填完仍落盘（这个按钮同时承担原来「保存参数」的职责）。 */
        actions.appendChild(button('填充示例', 'rs-btn-ghost', 'sample', function () {
          var k = sampleLangIdx();
          var set = SAMPLE_SETS[sampleIdx % SAMPLE_SETS.length];
          sampleIdx += 1;
          fTopic.value = set[k];                  /* 按当前界面语言取中文 / 英文主题 */
          fEngine.value = k === 1 ? EN_ENGINE : ZH_ENGINE;   /* 首页地址即可，后端按引擎补 /search?q= */
          status.classList.remove('rs-err', 'rs-warn');
          status.textContent = '';
          rememberParams();   /* 示例值也进缓存（2026-10-21）：切页回来不丢 */
          onSave();
        }));

        /* 右列：进度 + 运行日志（日志吃满列高，同响应蒸馏） */
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
        actions.appendChild(status);      /* 状态行并入按钮行：页脚恒一行高 */
        host.appendChild(actions);

        collect.startBtn = startBtn;
        collect.stopBtn = stopBtn;
        collect.fill = fill;
        collect.plabel = plabel;
        collect.logEl = logEl;
        setRunningUI(false);

        api.get('/api/webcurate/config').then(function (d) {
          if (GEN !== collect.gen) return;
          if (d && d.error) {
            status.classList.add('rs-err');
            status.textContent = '采集参数读取失败：' + d.error;
            return;
          }
          var p = (d && d.params) || {};
          PCACHE.output_dir = d.output_dir || '';
          fDir.value = PCACHE.output_dir;  /* 输出目录只读展示（用户 2026-10-21） */
          if (!PDIRTY) {   /* 用户已动手填（2026-10-21）：别把填好的字冲掉 */
            PCACHE.params = p;              /* 缓存参数：下次切回首帧就有字 */
            fillParams(p);
          }
          status.textContent = '';
          /* 页面刷新后若已有任务在跑，自动续上轮询 */
          pollOnce().then(function () {
            if (GEN !== collect.gen) return;
            if (collect.running && !collect.timer) startPolling();
          });
        });
      }

      /* ==================== 数据管理 ==================== */
      var DATA = { items: [], q: '', sel: {}, scope: 'fname', open: null, stale: true };

      /* 列里只放日期（与响应蒸馏同宽 perception：104px 放得下）；时分秒挂在 title 上 */
      function fmtDate(ts) {
        if (!ts) return '—';
        var d = new Date(Number(ts) * 1000);
        if (isNaN(d.getTime())) return '—';
        function two(n) { return (n < 10 ? '0' : '') + n; }
        return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate());
      }
      function fmtTime(ts) {
        if (!ts) return '';
        var d = new Date(Number(ts) * 1000);
        if (isNaN(d.getTime())) return '';
        function two(n) { return (n < 10 ? '0' : '') + n; }
        return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate()) +
          ' ' + two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
      }

      function loadData() {
        LOADED.data = true;
        var host = bodies.data;
        host.innerHTML = '';
        var stack = api.el('div', 'stack');

        var card = api.card('');
        var cardHead = card.querySelector('.card-head');
        if (cardHead) cardHead.remove();          /* 空卡头拿掉，工具行顶在卡内最上（同响应蒸馏） */
        card.body.style.marginTop = '0px';

        var toolbar = api.el('div', 'rs-toolbar');
        var status = api.el('p', 'rs-status');
        toolbar.appendChild(button('刷新', 'rs-btn-ghost', 'refresh', function () { fetchList(); }));
        var delBtn = button('删除所选', 'rs-btn-danger', 'trash', onDeleteSelected);
        delBtn.disabled = true;
        toolbar.appendChild(delBtn);
        var exportBtn = button('导出', 'rs-btn-soft', 'down', onExportSelected);
        exportBtn.disabled = true;
        toolbar.appendChild(exportBtn);
        var mergeBtn = button('合并', 'rs-btn-soft', 'merge', onMergeSelected);
        mergeBtn.disabled = true;
        toolbar.appendChild(mergeBtn);
        /* 返回同其他蒸馏页位置；网页整理产物是整篇 txt（无逐条视图），常灰 */
        var backBtn = button('返回', 'rs-btn-ghost', '', function () {});
        backBtn.disabled = true;
        toolbar.appendChild(backBtn);

        var rightWrap = api.el('span', 'rs-row rs-right');
        var scopePick = api.pick(api.el('input'),
          [['fname', '文件名'], ['topic', '主题']], 'rs-scope');
        scopePick.value = DATA.scope || 'fname';
        scopePick.addEventListener('change', function () {
          DATA.scope = scopePick.value;
          renderRows();
        });
        var search = api.el('input', 'rs-search');
        search.type = 'text';
        search.placeholder = '搜索';
        search.value = DATA.q;
        search.addEventListener('input', function () {
          DATA.q = search.value.trim();
          renderRows();
        });
        rightWrap.appendChild(scopePick);
        rightWrap.appendChild(search);
        toolbar.appendChild(rightWrap);
        card.body.appendChild(toolbar);

        var wrap = api.el('div', 'rs-tablewrap');
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
        [['col-fname', '文件名'], ['col-count', '字数'], ['col-topic', '主题'],
         ['col-dup', '重复率'], ['col-date', '日期']].forEach(function (h) {
          htr.appendChild(api.el('th', h[0], h[1]));
        });
        thead.appendChild(htr);
        table.appendChild(thead);
        allBox.addEventListener('change', function () {
          rowsShown().forEach(function (it) { DATA.sel[it.name] = allBox.checked; });
          renderRows();
        });
        var tbody = api.el('tbody');
        /* 数据没回来前全选框先禁用（用户 2026-10-20）：否则进页面那一两帧会先画成可勾选，再变灰 */
        allBox.disabled = true;
        DATA.tbody = tbody;
        table.appendChild(tbody);
        wrap.appendChild(table);
        card.body.appendChild(wrap);

        /* 预览进卡内底部（同响应蒸馏）：内容框常驻，握把可拉高 */
        var prev = api.el('div', 'rs-preview');
        card.body.appendChild(prev);
        stack.appendChild(card);
        var dmFit = api.dmLayout(wrap, prev, { key: 'webcurate' });

        /* 上次读到的产物列表挂在 window 上（页面每次进来都会重建 DATA）：有上次结果就先铺满格子，
           进场那几帧不会先空一下；一次都没读过才把表体藏起来等第一份数据（用户 2026-10-20）。 */
        if (window.__EAI_WC_LAST) {
          DATA.items = window.__EAI_WC_LAST.slice();
          DATA.stale = false;
        }
        /* 建好就先按已知的（有缓存用缓存，没有就是空态）铺一次：格子从第一帧起就有内容，
           不会先空一小段再出现 —— 用户 2026-10-20 说的「进去时小字和全选框闪一下」。 */
        renderRows();

        host.appendChild(stack);

        function rowsShown() {
          var q = DATA.q.toLowerCase();
          if (!q) return DATA.items;
          var scope = DATA.scope || 'fname';
          return DATA.items.filter(function (it) {
            var txt = scope === 'topic' ? (it.topic || '') : it.name;
            return String(txt).toLowerCase().indexOf(q) >= 0;
          });
        }

        function selectedNames() {
          return DATA.items.filter(function (it) { return DATA.sel[it.name]; })
            .map(function (it) { return it.name; });
        }

        function syncPick() {
          var shown = rowsShown();
          var n = 0;
          shown.forEach(function (it) { if (DATA.sel[it.name]) n++; });
          allBox.checked = shown.length > 0 && n >= shown.length;
          allBox.indeterminate = n > 0 && n < shown.length;
          /* 没数据时全选框不该显示成可勾选的样子（用户 2026-10-20） */
          allBox.disabled = shown.length === 0;
          delBtn.disabled = n === 0;
          exportBtn.disabled = n === 0;
          mergeBtn.disabled = n < 2;
        }

        /* 造一行产物（原 forEach 逻辑原样搬进来，用户 2026-10-23 分批画） */
        function buildProdRow(it) {
          var tr = api.el('tr');
          tr.style.cursor = 'pointer';
          tr.title = it.name + '（' + fmtTime(it.mtime) + '）';
          var tdPick = api.el('td', 'col-pick');
          var box = api.el('input', '');
          box.type = 'checkbox';
          box.checked = !!DATA.sel[it.name];
          var lb = api.el('label', 'rs-check');
          lb.appendChild(box);
          tdPick.appendChild(lb);
          tr.appendChild(tdPick);
          box.addEventListener('click', function (ev) { ev.stopPropagation(); });
          box.addEventListener('change', function () {
            DATA.sel[it.name] = box.checked;
            syncPick();
          });
          tr.appendChild(api.el('td', 'col-fname', it.name));
          tr.appendChild(api.el('td', 'col-count', String(it.chars || 0)));
          tr.appendChild(api.el('td', 'col-topic', it.topic || '—'));
          tr.appendChild(api.el('td', 'col-dup',
            it.dup_rate == null ? '—' : Number(it.dup_rate).toFixed(2)));
          tr.appendChild(api.el('td', 'col-date', fmtDate(it.mtime)));
          tr.addEventListener('click', function () {
            /* 单击=下方预览内容，且行保持高亮 */
            if (DATA.rowOn && DATA.rowOn.parentNode) DATA.rowOn.classList.remove('rs-row-on');
            tr.classList.add('rs-row-on');
            DATA.rowOn = tr;
            preview(it);
          });
          return tr;
        }

        function renderRows() {
          tbody.innerHTML = '';
          var rows = rowsShown();
          if (!rows.length) {
            var tr0 = api.el('tr', 'rs-empty-row');   /* 空态行：不高亮、不显示指针（用户 2026-10-20） */
            var td0 = api.el('td', 'rs-empty', DATA.items.length ? '没有匹配的产物' : '暂无产物');
            td0.colSpan = 6;
            tr0.appendChild(td0);
            tbody.appendChild(tr0);
            syncPick();
            return;
          }
          /* 分批画（用户 2026-10-23）：产物多时不再一次全建行，先 200 行 + 「显示更多」 */
          var batch = 200;
          (function drawRows(from) {
            var end = Math.min(rows.length, from + batch);
            var frag = document.createDocumentFragment();
            for (var i = from; i < end; i++) frag.appendChild(buildProdRow(rows[i]));
            tbody.appendChild(frag);
            if (end < rows.length) {
              var mtr = api.el('tr', 'rs-more-row');
              var mtd = api.el('td', 'rs-more-cell');
              mtd.colSpan = 6;
              var mbtn = api.el('button', 'rs-more-btn', window.bi('显示更多（还有 ' + (rows.length - end) + ' 条）', 'Show more (' + (rows.length - end) + ' left)'));
              mbtn.addEventListener('click', function () { mtr.remove(); drawRows(end); syncPick(); dmFit.fit(); });
              mtd.appendChild(mbtn);
              mtr.appendChild(mtd);
              tbody.appendChild(mtr);
            }
          })(0);
          syncPick();
          /* 内容框跟着列表走（用户 2026-10-22）：没有选中、或选中的已被筛掉 → 自动显示第一条 */
          (function () {
            var names = rows.map(function (x) { return String(x.name || ''); });
            if (!names.length) { DATA.open = ''; return; }
            if (names.indexOf(String(DATA.open || '')) < 0) preview(rows[0]);
          })();
          dmFit.fit();   /* 表格限高重算 */
        }

        function onDeleteSelected() {
          var names = selectedNames();
          if (!names.length) return;
          /* 自己的弹窗（同响应蒸馏口径）：移入回收站，可恢复 */
          api.notice(bi('删除所选产物：', 'Delete selected outputs:'),
            [bi('确定将所选的 ' + names.length + ' 个产物移入回收站',
                'Move the ' + names.length + ' selected ' + enPlural(names.length, 'output', 'outputs') +
                ' to the Recycle Bin')],
            { okText: '删除', cancelText: '取消', onOk: function () {
              api.post('/api/webcurate/delete', { names: names }).then(function (res) {
                if (!res || res.ok === false) {
                  status.classList.add('rs-err');
                  status.textContent = '删除失败：' + ((res && res.error) || '未知错误');
                  return;
                }
                names.forEach(function (n) { delete DATA.sel[n]; });
                fetchList();
              })['catch'](function (e) {
                status.classList.add('rs-err');
                status.textContent = '删除失败：' + ((e && e.message) ? e.message : e);
              });
            } });
        }

        function onExportSelected() {
          var names = selectedNames();
          if (!names.length) return;
          api.exportDataset({
            route: '/api/webcurate/export', defaultName: '网页整理产物', ext: 'txt',
            filterName: '文本文档', status: status, body: { names: names },
          });
        }

        function onMergeSelected() {
          var names = selectedNames();
          if (names.length < 2) return;
          api.prompt({
            title: '合并产物',
            label: '名称',
            value: 'merged_' + fmtDate(Math.floor(Date.now() / 1000)).replace(/-/g, ''),   /* 2026-10-23（用户）：默认名固定英文 */
            okText: '合并',
          }).then(function (name) {
            if (!name) return null;
            return api.post('/api/webcurate/merge', { names: names, name: name }).then(function (res) {
              if (!res || res.ok === false) {
                status.classList.add('rs-err');
                status.textContent = '合并失败：' + ((res && res.error) || '未知错误');
                return;
              }
              DATA.sel = {};
              fetchList();
            });
          })['catch'](function (e) {
            status.classList.add('rs-err');
            status.textContent = '合并失败：' + ((e && e.message) ? e.message : e);
          });
        }

        function preview(it) {
          DATA.open = it.name;
          /* 内容框显示哪一条，表格里那一行就高亮（用户 2026-10-22，三个数据管理页统一） */
          if (DATA.tbody) {
            [].forEach.call(DATA.tbody.querySelectorAll('tr'), function (tr) {
              var td = tr.querySelector('.col-fname');
              tr.classList.toggle('rs-row-on', !!td && td.textContent === it.name);
            });
          }
          dmFit.fit();   /* 表格限高重算 */
          prev.textContent = '正在读取 ' + it.name + ' …';
          api.get('/api/webcurate/preview?path=' + encodeURIComponent(it.path)).then(function (d) {
            if (GEN !== collect.gen) return;
            if (!d || d.ok === false || d.error) {
              prev.textContent = '读取失败：' + ((d && d.error) || '未知错误');
              dmFit.fit();
              return;
            }
            prev.textContent = (d.text || '（空文件）') +
              (d.truncated ? '\n\n……（已截断，完整内容看文件）' : '');
            dmFit.fit();
          });
        }

        function fetchList() {
          api.get('/api/webcurate/runs').then(function (d) {
            if (GEN !== collect.gen) return;
            if (!d || d.ok === false || d.error) {
              status.classList.add('rs-err');
              status.textContent = '读取产物失败：' + ((d && d.error) || '未知错误');
              if (DATA.tbody) DATA.tbody.style.visibility = '';
              return;
            }
            status.textContent = '';
            DATA.items = d.items || [];
            DATA.stale = false;
            /* 已经不在列表里的选择项清掉（比如刚删过） */
            var live = {};
            DATA.items.forEach(function (it) { live[it.name] = 1; });
            Object.keys(DATA.sel).forEach(function (k) { if (!live[k]) delete DATA.sel[k]; });
            renderRows();
            window.__EAI_WC_LAST = DATA.items.slice();          /* 留给下次进页面先铺 */
            if (DATA.tbody) DATA.tbody.style.visibility = '';   /* 第一份数据到位才露出来 */
          });
        }

        DATA.reload = fetchList;      /* 供 pickTab 在「有新产物」时刷新 */
        fetchList();
      }

      /* 三个面板先建好（切页签秒开），再按上次停留的页签显示；首次进「模型配置」 */
      loadConfig();
      loadCollect();
      loadData();
      pickTab(getLastTab() || 'config');
    },
  });
})();
