"""engine/tools/hf_epro.py —— 把 core.transformer.GPT 包成 transformers 可识别的模型，
给 llm-compressor（llmcompressor.oneshot）做模型裁切 / 量化用（用户 2026-10-15）。

为什么需要它：llm-compressor 的输入是 HuggingFace transformers 模型（PreTrainedModel），
而 EAI 的权重是自研小 GPT 的 .pt（{model, stoi, itos, args}）。这里做一层薄壳：
  EProConfig  —— PretrainedConfig，携带 GPT 的结构参数（与 ckpt["args"] 同源）
  EProModel   —— PreTrainedModel，内核就是 core.transformer.GPT 本体（nn.Linear 一层不少）
  load_gpt_checkpoint() —— 读 .pt 还原模型（与 bridge_api/chat.py 的重建口径逐字一致）
裁切/量化都作用在 GPT 自己的 nn.Linear 上，输出再由 compress_run.py 存回 .pt。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple

import torch

# 引擎目录进 sys.path（core.transformer 依赖它）
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from transformers import PretrainedConfig, PreTrainedModel  # noqa: E402
from transformers.modeling_outputs import CausalLMOutputWithPast  # noqa: E402


class EProConfig(PretrainedConfig):
    """GPT 结构参数（字段名与 ckpt["args"] 对齐）。"""

    model_type = "eproai_gpt"

    def __init__(
        self,
        vocab_size: int = 256,
        d_model: int = 128,
        n_head: int = 4,
        n_layer: int = 4,
        d_ff: Optional[int] = None,
        block_size: int = 128,
        tie_weights: bool = True,
        mtp: bool = False,
        mtp_k: int = 3,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.vocab_size = int(vocab_size)
        self.d_model = int(d_model)
        self.n_head = int(n_head)
        self.n_layer = int(n_layer)
        self.d_ff = int(d_ff) if d_ff else None
        self.block_size = int(block_size)
        self.tie_weights = bool(tie_weights)
        self.mtp = bool(mtp)
        self.mtp_k = int(mtp_k)


class EProModel(PreTrainedModel):
    """core.transformer.GPT 的 transformers 壳（forward 语义与 GPT.forward 一致）。"""

    config_class = EProConfig
    base_model_prefix = "gpt"
    supports_gradient_checkpointing = False
    _no_split_modules = ["Block"]   # llm-compressor 的 get_no_split_params 需要它（GPT 的解码块类名）

    def __init__(self, config: EProConfig) -> None:
        super().__init__(config)
        from core.transformer import GPT  # 懒 import（引擎自己的模型）

        self.gpt = GPT(
            vocab_size=config.vocab_size,
            d_model=config.d_model,
            n_head=config.n_head,
            n_layer=config.n_layer,
            max_len=config.block_size,
            dropout=0.0,
            tie_weights=config.tie_weights,
            d_ff=config.d_ff or None,
            use_mtp=config.mtp,
            mtp_k=config.mtp_k,
        )

    # ---- transformers 约定接口 ----
    def get_input_embeddings(self):
        return self.gpt.tok_emb

    def set_input_embeddings(self, value) -> None:
        self.gpt.tok_emb = value

    def get_output_embeddings(self):
        return self.gpt.head

    def set_output_embeddings(self, value) -> None:
        self.gpt.head = value

    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        **kwargs: Any,
    ) -> CausalLMOutputWithPast:
        if input_ids is None:
            raise ValueError("EProModel.forward 需要 input_ids")
        # 内联展开 GPT 的前向（不能整调 self.gpt(...)：那在 fx 图里是单个节点，
        # sequential 流水线切不出 Block 子图，裁切/逐层量化就全不生效 —— 实测踩过）
        g = self.gpt
        x = g.pos_enc(g.tok_emb(input_ids))
        for block in g.blocks:
            x = block(x)
        logits = g.head(g.ln_f(x))
        loss = None
        if labels is not None:
            import torch.nn.functional as F

            loss = F.cross_entropy(
                logits.reshape(-1, logits.size(-1)), labels.reshape(-1), ignore_index=-1
            )
        return CausalLMOutputWithPast(logits=logits, loss=loss)


def load_gpt_checkpoint(path: str) -> Tuple[PreTrainedModel, Dict[str, Any]]:
    """读 .pt → (EProModel, ckpt)。模型结构与 bridge_api/chat.py::_do_load 同口径。"""
    ckpt = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(ckpt, dict):
        raise RuntimeError("权重文件格式无法识别（顶层不是 dict）")
    stoi = ckpt.get("stoi") or {}
    if not stoi:
        raise RuntimeError("权重缺少词表（stoi 为空）")
    raw_itos = ckpt.get("itos") or {}
    if isinstance(raw_itos, dict):
        itos = {int(k): v for k, v in raw_itos.items()}
    else:
        itos = {i: ch for i, ch in enumerate(raw_itos)}

    args = ckpt.get("args") or ckpt.get("model_args") or {}
    a = args if isinstance(args, dict) else {}
    config = EProConfig(
        vocab_size=len(stoi),
        d_model=int(a.get("d_model", 128)),
        n_head=int(a.get("n_head", 4)),
        n_layer=int(a.get("n_layer", 4)),
        d_ff=a.get("d_ff") or None,
        block_size=int(a.get("block_size", 128)),
        tie_weights=bool(a.get("tie_weights", True)),
        mtp=bool(a.get("mtp", False)),
        mtp_k=int(a.get("mtp_k", 3)),
    )
    model = EProModel(config)
    state = ckpt.get("model") or ckpt.get("state_dict") or ckpt
    missing, unexpected = model.gpt.load_state_dict(state, strict=False)
    if missing or unexpected:
        print(
            f"[hf_epro] load_state_dict 非严格：missing={len(missing)} unexpected={len(unexpected)}",
            flush=True,
        )
    model.eval()
    return model, ckpt


def extract_state_dict(model: PreTrainedModel) -> Dict[str, torch.Tensor]:
    """把处理完的模型取出纯浮点 state_dict（GPT 键名，可直接 load_state_dict 回 GPT）。

    量化后的 Linear 权重可能是压缩张量（PackedTensor 等）—— 这里统一反量化回 float，
    保证产物仍是引擎能加载的 .pt（精度按量化网格，但结构不变）。
    """
    out: Dict[str, torch.Tensor] = {}
    gpt = getattr(model, "gpt", model)
    for key, val in gpt.state_dict().items():
        t = val
        if not torch.is_tensor(t):
            t = getattr(t, "data", t)
        if not torch.is_tensor(t):
            continue
        if not torch.is_floating_point(t):
            if hasattr(t, "dequantize"):
                t = t.dequantize()
            else:
                t = t.float()
        out[key] = t.detach().to("cpu", torch.float32).contiguous()
    return out
