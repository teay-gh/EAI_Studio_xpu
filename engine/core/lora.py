"""LoRA 低秩适配微调（Low-Rank Adaptation）。

思路：冻结原模型全部权重，只在选定的 Linear 层旁路挂一对低秩矩阵
    ΔW = (α/r) · B·A，  A: in→r（Kaiming 初始化），B: r→out（零初始化）
零初始化保证训练开始时网络输出与原模型完全一致，只训 A/B 两个小矩阵。

保存策略：
  - 推理用 checkpoint 存「合并后」的完整权重（chat 页无需改动即可加载）
  - 另存一份仅含 A/B 的适配器文件（可复用、可换基座）
"""

from __future__ import annotations

from typing import Iterable

import torch
import torch.nn as nn

# 目标层选择：给定模块点分名，判断是否挂 LoRA
# 支持的别名 → 匹配规则（匹配 blocks.N.attn.qkv 这类末端名）
TARGET_ALIASES: dict[str, tuple[str, ...]] = {
    "qkv": (".attn.qkv",),
    "proj": (".attn.proj",),
    "fc_in": (".ffn.fc_in",),
    "fc_out": (".ffn.fc_out",),
}
TARGET_CHOICES = tuple(TARGET_ALIASES.keys())
DEFAULT_TARGETS = "qkv,proj,fc_in,fc_out"


class LoRALinear(nn.Module):
    """在冻结的 base Linear 旁路加低秩适配。"""

    def __init__(self, base: nn.Linear, r: int, alpha: float, dropout: float) -> None:
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad = False
        self.r = int(r)
        self.alpha = float(alpha)
        self.scale = self.alpha / max(1, self.r)
        self.lora_dropout = nn.Dropout(float(dropout))
        self.lora_A = nn.Linear(base.in_features, self.r, bias=False)
        self.lora_B = nn.Linear(self.r, base.out_features, bias=False)
        nn.init.kaiming_uniform_(self.lora_A.weight, a=math_sqrt5())
        nn.init.zeros_(self.lora_B.weight)  # 零初始化：起步等价于原模型

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.base(x) + self.lora_B(self.lora_A(self.lora_dropout(x))) * self.scale


def math_sqrt5() -> float:
    return 5.0 ** 0.5


def parse_targets(spec: str | Iterable[str]) -> list[str]:
    """把 'qkv,proj' / ['qkv','proj'] 归一化为已知别名列表。"""
    if isinstance(spec, str):
        items = [t.strip().lower() for t in spec.replace("，", ",").split(",")]
    else:
        items = [str(t).strip().lower() for t in spec]
    out = [t for t in items if t in TARGET_ALIASES]
    return out or list(TARGET_ALIASES)


def _match(name: str, targets: list[str]) -> bool:
    return any(name.endswith(suffix) for t in targets for suffix in TARGET_ALIASES[t])


def apply_lora(model: nn.Module, r: int, alpha: float, dropout: float,
               targets: str | Iterable[str] = DEFAULT_TARGETS) -> int:
    """冻结全模型，在目标层挂 LoRA。返回挂载的 Linear 数量。"""
    tlist = parse_targets(targets)
    for p in model.parameters():
        p.requires_grad = False

    wrapped = 0
    for name, module in list(model.named_modules()):
        if not isinstance(module, nn.Linear) or isinstance(module, LoRALinear):
            continue
        if not _match(name, tlist):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = model.get_submodule(parent_name) if parent_name else model
        # 2026-10-17 修：挂载时模型可能已在 XPU/CUDA 上，新建的 A/B 默认在 CPU/FP32 →
        # 前向直接报 "mat2 is on cpu, different from other tensors on xpu"。按基座层对齐设备与 dtype。
        layer = LoRALinear(module, r, alpha, dropout)
        # 只搬 A/B（基座层已在目标设备上，别动它）
        layer.lora_A.to(device=module.weight.device, dtype=module.weight.dtype)
        layer.lora_B.to(device=module.weight.device, dtype=module.weight.dtype)
        setattr(parent, child, layer)
        wrapped += 1
    return wrapped


def lora_parameters(model: nn.Module):
    return [p for p in model.parameters() if p.requires_grad]


def lora_state_dict(model: nn.Module) -> dict[str, torch.Tensor]:
    """仅适配器权重（键名形如 blocks.0.attn.qkv.lora_A.weight）。"""
    return {k: v.detach().cpu() for k, v in model.state_dict().items() if ".lora_" in k}


def lora_config_of(model: nn.Module) -> dict:
    for m in model.modules():
        if isinstance(m, LoRALinear):
            return {"r": m.r, "alpha": m.alpha}
    return {}


def merged_state_dict(model: nn.Module) -> dict[str, torch.Tensor]:
    """合并 ΔW 后的完整 state dict（键名与原生 GPT 一致，chat 页可直接加载）。

    不改动传入的 live 模型，只在返回的副本上合并。
    """
    out: dict[str, torch.Tensor] = {}
    merges: dict[str, tuple[str, str, float]] = {}  # base_weight_key -> (A_key, B_key, scale)
    for name, m in model.named_modules():
        if isinstance(m, LoRALinear):
            prefix = name + "."
            merges[prefix + "weight"] = (
                prefix + "lora_A.weight", prefix + "lora_B.weight", m.scale,
            )
    for k, v in model.state_dict().items():
        if ".lora_" in k:
            continue
        if k.endswith(".base.weight"):
            k = k[: -len(".base.weight")] + ".weight"
        elif k.endswith(".base.bias"):
            k = k[: -len(".base.bias")] + ".bias"
        out[k] = v.detach().cpu().clone()
    for base_key, (a_key, b_key, scale) in merges.items():
        if base_key in out and a_key in model.state_dict() and b_key in model.state_dict():
            a = model.state_dict()[a_key]
            b = model.state_dict()[b_key]
            out[base_key] = out[base_key] + (b @ a) * scale
    return out


def lora_checkpoint_payload(model: nn.Module) -> dict:
    """适配器单独存档的内容。"""
    return {
        "format": "eproai-lora-v1",
        **lora_config_of(model),
        "lora": lora_state_dict(model),
    }


def load_lora_adapter(model: nn.Module, payload: dict) -> int:
    """把适配器权重加载回已 apply_lora 的模型（键名相同则直接拷贝）。"""
    sd = payload.get("lora") or {}
    own = model.state_dict()
    copied = 0
    for k, v in sd.items():
        if k in own and own[k].shape == v.shape:
            own[k].copy_(v.to(own[k].device))
            copied += 1
    return copied


def merge_adapter_file(base_path: str, adapter_path: str, out_path: str) -> dict:
    """把适配器合并进基座权重，落成完整 ckpt（chat 页可直接加载）。

    用户 2026-10-16：训练时不合并，导出时可选合并 —— 本函数是导出侧的合并实现。
    base_path   基座 ckpt（预训练/继续预训练产物，含 model/stoi/itos/args）
    adapter_path  *.lora.pt 适配器（lora_checkpoint_payload 格式，自描述 r/α/targets）
    out_path    合并产物（结构与 core/train.py save_ckpt 的全量 ckpt 一致）
    返回 {out, wrapped, copied, size}；失败抛异常（照实上抛给调用方）。
    """
    import os as _os

    import torch

    from core.transformer import GPT

    base = torch.load(base_path, map_location="cpu", weights_only=False)
    ad = torch.load(adapter_path, map_location="cpu", weights_only=False)
    if not isinstance(base, dict) or "model" not in base:
        raise RuntimeError("基座权重格式不对（缺 model）")
    if not isinstance(ad, dict) or not ad.get("lora"):
        raise RuntimeError("适配器格式不对（缺 lora 权重）")
    args = base.get("args") or {}
    stoi = dict(base.get("stoi") or {})
    itos = {int(k): v for k, v in (base.get("itos") or {}).items()}
    if not itos:
        raise RuntimeError("基座权重缺词表（itos）")
    model = GPT(
        vocab_size=len(itos),
        d_model=args["d_model"],
        n_head=args["n_head"],
        n_layer=args["n_layer"],
        max_len=args["block_size"],
        dropout=args.get("dropout", 0.1),
        tie_weights=args.get("tie_weights", True),
        d_ff=args.get("d_ff") or None,
        use_mtp=bool(args.get("mtp")),
        mtp_k=int(args.get("mtp_k", 3)),
    )
    model.load_state_dict(base["model"], strict=False)
    r = int(ad.get("r", 8))
    alpha = float(ad.get("alpha", 16))
    drop = float(ad.get("dropout", 0.05))
    targets = ad.get("targets") or DEFAULT_TARGETS
    n_wrapped = apply_lora(model, r, alpha, drop, targets)
    if n_wrapped == 0:
        raise RuntimeError(f"适配器目标层与基座模型不匹配：{targets}")
    # 2026-10-23（用户要求）：只拦「合并必坏」的结构性不兼容 —— 张量对不上会被
    # load_lora_adapter 静默跳过（B 是零初始化，ΔW 直接变 0，合出来就是基座副本），
    # 所以这里先判形状；词表大小对不上同样是必坏（同一串 id 指向不同字）。
    # 「同结构、不同基座训的」只是效果差，不拦，仅由上面的 base 提示。
    _own = model.state_dict()
    _sd = ad.get("lora") or {}
    _bad = [k for k, v in _sd.items() if k not in _own or tuple(_own[k].shape) != tuple(v.shape)]
    if _bad:
        _k = _bad[0]
        raise RuntimeError(
            "合并必坏：适配器有 %d/%d 个张量与基座结构不一致（基座 d_model=%s、层数=%s、词表=%s，"
            "适配器秩 r=%s）。例如 %s：适配器 %s，基座 %s。请选训练该适配器时用的基座。"
            % (len(_bad), len(_sd), args.get("d_model"), args.get("n_layer"), len(itos), r, _k,
               tuple(_sd[_k].shape), tuple(_own[_k].shape) if _k in _own else "缺失"))
    _av = ad.get("vocab")
    if isinstance(_av, int) and _av > 0 and _av != len(itos):
        raise RuntimeError(
            "合并必坏：适配器是在 %d 词表的基座上训的，现在的基座是 %d（词表不同，"
            "同一串 id 会指向不同的字，合并结果必然是乱的）。请换回原来的基座。" % (_av, len(itos)))
    copied = load_lora_adapter(model, ad)
    if copied == 0:
        raise RuntimeError("适配器权重没有加载进模型（键名不匹配）")
    merged = merged_state_dict(model)
    ckpt = {
        "model": merged,
        "stoi": stoi,
        "itos": itos,
        "args": {**args, "method": "full", "lora_merged": True},
    }
    _os.makedirs(_os.path.dirname(out_path) or ".", exist_ok=True)
    torch.save(ckpt, out_path)
    # 基座校验（用户 2026-10-22 排查）：适配器原本是挂在哪个基座上训的？合到别的基座上
    # 数学上能跑通，但效果通常比两个原件都差（实测把 A 基座的适配器合到 B 基座，留出集 loss 上升）。
    ad_base = str(ad.get("base") or (ad.get("base_args") or {}).get("init_from") or "")
    base_mismatch = ""
    if ad_base:
        same = _os.path.normcase(_os.path.basename(ad_base)) == _os.path.normcase(_os.path.basename(base_path))
        if not same:
            base_mismatch = ("适配器是在「%s」上训练的，这次合并到「%s」——不是同一个基座，"
                             "合出来的权重效果通常比两个原件都差。"
                             % (_os.path.basename(ad_base), _os.path.basename(base_path)))
    return {"out": out_path, "wrapped": n_wrapped, "copied": copied,
            "size": _os.path.getsize(out_path), "base_mismatch": base_mismatch}
