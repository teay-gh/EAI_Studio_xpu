"""engine/tools/compress_run.py —— 模型裁切 / 量化子进程（基于 llm-compressor）。

只在 .venv_compress（独立 Python 3.11 + llmcompressor + CPU torch）里跑，
主 venv 的 XPU torch 不受影响。由 bridge_api/_compress.py 拉起：

    python -u tools/compress_run.py --config <runtime.json>

runtime.json:
    {
      "mode": "prune" | "quant",
      "weight": "<输入 .pt>", "out": "<输出 .pt>",
      "method": "sparsegpt" | "wanda",          # mode=prune
      "sparsity": 0.5,
      "scheme": "W8A16" | "W4A16" | "FP8_DYNAMIC",  # mode=quant
      "calib_samples": 64, "seq_len": 128,
      "corpus": "<校准语料 txt 路径>",
      "stop_flag": "<停止标记文件路径>"
    }

流程：读 .pt → 包成 transformers 模型（tools/hf_epro.py）→ llmcompressor.oneshot
（SparseGPT/Wanda 裁切 或 GPTQ/FP8 量化）→ 把处理后的权重存回同格式 .pt
（{model, stoi, itos, args}，引擎可继续加载）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(TOOLS_DIR)
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)


def say(text: str) -> None:
    print(text, flush=True)


def check_stop(cfg: dict) -> None:
    """训练同口径：stop.flag 出现就干净退出（不产生产物）。"""
    flag = cfg.get("stop_flag") or ""
    if flag and os.path.exists(flag):
        say("[compress] 收到停止信号，本次压缩中止（未写出产物）")
        sys.exit(0)


CALIB_LABEL = {"pretrain": "预训练（文本）", "sft": "指令微调（对话）"}


def _windows_from(text: str, stoi: dict, seq_len: int, want: int, label: str) -> list:
    """从一段语料里均匀抽 want 个定长窗口。

    2026-10-23（用户）：原来是「从第一个字符起按 seq_len 连续切、切满就停」——只覆盖语料开头
    那一小段（64×128 只占 14 万字符语料的 6%）。改成**等间距步进**，让样本铺满整段语料。
    """
    ids = [stoi.get(c, 0) for c in text]
    if len(ids) < 8 or want <= 0:
        return []
    stride = max(seq_len, len(ids) // max(1, want))
    wins = []
    pos = 0
    while len(wins) < want and pos + seq_len <= len(ids):
        wins.append(ids[pos : pos + seq_len])
        pos += stride
    if not wins:                                   # 语料短于一窗：整段当一条
        wins = [ids[: min(len(ids), seq_len)]]
    say(f"[compress] 校准语料 {label} {len(text)} 字符 → {len(wins)} 个样本 × {seq_len} token（步进 {stride}）")
    return wins


def build_calibration(cfg: dict, stoi: dict, max_len: int):
    """把校准语料切成定长 id 窗口 → DataLoader(batch_size=1)（llm-compressor 直接收 DataLoader）。

    支持多来源（用户 2026-10-23）：cfg["corpora"] = [{"kind": "pretrain"|"sft", "path": ...}, ...]，
    样本数在来源之间**均分**（余数给前面的来源）；只给单个文件时退回旧口径。
    """
    import torch
    from torch.utils.data import DataLoader, Dataset

    seq_len = max(8, min(int(cfg.get("seq_len") or max_len), max_len))
    max_samples = max(1, int(cfg.get("calib_samples") or 64))

    srcs = [s for s in (cfg.get("corpora") or []) if s.get("path") and os.path.exists(s["path"])]
    if not srcs:
        corpus = cfg.get("corpus") or ""
        if not corpus or not os.path.exists(corpus):
            raise SystemExit(f"校准语料不存在：{corpus or '（未指定）'}")
        srcs = [{"kind": "pretrain", "path": corpus}]

    per = [max_samples // len(srcs)] * len(srcs)
    for i in range(max_samples - sum(per)):
        per[i] += 1
    windows = []
    for src, want in zip(srcs, per):
        path = src["path"]
        label = CALIB_LABEL.get(str(src.get("kind") or "").lower(), os.path.basename(path))
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        wins = _windows_from(text, stoi, seq_len, want, label)
        if not wins:
            say(f"[compress] 校准语料 {label} 太短（{len(text)} 字符 < 8），这一路跳过")
        windows.extend(wins)
    if not windows:
        raise SystemExit("校准语料太短：所有来源都切不出一个样本")
    say(f"[compress] 校准样本合计 {len(windows)} 条（要求 {max_samples}）× {seq_len} token，来源 {len(srcs)} 个")

    class _DS(Dataset):
        def __len__(self):
            return len(windows)

        def __getitem__(self, idx):
            return {"input_ids": torch.tensor(windows[idx], dtype=torch.long)}

    return DataLoader(_DS(), batch_size=1)


def build_recipe(cfg: dict):
    mode = cfg.get("mode")
    ignore = ["re:.*head", "re:.*mtp"]      # 输出头（tie_weights 会牵连词嵌入）与 MTP 头不动
    if mode == "prune":
        sparsity = float(cfg.get("sparsity") or 0.5)
        sparsity = max(0.05, min(0.95, sparsity))
        method = (cfg.get("method") or "sparsegpt").lower()
        # 坑（实测）：targets 必须是【列表】—— 裸字符串会被 match_named_modules 当字符序列，
        # 一个都匹配不上 → 钩子 0 个 → 假装跑完、实际什么都没剪。类名 'Linear' 用列表即可命中。
        if method == "wanda":
            from llmcompressor.modifiers.pruning.wanda import WandaPruningModifier

            say(f"[compress] 裁切方法 Wanda，稀疏度 {sparsity}")
            return WandaPruningModifier(sparsity=sparsity, targets=["Linear"], ignore=ignore)
        from llmcompressor.modifiers.pruning.sparsegpt import SparseGPTModifier

        say(f"[compress] 裁切方法 SparseGPT，稀疏度 {sparsity}")
        return SparseGPTModifier(sparsity=sparsity, targets=["Linear"], ignore=ignore)

    scheme = (cfg.get("scheme") or "W8A16").upper()
    if scheme == "FP8_DYNAMIC":
        from llmcompressor.modifiers.quantization import QuantizationModifier

        say("[compress] 量化方案 FP8_DYNAMIC（免校准）")
        return QuantizationModifier(scheme="FP8_DYNAMIC", targets=["Linear"], ignore=ignore)
    if scheme not in ("W8A16", "W4A16"):
        raise SystemExit(f"未知量化方案：{scheme}")
    from llmcompressor.modifiers.gptq import GPTQModifier

    say(f"[compress] 量化方案 {scheme}（GPTQ，需校准）")
    return GPTQModifier(scheme=scheme, targets=["Linear"], ignore=ignore)


class _CharProcessor:
    """哑分词器：llm-compressor 见到 processor 是对象就跳过 AutoProcessor 自动初始化，
    校准走我们自己的 DataLoader，真分词不需要；留 encode/decode/save_pretrained 备查。"""

    def __init__(self, stoi: dict) -> None:
        self.stoi = stoi or {}
        self.itos = {i: ch for ch, i in self.stoi.items()}
        self.vocab_size = max(len(self.stoi), 1)

    def encode(self, text: str):
        return [self.stoi.get(c, 0) for c in str(text or "")]

    def decode(self, ids) -> str:
        return "".join(self.itos.get(int(i), "") for i in (ids or []))

    def __call__(self, text, **kwargs):
        if isinstance(text, str):
            return {"input_ids": self.encode(text)}
        return {"input_ids": [self.encode(t) for t in text]}

    def save_pretrained(self, *args, **kwargs) -> None:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    t0 = time.time()
    mode = cfg.get("mode")
    weight = cfg.get("weight") or ""
    out = cfg.get("out") or ""
    say(f"[compress] mode={mode} weight={weight}")
    if not weight or not os.path.exists(weight):
        raise SystemExit(f"权重不存在：{weight}")
    if not out:
        raise SystemExit("缺少输出路径")
    check_stop(cfg)

    import torch

    say(f"[compress] torch {torch.__version__}（压缩专用环境，CPU）")
    from tools.hf_epro import extract_state_dict, load_gpt_checkpoint

    model, ckpt = load_gpt_checkpoint(weight)
    max_len = int(model.config.block_size)
    say(f"[compress] 模型 vocab={model.config.vocab_size} d_model={model.config.d_model} "
        f"n_layer={model.config.n_layer} max_len={max_len}")

    recipe = build_recipe(cfg)
    need_calib = not (mode == "quant" and (cfg.get("scheme") or "").upper() == "FP8_DYNAMIC")
    loader = build_calibration(cfg, ckpt.get("stoi") or {}, max_len) if need_calib else None
    check_stop(cfg)

    from llmcompressor import oneshot

    # llm-compressor 的 validate_model 只检查「输入模型是否已被别家量化过」，
    # 但它用 AutoConfig.from_pretrained(config.name_or_path) 回读磁盘 config —— 自研 GPT 没有磁盘 config，
    # 这里把该检查短路（本工具的输入恒为未量化的浮点 .pt，检查不适用）。
    try:
        from llmcompressor.entrypoints.oneshot import Oneshot

        Oneshot.validate_model = lambda self, model: None
    except Exception as exc:  # noqa: BLE001
        say(f"[compress] 短路 validate_model 失败（继续）：{exc}")

    # 裁切（SparseGPT/Wanda）是逐层更新，只有 sequential 流水线才会真正施剪；
    # basic 流水线下校准会跑完但稀疏掩码不生效（实测零值率 0）。量化 GPTQ 两者皆可。
    use_seq = mode == "prune"
    say("[compress] 开始 llmcompressor.oneshot …")
    model = oneshot(
        model=model,
        recipe=recipe,
        processor=_CharProcessor(ckpt.get("stoi") or {}),   # 对象即跳过 AutoProcessor 初始化
        dataset=loader,
        num_calibration_samples=int(cfg.get("calib_samples") or 64),
        max_seq_length=max_len,
        pipeline="sequential" if use_seq else ("basic" if loader is not None else "datafree"),
        sequential_targets=["Block"] if use_seq else None,
        output_dir=None,
    )
    check_stop(cfg)

    state = extract_state_dict(model)
    ref_keys = set((ckpt.get("model") or {}).keys())   # 只留 GPT 认识的键（量化元数据键丢弃）
    if ref_keys:
        state = {k: v for k, v in state.items() if k in ref_keys}
    payload = {
        "model": state,
        "stoi": ckpt.get("stoi") or {},
        "itos": ckpt.get("itos") or {},
        "args": {
            **(ckpt.get("args") or {}),
            "compress": {
                "mode": mode,
                "method": (cfg.get("method") or "") if mode == "prune" else "",
                "scheme": (cfg.get("scheme") or "") if mode == "quant" else "",
                "sparsity": cfg.get("sparsity") if mode == "prune" else None,
                "calib_samples": int(cfg.get("calib_samples") or 0),
                "elapsed_s": round(time.time() - t0, 1),
            },
        },
    }
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".eai-tmp"
    torch.save(payload, tmp)
    os.replace(tmp, out)

    in_mb = os.path.getsize(weight) / 1048576.0
    out_mb = os.path.getsize(out) / 1048576.0
    say(f"[compress] 完成：{out}  ({in_mb:.1f} MB → {out_mb:.1f} MB, {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"[compress] 失败：{exc}", flush=True)
        sys.exit(1)
