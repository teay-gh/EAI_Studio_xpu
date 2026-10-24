/* ============================================================
   托管（工作台分组）· 渲染层（用户 2026-10-17）
   ------------------------------------------------------------
   一页三段，按流程从左到右竖排：
     ① 目标：描述你想要的对话模型 → [生成方案]
     ② 方案：托管 AI 给的步骤/参数（可改）+ 预估（时间·存储·内存/显存·token）→ [确认开始托管]
     ③ 运行：步骤时间线 + 托管 AI 的放行/中止记录 + 事件流；跑完出「结论」
   护栏都在后端（bridge_api/host.py）：切任务前给 AI 2 分钟分析、勾了「强制托管训练完成」
   只提示不中止、5 分钟无进度判卡死可停；本页只负责展示与编辑，不自己下判断。
   token 一律走设置里的默认 API，本页不提供任何 api 输入框。
   ============================================================ */
(function () {
  'use strict';

var STEP_TXT = { pending: '待执行', running: '进行中', done: '完成', failed: '失败', stopped: '已停止' };
/* 目标模型示例（用户 2026-10-18，2026-10-20 扩成一组）：placeholder 用第一条，
   「填充示例主题」每点一次换下一条（第一次点出来的就是 placeholder 那句，之后依次变换）。
   全部是通用方向，不涉及具体公司/产品/事件。 */
/* 「填充示例主题」的示例（用户 2026-10-20）：每条是 [中文, 英文] 一对，
   点按钮时按当前界面语言取 —— 切语言不会改写已经填进框里的内容，只有再点一次才换成对应语言。
   全部是自然科学方向的模型需求，不涉及任何国家的历史与文化。 */
var BRIEF_SAMPLES = [
  ['一个懂基础物理的对话模型，回答简洁、公式与单位准确，能在本机上跑',
   'A chat model that knows basic physics: brief answers, accurate formulas and units, runs locally'],
  ['一个天文知识助手，把天体现象讲得通俗准确，不确定就直说',
   'An astronomy helper: plain and accurate explanations of celestial phenomena, says so when unsure'],
  ['一个生物医学科普助手，把发病机制讲成通俗语言，不给诊疗建议',
   'A biomedical explainer: turns mechanisms into plain language, never gives medical advice'],
  ['一个数学解题助手，分步骤推导并说明每步依据，最后给出简明答案',
   'A math solver: step-by-step derivation with the reason for each step, ending in a concise answer'],
  ['一个化学实验助手，讲清试剂特性与安全要点，步骤条理分明',
   'A chemistry lab assistant: clear reagent properties and safety notes, well-ordered steps'],
  ['一个地球科学助手，解释地质与气象现象，数据与单位准确',
   'An earth-science assistant: explains geological and weather phenomena with accurate data and units'],
  ['一个机器学习助教，用小例子讲清概念，示例代码能直接运行',
   'A machine-learning tutor: explains concepts with small examples, sample code runs as-is'],
  ['一个数据与统计助手，讲清指标含义与适用场景，公式标注清楚',
   'A statistics assistant: explains metrics and when to use them, formulas spelled out']
];
var BRIEF_SAMPLE = BRIEF_SAMPLES[0][0];   /* 兼容旧引用 */
var SAMPLE_CURSOR = 0;
/* 当前语言在示例对里的下标：0=中文，1=英文 */
function sampleLangIdx() { return (window.I18N && window.I18N.lang() === 'en') ? 1 : 0; }

var TIMER = null;
var MACHINE_TIMER = null;   /* 本机状态 5s 轮询（用户 2026-10-18） */
var CURSOR = 0;
var CUR = null;          /* 最近一次 state：方案/步骤/决策都从它来 */
var EVENTS = [];         /* 事件流累积（轮询是增量的） */

function el(api, tag, cls, txt) { return api.el(tag, cls, txt); }

function field(api, label, node, hint) {
  var row = el(api, 'div', 'ho-field');
  row.appendChild(el(api, 'label', 'ho-label', label));
  var wrap = el(api, 'div', 'ho-inputwrap');
  wrap.appendChild(node);
  if (hint) wrap.appendChild(el(api, 'div', 'ho-hint', hint));
  row.appendChild(wrap);
  return row;
}

function numIn(api, path, val, step, min, max) {
  var i = el(api, 'input', 'ho-in ho-num');
  i.type = 'number';
  i.dataset.path = path;
  i.value = val == null ? '' : val;
  if (step) i.step = step;
  if (min != null) i.min = min;
  if (max != null) i.max = max;
  return i;
}

function txtIn(api, path, val, rows) {
  if (rows) {
    var t = el(api, 'textarea', 'ho-in ho-area');
    t.dataset.path = path;
    t.rows = rows;
    t.value = val || '';
    return t;
  }
  var i = el(api, 'input', 'ho-in');
  i.type = 'text';
  i.dataset.path = path;
  i.value = val == null ? '' : val;
  return i;
}

/* 本机瓷片体积：直接产出终态文本（用户 2026-10-18）
   原来返回「525.0 MB」（数字与单位中间带空格），而 api.fitStats() 会在挂载后 80ms
   把同一段文本重写成「525.0MB」→ 单位往左跳一格，看起来就是「框内单位闪一下」。
   这里一次就按 fitStats 的口径（紧凑单位、无空格、≥100 不带小数）输出，首帧即终态。 */
function fmtMb(v) {
  if (v == null || v === '') return '—';
  var n = Number(v) || 0;
  var api = window.__EAI_API;
  function cm(x) {
    return (api && api.compactNum) ? api.compactNum(x)
      : (x >= 100 ? String(Math.round(x)) : String(Math.round(x * 10) / 10));
  }
  return n >= 1024 ? cm(n / 1024) + 'GB' : cm(n) + 'MB';
}

function stat(api, label, value, cls) {
  var box = el(api, 'div', 'ho-stat');
  box.appendChild(el(api, 'div', 'ho-stat-label', label));
  var v = el(api, 'div', 'ho-stat-val' + (cls ? ' ' + cls : ''), value);
  box.appendChild(v);
  return box;
}

/* 把界面上的可编辑字段写回方案对象（key 用 a.b.c 路径） */
function collectPlan(root, plan) {
  [].forEach.call(root.querySelectorAll('[data-path]'), function (n) {
    var p = n.dataset.path.split('.');
    var obj = plan;
    for (var i = 0; i < p.length - 1; i++) { obj = obj[p[i]] = obj[p[i]] || {}; }
    var key = p[p.length - 1];
    var v = n.value;
    if (n.type === 'number') {
      v = v === '' ? 0 : Number(v);
      if (isNaN(v)) v = 0;
    }
    if (n.dataset.bool === '1') v = !!n.checked;
    obj[key] = v;
  });
  return plan;
}

function fmtMin(m) {
  m = Number(m || 0);
  if (m >= 60) return (m / 60).toFixed(1) + ' 小时';
  return m + ' 分钟';
}

function fmtTok(t) {
  t = Number(t || 0);
  if (t >= 1000) return (t / 1000).toFixed(1) + 'k';
  return String(t);
}

EAIPages.register('host', {
  title: '托管',

  mock: {
    '/api/host/state': { ok: true, running: false, phase: 'idle', events: [], next: 0,
      plan: null, steps: [], decisions: [], report: '', force: false,
      api: { base_url: 'https://api.deepseek.com', model: 'deepseek-chat', key_set: true } },
  },

  mount: function (root, api) {
    CURSOR = 0; EVENTS = []; CUR = null;

    var hero = api.hero('托管');
    hero.classList.add('ho-hero');      /* 没有副标题了，页头不用再留 92px 空带 */
    root.appendChild(hero);

    /* 四块卡统一装一层容器：卡片之间要留间距（用户 2026-10-18） */
    var page = el(api, 'div', 'ho-page');
    root.appendChild(page);

    /* ---------------- ⓪ 本机状态（用户 2026-10-18：放目标模型上方，样式照训练页瓷片） ---------------- */
    function statTile(label) {
      var st = el(api, 'div', 'stat');
      st.appendChild(el(api, 'div', 'stat-label', label));
      var v = el(api, 'div', 'stat-val', '');
      st.appendChild(v);
      return { el: st, val: v };
    }
    var statsM = el(api, 'div', 'stats');
    var tEai = statTile('占用内存');
    var tFree = statTile('剩余内存');
    var tDev = statTile('训练设备');
    var tProj = statTile('占用存储');   /* 文案（用户 2026-10-20）：本工程占用 → 占用存储 */
    [tEai, tFree, tDev, tProj].forEach(function (t) { statsM.appendChild(t.el); });
    page.appendChild(statsM);

    function paintMachine(m) {
      if (!m || m.ok === false) return;
      tEai.val.textContent = m.eai_mb == null ? '—' : fmtMb(m.eai_mb);
      tFree.val.textContent = m.free_mb == null ? '—' : fmtMb(m.free_mb);
      tDev.val.textContent = (m.device || '—').toUpperCase();
      tProj.val.textContent = fmtMb(m.project_mb);
      tProj.el.title = m.project_root || '';
    }
    function loadMachine() {
      api.get('/api/host/machine').then(function (m) { paintMachine(m); });
    }
    loadMachine();
    MACHINE_TIMER = setInterval(loadMachine, 5000);

    /* ---------------- ① 目标 ---------------- */
    var c1 = api.card('目标模型');
    var brief = el(api, 'textarea', 'ho-in ho-area ho-brief');
    brief.rows = 4;
    /* 提示语与「填充示例主题」写的是同一句（用户 2026-10-18：打开看到的和点示例后的必须一致），
       主题只到「基础物理」这种笼统说法，示例是自然科学方向，不涉及任何国家的历史与文化 */
    brief.placeholder = (sampleLangIdx() === 1 ? 'e.g. ' : '例如：') + BRIEF_SAMPLES[0][sampleLangIdx()];
    c1.body.appendChild(brief);

    var row1 = el(api, 'div', 'ho-row');
    var bPlan = el(api, 'button', 'btn', '生成方案');
    bPlan.type = 'button';
    row1.appendChild(bPlan);
    /* 填充示例（用户 2026-10-18）：右边给一条中国历史方向的示例描述，点了直接写进目标框 */
    /* 文案（用户 2026-10-20）：叫「填充示例主题」，和两个蒸馏页一致 */
    var bSample = el(api, 'button', 'btn', '填充示例主题');
    bSample.type = 'button';
    bSample.addEventListener('click', function () {
      /* 每点一次换一条（用户 2026-10-20：点击示例会变换示例）；点到头再回第一条 */
      brief.value = BRIEF_SAMPLES[SAMPLE_CURSOR % BRIEF_SAMPLES.length][sampleLangIdx()];
      SAMPLE_CURSOR += 1;
      bSample.blur();
    });
    row1.appendChild(bSample);
    c1.body.appendChild(row1);
    page.appendChild(c1);

    /* ---------------- ② 方案 ---------------- */
    var c2 = api.card('方案与预估');
    c2.classList.add('ho-hide');
    page.appendChild(c2);

    /* ---------------- ③ 运行 ---------------- */
    var c3 = api.card('托管进度', '');
    c3.classList.add('ho-hide');
    var timeline = el(api, 'div', 'ho-timeline');
    c3.body.appendChild(timeline);
    var decBox = el(api, 'div', 'ho-decisions');
    c3.body.appendChild(decBox);
    var row3 = el(api, 'div', 'ho-row ho-runbar');
    /* 按钮带图标（用户 2026-10-18）：暂停 / 开始 / 取消 / 查看方案，图标照训练页那套 */
    function runBtn(cls, paths, text) {
      var b = el(api, 'button', 'btn ' + cls, '');
      b.type = 'button';
      var ic = el(api, 'span', 'ho-btn-ic', '');
      ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + '</svg>';
      b.appendChild(ic);
      b.appendChild(el(api, 'span', 'ho-btn-txt', text));
      return b;
    }
    var ICON_PAUSE = '<path d="M9.2 2.4v19.2"/><path d="M14.8 2.4v19.2"/>';
    var ICON_PLAY = '<path d="M8.4 5.4l9.8 6.6-9.8 6.6z" fill="currentColor" stroke="none"/>';
    var ICON_STOP = '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>';
    var ICON_DOC = '<path d="M7 3.6h6.6l3.8 3.8v13H7z"/><path d="M13.6 3.6v3.8h3.8"/>'
      + '<path d="M9.6 12.4h6"/><path d="M9.6 16.2h6"/>';
    var bPause = runBtn('', ICON_PAUSE, '暂停');
    var bResume = runBtn('', ICON_PLAY, '开始');       /* 暂停后点它继续跑 */
    var bStop = runBtn('btn-stop', ICON_STOP, '取消');
    var bPlanView = runBtn('', ICON_DOC, '查看方案');
    bPause.disabled = true;
    bStop.disabled = true;
    bPlanView.disabled = true;
    bResume.classList.add('ho-hide');
    row3.appendChild(bPause);
    row3.appendChild(bResume);
    row3.appendChild(bStop);
    row3.appendChild(bPlanView);
    c3.body.appendChild(row3);
    page.appendChild(c3);

    /* ---------------- ③b 运行日志 | Loss 曲线（同一行左右各半，用户 2026-10-18：不要 Loss 表） ---------------- */
    var runRow = el(api, 'div', 'ho-logrow');
    runRow.classList.add('ho-hide');

    var c5 = api.card('运行日志', '');
    var logBox = el(api, 'div', 'ho-logbox');
    var logPre = el(api, 'pre', 'ho-log ho-log-live');
    logPre.textContent = '暂无运行日志';
    logBox.appendChild(logPre);
    c5.body.appendChild(logBox);
    runRow.appendChild(c5);

    var c7 = api.card('Loss 曲线', '');
    var chartHost = el(api, 'div', 'ho-chart');
    c7.body.appendChild(chartHost);
    runRow.appendChild(c7);

    page.appendChild(runRow);

    /* 这一行跟着「托管进度」一起出现（都属运行期） */
    function showRunCards(v) {
      runRow.classList.toggle('ho-hide', !v);
    }
    var LOG_LINES = 0;

    /* CSV 尾部 → 曲线点（列按表头取，别按「每行最后一个数字」，那是 elapsed） */
    function parseLossCsv(text) {
      var LF = String.fromCharCode(10), CR = String.fromCharCode(13);
      var lines = String(text || '').split(LF).map(function (x) { return x.replace(CR, ''); })
        .filter(function (x) { return x.trim(); });
      if (lines.length < 2) return [];
      var head = lines[0].toLowerCase().split(',').map(function (x) { return x.trim(); });
      function col() {
        for (var a = 0; a < arguments.length; a++) {
          var k = head.indexOf(arguments[a]);
          if (k >= 0) return k;
        }
        return -1;
      }
      var iS = col('step'), iT = col('train_loss', 'loss'), iV = col('val_loss', 'val');
      var pts = [];
      for (var n = 1; n < lines.length; n++) {
        var c = lines[n].split(',');
        var st = Number(c[iS]), tr = Number(c[iT]);
        if (!isFinite(st) || !isFinite(tr)) continue;
        var vv = iV >= 0 ? Number(c[iV]) : NaN;
        pts.push({ step: st, train: tr, val: isFinite(vv) ? vv : null });
      }
      return pts;
    }

    /* 画法与训练页一致：600×180 viewBox、三条网格、train=ACCENT / val=OK */
    function drawChart(points) {
      chartHost.textContent = '';
      if (!points || points.length < 2) {
        chartHost.appendChild(el(api, 'div', 'ho-chart-empty', '暂无足够数据点'));
        return;
      }
      var W = 600, H = 180, pad = 30;
      var xs = points.map(function (p) { return p.step; });
      var tr = points.map(function (p) { return p.train; });
      var va = points.map(function (p) { return p.val == null ? p.train : p.val; });
      var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
      var ymin = Math.min.apply(null, tr.concat(va));
      var ymax = Math.max.apply(null, tr.concat(va));
      if (xmax === xmin) xmax = xmin + 1;
      if (ymax === ymin) ymax = ymin + 1;
      function X(x) { return pad + (x - xmin) / (xmax - xmin) * (W - 2 * pad); }
      function Y(y) { return H - pad - (y - ymin) / (ymax - ymin) * (H - 2 * pad); }
      function poly(arr) {
        return arr.map(function (v, i) { return X(xs[i]).toFixed(1) + ',' + Y(v).toFixed(1); }).join(' ');
      }
      var grid = '';
      for (var i = 0; i < 4; i++) {
        var gy = (pad + i * (H - 2 * pad) / 3).toFixed(1);
        grid += '<line x1="' + pad + '" y1="' + gy + '" x2="' + (W - pad) + '" y2="' + gy +
          '" stroke="var(--LINE_SOFT)" stroke-width="1"/>';
      }
      chartHost.innerHTML =
        '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="180" ' +
        'preserveAspectRatio="none" role="img" aria-label="Loss 曲线">' + grid +
        '<polyline fill="none" stroke="var(--ACCENT)" stroke-width="2" points="' + poly(tr) + '"/>' +
        '<polyline fill="none" stroke="var(--OK)" stroke-width="2" points="' + poly(va) + '"/>' +
        '<text x="' + pad + '" y="14" fill="var(--INK3)" font-size="9">' +
        '<tspan fill="var(--ACCENT)">—— train</tspan>    ' +
        '<tspan fill="var(--OK)">—— val</tspan></text>' +
        '<text x="' + (W - 8) + '" y="' + Y(ymax).toFixed(1) +
        '" text-anchor="end" fill="var(--INK3)" font-size="9">' + ymax.toFixed(2) + '</text>' +
        '<text x="' + (W - 8) + '" y="' + Y(ymin).toFixed(1) +
        '" text-anchor="end" fill="var(--INK3)" font-size="9">' + ymin.toFixed(2) + '</text>' +
        '</svg>';
    }
    drawChart([]);

    /* ---------------- 小步明细弹窗（用户 2026-10-18：四个大步骤样式不动，点开看小步） ---------------- */
    var subMask = el(api, 'div', 'ho-sub-mask ho-hide');
    var subCard = el(api, 'div', 'ho-sub-card');
    var subHead = el(api, 'div', 'ho-sub-head');
    var subTitle = el(api, 'span', 'ho-sub-title', '');
    var subX = el(api, 'button', 'ho-sub-x', '');
    subX.type = 'button';
    subX.setAttribute('aria-label', '关闭');
    subX.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
    subHead.appendChild(subTitle);
    subHead.appendChild(subX);
    /* 蒸馏 / 训练用「小步卡片 + 状态球」上下排布（用户 2026-10-18），其他步骤仍旧看日志行 */
    var SUB_STAGES = { distill: 1, train: 1 };
    var subList = el(api, 'div', 'ho-sub-list ho-hide');
    var subPre = el(api, 'pre', 'ho-sub-pre', '暂无明细');
    subCard.appendChild(subHead);
    subCard.appendChild(subList);
    subCard.appendChild(subPre);
    subMask.appendChild(subCard);
    page.appendChild(subMask);

    var DOT_TEXT = { idle: '未运行', run: '运行中', ok: '完成', err: '出错' };
    var subKey = '';
    function openSub(key, label) {
      subKey = key || '';
      subTitle.textContent = (label || key);      /* 标题里不再带「明细」（用户 2026-10-18） */
      var staged = !!SUB_STAGES[key];
      subList.classList.toggle('ho-hide', !staged);
      subPre.classList.toggle('ho-hide', staged);
      subMask.classList.remove('ho-hide');
      renderSub(CUR);
    }
    function closeSub() {
      subKey = '';
      subMask.classList.add('ho-hide');
    }
    function renderSub(s) {
      if (!subKey) return;
      if (SUB_STAGES[subKey]) {
        var rows = ((s && s.stages) || {})[subKey] || [];
        subList.innerHTML = '';
        if (!rows.length) {
          subList.appendChild(el(api, 'div', 'ho-sub-empty', '这一步还没开始'));
          return;
        }
        rows.forEach(function (r, i) {
          var state = r.state || 'idle';
          var row = el(api, 'div', 'ho-sub-row is-' + state);
          row.appendChild(el(api, 'span', 'ho-sub-dot'));
          row.appendChild(el(api, 'span', 'ho-sub-no', String(i + 1)));
          row.appendChild(el(api, 'span', 'ho-sub-label', r.label || ''));
          /* 训练这一步只留「步骤名 + 状态球」，右侧细节（lora · LoRA / 已写 N 条 step 记录）不显示（用户 2026-10-18） */
          if (r.note && subKey !== 'train') row.appendChild(el(api, 'span', 'ho-sub-note', r.note));
          row.appendChild(el(api, 'span', 'ho-sub-state', DOT_TEXT[state] || '未运行'));
          subList.appendChild(row);
        });
        return;
      }
      var lines = ((s && s.substeps) || {})[subKey] || [];
      subPre.textContent = lines.length ? lines.join(String.fromCharCode(10))
        : '暂无明细（这一步还没开始，或已经开始但还没有记录）';
      subPre.scrollTop = subPre.scrollHeight;
    }
    subX.addEventListener('click', closeSub);
    subMask.addEventListener('click', function (e) { if (e.target === subMask) closeSub(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSub(); });

    /* ---------------- ④ 结论 ---------------- */
    var c4 = api.card('结论', '');
    c4.classList.add('ho-hide');
    var rep = el(api, 'pre', 'ho-report');
    c4.body.appendChild(rep);
    var repPath = el(api, 'div', 'ho-hint', '');
    c4.body.appendChild(repPath);
    page.appendChild(c4);

    /* ================= 渲染方案 ================= */
    function renderPlan(plan) {
      if (plan && Object.keys(plan).length) bPlanView.disabled = false;   /* 有方案就能回看（用户 2026-10-18） */
      c2.body.innerHTML = '';
      var sum = el(api, 'div', 'ho-sum', plan.summary || '');
      c2.body.appendChild(sum);

      var g1 = el(api, 'div', 'ho-group');
      g1.appendChild(el(api, 'div', 'ho-gtitle', '数据蒸馏'));
      var reuseName = String((plan.dataset || {}).reuse || '');
      g1.appendChild(el(api, 'div', 'ho-hint', reuseName
        ? ('数据集来源：复用仓库里的「' + reuseName + '」')
        : '数据集来源：本轮新蒸馏'));
      g1.appendChild(field(api, '主题', txtIn(api, 'dataset.topic', (plan.dataset || {}).topic, 3)));
      var r1 = el(api, 'div', 'ho-grid2');
      r1.appendChild(field(api, '条数', numIn(api, 'dataset.count', (plan.dataset || {}).count, 10, 20, 5000)));
      r1.appendChild(field(api, '并发', numIn(api, 'dataset.concurrency', (plan.dataset || {}).concurrency, 1, 1, 8)));
      g1.appendChild(r1);
      g1.appendChild(field(api, '数据集名', txtIn(api, 'dataset.name', (plan.dataset || {}).name)));
      c2.body.appendChild(g1);

      var g2 = el(api, 'div', 'ho-group');
      var h2 = el(api, 'div', 'ho-ghead');
      h2.appendChild(el(api, 'div', 'ho-gtitle', '网页整理'));
      var sw = el(api, 'button', 'switch' + ((plan.web || {}).enabled ? ' on' : ''), '');
      sw.type = 'button';
      sw.setAttribute('role', 'switch');
      sw.setAttribute('aria-checked', (plan.web || {}).enabled ? 'true' : 'false');
      var knob = el(api, 'span', 'knob');
      sw.appendChild(knob);
      h2.appendChild(sw);
      g2.appendChild(h2);
      var webBody = el(api, 'div', 'ho-webbody');
      webBody.appendChild(field(api, '主题', txtIn(api, 'web.topic', (plan.web || {}).topic)));
      var r2 = el(api, 'div', 'ho-grid3');
      r2.appendChild(field(api, '轮次', numIn(api, 'web.rounds', (plan.web || {}).rounds, 1, 1, 6)));
      r2.appendChild(field(api, '每轮查询', numIn(api, 'web.queries_per_round', (plan.web || {}).queries_per_round, 1, 1, 12)));
      r2.appendChild(field(api, '每查询页数', numIn(api, 'web.pages_per_query', (plan.web || {}).pages_per_query, 1, 1, 8)));
      webBody.appendChild(r2);
      webBody.appendChild(field(api, '目标字数', numIn(api, 'web.target_chars', (plan.web || {}).target_chars, 500, 400, 60000)));
      g2.appendChild(webBody);
      c2.body.appendChild(g2);

      var g3 = el(api, 'div', 'ho-group');
      g3.appendChild(el(api, 'div', 'ho-gtitle', '训练'));
      /* 托管不允许加载权重微调（用户 2026-10-18）：训练方式固定预训练，从 0 开始 */
      g3.appendChild(el(api, 'div', 'ho-hint', '训练方式：预训练（从 0 开始，不加载任何已有权重）'));
      var r3 = el(api, 'div', 'ho-grid3');
      r3.appendChild(field(api, '步数', numIn(api, 'train.steps', (plan.train || {}).steps, 100, 100, 60000)));
      r3.appendChild(field(api, '学习率', numIn(api, 'train.lr', (plan.train || {}).lr, 0.0001, 0.00001, 1)));
      r3.appendChild(field(api, 'batch', numIn(api, 'train.batch_size', (plan.train || {}).batch_size, 1, 1, 128)));
      g3.appendChild(r3);
      g3.appendChild(field(api, '验证样本', numIn(api, 'validate.samples', (plan.validate || {}).samples, 1, 1, 64)));
      c2.body.appendChild(g3);

      /* 预估四块 */
      var est = plan.estimate || {};
      var stats = el(api, 'div', 'ho-stats');
      stats.appendChild(stat(api, '预计耗时', fmtMin(est.minutes)));
      stats.appendChild(stat(api, '存储', (est.disk_mb || 0) + ' MB'));
      stats.appendChild(stat(api, '内存/显存峰值', (est.mem_peak_mb || 0) + ' MB'));
      stats.appendChild(stat(api, 'token 消耗', fmtTok(est.tokens)));
      c2.body.appendChild(stats);
      if (est.note) c2.body.appendChild(el(api, 'div', 'ho-hint', est.note));

      if ((plan.risks || []).length) {
        var rk = el(api, 'div', 'ho-risks');
        rk.appendChild(el(api, 'div', 'ho-gtitle', '风险提示'));
        plan.risks.forEach(function (r) { rk.appendChild(el(api, 'div', 'ho-risk', '· ' + r)); });
        c2.body.appendChild(rk);
      }

      var row2 = el(api, 'div', 'ho-row');
      var bGo = el(api, 'button', 'btn ho-go', '确认开始托管');
      bGo.type = 'button';
      var bRegen = el(api, 'button', 'btn2', '重新生成');
      bRegen.type = 'button';
      row2.appendChild(bGo);
      row2.appendChild(bRegen);
      c2.body.appendChild(row2);

      var forceNote = el(api, 'div', 'ho-hint ho-force', '');
      c2.body.appendChild(forceNote);

      /* 开关：网页整理 */
      function setWeb(on) {
        plan.web = plan.web || {};
        plan.web.enabled = !!on;
        sw.classList.toggle('on', !!on);
        sw.setAttribute('aria-checked', on ? 'true' : 'false');
        webBody.style.display = on ? '' : 'none';
      }
      sw.addEventListener('click', function () { setWeb(!((plan.web || {}).enabled)); });
      setWeb(!!(plan.web || {}).enabled);

      bRegen.addEventListener('click', function () {
        c2.classList.add('ho-hide');
        post('/api/host/plan', { brief: brief.value.trim() }).then(afterPlan).catch(showErr(c2.body));
      });

      bGo.addEventListener('click', function () {
        var p = collectPlan(c2, JSON.parse(JSON.stringify(plan)));
        if (!String((p.dataset || {}).topic || '').trim()) { flash(brief, '先填数据蒸馏主题'); return; }
        bGo.disabled = true;
        post('/api/host/confirm', { plan: p }).then(function (r) {
          bGo.disabled = false;
          if (!r || !r.ok) { warnRow(row2, (r && r.error) || '启动失败'); return; }
          EVENTS = []; CURSOR = 0;
          c2.classList.add('ho-hide');
          startPolling();
        }).catch(function (e) {
          bGo.disabled = false;
          warnRow(row2, '启动失败：' + e.message);
        });
      });

      c2.classList.remove('ho-hide');
      c2.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function warnRow(row, text) {
      var old = row.querySelector('.ho-err');
      if (old) old.remove();
      row.appendChild(el(api, 'div', 'ho-err', text));
    }

    function flash(node, text) {
      node.classList.add('ho-bad');
      node.placeholder = text;
      node.focus();
      setTimeout(function () { node.classList.remove('ho-bad'); }, 900);
    }

    function showErr(host) {
      return function (e) {
        var old = host.querySelector('.ho-err');
        if (old) old.remove();
        host.appendChild(el(api, 'div', 'ho-err', (e && e.message) || '请求失败'));
      };
    }

    function post(path, body) { return api.post(path, body || {}); }

    function afterPlan(r) {
      if (!r || !r.ok) {
        warnRow(c1.querySelector('.ho-row'), (r && r.error) || '方案生成失败');
        return;
      }
      CUR = { plan: r.plan };
      renderPlan(r.plan);
    }

    bPlan.addEventListener('click', function () {
      var briefTxt = brief.value.trim();
      if (!briefTxt) { flash(brief, '先描述你想要的模型'); return; }
      bPlan.disabled = true;
      bPlan.textContent = '分析中…';
      var old = c1.querySelector('.ho-err');
      if (old) old.remove();
      post('/api/host/plan', { brief: briefTxt }).then(function (r) {
        bPlan.disabled = false;
        bPlan.textContent = '生成方案';
        afterPlan(r);
      }).catch(function (e) {
        bPlan.disabled = false;
        bPlan.textContent = '生成方案';
        warnRow(c1.querySelector('.ho-row'), '方案生成失败：' + e.message);
      });
    });

    /* ================= 运行视图 ================= */
    function renderSteps(steps) {
      timeline.innerHTML = '';
      (steps || []).forEach(function (s, i) {
        var chip = el(api, 'div', 'ho-step ho-' + s.state);
        chip.setAttribute('role', 'button');
        chip.tabIndex = 0;
        chip.title = '查看这一步的明细';
        chip.appendChild(el(api, 'span', 'ho-step-i', String(i + 1)));
        chip.appendChild(el(api, 'span', 'ho-step-t', s.label));
        chip.appendChild(el(api, 'span', 'ho-step-s', STEP_TXT[s.state] || s.state));
        chip.appendChild(el(api, 'span', 'ho-step-more', '明细 ›'));
        chip.addEventListener('click', function () { openSub(s.key, s.label); });
        chip.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSub(s.key, s.label); }
        });
        timeline.appendChild(chip);
      });
    }

    function renderDecisions(list) {
      if (!list || !list.length) { decBox.innerHTML = ''; return; }
      decBox.innerHTML = '';
      decBox.appendChild(el(api, 'div', 'ho-gtitle', '托管 AI 的决策'));
      list.forEach(function (d) {
        var row = el(api, 'div', 'ho-dec');
        var head = el(api, 'div', 'ho-dec-head',
          d.at + ' · ' + (d.decision === 'abort' ? '中止' : '放行') + ' · ' + d.waited_s + 's' +
          (d.forced ? ' · 强制模式仅提示' : ''));
        row.appendChild(head);
        row.appendChild(el(api, 'div', 'ho-dec-body', d.reason || ''));
        decBox.appendChild(row);
      });
    }

    function pushEvents(events) {
      if (!events || !events.length) return;
      events.forEach(function (e) { EVENTS.push(e); });
      if (EVENTS.length > 400) EVENTS = EVENTS.slice(-400);
      /* 事件只进「运行日志」卡（用户 2026-10-18：托管进度里那口实时框与它重复，已撤） */
      if (LOG_LINES === 0) logPre.textContent = '';
      events.forEach(function (e) {
        var t = (e.payload && (e.payload.text || e.payload.msg)) || '';
        if (!t) return;
        logPre.textContent += '[' + ((e.payload && e.payload.t) || '') + '] ' + t + '\n';
        LOG_LINES += 1;
      });
      logPre.scrollTop = logPre.scrollHeight;
    }

    function startPolling() {
      c3.classList.remove('ho-hide');
      showRunCards(true);
      c4.classList.add('ho-hide');
      EVENTS = []; CURSOR = 0; LOG_LINES = 0; logPre.textContent = '暂无运行日志';
      if (TIMER) clearInterval(TIMER);
      tick();
      TIMER = setInterval(tick, 900);
    }

    function tick() {
      api.get('/api/host/state?since=' + CURSOR).then(function (s) {
        if (!s || !s.ok) return;
        CUR = s;
        if (s.next) CURSOR = s.next;
        pushEvents(s.events);
        renderSteps(s.steps);
        if (subKey) renderSub(s);            /* 明细弹窗开着就跟着刷新（用户 2026-10-18） */
        renderDecisions(s.decisions);
        syncRunBtns(s);
        var c3sub = c3.querySelector('.card-sub');
        if (c3sub) {
          c3sub.textContent = s.running ? '运行中 · 只有上一件事做完才叫托管 AI 分析下一步'
                                       : (s.phase === 'done' ? '已完成' : (s.phase === 'stopped' ? '已停止' : (s.phase === 'failed' ? '失败' : '')));
        }
        var forceEl = c2.querySelector('.ho-force');
        if (forceEl) forceEl.textContent = s.force ? '已勾选「强制托管训练完成」：托管 AI 只提示风险，不会中止（卡死仍会停）' : '';
        if (s.report) {
          rep.textContent = s.report;
          c4.classList.remove('ho-hide');
        }
        if (!s.running) {
          if (TIMER) { clearInterval(TIMER); TIMER = null; }
          bPlan.disabled = false;
          if (s.plan && s.plan.brief) brief.value = s.plan.brief;
        }
        if (s.error) {
          var old = c3.querySelector('.ho-err');
          if (!old) c3.body.appendChild(el(api, 'div', 'ho-err', s.error));
        }
      }).catch(function () { /* bridge 短暂不可用：下一轮再取 */ });
    }

    function syncRunBtns(s) {
      var running = !!(s && s.running);
      var paused = !!(s && s.paused);
      bPause.disabled = !running || paused;
      bResume.disabled = !running || !paused;
      bStop.disabled = !running;
      bPause.classList.toggle('ho-hide', paused);
      bResume.classList.toggle('ho-hide', !paused);
      bPlanView.disabled = !(s && s.plan && Object.keys(s.plan).length);
    }
    bPause.addEventListener('click', function () {
      bPause.disabled = true;
      post('/api/host/pause', {}).then(function () { /* 等轮询把状态带回来 */ });
    });
    bResume.addEventListener('click', function () {
      bResume.disabled = true;
      post('/api/host/resume', {}).then(function () { /* 同上 */ });
    });
    bStop.addEventListener('click', function () {
      bStop.disabled = true;
      post('/api/host/stop', {}).then(function () { /* 等轮询把状态带回来 */ });
    });

    /* ---------------- 查看方案：把 AI 给的方案再摊开看一遍（用户 2026-10-18） ---------------- */
    var planMask = el(api, 'div', 'ho-plan-mask ho-hide');
    var planCard = el(api, 'div', 'ho-plan-card');
    var planHead = el(api, 'div', 'ho-plan-head');
    planHead.appendChild(el(api, 'span', 'ho-plan-title', 'AI 方案'));
    var planX = el(api, 'button', 'ho-sub-x', '');
    planX.type = 'button';
    planX.setAttribute('aria-label', '关闭');
    planX.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>';
    planHead.appendChild(planX);
    var planBody = el(api, 'div', 'ho-plan-body');
    planCard.appendChild(planHead);
    planCard.appendChild(planBody);
    planMask.appendChild(planCard);
    page.appendChild(planMask);

    function planRows(pl) {
      var ds = pl.dataset || {}, web = pl.web || {}, tr = pl.train || {},
          va = pl.validate || {}, est = pl.estimate || {};
      var KIND_TXT = { pretrain: '预训练', continue: '继续预训练', sft: '指令微调', lora: 'LoRA 微调' };
      return [
        ['数据蒸馏', [
          ['主题', ds.topic], ['条数', ds.count], ['并发', ds.concurrency], ['数据集名', ds.name],
        ]],
        ['网页整理', [
          ['启用', web.enabled ? '是' : '否'], ['主题', web.topic], ['轮次', web.rounds],
          ['每次检索', web.queries_per_round], ['每词页数', web.pages_per_query],
          ['目标字数', web.target_chars], ['输出名', web.out_name],
        ]],
        ['训练', [
          ['方式', KIND_TXT[String(tr.kind || '').toLowerCase()] || tr.kind],
          ['基座', tr.base], ['步数', tr.steps], ['学习率', tr.lr], ['batch_size', tr.batch_size],
          ['LoRA r', String(tr.kind || '').toLowerCase() === 'lora' ? tr.lora_r : ''],
          ['缩放 α', String(tr.kind || '').toLowerCase() === 'lora' ? tr.lora_alpha : ''],
        ]],
        ['验证', [['样例数', va.samples]]],
        ['预估', [
          ['预计耗时', est.minutes == null ? '' : est.minutes + ' 分钟'],
          ['预计存储', est.disk_mb == null ? '' : est.disk_mb + ' MB'],
          ['内存/显存峰值', est.mem_peak_mb == null ? '' : est.mem_peak_mb + ' MB'],
          ['token 消耗', est.tokens == null ? '' : fmtTok(est.tokens)],
        ]],
      ];
    }
    function openPlanView(pl) {
      if (!pl) return;
      planBody.innerHTML = '';
      if (pl.summary) planBody.appendChild(el(api, 'div', 'ho-plan-sum', pl.summary));
      planRows(pl).forEach(function (group) {
        var sec = el(api, 'div', 'ho-plan-sec');
        sec.appendChild(el(api, 'div', 'ho-plan-gt', group[0]));
        group[1].forEach(function (row) {
          if (row[1] == null || row[1] === '') return;
          var r = el(api, 'div', 'ho-plan-row');
          r.appendChild(el(api, 'span', 'ho-plan-k', row[0]));
          r.appendChild(el(api, 'span', 'ho-plan-v', String(row[1])));
          sec.appendChild(r);
        });
        if (sec.querySelector('.ho-plan-v')) planBody.appendChild(sec);
      });
      var repo = pl.repo || {};
      var dsList = repo.datasets || [];
      if (dsList.length) {
        var rs = el(api, 'div', 'ho-plan-sec');
        rs.appendChild(el(api, 'div', 'ho-plan-gt', '仓库体检'));
        dsList.forEach(function (d) {
          var r = el(api, 'div', 'ho-plan-row');
          r.appendChild(el(api, 'span', 'ho-plan-k', d.usable ? '可用' : '条数不足'));
          r.appendChild(el(api, 'span', 'ho-plan-v', String(d.name || '') + ' · ' + String(d.lines || 0) + ' 条'));
          rs.appendChild(r);
        });
        planBody.appendChild(rs);
      }
      if ((pl.risks || []).length) {
        var rk = el(api, 'div', 'ho-plan-sec');
        rk.appendChild(el(api, 'div', 'ho-plan-gt', '风险与对策'));
        pl.risks.forEach(function (t) {
          rk.appendChild(el(api, 'div', 'ho-plan-risk', t));
        });
        planBody.appendChild(rk);
      }
      planMask.classList.remove('ho-hide');
      planBody.scrollTop = 0;
    }
    function closePlanView() { planMask.classList.add('ho-hide'); }
    planX.addEventListener('click', closePlanView);
    planMask.addEventListener('click', function (e) { if (e.target === planMask) closePlanView(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePlanView(); });
    bPlanView.addEventListener('click', function () { openPlanView((CUR && CUR.plan) || null); });

    /* 首屏：已有运行中的托管就直接接上。
       冷启动时 bridge 还没握手，api.get 会返回 {error} —— 必须重试，否则整页空着不再刷新（2026-10-18 实测） */
    function initialLoad(tries) {
    api.get('/api/host/state').then(function (s) {
      if ((!s || !s.ok) && (tries || 0) < 12) {
        setTimeout(function () { initialLoad((tries || 0) + 1); }, 400);
        return;
      }
      if (!s || !s.ok) return;
      CUR = s;                                    /* 冷启动也要能「查看方案」（用户 2026-10-18） */
      syncRunBtns(s);
      if (s.plan && s.plan.brief) brief.value = s.plan.brief;
      if (s.plan && !s.running && !s.report) renderPlan(s.plan);
      if (s.running || s.report || (s.steps || []).length) {
        CURSOR = s.next || 0;
        EVENTS = [];
        pushEvents(s.events);
        if (LOG_LINES === 0 && (s.log || []).length) {      /* 上一轮的日志：重启后从 last_run.json 补回来 */
          logPre.textContent = s.log.join('\n');
          LOG_LINES = s.log.length;
          logPre.scrollTop = logPre.scrollHeight;
        }
        renderSteps(s.steps);
        renderDecisions(s.decisions);
        c3.classList.remove('ho-hide');
        showRunCards(true);
        drawChart(parseLossCsv(s.train_log || ''));
        syncRunBtns(s);
        if (s.report) { rep.textContent = s.report; c4.classList.remove('ho-hide'); }
        if (s.running) { if (TIMER) clearInterval(TIMER); TIMER = setInterval(tick, 900); }
      }
    }).catch(function () {});
    }
    initialLoad(0);
  },

  unmount: function () {
    if (TIMER) { clearInterval(TIMER); TIMER = null; }
    if (MACHINE_TIMER) { clearInterval(MACHINE_TIMER); MACHINE_TIMER = null; }
    CURSOR = 0; EVENTS = []; CUR = null;
  },
});
/* 工程切换：丢掉上一个工程的缓存（用户 2026-10-18） */
(window.__EAI_API || {}).onProjectChange && window.__EAI_API.onProjectChange(function () { CUR = null; CURSOR = 0; if (TIMER) { clearInterval(TIMER); TIMER = null; } });

})();
