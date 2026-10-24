"""悬停提示：专业词汇与功能说明。

用法：
    tip(widget, "d_model")          # 查术语表
    tip(widget, "自定义说明文字…")     # 或直接给文案
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

from . import theme as T

# ---------- 术语表：键 → (标题, 说明) ----------
GLOSSARY: dict[str, tuple[str, str]] = {
    # —— 模型结构 ——
    "d_model": (
        "隐藏维度 d_model",
        "模型每个 token 的向量宽度。越大容量越强，参数量大约按 d² 增长，显存和算力也跟着涨。\n"
        "字符级小模型常用 128～512；先小后大，确认能收敛再加宽。",
    ),
    "n_head": (
        "注意力头数 n_head",
        "把 d_model 切成多少个并行的注意力子空间。必须整除 d_model。\n"
        "头越多，能同时关注的关系类型越多；但每头维度变小，单头表达力下降。\n"
        "经验：d_model 128→4 头，256→8 头，512→8～16 头。",
    ),
    "n_layer": (
        "层数 n_layer",
        "Transformer 块堆叠的深度。越深表达能力越强，但更吃数据、更难训。\n"
        "字符级小模型 4～8 层通常够用；很深时务必开 warmup + 梯度裁剪。",
    ),
    "block_size": (
        "上下文长度 block_size",
        "模型一次能看到的最大历史字符数。训练和推理都按这个窗口截断。\n"
        "太长会拖慢训练且小模型学不好长依赖；对话任务 128～256 一般足够。",
    ),
    "d_ff": (
        "前馈宽度 d_ff",
        "Transformer 块里前馈层的中间维度。0 / 空 表示自动取 4×d_model。\n"
        "前馈负责「单 token 内部变换」，一般不小于 d_model。",
    ),
    "tie_weights": (
        "权重绑定 tie_weights",
        "让输入词嵌入与输出预测头共享同一份权重，省一半词表参数。\n"
        "字符级小模型上通常更好；关闭则输出头独立训练，容量略大。",
    ),
    "embedding": (
        "词嵌入 Embedding",
        "把每个字符/词映射成一个向量，是模型的「输入入口」。\n"
        "参数量 ≈ 词表大小 × d_model。",
    ),
    "lm_head": (
        "LM Head（输出头）",
        "把最后一层隐藏向量映射回词表大小的 logits，用来预测下一个字符。",
    ),
    "mtp": (
        "Multi-Token Prediction（多 token 预测）",
        "除了预测下一个字符，还额外预测后面 k 个字符，训练信号更稠密。\n"
        "小模型上常有收益，但会多占参数和算力；k 越大负担越重。",
    ),
    "mtp_k": (
        "MTP 预测个数 k",
        "一次预测到第几个未来字符（含下一位）。k=3 表示同时学 t+1 / t+2 / t+3。\n"
        "一般 2～4；太大信号会稀释到难学的远期目标上。",
    ),
    # —— 训练 ——
    "batch_size": (
        "批大小 batch_size",
        "每一步并行喂进多少条序列。越大梯度估计越稳，但更吃显存。\n"
        "显存不够时用「梯度累积」等效放大 batch，而不是硬加这个数。",
    ),
    "steps": (
        "训练步数 steps",
        "一共做多少次参数更新。总训练量 ≈ steps × batch_size × block_size 字符。\n"
        "预训练可以多训几遍语料；微调步数宜少，过头会把预训练学到的语言能力冲掉。",
    ),
    "lr": (
        "学习率 lr",
        "每一步参数往梯度反方向走多远。太大震荡/发散，太小学得极慢。\n"
        "预训练常用 1e-3～3e-3；对话微调应降到预训练的 1/4～1/10。",
    ),
    "warmup": (
        "预热步数 warmup",
        "前多少步把学习率从 0 线性升到目标值。开局 Adam 二阶动量不准，步子大容易爆。\n"
        "一般取总步数的 2%～5%；小模型 200～500 步即可。",
    ),
    "dropout": (
        "丢弃率 dropout",
        "训练时随机把一部分激活置 0，强迫模型不过分依赖某些神经元。\n"
        "小数据 0.1～0.2；数据充足可设 0。推理时自动关闭。",
    ),
    "weight_decay": (
        "权重衰减 weight_decay",
        "对权重做 L2 惩罚，压住权重变大，提升泛化。\n"
        "常用 0.01～0.1。偏置和 LayerNorm 不做衰减。",
    ),
    "grad_clip": (
        "梯度裁剪 grad_clip",
        "限制梯度整体范数上限，超过就按比例缩小。防止某几步 loss 突然爆炸。\n"
        "常用 0.5～1.0；设 0 表示不裁剪。",
    ),
    "grad_accum": (
        "梯度累积 grad_accum",
        "累积几个小 batch 再更新一次权重，等效于放大 batch_size。\n"
        "显存紧张时很有用：真实 batch = batch_size × grad_accum。",
    ),
    "amp": (
        "混合精度 AMP",
        "用 bf16/fp16 做前向反向，再用 fp32 更新权重，省显存并提速。\n"
        "仅在 xpu/cuda 上生效；数值不稳时关掉或改用 bf16。",
    ),
    "amp_dtype": (
        "AMP 精度",
        "bf16：动态范围大、更稳，Ampere 及更新卡（sm_80+）首选。\n"
        "fp16：Pascal / GTX 10 系等老卡的原生半精度，更快更稳。\n"
        "选 auto 时程序会按显卡算力自动挑。",
    ),
    "cosine_lr": (
        "Cosine 学习率退火",
        "warmup 结束后，学习率按余弦曲线缓慢降到峰值的 10%。\n"
        "比恒定 lr 收敛更细腻；关闭则 warmup 后保持峰值。",
    ),
    "seed": (
        "随机种子 seed",
        "固定后数据顺序、初始化、dropout 掩码都可复现。\n"
        "对比实验时保持种子一致，只改你要测的那个变量。",
    ),
    "eval_every": (
        "评估间隔（步）",
        "每隔多少步在验证集上算一次 loss 并保存权重。\n"
        "太密会拖慢训练，太疏则难以及时发现过拟合；一般 200～500 步。",
    ),
    "eval_iters": (
        "评估迭代 eval_iters",
        "评估时采样多少个 batch 取平均。越大越稳，但每次评估越慢。\n"
        "小数据 10～20 次足够。",
    ),
    "pretrain": (
        "预训练 Pretrain",
        "在大量纯文本上学习语法、连贯和常识，打好语言底子。\n"
        "对应阶段一，学习率较大、步数较多。",
    ),
    "sft": (
        "微调 SFT（Supervised Fine-Tuning）",
        "用高质量问答对，把预训练模型调整成「会按对话格式回答」。\n"
        "对应阶段二，学习率更小、步数更少，防止灾难性遗忘。",
    ),
    "loss": (
        "损失 Loss",
        "模型预测与真实答案的差距。越小说明拟合越好。\n"
        "train loss 下降而 val loss 上升，通常意味着过拟合。",
    ),
    "val_loss": (
        "验证损失 Val Loss",
        "在模型没用来更新参数的数据上算的 loss，用来估泛化能力。",
    ),
    "ppl": (
        "困惑度 PPL（Perplexity）",
        "≈ exp(val_loss)，表示模型「平均有多少种等可能的猜测」。\n"
        "越小越好；语言模型上 10～50 已不错，个位数非常强。",
    ),
    "checkpoint": (
        "权重 Checkpoint",
        "训练到某一刻的模型参数快照（.pt 文件）。可续训、可直接推理。",
    ),
    "vocab": (
        "词表 Vocabulary",
        "模型认识的全部字符/词的集合。字符级模型按语料统计得到。\n"
        "词表变大时会自动扩表，保留旧字符 id，已学能力不丢。",
    ),
    "device": (
        "设备 device",
        "auto / cpu / cuda / xpu。\n"
        "auto：优先 NVIDIA CUDA，其次 Intel XPU，最后 CPU。\n"
        "显卡不可用时会自动回退。",
    ),
    # —— 数据 ——
    "corpus": (
        "语料 Corpus",
        "用来训练的文本集合。预训练用纯文本；微调用问答对。",
    ),
    "jsonl": (
        "JSONL 格式",
        "每行一个 JSON 对象，便于流式读写和追加，是数据集常用格式。",
    ),
    "sharegpt": (
        "ShareGPT 格式",
        "conversations 列表（human / gpt 交替），很多微调框架直接认这个格式。",
    ),
    "dataset": (
        "数据集 Dataset",
        "已整理、可喂给训练的样本集合。本项目支持导出 JSONL / CSV / Markdown / ShareGPT。",
    ),
    "merge_corpus": (
        "合并语料",
        "把多个数据文件按规则拼成一份训练用大文本。\n"
        "预训练输出纯文本；微调输出带 \\x01问\\x02答\\x03 分隔符的对话语料。",
    ),
    "format_check": (
        "格式检查",
        "扫描文件编码、是否混入问答字段/分隔符，并给出能否合并的报告。",
    ),
    # —— 蒸馏 / API ——
    "distill": (
        "蒸馏 Distill",
        "用一个（或多个）强模型批量生成高质量「输入—输出」样本，再拿去训练小模型。\n"
        "本项目的文章蒸馏 / 响应蒸馏都是生成训练数据，不是压缩已有大模型。",
    ),
    "article_distill": (
        "文章蒸馏",
        "流水线：主题 → 生成标题 → 撰写全文 → 审阅打分。\n"
        "审阅发现重大错误则丢弃，留下的文章可作文预训练/写作数据。",
    ),
    "response_distill": (
        "响应蒸馏",
        "流水线：主题 → 生成问题 → 作答 → 审阅打分。\n"
        "产出问答对，可直接用于对话微调（SFT）。",
    ),
    "base_url": (
        "API 地址 base_url",
        "OpenAI 兼容接口的根地址，通常以 /v1 结尾。\n"
        "例如 https://api.openai.com/v1 或自建网关。",
    ),
    "api_key": (
        "API 密钥 api_key",
        "调用接口的凭证。只保存在本机 config.json，请勿分享给他人。",
    ),
    "model": (
        "模型名称 model",
        "接口侧的模型 ID，例如 gpt-4o-mini、mimo-v2.6-pro 等。",
    ),
    "temperature": (
        "温度 temperature",
        "控制输出随机性。0≈固定选最高概率；0.7～1.0 更有创造性；过高易胡言。\n"
        "出题/写标题可偏高，审阅宜偏低。",
    ),
    "max_tokens": (
        "最大输出长度 max_tokens",
        "单次回复最多生成多少 token。太小会被截断；推理模型通常要给足余量。",
    ),
    "system_prompt": (
        "系统提示词 system_prompt",
        "给模型的角色与规则设定，会作为对话前缀持续生效。\n"
        "改这里就能换「专家人设」和输出约束。",
    ),
    "titler": (
        "题目模型 Titler",
        "根据主题生成文章标题。建议温度略高，多样性更好。",
    ),
    "writer": (
        "写作模型 Writer",
        "根据标题撰写完整文章，输出会成为收集到的训练数据。",
    ),
    "reviewer": (
        "审阅模型 Reviewer",
        "检查是否有重大错误（事实/逻辑/偏题）。未通过则丢弃。建议温度低。",
    ),
    "generator": (
        "出题模型 Generator",
        "根据主题生成清晰、可独立回答的问题。",
    ),
    "responder": (
        "答题模型 Responder",
        "对问题给出结构清晰、准确完整的回答。",
    ),
    "concurrency": (
        "并发数 concurrency",
        "同时进行的采集任务数。越大越快，但更容易触发接口限流。",
    ),
    "discard_on_reject": (
        "审阅不通过则丢弃",
        "被审阅模型判为有重大错误的样本不写入正式数据集，保证数据质量。",
    ),
    "save_rejected": (
        "保存被拒样本",
        "把未通过审阅的样本另存一份，便于分析失败原因。",
    ),
    "mode_stream": (
        "流式模式 stream",
        "逐条「出题→答题→审阅」，反馈更及时，便于观察日志。",
    ),
    "mode_batch": (
        "批量模式 batch",
        "先批量出题再批量作答，吞吐可能更高，但中途失败时粒度更粗。",
    ),
    "auto_topic": (
        "自动出题",
        "按主题列表轮流出题；关闭后更依赖主题本身的具体程度。",
    ),
    # —— 功能 ——
    "home": (
        "总览",
        "数据、训练与模型状态一览，并给出从采集到对话的标准工作流。",
    ),
    "chat": (
        "对话验证",
        "加载本地训练出的权重，用聊天界面试玩，检查格式与回答质量。",
    ),
    "network": (
        "网络结构",
        "按当前配置绘制模型计算图，并估算参数量，便于调结构。",
    ),
    "train_center": (
        "训练中心",
        "配置超参、启动两阶段训练、查看日志与 Loss 曲线。",
    ),
    "train_pretrain": (
        "预训练",
        "阶段一：用纯文本语料从零训练语言模型，打好语法与常识底子。\n"
        "产出 checkpoints/eproai_pretrain.pt。",
    ),
    "train_full": (
        "全量微调",
        "阶段二全参 SFT：优化器更新模型全部参数。\n"
        "效果上限高，但需控制步数与学习率，防止灾难性遗忘。",
    ),
    "train_lora": (
        "LoRA 微调",
        "阶段二参数高效 SFT：冻结基座权重，只训练低秩旁路矩阵（通常 <5% 参数）。\n"
        "更快、更省显存、不易遗忘；产出合并权重 + 独立适配器（*.lora.pt）。",
    ),
    "lora_r": (
        "LoRA 秩 r",
        "低秩矩阵的秩。越大拟合能力越强、参数越多；小模型 4～16 足够。",
    ),
    "lora_alpha": (
        "LoRA 缩放 α",
        "适配器输出乘以 α/r。常用 α = 2r（如 r=8、α=16）。",
    ),
    "lora_dropout": (
        "LoRA dropout",
        "适配器分支的丢弃率。小数据防过拟合，常用 0.05。",
    ),
    "lora_targets": (
        "挂载目标层",
        "逗号分隔：qkv（注意力 QKV 投影）、proj（注意力输出投影）、\n"
        "fc_in / fc_out（前馈两层）。挂得越多容量越大、参数越多。",
    ),
    "sft_init_from": (
        "初始权重 init_from",
        "微调的起点权重文件。留空则用 pretrain_out。\n"
        "LoRA 微调必须指向一个已存在的基座权重。",
    ),
    "sample_preview": (
        "训练中采样预览",
        "每到评估点打印一段当前模型生成的文本，便于肉眼观察是否在进步。",
    ),
    "top_k": (
        "Top-K 采样",
        "只从概率最高的 K 个字符里采样。K 小更稳，K 大更多样。",
    ),
    "temperature_chat": (
        "生成温度",
        "对话时的随机性。0.7～0.9 较平衡；要更严谨可降到 0.3。",
    ),
}


# 全局只保留一个气泡，避免相邻控件各自弹窗叠在一起
_ACTIVE: dict[str, Any] = {
    "win": None,        # 当前可见的 Toplevel
    "after": None,      # 待显示的 after id
    "after_widget": None,  # 排队所用的 widget，用于 after_cancel
    "owner": None,      # 当前气泡归属的 ToolTip
    "pending": None,    # 待显示的 ToolTip
}


def _destroy_win() -> None:
    win = _ACTIVE.get("win")
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass
    _ACTIVE["win"] = None
    _ACTIVE["owner"] = None


def _cancel_pending(only_self: ToolTip | None = None) -> None:
    """取消排队中的显示。only_self 为 None 时取消任意排队。"""
    pending = _ACTIVE.get("pending")
    if pending is None:
        return
    if only_self is not None and pending is not only_self:
        return
    after = _ACTIVE.get("after")
    widget = _ACTIVE.get("after_widget") or getattr(pending, "widget", None)
    if after is not None and widget is not None:
        try:
            widget.after_cancel(after)
        except Exception:
            pass
    _ACTIVE["after"] = None
    _ACTIVE["after_widget"] = None
    _ACTIVE["pending"] = None


class ToolTip:
    """鼠标悬停时弹出的说明气泡（全局单例，绝不叠层）。"""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._force_hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        # 换控件：取消旧排队、收起旧气泡，再排队自己的
        _cancel_pending()
        _destroy_win()
        _ACTIVE["pending"] = self
        _ACTIVE["after_widget"] = self.widget
        _ACTIVE["after"] = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event: Any = None) -> None:
        # 只取消自己的排队，不影响刚进入的邻居控件
        _cancel_pending(only_self=self)
        if _ACTIVE.get("owner") is self:
            _destroy_win()

    def _force_hide(self, _event: Any = None) -> None:
        _cancel_pending(only_self=self)
        if _ACTIVE.get("owner") is self or _ACTIVE.get("owner") is None:
            _destroy_win()

    def _show(self) -> None:
        _ACTIVE["after"] = None
        _ACTIVE["after_widget"] = None
        _ACTIVE["pending"] = None
        # 显示前强制清残留，保证全局最多一个
        _destroy_win()

        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return

        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        win.wm_attributes("-topmost", True)

        bg = T.TT_BG
        fg = T.TT_FG
        # 圆角气泡：透明键色窗底 + PIL 圆角底图，四角透出桌面
        from .widgets import rounded_rect, corner_bite_photo
        key = "#010203"
        win.configure(bg=key)
        try:
            win.wm_attributes("-transparentcolor", key)
        except tk.TclError:
            pass
        canvas = tk.Canvas(win, bg=key, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        frame = tk.Frame(canvas, bg=bg, padx=12, pady=10)
        parts = self.text.split("\n", 1)
        title = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        tk.Label(frame, text=title, bg=bg, fg=T.TT_TITLE,
                 font=(T.UI, T.FS_CAPTION, "bold"), anchor="w", justify="left",
                 wraplength=360).pack(fill="x")
        if body:
            tk.Label(frame, text=body, bg=bg, fg=fg,
                     font=(T.UI, T.FS_CAPTION), anchor="w", justify="left",
                     wraplength=360).pack(fill="x", pady=(4, 0))
        win.update_idletasks()
        fw = max(frame.winfo_reqwidth(), 2)
        fh = max(frame.winfo_reqheight(), 2)
        canvas.configure(width=fw, height=fh)
        rounded_rect(canvas, 0, 0, fw, fh, 10, bg, T.LINE, 1, tags="bg")
        canvas.create_window(0, 0, window=frame, anchor="nw", tags="body")
        # 四角透明咬口（控件层，盖住内容框方角）：弧外=透明键色、弧内=底色
        for corner in ("tl", "tr", "br", "bl"):
            bc = tk.Canvas(win, width=10, height=10, bg=key,
                           highlightthickness=0, bd=0)
            bx = 0 if corner in ("tl", "bl") else max(0, fw - 10)
            by = 0 if corner in ("tl", "tr") else max(0, fh - 10)
            bc.place(x=bx, y=by)
            photo = corner_bite_photo(bc, 10, corner, bg, key)
            bc.create_image(0, 0, image=photo, anchor="nw", tags="bite")

        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        tw = win.winfo_width()
        th = win.winfo_height()
        if x + tw > sw:
            x = max(8, sw - tw - 12)
        if y + th > sh:
            y = max(8, self.widget.winfo_rooty() - th - 8)
        win.wm_geometry(f"+{x}+{y}")

        _ACTIVE["win"] = win
        _ACTIVE["owner"] = self


def hide_all_tooltips() -> None:
    """手动收起所有气泡（切换页面时调用）。"""
    _cancel_pending()
    _destroy_win()


def tip(widget: tk.Widget, key_or_text: str) -> tk.Widget:
    """给控件挂悬停说明。key_or_text 可以是 GLOSSARY 的键，或直接是说明文字。"""
    if key_or_text in GLOSSARY:
        title, body = GLOSSARY[key_or_text]
        text = f"{title}\n{body}" if body else title
    else:
        text = key_or_text
    ToolTip(widget, text)
    return widget


def tip_key(key: str) -> str:
    """取出术语表正文（不含标题），便于放进帮助面板。"""
    if key in GLOSSARY:
        return GLOSSARY[key][1] or GLOSSARY[key][0]
    return key
