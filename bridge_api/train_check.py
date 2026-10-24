"""训练参数审查（用户 2026-10-18：训练页参数要给风险/不可用提醒）

POST /api/train/check   {kind, params}

返回：
    {"ok": true, "issues": [
        {"level": "error"|"warn", "field": "sft_lr", "label": "学习率",
         "value": "1.5", "msg": "学习率 1.5 远高于常用上限 0.01，训练大概率直接发散"},
        ...
    ]}

约定：
  - error = 不可用，前端不给「仍然开始」，必须回去改；
  - warn  = 有风险，前端弹窗提示，用户可以选择仍要继续；
  - 只做静态规则（不做真实显存估算），阈值取「本机小模型 + 常见做法」的经验区间，
    宁可少报也不要天天误报：只有在明显不合理时才说话。
  - 缺省键回落到 config.train 的当前值，保证只传改动的页（如 LoRA 页只传 7 个键）也能审。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

KIND_LABEL = {
    "pretrain": "预训练",
    "continue": "继续预训练",
    "full": "指令微调",
    "lora": "LoRA 微调",
    "merge": "LoRA 合并",
    "prune": "模型裁切",
    "quant": "量化",
    "pipeline": "训练流水线",
}

# 字段 → 展示名（弹窗里写「学习率：1.5」这种）
FIELD_LABEL = {
    "d_model": "模型维度 d_model",
    "n_head": "注意力头 n_head",
    "n_layer": "网络层数 n_layer",
    "block_size": "上下文长度 block_size",
    "batch_size": "批量大小 batch_size",
    "dropout": "丢弃率 dropout",
    "grad_clip": "梯度裁剪 grad_clip",
    "grad_accum": "梯度累积 grad_accum",
    "pretrain_steps": "预训练步数",
    "pretrain_lr": "预训练学习率",
    "continue_steps": "继续预训练步数",
    "continue_lr": "继续预训练学习率",
    "sft_steps": "指令微调步数",
    "sft_lr": "指令微调学习率",
    "lora_steps": "LoRA 步数",          # 2026-10-23：LoRA 独立成键
    "lora_lr": "LoRA 学习率",
    "lora_init_from": "LoRA 基座权重",
    "lora_r": "LoRA 秩 r",
    "lora_alpha": "LoRA 缩放 alpha",
    "lora_dropout": "LoRA dropout",
    "lora_targets": "挂载目标层",
    "amp": "混合精度 AMP",
    "prune_ratio": "裁剪比例",
    "quant_bits": "量化位宽",
}

# ---------------------------------------------------------------
# 英文（用户 2026-10-23：英文界面下这个弹窗里参数名/原因/按钮还是中文）
# lang 由前端随 POST body 传进来（body.lang），每次请求先置 _LANG；
# 出文案的地方一律走 _T(中文, 英文)，不查表、不会漏键。
# ---------------------------------------------------------------
_LANG = "zh"


def _T(zh: str, en: str) -> str:
    return en if _LANG == "en" else zh


def _KIND_LABEL_EN_OR_ZH(kind: str) -> str:
    return (KIND_LABEL_EN if _LANG == "en" else KIND_LABEL).get(kind, "")


FIELD_LABEL_EN = {
    "d_model": "Model dim d_model",
    "n_head": "Attention heads n_head",
    "n_layer": "Layers n_layer",
    "block_size": "Context length block_size",
    "batch_size": "Batch size batch_size",
    "dropout": "Dropout dropout",
    "grad_clip": "Grad clip grad_clip",
    "grad_accum": "Grad accum grad_accum",
    "pretrain_steps": "Pretrain steps",
    "pretrain_lr": "Pretrain LR",
    "continue_steps": "Continue-pretrain steps",
    "continue_lr": "Continue-pretrain LR",
    "sft_steps": "Instruction-tune steps",
    "sft_lr": "Instruction-tune LR",
    "lora_steps": "LoRA steps",
    "lora_lr": "LoRA LR",
    "lora_init_from": "LoRA base weights",
    "lora_r": "LoRA rank r",
    "lora_alpha": "LoRA alpha",
    "lora_dropout": "LoRA dropout",
    "lora_targets": "Target modules",
    "amp": "Mixed precision AMP",
    "prune_ratio": "Prune ratio",
    "quant_bits": "Quant bits",
}

KIND_LABEL_EN = {
    "pretrain": "Pretrain",
    "continue": "Continue Pretrain",
    "full": "Instruction Tune",
    "lora": "LoRA Tune",
    "merge": "LoRA Merge",
    "prune": "Prune",
    "quant": "Quantize",
    "pipeline": "Pipeline",
}

_LR_MAX = 0.01          # 超过它就基本一定发散
_LR_MIN = 1e-6          # 低于它基本学不动
_STEPS_MIN = 20
_STEPS_MAX = 200000
_TARGETS_OK = ("qkv", "proj", "fc_in", "fc_out")


def _f(params: Dict[str, Any], cfg_train: Dict[str, Any], key: str) -> Optional[float]:
    v = params.get(key, None)
    if v is None or v == "" or v == "—":
        v = cfg_train.get(key, None)
    if v is None or v == "" or v == "—":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(params: Dict[str, Any], cfg_train: Dict[str, Any], key: str) -> str:
    v = params.get(key, None)
    if v is None or v == "":
        v = cfg_train.get(key, "")
    return "" if v is None else str(v)


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e9:
        return str(int(v))
    return ("%g" % v)


def _issue(level: str, field: str, msg: str, value: Any = None) -> Dict[str, Any]:
    labels = FIELD_LABEL_EN if _LANG == "en" else FIELD_LABEL
    return {
        "level": level,
        "field": field,
        "label": labels.get(field, field),
        "value": "" if value is None else str(value),
        "msg": msg,
    }


def _check_lr(out: List[Dict[str, Any]], field: str, lr: Optional[float]) -> None:
    if lr is None:
        return
    if lr <= 0:
        out.append(_issue("error", field, _T("学习率必须大于 0（当前 %s），否则参数永远不会更新", "Learning rate must be greater than 0 (currently %s), otherwise the weights never update") % _fmt(lr), _fmt(lr)))
        return
    if lr > _LR_MAX:
        out.append(_issue("warn", field,
                          _T("学习率 %s 高于常用上限 %s，训练大概率直接发散（loss 变成 nan）", "Learning rate %s is above the usual ceiling %s; training will likely diverge (loss becomes NaN)") % (_fmt(lr), _fmt(_LR_MAX)),
                          _fmt(lr)))
    elif lr < _LR_MIN:
        out.append(_issue("warn", field,
                          _T("学习率 %s 低于 %s，几乎学不动（loss 基本一条直线）", "Learning rate %s is below %s; the model barely learns (loss stays flat)") % (_fmt(lr), _fmt(_LR_MIN)),
                          _fmt(lr)))


def _check_steps(out: List[Dict[str, Any]], field: str, steps: Optional[float]) -> None:
    if steps is None:
        return
    if steps <= 0:
        out.append(_issue("error", field, _T("步数必须大于 0（当前 %s）", "Steps must be greater than 0 (currently %s)") % _fmt(steps), _fmt(steps)))
        return
    if steps < _STEPS_MIN:
        out.append(_issue("warn", field, _T("只跑 %s 步太少，模型基本学不到东西（建议 ≥%d）", "%s steps is too few; the model learns almost nothing (%d or more recommended)") % (_fmt(steps), _STEPS_MIN),
                          _fmt(steps)))
    elif steps > _STEPS_MAX:
        out.append(_issue("warn", field, _T("步数 %s 极多，本机可能要跑很久（建议先用小步数验证能跑通）", "%s steps is a lot; this machine will take a long time (try a short run first)") % _fmt(steps),
                          _fmt(steps)))


def _check_batch(out: List[Dict[str, Any]], batch: Optional[float]) -> None:
    if batch is None:
        return
    if batch < 1:
        out.append(_issue("error", "batch_size", _T("批量大小至少为 1（当前 %s）", "Batch size must be at least 1 (currently %s)") % _fmt(batch), _fmt(batch)))
        return
    if batch < 2:
        out.append(_issue("warn", "batch_size", _T("批量大小 1 时梯度噪声很大，训练会抖（常用 8~64）", "Batch size 1 gives very noisy gradients and jumpy training (usually 8~64)"), _fmt(batch)))
    elif batch > 256:
        out.append(_issue("warn", "batch_size", _T("批量大小 %s 很大，内存/显存可能不够（XPU 上尤其容易 OOM）", "Batch size %s is large; RAM/VRAM may not be enough (XPU runs out of memory easily)") % _fmt(batch),
                          _fmt(batch)))


def _check_dropout(out: List[Dict[str, Any]], field: str, d: Optional[float]) -> None:
    if d is None:
        return
    if d < 0:
        out.append(_issue("error", field, _T("丢弃率不能为负（当前 %s）", "Dropout cannot be negative (currently %s)") % _fmt(d), _fmt(d)))
    elif d >= 1:
        out.append(_issue("error", field, _T("丢弃率 %s 会把所有激活都丢掉，模型学不到任何东西（常用 0~0.3）", "Dropout %s throws away every activation, so the model learns nothing (usually 0~0.3)") % _fmt(d),
                          _fmt(d)))
    elif d > 0.5:
        out.append(_issue("warn", field, _T("丢弃率 %s 偏高，收敛会明显变慢（常用 0~0.3）", "Dropout %s is high; convergence slows down noticeably (usually 0~0.3)") % _fmt(d), _fmt(d)))


def _check_amp(out: List[Dict[str, Any]], params: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    amp = params.get("amp", None)
    if amp is None:
        amp = (cfg.get("train") or {}).get("amp")
    if not amp:
        return
    device = str((cfg.get("train") or {}).get("device") or "cpu").lower()
    if device in ("cpu", "mps"):
        out.append(_issue("warn", "amp",
                          _T("当前训练设备是 %s，混合精度没有收益，部分算子还会直接报错（建议关掉 AMP）", "Training device is %s; mixed precision gives no speedup and some ops fail (turn AMP off)") % device.upper()))


_SFT_STATS = {"key": "", "lens": None}


def _sft_sample_lens(cfg: Dict[str, Any]) -> list:
    """读 SFT 语料的每条样本长度（<\\x01问\\x02答\\x03>），按 (路径, mtime) 缓存。"""
    t = cfg.get("train") or {}
    path = str(t.get("sft_corpus") or "").strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        key = "%s|%s" % (path, os.path.getmtime(path))
        if _SFT_STATS["key"] == key:
            return _SFT_STATS["lens"] or []
        text = open(path, "r", encoding="utf-8", errors="replace").read()
        lens = []
        for chunk in text.split("\x03"):
            k = chunk.find("\x01")
            if k < 0 or "\x02" not in chunk:
                continue
            body = chunk[k:]
            q, a = body.split("\x02", 1)
            if not a.strip():
                continue
            lens.append(len(q) + len(a) + 2)
        _SFT_STATS["key"] = key
        _SFT_STATS["lens"] = lens
        return lens
    except Exception:                                # noqa: BLE001
        return []


def _check_sft_block(out: List[Dict[str, Any]], params: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """指令微调：上下文长度装不下问答样本时，训练只看到答句开头（用户 2026-10-19 排查结论）。"""
    t = cfg.get("train") or {}
    block = _f(params, t, "block_size")
    if block is None:
        return
    lens = _sft_sample_lens(cfg)
    if not lens:
        return
    fit = sum(1 for L in lens if L + 1 <= block)
    if fit >= len(lens) * 0.9:
        return
    med = sorted(lens)[len(lens) // 2]
    out.append(_issue(
        "warn", "block_size",
        _T("上下文长度 %s 只能完整装下 %d/%d 条问答（样本中位 %d 字符）。被截断的样本学不到「答完即停」，"
           "生成会像在语料里漫天续写；建议调到 %d 以上",
           "Context length %s fully fits only %d/%d QA pairs (median sample %d chars). Truncated samples never learn "
           "when to stop, so generation keeps rambling; set it to at least %d")
        % (_fmt(block), fit, len(lens), med,
           min(2048, max(256, int(med * 2)))),
        _fmt(block)))


def _check_pretrain(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    d_model = _f(params, t, "d_model")
    n_head = _f(params, t, "n_head")
    n_layer = _f(params, t, "n_layer")
    block = _f(params, t, "block_size")

    if d_model is not None and d_model < 16:
        out.append(_issue("error", "d_model", _T("模型维度 %s 太小，连词表嵌入都撑不住（至少 16）", "Model dim %s is too small even for the token embedding (at least 16)") % _fmt(d_model),
                          _fmt(d_model)))
    elif d_model is not None and d_model < 64:
        out.append(_issue("warn", "d_model", _T("模型维度 %s 偏小，表达能力有限（常用 128~512）", "Model dim %s is small; capacity is limited (usually 128~512)") % _fmt(d_model),
                          _fmt(d_model)))
    elif d_model is not None and d_model > 1024:
        out.append(_issue("warn", "d_model", _T("模型维度 %s 很大，本机训练会非常慢（常用 128~512）", "Model dim %s is large; training will be very slow here (usually 128~512)") % _fmt(d_model),
                          _fmt(d_model)))

    if n_head is not None:
        if n_head < 1:
            out.append(_issue("error", "n_head", _T("注意力头数至少为 1（当前 %s）", "Attention heads must be at least 1 (currently %s)") % _fmt(n_head), _fmt(n_head)))
        elif d_model is not None and n_head > 0 and int(d_model) % int(n_head) != 0:
            out.append(_issue("error", "n_head",
                              _T("%s 不能被 %s 整除，每个头的维度不是整数，训练直接报错（常见组合：d_model 128 / n_head 4、8）", "%s is not divisible by %s, so head size is not an integer and training fails (try d_model 128 with 4 or 8 heads)")
                              % (_fmt(d_model), _fmt(n_head)), _fmt(n_head)))

    if n_layer is not None:
        if n_layer < 1:
            out.append(_issue("error", "n_layer", _T("层数至少为 1（当前 %s）", "Layers must be at least 1 (currently %s)") % _fmt(n_layer), _fmt(n_layer)))
        elif n_layer > 24:
            out.append(_issue("warn", "n_layer", _T("层数 %s 偏多，本机训练会明显变慢（常用 4~12）", "Layer count %s is high; training slows down a lot here (usually 4~12)") % _fmt(n_layer),
                              _fmt(n_layer)))

    if block is not None:
        if block < 8:
            out.append(_issue("error", "block_size", _T("上下文长度 %s 太短，一条样本都装不下（至少 8）", "Context length %s is too short to hold even one sample (at least 8)") % _fmt(block),
                              _fmt(block)))
        elif block < 64:
            out.append(_issue("warn", "block_size", _T("上下文长度 %s 偏短，问答/多轮样本会被切碎（常用 128~512）", "Context length %s is short; QA and multi-turn samples get chopped (usually 128~512)") % _fmt(block),
                              _fmt(block)))
        elif block > 2048:
            out.append(_issue("warn", "block_size",
                              _T("上下文长度 %s 很长，注意力开销按平方增长，内存/显存容易爆（常用 128~512）", "Context length %s is very long; attention cost grows quadratically and memory blows up (usually 128~512)") % _fmt(block),
                              _fmt(block)))

    _check_batch(out, _f(params, t, "batch_size"))
    _check_dropout(out, "dropout", _f(params, t, "dropout"))
    _check_steps(out, "pretrain_steps", _f(params, t, "pretrain_steps"))
    _check_lr(out, "pretrain_lr", _f(params, t, "pretrain_lr"))

    for key in ("grad_clip", "grad_accum"):
        v = _f(params, t, key)
        if v is None:
            continue
        if key == "grad_clip":
            if v <= 0:
                out.append(_issue("error", "grad_clip", _T("梯度裁剪必须大于 0（当前 %s）", "Gradient clip must be greater than 0 (currently %s)") % _fmt(v), _fmt(v)))
            elif v > 10:
                out.append(_issue("warn", "grad_clip", _T("梯度裁剪 %s 偏大，几乎等于不裁剪（常用 1.0）", "Gradient clip %s is large, close to no clipping (usually 1.0)") % _fmt(v), _fmt(v)))
        else:
            if v < 1:
                out.append(_issue("error", "grad_accum", _T("梯度累积至少为 1（当前 %s）", "Gradient accumulation must be at least 1 (currently %s)") % _fmt(v), _fmt(v)))
            elif v > 32:
                out.append(_issue("warn", "grad_accum", _T("梯度累积 %s 很大，等效批量大小被放大很多倍（常用 1~8）", "Gradient accumulation %s is large; the effective batch is multiplied many times (usually 1~8)") % _fmt(v),
                                  _fmt(v)))

    _check_amp(out, params, cfg)
    return out


def _check_continue(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    _check_batch(out, _f(params, t, "batch_size"))
    _check_dropout(out, "dropout", _f(params, t, "dropout"))
    steps = _f(params, t, "continue_steps")
    lr = _f(params, t, "continue_lr")
    _check_steps(out, "continue_steps", steps)
    _check_lr(out, "continue_lr", lr)
    if lr is not None and 5e-4 < lr <= _LR_MAX:      # 超过 _LR_MAX 已经报过「会发散」，不重复
        out.append(_issue("warn", "continue_lr",
                          _T("继续预训练的学习率 %s 偏大，容易把已有能力冲掉（常见 1e-5 ~ 1e-4）", "Continue-pretrain LR %s is high and can wash out existing ability (usually 1e-5 ~ 1e-4)") % _fmt(lr), _fmt(lr)))
    _check_amp(out, params, cfg)
    return out


def _check_full(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    _check_batch(out, _f(params, t, "batch_size"))
    _check_dropout(out, "dropout", _f(params, t, "dropout"))
    _check_steps(out, "sft_steps", _f(params, t, "sft_steps"))
    lr = _f(params, t, "sft_lr")
    _check_lr(out, "sft_lr", lr)
    if lr is not None and 2e-3 < lr <= _LR_MAX:      # 同上：发散警告已经覆盖更大的值
        out.append(_issue("warn", "sft_lr",
                          _T("指令微调学习率 %s 偏大，容易把预训练学到的语言能力冲掉（常见 1e-4 ~ 5e-4）",
                             "Instruction-tune LR %s is high and can wash out the language ability learned in pretraining (usually 1e-4 ~ 5e-4)")
                          % _fmt(lr), _fmt(lr)))
    _check_amp(out, params, cfg)
    return out


def _check_lora(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    r = _f(params, t, "lora_r")
    alpha = _f(params, t, "lora_alpha")
    if r is not None:
        if r < 1:
            out.append(_issue("error", "lora_r", _T("LoRA 秩至少为 1（当前 %s）", "LoRA rank must be at least 1 (currently %s)") % _fmt(r), _fmt(r)))
        elif r > 128:
            out.append(_issue("warn", "lora_r", _T("LoRA 秩 %s 偏大，参数量接近全参微调，小语料上很容易过拟合（常用 4~32）", "LoRA rank %s is large; trainable params approach full fine-tuning and small corpora overfit (usually 4~32)")
                              % _fmt(r), _fmt(r)))
    if r and alpha and r > 0:
        ratio = round(alpha / r, 2)
        if ratio < 0.25:
            out.append(_issue("warn", "lora_alpha",
                              _T("alpha/秩 = %g 偏小，LoRA 更新会被压得很弱（常用 1 或 2，即 alpha = r 或 2r）", "alpha/rank = %g is low; LoRA updates get heavily damped (usually 1 or 2, i.e. alpha = r or 2r)") % ratio,
                              _fmt(alpha)))
        elif ratio > 8:
            out.append(_issue("warn", "lora_alpha",
                              _T("alpha/秩 = %g 偏大，更新幅度过强容易训崩（常用 1 或 2）", "alpha/rank = %g is high; updates are too strong and training can blow up (usually 1 or 2)") % ratio, _fmt(alpha)))
    _check_dropout(out, "lora_dropout", _f(params, t, "lora_dropout"))
    targets = _s(params, t, "lora_targets").strip()
    if targets:
        bad = [x.strip() for x in targets.split(",") if x.strip() and x.strip().lower() not in _TARGETS_OK]
        if bad:
            out.append(_issue("warn", "lora_targets",
                              _T("挂载目标里 %s 不认识，实际只认 qkv / proj / fc_in / fc_out；"
                                 "全都无效时会回落到全部层（小语料建议只挂 qkv,proj）",
                                 "Unknown target(s) %s; only qkv / proj / fc_in / fc_out are accepted, and if none is valid "
                                 "it falls back to every layer (with a small corpus use qkv,proj)")
                              % "、".join(bad), targets))
    _check_batch(out, _f(params, t, "batch_size"))
    # 2026-10-23：LoRA 用自己的步数/学习率键（原来和指令微调共用 sft_*）
    _check_steps(out, "lora_steps", _f(params, t, "lora_steps"))
    _check_lr(out, "lora_lr", _f(params, t, "lora_lr"))
    # 已 SFT 的基座再挂 LoRA，学习率超过 2e-4 会把基座学好的东西拆掉（实测 3e-4 连训练集 Loss 都变差）
    lr = _f(params, t, "lora_lr")
    if lr is not None and lr > 2e-4:
        out.append(_issue("warn", "lora_lr",
                          _T("LoRA 学习率 %s 偏大：基座若已完成指令微调，>2e-4 容易把已学好的能力练坏（建议 1e-4~2e-4）", "LoRA LR %s is high: on an instruction-tuned base, >2e-4 can degrade what it already learned (try 1e-4~2e-4)") % _fmt(lr),
                          _fmt(lr)))
    _check_amp(out, params, cfg)
    return out


def _check_merge(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 合并页目前只有「基座 + 适配器」，参数审查只兜住权重缺失这一种明显不可用
    return []


def _check_prune(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    # 压缩页的键叫 sparsity，旧接口叫 prune_ratio —— 两个都认
    ratio = _f(params, t, "prune_ratio")
    if ratio is None:
        ratio = _f(params, t, "sparsity")
    if ratio is None:
        return out
    if ratio < 0 or ratio >= 1:
        out.append(_issue("error", "prune_ratio", _T("裁剪比例必须在 0 ~ 1 之间（当前 %s）", "Prune ratio must be between 0 and 1 (currently %s)") % _fmt(ratio), _fmt(ratio)))
    elif ratio > 0.9:
        out.append(_issue("warn", "prune_ratio",
                          _T("裁剪 %s 后模型基本只剩骨架，效果会明显下降（常用 0.1~0.5）",
                             "After pruning %s the model is mostly a skeleton and quality drops sharply (usually 0.1~0.5)")
                          % _fmt(ratio), _fmt(ratio)))
    return out


def _check_quant(params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = cfg.get("train") or {}
    bits = _f(params, t, "quant_bits")
    scheme = _s(params, t, "scheme").strip().upper()
    if bits is None and scheme:
        m = re.match(r"^W(\d+)A(\d+)$", scheme)
        if not m:
            out.append(_issue("warn", "quant_bits",
                              _T("量化方案「%s」不认识，常见写法是 W8A16 / W4A16 / W2A16", "Unknown quantization scheme \u201c%s\u201d; use W8A16 / W4A16 / W2A16") % scheme, scheme))
            return out
        bits = float(m.group(1))
    if bits is None:
        return out
    if int(bits) not in (2, 4, 8):
        out.append(_issue("error", "quant_bits",
                          _T("权重量化位宽只支持 2 / 4 / 8（当前 W%d）", "Weight bits only support 2 / 4 / 8 (currently W%d)") % int(bits), "W%d" % int(bits)))
    return out


_CHECKERS = {
    "pretrain": _check_pretrain,
    "continue": _check_continue,
    "full": _check_full,
    "lora": _check_lora,
    "merge": _check_merge,
    "prune": _check_prune,
    "quant": _check_quant,
}


def check(kind: str, params: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    fn = _CHECKERS.get(kind)
    if not fn:
        return []
    return fn(params or {}, cfg or {})


def _pipeline_issues(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """流水线：把每一步的参数按它自己的训练方式审一遍（服务端读已保存的步骤）。"""
    steps = ((cfg.get("pipeline") or {}).get("steps")) or []
    out: List[Dict[str, Any]] = []
    for idx, it in enumerate(steps):
        if not isinstance(it, dict):
            continue
        task = str(it.get("task") or "").strip()
        kind = {"sft": "full"}.get(task, task)
        name = str(it.get("name") or (_KIND_LABEL_EN_OR_ZH(kind) or task))
        params = it.get("params") or {}
        for one in check(kind, params, cfg):
            one = dict(one)
            one["label"] = _T("第 %d 步 %s · %s", "Step %d %s · %s") % (
                idx + 1, name, one.get("label") or one.get("field"))
            out.append(one)
    return out


def api_check(body: Dict[str, Any]) -> Dict[str, Any]:
    import _host                                     # 局部导入：避免模块加载顺序问题

    body = body or {}

    global _LANG
    _LANG = "en" if str(body.get("lang") or "").lower().startswith("en") else "zh"

    kind = str(body.get("kind") or "").strip()
    params = body.get("params") or {}
    try:
        cfg = _host.load_cfg()
    except Exception:                                # noqa: BLE001
        cfg = {}
    if kind == "pipeline":
        issues = _pipeline_issues(cfg)
    else:
        issues = check(kind, params, cfg)
        if kind == "full":                            # 指令微调：上下文长度 vs 样本长度（用户 2026-10-19）
            extra: List[Dict[str, Any]] = []
            _check_sft_block(extra, params, cfg)
            issues = list(issues) + extra
    return {
        "ok": True,
        "kind": kind,
        "kind_label": (_KIND_LABEL_EN_OR_ZH(kind) or kind),
        "issues": issues,
        "has_error": any(i.get("level") == "error" for i in issues),
    }



def register(router) -> None:
    router.post("/api/train/check", api_check)
