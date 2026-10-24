"""统一配置：API 模型、采集、训练参数。"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from typing import Any

# 2026-10-23：config.json 是全应用唯一配置文件（模型配置 + 各 10 处 API key 都在里面）。
# 以前 save_config 直接 open(path, "w") 覆盖：桥是 ThreadingHTTPServer，两个保存并发
# 就会互相截断；写一半被杀 / 断电则变成半截 JSON，应用起不来且密钥全丢。
# 现在改为「临时文件 + flush/fsync + os.replace 原子替换」，并用进程内锁串行化写入。
# 2026-10-24：这套实现抽到 core/atomic.py，ui_state / 权重溯源 / 运行快照也共用同一套，
# 这里的名字保留成别名（读写共用一把锁，别各建一把）。
try:                                        # 以 core.config 导入时走相对包，直接跑脚本时退回同级模块
    from . import atomic as _atomic
except ImportError:                          # pragma: no cover
    import atomic as _atomic                 # type: ignore
try:                                        # 2026-10-24：兜底异常落盘（排查报告第 2 条）
    from . import errlog as _errlog
except ImportError:                          # pragma: no cover
    import errlog as _errlog                 # type: ignore

_SAVE_LOCK = _atomic._LOCK
_RETRY = _atomic._RETRY
_backoff = _atomic._backoff
_retry_replace = _atomic.retry_replace
_open_retry = _atomic.open_retry

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "name": "EAI Studio",
        "version": "0.1.0",
        "theme": "dark",          # dark | light（黑 / 白主题）
        "sidebar": True,          # 侧栏展开状态
        "ai_panel": True,         # 右侧 AI 助手展开状态
    },
    "assistant": {
        # 右侧 AI 助手：留空 base_url/api_key/model 时自动回退到
        # 1) mimo CLI（MimoC 安装）的 llm-server  2) article.titler 配置
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.7,
        "max_tokens": 4096,
        "system_prompt": (
            "你是 EAI Studio 的内置助手，内嵌在应用右侧侧栏中。"
            "当前应用包含：工作台（总览/仓库）、数据工厂（语料蒸馏/响应蒸馏/数据集）、训练中心（预训练/继续预训练/指令微调/LoRA 微调）、模型（对话验证/导出模型）、回收站（数据集/权重）、全局设置。"
            "用户可以请你查询或修改应用内的参数。修改参数时，你必须且只能输出一个 JSON 代码块，"
            "格式如下（可包含多条 actions）：\n"
            "```json\n"
            '{"actions": [{"tool": "set_config", "path": "train.d_model", "value": 256}]}\n'
            "```\n"
            "可用 tool：\n"
            '- set_config：path 为配置点分路径（如 train.batch_size、article.collection.count、app.theme），value 为新值（自动按现有类型转换）。写入 config.json 并立即生效。\n'
            '- get_config：读取配置，path 可省略（省略返回整份配置摘要）。\n'
            '- navigate：跳转页面，value 为 home/repo/article/response/dataset/train_pretrain/train_continue/train_full/train_lora/train_prune/train_quant/chat/recycle_dataset/recycle_weight/settings（train_full=指令微调；train_prune=模型裁切、train_quant=量化；旧值 train 等价于 train_pretrain；recycle_dataset=回收站数据集、recycle_weight=回收站权重）。\n'
            '- set_theme：切换主题，value 为 dark 或 light。\n'
            "除 JSON 块之外，你可以输出正常的中文解释。回答保持简洁、直接。"
            "不要编造不存在的配置项；不确定 path 时先用 get_config 查询。"
        ),
    },
    "article": {
        "titler": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
            "temperature": 0.9,
            "max_tokens": 256,
            # 蒸馏提示词（用户 2026-10-20 定稿，之后不再改）：语言要求直接写在提示词里
            "system_prompt": (
                "你是一名中文选题编辑，为语言模型准备预训练语料。根据主题方向拟一个标题："
                "只锁定一个具体话题，写清对象与限定（如领域、范围、时间），能撑起一篇有信息量的长文；"
                "不要问句、不要编号、不要书名号、不要标题党。只输出标题本身，不要解释。"
                "所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are an English topic editor preparing pretraining corpora for a language model. "
                "Given a theme, draft a title that locks onto one concrete subject and names its scope "
                "(field, range or period) so it can carry an informative long-form article. "
                "No questions, no numbering, no quotation marks, no clickbait. Output the title only. "
                "All output must be in English."
            ),
        },
        "writer": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.8,
            "max_tokens": 8192,
            "system_prompt": (
                "你是一名中文科普作者，为语言模型撰写预训练语料。围绕给定标题写一篇完整文章："
                "开头直接点题；主体分 2-4 个小节层层展开，每节给出一个明确论点，并用事实、机制、数据或例证支撑；"
                "结尾收束全文并指出意义或常见误区。要求事实准确、术语规范、逻辑连贯，"
                "避免空话套话、避免罗列式清单；不要重复标题，不要写任何说明性文字。"
                "所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are an English science writer producing pretraining corpora for a language model. "
                "Write a complete article for the given title: open by stating the subject; develop the body "
                "in 2-4 sections, each with one clear claim backed by facts, mechanisms, data or examples; "
                "close by summing up and noting significance or common misconceptions. Be factually accurate, "
                "use precise terminology, keep the logic coherent, and avoid filler, cliches and bullet lists. "
                "Do not repeat the title and add no commentary. All output must be in English."
            ),
        },
        "reviewer": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.2,
            "max_tokens": 1024,
            "system_prompt": (
                "你是一名严格的中文语料审稿人。只判断是否存在重大错误："
                "事实错误、逻辑硬伤、严重偏题、伪造引用或数据、明显虚假信息、通篇空洞套话。"
                "文风偏好与详略取舍不算重大错误。只输出一个 JSON 对象，不要任何其他文字：\n"
                '{"pass": true或false, "score": 0到10的整数, "major_errors": ["重大错误逐条列出"], "comment": "一句话总评"}'
            ),
            "system_prompt_en": (
                "You are a strict English corpus reviewer. Judge only major errors: factual mistakes, "
                "broken logic, serious off-topic drift, fabricated citations or data, obvious misinformation, "
                "or content that is entirely empty filler. Style preferences and level of detail do not count. "
                "Output only a JSON object, nothing else:\n"
                '{"pass": true or false, "score": integer 0-10, "major_errors": ["each major error, one line"], '
                '"comment": "one-sentence summary"}'
            ),
        },
        "collection": {
            "themes": "",                    # 2026-10-18: 不预填主题（未输入就是空，示例走「填充示例主题」）
            "count": 50,
            "concurrency": 2,
            "min_words": 50,          # 用户 2026-10-20：新建工程默认 50 / 500（原来 600/2000 太大）
            "max_words": 500,
            "discard_on_reject": True,
            "save_rejected": False,
            "output_dir": "",
            "dataset_name": "article_set",
        },
    },
    "response": {
        "generator": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
            "temperature": 0.9,
            "max_tokens": 512,
            "system_prompt": (
                "你是一名中文出题专家，为语言模型蒸馏数据设计问题。问题要具体、可独立回答、"
                "有明确落点（概念、机制、对比、因果或应用），难度适中偏高；"
                "不要是非题、不要需要追问才能作答的开放题、不要出现「根据上文」这类无指代的说法。"
                "只输出问题本身，不要编号、引号或解释。所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are an English question designer building distillation data for a language model. "
                "Questions must be specific, self-contained and sharply focused (concept, mechanism, "
                "comparison, cause and effect, or application), of medium-to-hard difficulty. Avoid yes/no "
                "questions, avoid open prompts that need follow-up questions, and never use context "
                "references such as \"according to the passage\". Output the question only: no numbering, "
                "no quotes, no explanation. All output must be in English."
            ),
        },
        "responder": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.7,
            "max_tokens": 2048,
            "system_prompt": (
                "你是一名严谨、有帮助的中文助手。直接回答问题：先给结论，再给必要的解释、步骤或例证；"
                "术语准确、逻辑清晰、只写与问题相关的内容，不重复问题、不写「好的」这类开场白。"
                "只输出回答正文。所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are a rigorous, helpful English assistant. Answer directly: state the conclusion first, "
                "then the necessary explanation, steps or examples. Use precise terminology, keep the logic "
                "clear, stay on the question, do not restate it and do not open with pleasantries. "
                "Output the answer body only. All output must be in English."
            ),
        },
        "reviewer": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.1,
            "max_tokens": 512,
            "system_prompt": (
                "你是一名严格的中文质检审核员。只判断问答对是否存在重大问题："
                "答非所问、事实错误、空洞胡编、明显有害内容、严重不完整或自相矛盾。"
                "文笔与详略问题不算重大问题。只输出一个 JSON 对象，不要任何其他文字：\n"
                '{"pass": true或false, "score": 0到10的整数, "major_errors": ["重大问题逐条列出"], "comment": "一句话总评"}'
            ),
            "system_prompt_en": (
                "You are a strict English QA reviewer. Judge only major errors in the question-answer pair: "
                "answers that miss the question, factual mistakes, empty fabrication, clearly harmful content, "
                "serious incompleteness or self-contradiction. Style and level of detail do not count. "
                "Output only a JSON object, nothing else:\n"
                '{"pass": true or false, "score": integer 0-10, "major_errors": ["each major issue, one line"], '
                '"comment": "one-sentence summary"}'
            ),
        },
        "collection": {
            "topics": "",                    # 2026-10-18: 不预填主题
            "count": 50,
            "concurrency": 2,
            "mode": "stream",
            "auto_topic": True,
            "discard_on_reject": True,
            "save_rejected": False,
            "output_dir": "",
            "dataset_name": "response_set",
        },
    },
    # 网页整理（用户 2026-10-20：网页整理也是蒸馏的一种 —— 提示词同样定稿、带语言要求，
    # 且和引擎实际用的提示词是同一份：改了这里就真的会生效）。
    # 模型档案独立存 config.webcurate.models，不再借用 config.article，避免两页互相覆盖提示词。
    "webcurate": {
        "planner": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
            "temperature": 0.4,
            "max_tokens": 1024,
            "system_prompt": (
                "你是检索策略规划助手，为语言模型蒸馏语料规划网页检索。根据主题给出互不重叠的中文检索式，"
                "覆盖不同角度（定义、原理/机制、具体做法、数据或指标、案例、对比、常见问题）。"
                "每条 6~16 个字、带 2 个以上实词（例如「大语言模型 微调 方法」），不要引号、不要布尔语法、"
                "不要站点限定；不要与「已用检索式」重复；若给了审阅反馈，必须换角度或换同义词。"
                '只输出 JSON：{"queries":[{"q":"检索式","angle":"这条想拿到什么"}]}，不要任何解释。'
                "所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are a search-strategy planner building distillation corpora for a language model. Given a "
                "topic, produce non-overlapping English search queries covering different angles (definition, "
                "mechanism, practice, data or metrics, cases, comparisons, common questions). Each query: 3-10 "
                "words with at least two content terms; no quotes, no boolean operators, no site: filters; never "
                "repeat an already-used query; if review feedback is given, change the angle or use synonyms. "
                'Output only JSON: {"queries":[{"q":"query","angle":"what this should fetch"}]}, no explanation. '
                "All output must be in English."
            ),
        },
        "extractor": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.3,
            "max_tokens": 4096,
            "system_prompt": (
                "你是网页正文整理助手，为语言模型蒸馏语料整理网页文本。把给定网页文本整理成干净的中文纯文本："
                "1) 只保留与主题相关、可复用的事实性内容，与主题无关就只输出空字符串；"
                "2) 去掉导航、广告、版权、作者署名、评论区、代码块、目录、表情与 markdown 符号；"
                "3) 按语义分成 2~6 段、每段 1~3 句、段间空一行，全文不超过 600 字，不要小标题、不要编号、"
                "不要解释、不要「以下是整理结果」这类前言后语；"
                "4) 保留原文里的数字、专有名词与结论，不要编造。"
                "所有输出一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are a web-text extractor preparing distillation corpora for a language model. Turn the given "
                "page text into clean English plain text: 1) keep only reusable factual content relevant to the "
                "topic, output an empty string when nothing is relevant; 2) drop navigation, ads, copyright "
                "lines, bylines, comments, code blocks, tables of contents, emoji and markdown symbols; 3) split "
                "by meaning into 2-6 paragraphs of 1-3 sentences separated by blank lines, keep the whole text "
                "under about 400 words, no headings, no numbering, no explanation, no lead-in or closing remarks; "
                "4) keep the original numbers, proper nouns and conclusions, never invent facts. "
                "All output must be in English."
            ),
        },
        "reviewer": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
            "temperature": 0.1,
            "max_tokens": 512,
            "system_prompt": (
                "你是语料审阅助手，负责判断一份文本能否直接用于训练。看两件事："
                "①相对主题的覆盖度（角度够不够、有没有缺关键侧面）；②文本内部重复率（同一件事反复说、近义段落堆叠）。"
                '只输出 JSON：{"dup_rate":0~1,"coverage":0~1,"verdict":"ok|redo",'
                '"advice":"给检索规划的具体建议：哪些角度重复了、还缺什么角度、下一轮换哪些检索式",'
                '"dups":["重复片段示例(不超过30字)"]}。不要解释、不要 markdown。'
                "JSON 之外的说明一律使用简体中文。"
            ),
            "system_prompt_en": (
                "You are a corpus reviewer deciding whether a text can be used for training. Judge two things: "
                "(1) coverage of the topic (enough angles, no missing key facets); (2) internal duplication "
                "(the same point repeated, near-duplicate paragraphs). "
                'Output only JSON: {"dup_rate":0~1,"coverage":0~1,"verdict":"ok|redo",'
                '"advice":"concrete advice for the search planner: which angles repeat, what is missing, which '
                'queries to use next round","dups":["short duplicate snippet, max 30 chars"]}. '
                "No explanation, no markdown. All prose inside the JSON must be in English."
            ),
        },
    },
    "train": {
        "device": "auto",
        "method": "full",          # full 全量微调 | lora 低秩适配（仅 SFT 阶段可用）
        "lora_r": 8,               # 低秩秩：越大容量越强，参数越多
        # 2026-10-23（用户要求）：默认参数按「基座已完成指令微调」给 —— α=r（比例 1）、只挂 qkv。
        # 只做过预训练的用户自己调大（α=2r、挂 qkv,proj,fc_in,fc_out、步数 3000~8000、lr 3e-4~1e-3）。
        "lora_alpha": 8,           # 缩放系数 α，实际缩放 = α/r
        "lora_dropout": 0.05,
        "lora_targets": "qkv",
        "lora_steps": 1200,        # LoRA 自己的步数（不再和 sft_steps 共用）
        "lora_lr": 2e-4,           # LoRA 自己的学习率（不再和 sft_lr 共用）
        "lora_init_from": "",      # LoRA 自己的基座（不再和 sft_init_from 共用）
        "data_roots": [],
        "seed": 1337,
        "d_model": 128,
        "n_head": 4,
        "n_layer": 4,
        "d_ff": 0,
        "block_size": 128,
        "batch_size": 32,
        "dropout": 0.1,
        "tie_weights": True,
        "grad_clip": 1.0,
        "grad_accum": 1,
        "mtp": False,
        "mtp_k": 3,
        "amp": False,
        "amp_dtype": "bf16",
        "sample": True,
        "cosine_lr": True,
        "weight_decay": 0.05,
        "eval_iters": 10,
        "pretrain_corpus": "",
        "pretrain_steps": 30000,
        "pretrain_lr": 2e-3,
        "pretrain_warmup": 500,
        "pretrain_eval_every": 300,
        "pretrain_out": "checkpoints/eproai_pretrain.pt",
        "pretrain_log_csv": "logs/pretrain_loss.csv",
        "sft_corpus": "",
        "sft_steps": 8000,
        "sft_lr": 5e-4,
        "sft_warmup": 200,
        "sft_eval_every": 200,
        "sft_out": "checkpoints/eproai_model.pt",
        "sft_log_csv": "logs/sft_loss.csv",
        "sft_init_from": "checkpoints/eproai_pretrain.pt",
        "lora_out": "checkpoints/eproai_lora.pt",
        "lora_log_csv": "logs/lora_loss.csv",
        "prompt": "你好",
        "sample_chars": 120,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config(path, cfg)
        return cfg
    # 2026-10-24（用户复测 P1）：读也拿同一把锁 —— 进程内读者不再和写者抢文件句柄。
    # Windows 的 open() 不带 FILE_SHARE_DELETE，读者开着的瞬间 os.replace 会被拒（WinError 5），
    # 光靠重试在「多线程死循环读 + 连续保存」下还会漏 1% 的写失败；读写共用一把锁后为 0。
    with _SAVE_LOCK:
        try:
            with _open_retry(path) as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            # 2026-10-23：文件被外部改坏、或历史遗留的半截 JSON —— 先退到同目录 .bak，
            # 再退出厂默认值，不让「配置读不出来」把应用卡死在启动（也不覆盖坏文件，
            # 留给用户/日志排查）。
            sys.stderr.write("[config] 读取失败 %s: %s\n" % (path, e))
            _errlog.log_exc("config.load", e, path=path)      # 兜底也要留证据（2026-10-24）
            raw = None
            bak = path + ".bak"
            if os.path.isfile(bak):
                try:
                    with _open_retry(bak) as f:
                        raw = json.load(f)
                    sys.stderr.write("[config] 已回退到 %s\n" % bak)
                    _errlog.log_line("config.load", "已回退到 .bak", path=bak)
                except (OSError, ValueError):
                    raw = None
            if not isinstance(raw, dict):
                sys.stderr.write("[config] 回退出厂默认值\n")
                _errlog.log_line("config.load", "回退出厂默认值（坏文件保留在原位）", path=path)
                return copy.deepcopy(DEFAULT_CONFIG)
    return _deep_merge(DEFAULT_CONFIG, raw)


def save_config(path: str, config: dict[str, Any]) -> None:
    """原子写配置（2026-10-23）。

    · 同进程多线程：_SAVE_LOCK 串行化 —— 桥是 ThreadingHTTPServer，所有保存都走这里。
    · 跨进程（训练子进程等）：各自写各自的临时文件再原子替换，读到的永远是完整 JSON，
      最坏是后写者覆盖先写者（丢更新），不会出现半截文件。
    · 目标文件与临时文件同目录 —— 同卷，Windows/POSIX 的 os.replace 都是原子替换。
    """
    # 实现统一在 core/atomic.py（2026-10-24）：临时文件 + fsync + os.replace + 重试 + 同一把锁
    try:
        _atomic.write_json(path, config, indent=2)
    except Exception as exc:                     # noqa: BLE001  落盘失败：记堆栈后再往上抛
        _errlog.log_exc("config.save", exc, path=path, bytes=len(json.dumps(config)))
        raise


def parse_lines(text: str) -> list[str]:
    items: list[str] = []
    for line in (text or "").replace(",", "\n").splitlines():
        item = line.strip()
        if item:
            items.append(item)
    return items


# 兼容旧命名
parse_themes = parse_lines
parse_topics = parse_lines
