"""EAI 训练入口。

配置来自 JSON（由 UI 生成），避免超长命令行。
支持优化项：权重绑定、dropout、梯度裁剪、cosine LR、混合精度、MTP、梯度累积、LoRA。
评估间隔默认 300 步（可在配置里改）。

两阶段：
  - stage=pretrain  用纯文本语料从零训语言模型
  - stage=sft       从 pretrain 权重续训对话/问答语料（init_from 指定权重）

微调方式（method）：
  - full  全量微调：所有参数参与更新（默认）
  - lora  低秩适配：冻结原权重，只训旁路 A/B 小矩阵；
          只存 *.lora.pt 适配器（可复用/换基座），训练时不合并；
          合并成完整权重放到「导出模型」页可选做（core/lora.merge_adapter_file）。
          lora 必须基于已有权重续训。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import backend_marker  # noqa: E402  与设置页/总览共用的落盘缓存
from core.transformer import GPT  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def load_corpus(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("\r\n", "\n")
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def build_vocab(text: str):
    chars = sorted(set(text))
    stoi = {c: i for i, c in enumerate(chars)}
    itos = {i: c for c, i in stoi.items()}
    return chars, stoi, itos


def encode(text: str, stoi: dict):
    import numpy as np

    arr = np.fromiter((stoi.get(c, 0) for c in text), dtype=np.int32, count=len(text))
    return torch.from_numpy(arr).long()


def get_batch(data, batch_size, block_size, generator, device):
    # 语料太短时报人话（否则 torch.randint 只丢一句 from>=to，看不出是语料问题）
    if len(data) <= block_size + 1:
        raise SystemExit(
            f"语料太短：{len(data)} 字符 < block_size {block_size} + 2。"
            f"请换更长的语料，或把「块大小 block_size」调小。"
        )
    ix = torch.randint(len(data) - block_size - 1, (batch_size,), generator=generator)
    x = torch.stack([data[i : i + block_size] for i in ix])
    y = torch.stack([data[i + 1 : i + 1 + block_size] for i in ix])
    # 关键：不能用 non_blocking=True 做 CPU→XPU 的异步拷贝（用户 2026-10-19）。
    # 两次异步拷贝会共用同一块中转缓冲，y 经常被 x 的内容覆盖 → 标签等于输入，
    # 模型学会"复读最后一个字符"，训练 loss 掉得飞快、生成却是乱的（XPU 上 200 个
    # batch 里 196 个 y == x）。改成同步拷贝，宁可慢一点。
    return x.to(device), y.to(device)


def build_sft_examples(text: str, stoi: dict, block_size: int) -> list:
    """把 <\x01问\x02答\x03> 语料切成一条条样本（用户 2026-10-19）。

    原来 SFT 和预训练一样按「整条语料随机截 256 字」抽样，结果：
      · 一个 batch 里很少同时包含问、答、结束符，模型学不到「答完要停」；
      · \x03 在 31 万字里只出现 321 次（0.1%），EOS 基本学不会，生成永远跑满上限；
      · 问句也算 loss，答句的监督被稀释。
    这里按 \x03 切样本，返回 [(ids, sep_pos)]，sep_pos 之前的 token 不计 loss。
    """
    examples = []
    sot, sep_tok, eot = stoi.get("\x01"), stoi.get("\x02"), stoi.get("\x03")
    if sot is None or sep_tok is None:
        return examples
    for chunk in text.split("\x03"):
        k = chunk.find("\x01")
        if k < 0:
            continue
        chunk = chunk[k:]
        if "\x02" not in chunk:
            continue
        ids = [stoi[c] for c in chunk if c in stoi]
        if not ids:
            continue
        if eot is not None:
            ids.append(eot)
        ids = ids[:block_size]
        if sep_tok not in ids:
            continue
        sep = ids.index(sep_tok)
        if sep <= 0 or sep >= len(ids) - 1:
            continue
        examples.append((ids, sep))
    return examples


def get_batch_sft(examples: list, batch_size: int, block_size: int, generator, device, pad_id: int = 0):
    """SFT 取批：每条从 \x01 开始、到 \x03 结束；问句与补齐位不算 loss（目标置 -1）。"""
    pick = torch.randint(len(examples), (batch_size,), generator=generator)
    x = torch.full((batch_size, block_size), pad_id, dtype=torch.long)
    y = torch.full((batch_size, block_size), -1, dtype=torch.long)
    for row, i in enumerate(pick.tolist()):
        ids, sep = examples[i]
        n = min(len(ids) - 1, block_size)
        if n <= 0:
            continue
        x[row, :n] = torch.tensor(ids[:n])
        tgt = torch.tensor(ids[1:n + 1])
        tgt[: max(0, min(n, sep))] = -1       # 问句 + \x02 本身不监督
        y[row, :n] = tgt
    return x.to(device), y.to(device)


@torch.no_grad()
def estimate_loss(model, splits, batch_size, block_size, eval_iters, generator, device, batch_fn=None):
    model.eval()
    out = {}
    for name, data in splits.items():
        losses = torch.zeros(eval_iters)
        # 验证集固定抽样（用户 2026-10-22 排查）：原来和训练共用随机 generator，
        # 每次 eval 抽到的样本不同 → 同一步的 val 换个 eval_every 就上下飘（实测同一次
        # LoRA 训练里 val 在 1.76 ~ 2.65 之间跳，看不出到底学没学到）。
        # 这里每次评估都用固定种子重新抽同一批 → 各步之间可比。
        eval_gen = torch.Generator().manual_seed(20261022)
        for k in range(eval_iters):
            x, y = (batch_fn(data, eval_gen) if batch_fn
                    else get_batch(data, batch_size, block_size, eval_gen, device))
            _, loss = model(x, y)
            losses[k] = loss.item()
        out[name] = losses.mean().item()
    model.train()
    return out


def cosine_lr(step, total, base_lr, warmup, min_ratio=0.1):
    if step < warmup:
        return base_lr * (step + 1) / max(1, warmup)
    if step >= total:
        return base_lr * min_ratio
    progress = (step - warmup) / max(1, total - warmup)
    return base_lr * (min_ratio + 0.5 * (1 - min_ratio) * (1 + math.cos(math.pi * progress)))


def expand_vocab(model, stoi: dict, itos: dict, extra_chars: str):
    """把新语料里出现、旧词表没有的字符补进模型，扩写嵌入层/输出头。

    两阶段训练时阶段二的对话语料往往含中文/标点，阶段一纯文本未必见过；
    直接丢掉这些字符会让模型答非所问，所以这里做一次安全扩表。
    返回 (stoi, itos, 新增字符数)。
    """
    import torch.nn as nn

    new_chars = [c for c in sorted(set(extra_chars)) if c not in stoi]
    if not new_chars:
        return stoi, itos, 0

    old_v = len(itos)
    new_v = old_v + len(new_chars)
    device = model.tok_emb.weight.device
    dtype = model.tok_emb.weight.dtype
    # 先记住是否与嵌入绑定，替换 tok_emb 后 identity 会失效
    was_tied = model.head.weight is model.tok_emb.weight

    def _expand_linear(linear: nn.Linear, out_features: int):
        old_w = linear.weight.data
        new_w = torch.zeros(out_features, old_w.size(1), device=device, dtype=dtype)
        new_w[: old_w.size(0)] = old_w
        nn.init.normal_(new_w[old_w.size(0) :], mean=0.0, std=0.02)
        linear.weight = nn.Parameter(new_w)
        linear.out_features = out_features

    # 词嵌入
    old_emb = model.tok_emb.weight.data
    new_emb = torch.zeros(new_v, model.d_model, device=device, dtype=dtype)
    new_emb[:old_v] = old_emb
    nn.init.normal_(new_emb[old_v:], mean=0.0, std=0.02)
    model.tok_emb = nn.Embedding(new_v, model.d_model, device=device)
    model.tok_emb.weight.data.copy_(new_emb)
    model.vocab_size = new_v

    # 输出头
    if was_tied:
        model.head.weight = model.tok_emb.weight
    else:
        _expand_linear(model.head, new_v)

    if model.mtp_heads:
        for h in model.mtp_heads:
            _expand_linear(h, new_v)

    for i, c in enumerate(new_chars):
        idx = old_v + i
        stoi[c] = idx
        itos[idx] = c
    return stoi, itos, len(new_chars)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    cfg = json.load(open(args.config, "r", encoding="utf-8-sig"))

    def pick_device(requested: str):
        """把 auto/显式设备名解析成真正可用的 torch.device。"""
        req = (requested or "auto").strip().lower()
        if req == "auto":
            if torch.cuda.is_available():
                return torch.device("cuda")
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                return torch.device("xpu")
            return torch.device("cpu")
        if req == "cuda" and not torch.cuda.is_available():
            print("配置要求 cuda 但当前不可用，自动回退")
            return pick_device("auto")
        if req == "xpu" and not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            print("配置要求 xpu 但当前不可用，自动回退")
            return pick_device("auto")
        return torch.device(req)

    device = pick_device(cfg.get("device", "auto"))
    backend_marker.note(torch)                  # 训练是本进程第一次真拿到 torch 的地方：顺手落盘
    if device.type == "xpu":
        desc = torch.xpu.get_device_name(0)
    elif device.type == "cuda":
        desc = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        desc += f" · sm_{cap[0]}{cap[1]}"
    else:
        desc = f"{torch.get_num_threads()} 线程"
    print(f"设备 {device}  ({desc})，torch {torch.__version__}")
    print(f"配置 {args.config}")

    torch.manual_seed(cfg.get("seed", 1337))
    g = torch.Generator().manual_seed(cfg.get("seed", 1337))

    stage = cfg.get("stage", "pretrain")
    init_from = cfg.get("init_from") or ""
    text = load_corpus(cfg["corpus"])
    print(f"语料 {os.path.basename(cfg['corpus'])}：{len(text):,} 字符")
    print(f"阶段 {stage}" + (f" · 续训自 {os.path.basename(init_from)}" if init_from else " · 从零开始"))

    use_mtp = bool(cfg.get("mtp", False))
    mtp_k = int(cfg.get("mtp_k", 3))
    ckpt_payload = None
    if init_from:
        if not os.path.exists(init_from):
            print(f"找不到初始权重 {init_from}，改为从零训练", flush=True)
            init_from = ""
        else:
            ckpt_payload = torch.load(init_from, map_location="cpu", weights_only=False)
            # 词表以旧权重为准，后面按新语料扩表，保证已学到的字符 id 不变
            stoi = dict(ckpt_payload["stoi"])
            itos = {int(k): v for k, v in ckpt_payload["itos"].items()}
            old_args = ckpt_payload.get("args") or {}
            arch = {
                "d_model": old_args.get("d_model", cfg["d_model"]),
                "n_head": old_args.get("n_head", cfg["n_head"]),
                "n_layer": old_args.get("n_layer", cfg["n_layer"]),
                "block_size": old_args.get("block_size", cfg["block_size"]),
                "tie_weights": old_args.get("tie_weights", cfg.get("tie_weights", True)),
                "d_ff": old_args.get("d_ff") or cfg.get("d_ff") or None,
                "mtp": old_args.get("mtp", use_mtp),
                "mtp_k": old_args.get("mtp_k", mtp_k),
            }
            cfg.update({k: v for k, v in arch.items() if v is not None})
            use_mtp = bool(cfg["mtp"])
            mtp_k = int(cfg["mtp_k"])
    new_chars: list = []
    if ckpt_payload is None:
        # 从零训练。这里必须用 ckpt_payload 判断而不是 else：
        # 「init_from 有值但文件不存在」时上面把 init_from 清空了、却跳过了 else 分支，
        # 于是 stoi/itos 从未定义，下面 len(itos) 直接 UnboundLocalError（真 bug，已修）。
        chars, stoi, itos = build_vocab(text)

    model = GPT(
        vocab_size=len(itos),
        d_model=cfg["d_model"],
        n_head=cfg["n_head"],
        n_layer=cfg["n_layer"],
        max_len=cfg["block_size"],
        dropout=cfg.get("dropout", 0.1),
        tie_weights=cfg.get("tie_weights", True),
        d_ff=cfg.get("d_ff") or None,
        use_mtp=use_mtp,
        mtp_k=mtp_k,
    ).to(device)

    if ckpt_payload is not None:
        missing, unexpected = model.load_state_dict(ckpt_payload["model"], strict=False)
        if missing:
            print(f"权重缺失键 {len(missing)} 个（多为新建头）: {missing[:6]}…")
        if unexpected:
            print(f"权重多余键 {len(unexpected)} 个: {unexpected[:6]}…")
        _vocab_before = set(stoi)
        stoi, itos, n_new = expand_vocab(model, stoi, itos, text)
        new_chars = [c for c in sorted(set(text)) if c not in _vocab_before]
        print(f"已加载初始权重 · 词表 {len(itos):,}（新扩 {n_new} 字符）")
    else:
        # 从零训练时也确保词表覆盖全文
        pass

    # ---------- LoRA：冻结全量，只训低秩适配 ----------
    from core import lora as lora_lib

    method = (cfg.get("method") or "full").strip().lower()
    use_lora = method == "lora"
    if use_lora:
        if ckpt_payload is None:
            raise SystemExit(
                "LoRA 微调必须基于已有权重：请先完成预训练，"
                "或在配置里指定 init_from（LoRA 不从零训）。"
            )
        lora_r = int(cfg.get("lora_r", 8))
        lora_alpha = float(cfg.get("lora_alpha", 16))
        lora_drop = float(cfg.get("lora_dropout", 0.05))
        lora_targets = cfg.get("lora_targets") or lora_lib.DEFAULT_TARGETS
        # 词表扩写与 LoRA 冲突（用户 2026-10-22 排查确认）：expand_vocab 会新建嵌入行并随机初始化，
        # 而 apply_lora 之后全模型（含新建行）都被冻结、适配器也只存 .lora_ 张量 ——
        # 新字符拿到的是**永远不更新的随机向量**，训练白跑、合并回去还会丢词表。直接拦下来。
        if new_chars:
            _sample = "".join(new_chars[:10])
            raise SystemExit(
                "LoRA 微调不能扩词表：这份语料里有 %d 个字符不在基座词表里（%s…）。"
                "LoRA 只训低秩旁路、冻结嵌入层，这些新字符会拿到随机向量且永远学不到。"
                "请先用同样的语料做一次「指令微调」或「继续预训练」把词表扩好，再在它的产物上挂 LoRA。"
                % (len(new_chars), _sample))
        n_wrapped = lora_lib.apply_lora(model, lora_r, lora_alpha, lora_drop, lora_targets)
        if n_wrapped == 0:
            raise SystemExit(f"LoRA 未匹配到任何目标层：{lora_targets}")
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        total = model.n_params()
        print(
            f"LoRA 启用 · r={lora_r} α={lora_alpha} dropout={lora_drop} "
            f"目标={lora_targets} · 挂载 {n_wrapped} 层"
        )
        print(f"可训练参数 {trainable:,} / 总参数 {total:,}（{trainable / max(1, total) * 100:.2f}%）")
        cfg["lora"] = {
            "r": lora_r, "alpha": lora_alpha, "dropout": lora_drop,
            "targets": lora_targets, "wrapped": n_wrapped,
        }
        # 自动续训（用户 2026-10-19）：LoRA 只存适配器，崩溃后从上次的 *.lora.pt 接着练
        resume_adapter = str(cfg.get("resume_adapter") or "").strip()
        if resume_adapter and os.path.isfile(resume_adapter):
            try:
                payload = torch.load(resume_adapter, map_location="cpu", weights_only=False)
                loaded = lora_lib.load_lora_adapter(model, payload)
                if loaded:
                    print(f"续训：已从适配器 {os.path.basename(resume_adapter)} 载入 {loaded} 个张量", flush=True)
                else:
                    print("续训：[警告] 旧适配器与当前 r / 挂载目标不匹配（载入 0 个张量），"
                          "本次从零训练适配器", flush=True)
            except Exception as exc:                     # noqa: BLE001
                print(f"续训：适配器载入失败（{type(exc).__name__}: {exc}），本次从零训练适配器", flush=True)
    else:
        method = "full"

    print(f"模型 {model.n_params():,} 参数 · 词表 {len(itos):,} · MTP={'开' if use_mtp else '关'}"
          f" · 微调方式={method}")
    data = encode(text, stoi)
    val_ratio = float(cfg.get("val_ratio") or 0.1)      # 验证集比例（用户 2026-10-19）
    val_ratio = min(0.5, max(0.02, val_ratio))
    sft_mode = False
    batch_fn = None
    if stage == "sft":
        examples = build_sft_examples(text, stoi, int(cfg["block_size"]))
        if len(examples) < 8:
            raise SystemExit("SFT 语料里找不到 <\\x01问\\x02答\\x03> 结构的样本，请检查数据集。")
        cut = int((1.0 - val_ratio) * len(examples))
        splits = {"train": examples[:cut], "val": examples[cut:]}
        sft_mode = True
        avg = sum(len(e[0]) for e in examples) / len(examples)
        print("SFT 样本 %d 条（按 \\x01问\\x02答\\x03 切分）· 平均 %.0f 字符/条 · 只监督答句"
              % (len(examples), avg))
        print("训练集 %d 条，验证集 %d 条" % (cut, len(examples) - cut))
        sft_pad = stoi.get("\x00", 0)
    else:
        n = int((1.0 - val_ratio) * len(data))
        splits = {"train": data[:n], "val": data[n:]}
        print(f"训练集 {n:,} 字符，验证集 {len(data) - n:,} 字符")

    tokens = cfg["steps"] * cfg["batch_size"] * cfg["block_size"]
    print(f"计划训练量 {tokens:,} 字符 ≈ 语料读 {tokens / max(1, len(text)):.1f} 遍")

    # LoRA 时只把可训练参数（A/B）交给优化器；全量则交给全部参数
    trainable_params = [p for p in model.parameters() if p.requires_grad]
    # 2026-10-18：XPU（Arc 140V 共享显存）上默认的多张量 / fused 路径会在 step() 里
    # 一次性为所有参数申请临时缓冲，直接 UR_RESULT_ERROR_OUT_OF_DEVICE_MEMORY。
    # 强制逐参数更新，峰值内存降到最低；8k 万参数以内速度差别可以忽略。
    _opt_kw = dict(lr=cfg.get("lr", 3e-3), weight_decay=cfg.get("weight_decay", 0.05),
                   betas=(0.9, 0.95), foreach=False)
    try:
        opt = torch.optim.AdamW(trainable_params, fused=False, **_opt_kw)
    except TypeError:                                   # 老版本 torch 没有 fused 关键字
        opt = torch.optim.AdamW(trainable_params, **_opt_kw)

    base_lr = cfg.get("lr", 3e-3)
    warmup = cfg.get("warmup", 2000)
    steps = cfg["steps"]
    batch_size = cfg["batch_size"]
    block_size = cfg["block_size"]
    eval_every = cfg.get("eval_every", 300)
    eval_iters = cfg.get("eval_iters", 20)
    grad_clip = cfg.get("grad_clip", 1.0) or 0.0
    accum = max(1, int(cfg.get("grad_accum", 1)))
    # SFT：按样本取批（只监督答句）；预训练仍按连续窗口取批
    if sft_mode:
        # gen 可外部指定（验证集用固定种子的 generator，用户 2026-10-22）
        batch_fn = (lambda split, gen=None:
                    get_batch_sft(split, batch_size, block_size, gen or g, device, sft_pad))
    use_amp = cfg.get("amp", False) and device.type in ("xpu", "cuda")
    amp_name = (cfg.get("amp_dtype") or "auto").lower()
    if amp_name == "auto":
        # Pascal（GTX 10 系）原生 fp16；Ampere+ 用 bf16 更稳
        if device.type == "cuda":
            cap = torch.cuda.get_device_capability(0)
            amp_name = "bf16" if cap[0] >= 8 else "fp16"
        else:
            amp_name = "bf16"
    amp_dtype = torch.bfloat16 if amp_name == "bf16" else torch.float16
    if use_amp:
        print(f"混合精度 {amp_name}（{amp_dtype}）")
    out_path = cfg.get("out", "checkpoints/model.pt")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    log_csv = cfg.get("log_csv", "logs/loss_curve.csv")
    os.makedirs(os.path.dirname(log_csv) or ".", exist_ok=True)
    prompt = cfg.get("prompt", "你好")
    sample_chars = int(cfg.get("sample_chars", 150))
    # UI 点「停止」时会创建这个文件；见到就保存权重并退出
    stop_flag = os.path.join(os.path.dirname(os.path.abspath(args.config)), "stop.flag")

    scaler = torch.amp.GradScaler(device.type, enabled=use_amp and amp_dtype == torch.float16)
    autocast = torch.amp.autocast(device.type, dtype=amp_dtype, enabled=use_amp)

    def ffn_width():
        """d_ff = FFN 隐层宽度。LoRA 会把 fc_in/fc_out 包成 LoRALinear（没有 out_features），
        必须拆到底层 Linear 再取，否则存档时 AttributeError（真 bug，已修）。"""
        if not getattr(model, "blocks", None):
            return None
        ffn = getattr(model.blocks[0], "ffn", None)
        for attr in ("fc_in", "fc_out"):
            layer = getattr(ffn, attr, None)
            if layer is None:
                continue
            layer = getattr(layer, "base", layer)      # LoRALinear → 原 Linear
            width = getattr(layer, "out_features", None)
            if width:
                return int(width)
        return None

    def method_label() -> str:
        """这次训练在总览日志里该叫什么（用户 2026-10-20）。"""
        if method == "lora":
            return "LoRA 微调"
        return {"pretrain": "预训练", "continue": "继续预训练", "sft": "指令微调"}.get(str(stage), "训练")

    def note_method(path: str) -> None:
        """把「这个产物是哪一种训练方式」记进 <工程>/logs/_train_methods.json。

        总览日志原来只靠文件名猜（lora/continue/pretrain/sft/model），
        TCLS_Check3.pt、TCLS.progress.json 这种全落到笼统的「训练」；
        用户 2026-10-20：指令微调的检查点就该写「指令微调」。
        """
        try:
            proj = os.path.dirname(os.path.dirname(os.path.abspath(out_path)))
            logs = os.path.join(proj, "logs")
            os.makedirs(logs, exist_ok=True)
            fp = os.path.join(logs, "_train_methods.json")
            data: dict = {}
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    data = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    data = {}
            data[os.path.basename(path)] = method_label()
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def save_ckpt(step: int, target: str | None = None, announce: bool = False) -> None:
        """落盘：全量存原生权重；LoRA 只存独立适配器（用户 2026-10-16：
        训练时不合并 —— 合并放到「导出模型」页可选做，见 core/lora.merge_adapter_file）。

        target：检查点专用（用户 2026-10-19）；给定时写到该路径，且不写 .progress.json
        （进度文件只代表正式产物，免得自动续训读错步数）。
        announce：这次落盘要不要打「已保存」文字（用户 2026-10-20：「训练输出不要每步都输出
        已保存....，只有check和最终保存时输出已保存的文字」）——检查点/停止落档传 True，
        eval 周期的静默保存不传。
        """
        from core import lora as lora_lib

        out_use = target or out_path
        if use_lora:
            _stem = os.path.splitext(out_use)[0]
            if _stem.lower().endswith(".lora"):
                _stem = _stem[:-5]        # 输出名自己带 .lora 时别再拼一层（否则 X.lora.lora.pt）
            adapter_path = _stem + ".lora.pt"
            os.makedirs(os.path.dirname(adapter_path) or ".", exist_ok=True)
            torch.save(
                {
                    **lora_lib.lora_checkpoint_payload(model),
                    # 自描述：合并时要按同一套 targets/dropout 复原（原来只存 r/alpha，
                    # 合并端只能回落到默认 targets），并记下训练用的基座，便于校验是否合错基座
                    "targets": str((cfg.get("lora") or {}).get("targets") or lora_lib.DEFAULT_TARGETS),
                    "dropout": float((cfg.get("lora") or {}).get("dropout") or 0.05),
                    "base": str(cfg.get("init_from") or ""),
                    "vocab": int(len(itos)),      # 词表大小：合并时校验是否同一套词表（2026-10-23）
                    # base_args 如实记本次训练方式（原来写死 "full"，审计/续训会把 LoRA 当全量微调）
                    "base_args": {**cfg, "method": method},
                    "stopped_at_step": step,              # 续训用（用户 2026-10-19）
                },
                adapter_path,
            )
            note_method(adapter_path)
            if target is None:                     # 检查点不写进度文件（用户 2026-10-19）
                try:
                    prog = os.path.splitext(adapter_path)[0] + ".progress.json"
                    with open(prog, "w", encoding="utf-8") as pf:
                        json.dump({"step": int(step), "out": adapter_path, "ts": time.time()}, pf)
                    note_method(prog)
                except OSError:
                    pass
            if announce:
                print(f"已保存 LoRA 适配器 → {adapter_path}（不合并，导出时可选合并）", flush=True)
            return
        ckpt = {
            "model": model.state_dict(),
            "stoi": stoi,
            "itos": itos,
            "args": {
                **cfg,
                "vocab_size": len(itos),
                "d_ff": ffn_width(),
                "stopped_at_step": step,
                "stage": stage,
                "method": method,
            },
        }
        torch.save(ckpt, out_use)
        note_method(out_use)
        if target is not None:                     # 检查点：只存权重，不动进度文件（用户 2026-10-19）
            if announce:
                print(f"[检查点] 第 {step} 步权重 → {out_use}", flush=True)
            return
        # 自动续训（用户 2026-10-19）：进度标记与权重同时落盘，宿主读它算「从第几步继续」
        try:
            prog = os.path.splitext(out_path)[0] + ".progress.json"
            with open(prog, "w", encoding="utf-8") as pf:
                json.dump({"step": int(step), "out": out_path, "ts": time.time()}, pf)
            note_method(prog)
        except OSError:
            pass

    def loss_snapshot(stem: str) -> None:
        """把「到此刻为止」的 Loss 曲线另存一份到 <工程>/logs/<stem>_loss.csv（用户 2026-10-20）。

        检查点要的是它自己保存那一刻的曲线，不是整场训练跑完的最终曲线：
        TCLS_Check1.pt ↔ logs/TCLS_Check1_loss.csv，TCLS.pt ↔ logs/TCLS_loss.csv。
        """
        try:
            proj = os.path.dirname(os.path.dirname(os.path.abspath(out_path)))
            logs = os.path.join(proj, "logs")
            if not os.path.isdir(logs):
                return
            try:
                loss_csv.flush()              # 检查点可能落在两次 eval 之间，先刷到磁盘
            except Exception:                 # noqa: BLE001  已关闭也不影响复制
                pass
            shutil.copyfile(log_csv, os.path.join(logs, stem + "_loss.csv"))
        except (OSError, shutil.Error):
            pass

    append_csv = bool(cfg.get("append_csv"))              # 自动续训：接着写，不覆盖前面的 Loss 曲线
    # 自动续训（用户 2026-10-19）：续跑时把步数接着往上数，Loss 曲线不会跳回 1
    step_offset = max(0, int(cfg.get("start_step") or 0))
    if step_offset:
        print(f"续训：本次接着第 {step_offset + 1} 步编号继续（共 {step_offset + int(cfg['steps'])} 步）", flush=True)
    loss_csv = open(log_csv, "a" if append_csv else "w", encoding="utf-8")
    if not append_csv or os.path.getsize(log_csv) == 0:
        loss_csv.write("step,train_loss,val_loss,lr,elapsed\n")
    # 检查点（用户 2026-10-19）：训练中途按等分点存中间权重，过拟合时能取回半成品。
    # N=1 → 1/2 处；N=2 → 1/3、2/3 处；最多 5 个。
    ckpt_count = max(0, min(5, int(cfg.get("ckpt_count") or 0)))
    ckpt_at = {}                      # 步号 → 第几次保存（1 起，用于 _Check<k> 文件名）
    if ckpt_count and steps >= 3:
        for k in range(1, ckpt_count + 1):
            at = int(round(steps * k / (ckpt_count + 1)))
            at = min(max(1, at), steps - 1)
            ckpt_at[at] = k
        ckpt_stem = os.path.splitext(os.path.basename(out_path))[0]
        print("检查点：共 %d 个，落在第 %s 步（1/（N+1）等分点）→ %s_Check1..%d" % (
            len(ckpt_at), "、".join(str(s + step_offset) for s in sorted(ckpt_at)),
            ckpt_stem, len(ckpt_at)), flush=True)
    t0 = time.time()
    model.train()
    print("--- 开始训练 ---")

    for step in range(steps):
        # 优雅停止：UI 写 stop.flag 后，这里存档退出，权重仍可用
        if stop_flag and os.path.exists(stop_flag):
            print(f"收到停止信号，保存当前权重（step {step}）…", flush=True)
            save_ckpt(step + step_offset, announce=True)
            break

        lr = (
            cosine_lr(step, steps, base_lr, warmup)
            if cfg.get("cosine_lr", True)
            else (base_lr * min(1.0, (step + 1) / max(1, warmup)))
        )
        for pg in opt.param_groups:
            pg["lr"] = lr

        opt.zero_grad(set_to_none=True)
        loss_acc = 0.0
        for _ in range(accum):
            x, y = (batch_fn(splits["train"]) if batch_fn
                    else get_batch(splits["train"], batch_size, block_size, g, device))
            with autocast:
                _, loss = model(x, y)
                loss = loss / accum
            scaler.scale(loss).backward()
            loss_acc += loss.item()
        if grad_clip > 0:
            scaler.unscale_(opt)
            # 2026-10-19：XPU 上 clip_grad_norm_ 默认走 foreach 路径（内部 _foreach_norm +
            # torch.stack + vector_norm），在 Arc 140V 上第 2~3 次调用就把 level_zero 打挂
            # （UR_RESULT_ERROR_OUT_OF_RESOURCES，报错还会落到下一步的 optimizer.step 里，
            # 看着像「训练到一半显存不够」）。实测 6 个 256×256 参数：默认 3 次必崩，
            # foreach=False 20/20 通过；真实 GPT 循环里默认第 2 步崩、foreach=False 40 步全过。
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip, foreach=False)
        scaler.step(opt)
        scaler.update()

        # 检查点（用户 2026-10-20）：等分点到了就存，和 eval_every 无关。
        # 原来这段写在下面的 eval 块里，只有等分点刚好是 eval_every 的倍数才会执行
        # （30000 步、2 个 → 10000 / 20000，都不是 300 的倍数，结果一个检查点都没落盘）。
        _ck_no = ckpt_at.get(step + 1)
        if _ck_no:
            _d, _b = os.path.split(out_path)
            _stem, _ext = os.path.splitext(_b)
            save_ckpt(step + 1 + step_offset,
                      target=os.path.join(_d, "%s_Check%d%s" % (_stem, _ck_no, _ext)),
                      announce=True)
            loss_snapshot("%s_Check%d" % (_stem, _ck_no))   # 检查点自己的曲线（到这一刻为止）

        if (step + 1) % eval_every == 0 or step == 0 or step == steps - 1:
            losses = estimate_loss(model, splits, batch_size, block_size, eval_iters, g, device,
                                   batch_fn=batch_fn)
            elapsed = time.time() - t0
            ppl = math.exp(min(20.0, losses["val"]))
            rate = (step + 1) * batch_size * block_size * accum / max(elapsed, 1e-6)
            print(
                f"step {step + 1 + step_offset:6d} | lr {lr:.2e} | train {losses['train']:.4f} | "
                f"val {losses['val']:.4f} | 困惑度 {ppl:.2f} | {elapsed:6.1f}s | {rate:5.0f} 字符/秒",
                flush=True,
            )
            # 步骤一律按 1 基整百输出（用户 2026-10-18）：原来打印 0 基的 step，读起来是 299 / 599
            loss_csv.write(f"{step + 1 + step_offset},{losses['train']:.4f},{losses['val']:.4f},{lr:.6e},{elapsed:.1f}\n")
            loss_csv.flush()

            if cfg.get("sample", True):
                model.eval()
                ids = [stoi[c] for c in prompt if c in stoi]
                if ids:            # 护栏：提示词字符全不在词表时跳过采样（空序列会让 generate 崩）
                    idx = torch.tensor([ids], dtype=torch.long, device=device)
                    out = model.generate(idx, max_new_tokens=sample_chars, temperature=0.8, top_k=40,
                                         repetition_penalty=float(cfg.get("repetition_penalty") or 1.15))
                    print(">>>", "".join(itos[i] for i in out[0].tolist()), flush=True)
                else:
                    print(f">>> [跳过采样：提示词「{prompt}」的字符都不在词表]", flush=True)
                model.train()

            save_ckpt(step + 1 + step_offset)

    loss_csv.close()
    # 正式权重也留一份完整曲线（用户 2026-10-20）：logs/pretrain_loss.csv 这类是各页共用，
    # 下次训练就被覆盖了；仓库「详细信息」要能随时看这个权重那次的完整曲线。
    _wstem = os.path.splitext(os.path.basename(out_path))[0]
    if _wstem.endswith(".lora"):
        _wstem = _wstem[:-5]
    loss_snapshot(_wstem)
    # 落盘路径按实际产物报（用户 2026-10-22）：LoRA 只存适配器，报 out_path（.pt）会指向不存在的文件
    _disp = out_path
    if use_lora:
        _dst = os.path.splitext(out_path)[0]
        if _dst.lower().endswith(".lora"):
            _dst = _dst[:-5]
        _disp = _dst + ".lora.pt"
    if stop_flag and os.path.exists(stop_flag):
        try:
            os.remove(stop_flag)
        except OSError:
            pass
        print(f"训练已中止，权重在 {_disp}")
    else:
        print(f"完成，权重已保存到 {_disp}")


if __name__ == "__main__":
    main()
