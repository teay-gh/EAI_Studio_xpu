/* ============================================================
   EAI_Studio · renderer/ai_prompts.js
   ------------------------------------------------------------
   AI 助手的开场提示（药丸）按「当前停在哪一页 / 哪一个小页签」给，
   不再是一套通用话术（用户 2026-10-17）。

   三条口径：
   1) 提示只针对用户正在看的页（含小页签）。页面键与 app.js 的 NAV 一致；
      小页签取值：article/response/webcurate = config|collect|data，
      dataset = 训练方式页签的中文标签（预训练 / 继续预训练 / 指令微调 / LoRA 微调）。
   2) 「切换到深色主题」只在总览页出现、只在现实时间夜间、平均每 10 次刷一次；
      「切换到浅色主题」只在总览页出现、只在白天、平均每 20 次刷一次（用户 2026-10-17）。
   3) 训练页的提示要带上真实体量：语料大小来自该页 config 的 outputs.corpus，
      初始/基座权重来自 outputs.init_from（裁切/量化是 outputs.weight），
      LoRA 合并来自 facts.base / facts.adapter。
   ============================================================ */
(function () {
  'use strict';

  /* ---------- 小工具 ---------- */
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (!n) return '';
    if (n >= 1048576) return (n / 1048576).toFixed(1) + 'MB';
    if (n >= 1024) return Math.round(n / 1024) + 'KB';
    return n + 'B';
  }
  function fmtNum(n) {
    n = Number(n) || 0;
    return n ? String(n) : '';
  }

  /* 当前小页签：优先 dataset.value（article/response/webcurate 的 config/collect/data），
     退回按钮文字（dataset 的四个训练方式页签没有 dataset.value） */
  function activeSub(pageEl, page) {
    if (!pageEl) return '';
    var seg = null;
    if (page === 'dataset') seg = pageEl.querySelector('.ds-seg-row .seg');
    if (!seg) seg = pageEl.querySelector('.seg[role="tablist"]');
    if (!seg) return '';
    var on = seg.querySelector('.seg-opt.on');
    return on ? String(on.dataset && on.dataset.value || on.textContent || '').trim() : '';
  }

  /* 训练页体量：各页 config 形状不同，这里归一成 {corpus, base, adapter}（字节） */
  function pickSizes(page, d) {
    d = d || {};
    var o = d.outputs || {};
    var f = d.facts || {};
    var num = function (x) { return (x && Number(x.size_bytes)) || 0; };
    var corpus = num(o.corpus);
    var base = num(o.init_from) || num(o.weight) || num(f.base);
    var adapter = num(f.adapter) || num(o.adapter);
    return { corpus: corpus, base: base, adapter: adapter };
  }

  /* ---------- 各页提示（不含主题那条，主题在 themeChip 里单独判） ---------- */
  var PAGES = {
    home: [
      '这个工程现在最该先做哪一步？',
      '数据、权重、训练进度各自到哪了？',
      '从零到能对话，最短的一条路怎么走？',
      '现在这些数据够训练吗？',
    ],
    repo: [
      '仓库里这几份权重怎么挑一份来用？',
      '哪些权重是带 LoRA 适配器的？',
      '删除的权重还能在回收站找回吗？',
    ],
    dataset: {
      '预训练': [
        '预训练要多少纯文本才够？',
        '一个数据集能标两次训练方式吗？',
        '怎么把几个 txt 合成一份预训练语料？',
        '标错了训练方式怎么改回来？',
      ],
      '继续预训练': [
        '继续预训练该用哪份数据？',
        '继续预训练会不会把原来的知识冲掉？',
        '继续预训练要多少新语料才有意义？',
      ],
      '指令微调': [
        '问答对多少条才够指令微调？',
        '问答格式不合格的文件会被跳过吗？',
        '指令微调用哪份权重当起点？',
      ],
      'LoRA 微调': [
        'LoRA 微调用哪份数据最合适？',
        '同一份数据标了两种训练方式会重复训练吗？',
        'LoRA 适配器能不能叠在别人的微调权重上？',
      ],
      '': [
        '这里列的数据集哪个能直接拿去训练？',
        '「可用数据集」和「标注数据集」差在哪？',
      ],
    },
    article: {
      config: [
        '这三个模型各自负责什么？',
        '整理模型和审阅模型的温度该怎么设？',
        'api_key 留空保存会不会把原来的覆盖掉？',
      ],
      collect: [
        '一个主题要采多少篇才够？',
        '重复率高说明什么，降到多少算合格？',
        '采集跑到一半停下会丢已采的内容吗？',
      ],
      data: [
        '采出来的 txt 怎么进数据集页训练？',
        '产物重复率高还能用吗？',
      ],
      '': [
        '语料蒸馏整条流程是怎么走的？',
      ],
    },
    response: {
      config: ['三个模型的分工是什么？', '审阅模型回 JSON 不合格怎么办？'],
      collect: ['问答对生成多少条够用？', '重复率高的话改哪个参数？'],
      data: ['生成的 jsonl 和 txt 有什么区别？', '怎么把产物并进指令微调语料？'],
      '': ['响应蒸馏整条流程怎么走？'],
    },
    webcurate: {
      config: ['三张模型卡分别管什么？', '采集用的搜索引擎怎么换？'],
      collect: ['一个主题跑几轮比较合适？', '重复率阈值调高会怎样？', '目标字数设多少合适？'],
      data: ['整理出来的 txt 直接能当预训练语料吗？', '产物怎么进数据集页？'],
      '': ['网页整理和响应蒸馏有什么不同？'],
    },
    formatconv: [
      '本地能直接读的 JSON 有哪几种？',
      '转换出来的是问答格式还是纯文本？',
      '读不出来的时候 AI 会怎么补？',
    ],
    train_pipeline: [
      '流水线里各步的顺序怎么排才合理？',
      '每一步的参数怎么配才不冲突？',
      '流水线跑到一半能停下改参数吗？',
    ],
    train_pretrain: [
      '预训练到什么程度算收敛？',
      'block size 和 batch size 怎么配？',
      '学习率调度选哪个？',
    ],
    train_continue: [
      '继续预训练和预训练到底差在哪？',
      '继续预训练会不会忘掉原来的知识？',
    ],
    train_full: [
      '过拟合了是什么表现，怎么救？',
      '问答对太少会不会学不会？',
    ],
    train_lora: [
      '挂载目标层要不要全选？',
      'LoRA 微调 8000 步够不够？',
      'LoRA 适配器能不能接着别人的继续训？',
    ],
    train_lora_merge: [
      '合并后精度会不会掉？',
      '合并完还要不要再验一遍对话？',
    ],
    train_prune: [
      'SparseGPT 和 Wanda 怎么选？',
      '裁切完还要重新微调吗？',
    ],
    train_quant: [
      '量化后困惑度会掉多少？',
      'FP8 我这台机器跑得动吗？',
    ],
    chat: [
      '为什么回答总是重复？',
      '换一份权重试试会有什么差别？',
      '回答很短是参数问题还是权重问题？',
    ],
    export: [
      '导出后怎么在别的地方加载？',
      '导出要不要连 LoRA 一起合并？',
    ],
    recycle_dataset: ['删掉的数据集还能恢复吗？', '回收站会自动清理吗？'],
    recycle_weight: ['删掉的权重还能恢复吗？', '恢复后名字会变吗？'],
    settings: [
      '主题和训练设备在哪里改？',
      '切到 XPU 需要重启吗？',
    ],
  };

  /* 英文版提示（用户 2026-10-22：AI 窗口的示例没翻译）。键与 PAGES 一致，
     数据集页的小页签在英文下是 Pretrain / Continue Pretrain / …（forPage 会换回中文键再查）。 */
  var PAGES_EN = {
    home: [
      'What should this project do first?',
      'Where do data, weights and training stand right now?',
      'What is the shortest path from nothing to a working chat?',
      'Is there enough data to train now?',
    ],
    repo: [
      'Which of these weights should I use?',
      'Which weights carry a LoRA adapter?',
      'Can a deleted weight be recovered from the Recycle Bin?',
    ],
    dataset: {
      '预训练': [
        'How much plain text does pretraining need?',
        'Can one dataset be labeled for two training methods?',
        'How do I merge several txt files into one pretraining corpus?',
        'How do I fix a training method I labeled wrong?',
      ],
      '继续预训练': [
        'Which data should continued pretraining use?',
        'Will continued pretraining wash out what the model already knows?',
        'How much new corpus makes continued pretraining worthwhile?',
      ],
      '指令微调': [
        'How many QA pairs does instruction tuning need?',
        'Are files with a broken QA format skipped?',
        'Which weights should instruction tuning start from?',
      ],
      'LoRA 微调': [
        'Which data fits LoRA tuning best?',
        'Will labeling one dataset twice train it twice?',
        'Can a LoRA adapter stack on someone else’s fine-tuned weights?',
      ],
      '': [
        'Which dataset here can go straight into training?',
        'What is the difference between Available and Labeled?',
      ],
    },
    article: {
      config: [
        'What does each of the three models do?',
        'How should I set temperature for the writer and reviewer?',
        'If I save with api_key empty, does it overwrite the old one?',
      ],
      collect: [
        'How many articles does one topic need?',
        'What does a high duplicate rate mean, and what counts as acceptable?',
        'If I stop mid-collection, do I lose what was already collected?',
      ],
      data: [
        'How do the collected txt files get into training on the Dataset page?',
        'Can outputs with a high duplicate rate still be used?',
      ],
      '': [
        'How does the whole corpus distillation flow work?',
      ],
    },
    response: {
      config: ['Who does what among the three models?', 'What if the reviewer returns invalid JSON?'],
      collect: ['How many QA pairs are enough?', 'Which parameter do I change when the duplicate rate is high?'],
      data: ['What is the difference between the generated jsonl and txt?', 'How do I fold outputs into the instruction-tuning corpus?'],
      '': ['How does the whole response distillation flow work?'],
    },
    webcurate: {
      config: ['What does each of the three model cards control?', 'How do I switch the search engine used for collection?'],
      collect: ['How many rounds per topic make sense?', 'What happens if I raise the duplicate threshold?', 'What target word count should I set?'],
      data: ['Can the curated txt be used directly as a pretraining corpus?', 'How do outputs get onto the Dataset page?'],
      '': ['How is web curation different from response distillation?'],
    },
    formatconv: [
      'Which JSON formats can be read locally?',
      'Does the conversion produce QA format or plain text?',
      'How does the AI fill in what it cannot read?',
    ],
    train_pipeline: [
      'What order should the pipeline steps be in?',
      'How do I set each step’s parameters so they do not clash?',
      'Can I stop a pipeline halfway and change parameters?',
    ],
    train_pretrain: [
      'When is pretraining considered converged?',
      'How should block size and batch size be paired?',
      'Which learning-rate schedule should I pick?',
    ],
    train_continue: [
      'How is continued pretraining actually different from pretraining?',
      'Will continued pretraining make the model forget what it knew?',
    ],
    train_full: [
      'What does overfitting look like, and how do I rescue it?',
      'Will too few QA pairs mean the model learns nothing?',
    ],
    train_lora: [
      'Should I select all target modules?',
      'Are 8000 steps enough for LoRA tuning?',
      'Can a LoRA adapter continue training from someone else’s?',
    ],
    train_lora_merge: [
      'Will accuracy drop after merging?',
      'Should I test the chat again after merging?',
    ],
    train_prune: [
      'SparseGPT or Wanda — which one?',
      'Do I need to fine-tune again after pruning?',
    ],
    train_quant: [
      'How much will perplexity drop after quantization?',
      'Can this machine run FP8?',
    ],
    chat: [
      'Why do the answers keep repeating?',
      'What changes if I try a different weight?',
      'Are short answers a parameter problem or a weight problem?',
    ],
    export: [
      'How do I load the export somewhere else?',
      'Should the export merge the LoRA adapter in?',
    ],
    recycle_dataset: ['Can a deleted dataset be restored?', 'Does the Recycle Bin clean itself up?'],
    recycle_weight: ['Can a deleted weight be restored?', 'Does the name change after restoring?'],
    settings: [
      'Where do I change theme and training device?',
      'Does switching to XPU need a restart?',
    ],
  };

  /* 训练页：按「语料 / 初始·基座权重」体量给参数建议（用户 2026-10-17） */
  function isEn() {
    return !!(window.I18N && window.I18N.lang() === 'en');
  }

  /* 训练页：按「语料 / 初始·基座权重」体量给参数建议（用户 2026-10-17）。
     英文版拼法与中文一一对应（用户 2026-10-22：AI 窗口示例没翻译） */
  function trainTips(page, sizes, ov) {
    var en = isEn();
    var c = fmtBytes(sizes.corpus);
    var b = fmtBytes(sizes.base);
    var a = fmtBytes(sizes.adapter);
    var qa = fmtNum(ov.response);
    var small = sizes.corpus > 0 && sizes.corpus < 1048576;   /* 不到 1MB 视为小语料 */
    var out = [];
    var parts = function (arr) { return arr.join(en ? ' · ' : ' · '); };
    if (page === 'train_pretrain') {
      if (small) out.push(en ? ('Only ' + c + ' of corpus — how many pretraining steps?')
                             : ('语料只有 ' + c + '，预训练步数该怎么定？'));
      else if (c) out.push(en ? ('Corpus is ' + c + ' — how should I set learning rate and steps?')
                              : ('语料 ' + c + '，预训练的学习率和步数怎么配？'));
      out.push(en ? 'When is pretraining considered converged?' : '预训练到什么程度算收敛？');
      out.push(en ? 'How should block size and batch size be paired?' : 'block size 和 batch size 怎么配？');
    } else if (page === 'train_continue') {
      if (small) out.push(en ? ('Continuing on ' + c + ' of new corpus — how many steps are worth it?')
                             : (c + ' 的新语料继续预训练，跑多少步不亏？'));
      else if (c) out.push(en ? ('Continued pretraining on ' + c + ' of corpus — how small should the learning rate be?')
                              : ('继续预训练用 ' + c + ' 语料，学习率要调多小？'));
      out.push(en ? 'Will continued pretraining make the model forget what it knew?'
                  : '继续预训练会不会忘掉原来的知识？');
    } else if (page === 'train_full') {
      var s1 = [];
      if (qa) s1.push(en ? (qa + ' QA pairs') : ('问答 ' + qa + ' 条'));
      if (c) s1.push(en ? ('corpus ' + c) : ('语料 ' + c));
      if (b) s1.push(en ? ('base ' + b) : ('基座 ' + b));
      if (s1.length) out.push(parts(s1) + (en ? ' — how should I set steps and learning rate for instruction tuning?'
                                              : '，指令微调的步数和学习率怎么定？'));
      out.push(en ? 'How many QA pairs keep instruction tuning from underfitting?'
                  : '指令微调要多少问答对才不欠拟合？');
      out.push(en ? 'What does overfitting look like, and how do I rescue it?'
                  : '过拟合了是什么表现，怎么救？');
    } else if (page === 'train_lora') {
      var s2 = [];
      if (b) s2.push(en ? ('base ' + b) : ('基座 ' + b));
      if (c) s2.push(en ? ('corpus ' + c) : ('语料 ' + c));
      if (qa) s2.push(en ? (qa + ' QA pairs') : ('问答 ' + qa + ' 条'));
      if (s2.length) {
        out.push(parts(s2) + (en ? ' — how should I set LoRA r and α?' : '，LoRA 的 r 和 α 怎么配？'));
      } else {
        out.push(en ? 'How should I set LoRA r and α?' : 'LoRA 的 r 和 α 怎么配？');
      }
      out.push(en ? 'Should I select all target modules?' : '挂载目标层要不要全选？');
    } else if (page === 'train_lora_merge') {
      if (b && a) out.push(en ? ('Base ' + b + ' + adapter ' + a + ' — will merging cost accuracy?')
                              : ('基座 ' + b + ' + 适配器 ' + a + '，合并后精度会掉吗？'));
      else if (b) out.push(en ? ('Base is ' + b + ' — how should I pick the adapter?')
                              : ('基座 ' + b + '，适配器怎么挑？'));
      out.push(en ? 'Should I test the chat again after merging?' : '合并完还要不要再验一遍对话？');
    } else if (page === 'train_prune') {
      if (b) out.push(en ? ('Weights ' + b + ' at 0.5 sparsity — how many points will I lose?')
                         : ('初始权重 ' + b + '，稀疏度 0.5 会掉多少点？'));
      out.push(en ? 'SparseGPT or Wanda — which one?' : 'SparseGPT 和 Wanda 怎么选？');
      out.push(en ? 'Do I need to fine-tune again after pruning?' : '裁切完还要重新微调吗？');
    } else if (page === 'train_quant') {
      if (b) out.push(en ? ('Weights ' + b + ' — W4A16 or W8A16?')
                         : ('初始权重 ' + b + '，W4A16 和 W8A16 怎么选？'));
      out.push(en ? 'How much will perplexity drop after quantization?' : '量化后困惑度会掉多少？');
      out.push(en ? 'Can this machine run FP8?' : 'FP8 我这台机器跑得动吗？');
    }
    return out;
  }

  /* 数据集页小页签：英文标签 → 中文键（PAGES/PAGES_EN 的键统一用中文） */
  var SUB_ZH = {
    'Pretrain': '预训练', 'Continue Pretrain': '继续预训练',
    'Instruction Tune': '指令微调', 'LoRA Tune': 'LoRA 微调',
  };

  function forPage(ctx) {
    ctx = ctx || {};
    var page = ctx.page || 'home';
    var sub = ctx.sub || '';
    var en = isEn();
    /* 英文：数据集页的小页签是英文标签，先换回中文键再查 */
    if (en && SUB_ZH[sub]) sub = SUB_ZH[sub];
    var node = (en ? PAGES_EN : PAGES)[page];
    var list = [];
    if (Array.isArray(node)) list = node.slice();
    else if (node && typeof node === 'object') {
      list = (node[sub] || node[''] || []).slice();
    }
    /* 训练页：体量相关的参数建议放最前面 */
    var tr = trainTips(page, ctx.sizes || {}, ctx.ov || {});
    if (tr.length) list = tr.concat(list);
    /* 去重（按原文）：训练页的体量版建议和静态文案可能撞车 */
    var seen = {};
    list = list.filter(function (t) { if (seen[t]) return false; seen[t] = 1; return true; });
    if (!list.length) list = [en ? 'What can this page do?' : '这个页面能做什么？'];
    return list;
  }

  /* 主题提示：只在总览页；夜间（18:00–06:00）才可能出切深色（1/10），白天才可能出切浅色（1/20）。
     当前主题已经是目标主题时不提示（没意义）。 */
  function themeChip(ctx) {
    ctx = ctx || {};
    if ((ctx.page || 'home') !== 'home') return null;
    var hour = (typeof ctx.hour === 'number') ? ctx.hour : new Date().getHours();
    var theme = ctx.theme || document.documentElement.getAttribute('data-theme') || 'light';
    var night = (hour >= 18 || hour < 6);
    var rnd = Math.random();
    if (night) {
      if (theme === 'dark') return null;
      return (rnd < 1 / 10) ? (isEn() ? 'Switch to dark theme' : '切换到深色主题') : null;
    }
    if (theme !== 'dark') return null;
    return (rnd < 1 / 20) ? (isEn() ? 'Switch to light theme' : '切换到浅色主题') : null;
  }

  window.AIPrompts = {
    activeSub: activeSub,
    pickSizes: pickSizes,
    forPage: forPage,
    themeChip: themeChip,
    pages: PAGES,
  };
})();
