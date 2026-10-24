"""
从零实现 Transformer（GPT 解码器架构）。

这里刻意不使用 nn.MultiheadAttention / nn.TransformerEncoderLayer 这类现成组件：
注意力打分、因果掩码、位置编码、残差连接、归一化全部手写，
目的就是让每一层的张量形状和数值流向都看得见。

约定：B = batch，T = 时间步（序列长度），C = d_model（隐藏维度）。
"""

import math

import torch
import torch.nn as nn
import torch.nn.functional as F


class CausalSelfAttention(nn.Module):
    """多头因果自注意力：输入 (B, T, C)，输出 (B, T, C)。

    三步：
      1. 一个线性层把 x 同时投影成 Q、K、V；
      2. 拆成 n_head 个头，各自算 softmax(QK^T / sqrt(d_head)) @ V；
      3. 因果掩码把位置 t 的可见范围限制在 0..t，保证不会偷看未来。
    """

    def __init__(self, d_model, n_head, max_len, dropout=0.0):
        super().__init__()
        assert d_model % n_head == 0, "d_model 必须能被 n_head 整除"
        self.n_head = n_head
        self.d_head = d_model // n_head

        self.qkv = nn.Linear(d_model, 3 * d_model)
        self.proj = nn.Linear(d_model, d_model)
        self.attn_dropout = nn.Dropout(dropout)
        self.resid_dropout = nn.Dropout(dropout)

        # 上三角（不含对角线）为 True 的位置就是"未来"，调用时填成 -inf。
        # 它是常量，注册成 buffer 以便随模型一起搬到别的设备。
        mask = torch.triu(torch.ones(max_len, max_len, dtype=torch.bool), diagonal=1)
        self.register_buffer("mask", mask, persistent=False)

    def forward(self, x, return_attn=False):
        B, T, C = x.shape

        q, k, v = self.qkv(x).split(C, dim=2)
        # (B, T, C) -> (B, n_head, T, d_head)：把 C 拆成"头数 × 每头维度"
        q = q.view(B, T, self.n_head, self.d_head).transpose(1, 2)
        k = k.view(B, T, self.n_head, self.d_head).transpose(1, 2)
        v = v.view(B, T, self.n_head, self.d_head).transpose(1, 2)

        # 缩放点积注意力。除以 sqrt(d_head) 是把点积的方差拉回 1 附近，
        # 否则维度一大，softmax 就冲进饱和区，梯度几乎消失。
        att = (q @ k.transpose(-2, -1)) / math.sqrt(self.d_head)  # (B, n_head, T, T)
        att = att.masked_fill(self.mask[:T, :T], float("-inf"))
        att = F.softmax(att, dim=-1)  # 每一行对"能看到的位置"归一化
        att = self.attn_dropout(att)

        y = att @ v  # (B, n_head, T, d_head)
        y = y.transpose(1, 2).contiguous().view(B, T, C)  # 各头拼回 (B, T, C)
        y = self.resid_dropout(self.proj(y))

        return (y, att) if return_attn else y


class FeedForward(nn.Module):
    """逐位置前馈网络：先升维再降维，两层之间夹一个非线性。

    注意力负责"token 之间交换信息"，前馈层负责"在单个 token 内部做变换"，
    两者交替堆叠才构成完整的 Transformer 块。
    """

    def __init__(self, d_model, d_ff, dropout=0.0):
        super().__init__()
        self.fc_in = nn.Linear(d_model, d_ff)
        self.fc_out = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.dropout(self.fc_out(F.gelu(self.fc_in(x))))


class Block(nn.Module):
    """一个 Transformer 块：注意力子层 + 前馈子层，各自带残差和归一化。

    用的是 Pre-LN 结构（LayerNorm 在子层之前、残差直连主干），
    这是 GPT-2 之后的主流写法：主干上有一条不经过任何归一化的"高速公路"，
    梯度能直接从最后一层流回第一层，因此深模型不需要小心翼翼地调 warmup。
    """

    def __init__(self, d_model, n_head, d_ff, max_len, dropout=0.0):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.attn = CausalSelfAttention(d_model, n_head, max_len, dropout)
        self.ln2 = nn.LayerNorm(d_model)
        self.ffn = FeedForward(d_model, d_ff, dropout)

    def forward(self, x, return_attn=False):
        a, att = self.attn(self.ln1(x), return_attn=True)
        x = x + a
        x = x + self.ffn(self.ln2(x))
        return (x, att) if return_attn else x


class SinusoidalPositionalEncoding(nn.Module):
    """正弦位置编码（原论文的做法）。

    注意力本身对顺序是"无感"的——打乱输入顺序，输出也跟着打乱，结果完全一样。
    所以必须显式把位置信息加进去。这里用不同频率的 sin/cos 给每个位置一个
    固定指纹，奇数维用 sin、偶数维用 cos：好处是不用训练参数，
    并且相对位移可以被线性变换表示出来。
    """

    def __init__(self, d_model, max_len, dropout=0.0):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(max_len, dtype=torch.float).unsqueeze(1)  # (max_len, 1)
        freq = torch.exp(
            torch.arange(0, d_model, 2, dtype=torch.float) * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(pos * freq)
        pe[:, 1::2] = torch.cos(pos * freq)
        self.register_buffer("pe", pe.unsqueeze(0), persistent=False)  # (1, max_len, C)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.dropout(x + self.pe[:, : x.size(1)])


class GPT(nn.Module):
    """解码器-only 的 Transformer 语言模型：预测下一个 token。"""

    def __init__(
        self,
        vocab_size,
        d_model=128,
        n_head=4,
        n_layer=4,
        d_ff=None,
        max_len=64,
        dropout=0.0,
        tie_weights=False,
        use_mtp=False,
        mtp_k=3,
    ):
        super().__init__()
        d_ff = d_ff or 4 * d_model
        self.max_len = max_len
        self.n_layer = n_layer
        self.d_model = d_model
        self.n_head = n_head
        self.vocab_size = vocab_size
        self.use_mtp = use_mtp
        self.mtp_k = mtp_k if use_mtp else 1

        self.tok_emb = nn.Embedding(vocab_size, d_model)
        self.pos_enc = SinusoidalPositionalEncoding(d_model, max_len, dropout)
        self.blocks = nn.ModuleList(
            Block(d_model, n_head, d_ff, max_len, dropout) for _ in range(n_layer)
        )
        self.ln_f = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size, bias=False)
        if tie_weights:
            # 输入嵌入和输出投影共享权重，能省一大块参数（浅层任务上效果也不差）
            self.head.weight = self.tok_emb.weight

        # Multi-Token Prediction：额外预测 t+2 … t+k，训练信号更稠密
        if self.use_mtp and self.mtp_k > 1:
            self.mtp_heads = nn.ModuleList(
                nn.Linear(d_model, vocab_size, bias=False) for _ in range(self.mtp_k - 1)
            )
            self.mtp_norms = nn.ModuleList(nn.LayerNorm(d_model) for _ in range(self.mtp_k - 1))
        else:
            self.mtp_heads = None
            self.mtp_norms = None

        self.apply(self._init_weights)
        self._scale_residual_projections()

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def _scale_residual_projections(self):
        # 残差分支的输出投影按 1/sqrt(2 * n_layer) 缩放（GPT-2 的做法）。
        # 主干上叠了 2*n_layer 条残差路径，不缩放的话激活方差会随深度线性增长。
        std = 0.02 / math.sqrt(2 * self.n_layer)
        for name, p in self.named_parameters():
            if name.endswith("proj.weight") or name.endswith("fc_out.weight"):
                nn.init.normal_(p, mean=0.0, std=std)

    def forward(self, idx, targets=None, return_attn=False):
        B, T = idx.shape
        assert T <= self.max_len, f"序列长度 {T} 超过 max_len {self.max_len}"

        x = self.tok_emb(idx)  # (B, T, C)
        x = self.pos_enc(x)

        attns = []
        for block in self.blocks:
            if return_attn:
                x, a = block(x, return_attn=True)
                attns.append(a)
            else:
                x = block(x)

        h = self.ln_f(x)
        logits = self.head(h)  # (B, T, vocab_size)

        loss = None
        if targets is not None:
            # 把 batch 和时间维摊平成一维的"每个位置预测下一个 token"的问题。
            # ignore_index=-1 让调用方能标记"这些位置不算 loss"。
            loss = F.cross_entropy(
                logits.reshape(-1, logits.size(-1)),
                targets.reshape(-1),
                ignore_index=-1,
            )
            if self.mtp_heads:
                mtp_loss = torch.zeros((), device=logits.device, dtype=logits.dtype)
                for i, head in enumerate(self.mtp_heads):
                    k = i + 2
                    shift = targets[:, k - 1 :]  # 预测 t+k
                    pred = head(h[:, : shift.size(1), :])
                    mtp_loss = mtp_loss + F.cross_entropy(
                        pred.reshape(-1, pred.size(-1)),
                        shift.reshape(-1),
                        ignore_index=-1,
                    )
                loss = loss + 0.3 * mtp_loss / max(1, len(self.mtp_heads))
        return (logits, loss, attns) if return_attn else (logits, loss)

    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None, repetition_penalty=1.0):
        """自回归采样：每次把已有的序列喂进去，取最后一个位置的概率分布采样一个 token，
        再接回序列末尾。temperature=0 表示贪心取最大概率。

        repetition_penalty > 1 会压低已经出现过的 token 的概率。小模型很容易掉进
        "吾吾吾吾"这种复读循环，这个开关是把它拽出来最省事的办法。
        """
        self.eval()
        for _ in range(max_new_tokens):
            idx_cond = idx[:, -self.max_len :]
            logits, _ = self(idx_cond)
            logits = logits[:, -1, :]  # 只关心最后一个位置的预测

            if repetition_penalty and repetition_penalty != 1.0:
                seen = torch.zeros_like(logits, dtype=torch.bool)
                seen.scatter_(1, idx_cond, True)
                # 概率被抬高过的正 logit 除以惩罚、被压低过的负 logit 乘以惩罚，
                # 两个方向都是"让它的概率变小"
                penalized = torch.where(
                    logits > 0, logits / repetition_penalty, logits * repetition_penalty
                )
                logits = torch.where(seen, penalized, logits)

            if temperature == 0:
                idx_next = logits.argmax(dim=-1, keepdim=True)
            else:
                logits = logits / temperature
                if top_k is not None:
                    v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                    logits[logits < v[:, [-1]]] = float("-inf")
                probs = F.softmax(logits, dim=-1)
                idx_next = torch.multinomial(probs, num_samples=1)

            idx = torch.cat([idx, idx_next], dim=1)
        return idx

    def configure_optimizers(self, lr, weight_decay, betas=(0.9, 0.95)):
        """按参数维度分组做 weight decay。

        权重矩阵（二维及以上）才做衰减；偏置、LayerNorm 的缩放因子这些
        一维参数不做——把它们往 0 拉只会破坏模型，没有任何正则收益。
        """
        decay, no_decay = [], []
        for p in self.parameters():
            if not p.requires_grad:
                continue
            (decay if p.dim() >= 2 else no_decay).append(p)
        groups = [
            {"params": decay, "weight_decay": weight_decay},
            {"params": no_decay, "weight_decay": 0.0},
        ]
        return torch.optim.AdamW(groups, lr=lr, betas=betas)

    def n_params(self):
        return sum(p.numel() for p in self.parameters())

    def param_breakdown(self):
        """供 UI 可视化：各组件参数量。"""
        buckets = {
            "词嵌入": 0,
            "注意力": 0,
            "前馈": 0,
            "归一化": 0,
            "输出头": 0,
            "MTP": 0,
        }
        for name, p in self.named_parameters():
            n = p.numel()
            if name.startswith("tok_emb") or name.startswith("pos_enc"):
                buckets["词嵌入"] += n
            elif "mtp_" in name:
                buckets["MTP"] += n
            elif name.startswith("head"):
                buckets["输出头"] += n
            elif "attn" in name or name.endswith("proj.weight") or name.endswith("proj.bias") or ".qkv" in name:
                buckets["注意力"] += n
            elif "ffn" in name or "fc_in" in name or "fc_out" in name:
                buckets["前馈"] += n
            elif "ln" in name:
                buckets["归一化"] += n
            else:
                buckets["归一化"] += n
        return buckets

    def layer_param_estimate(self):
        """单层约 12*d^2，用于架构图标注。"""
        d = self.d_model
        return 12 * d * d


def cosine_lr(step, max_steps, base_lr, warmup_steps, min_lr_ratio=0.1):
    """带线性预热的余弦退火。

    预热是因为训练初期 AdamW 的二阶动量估计还不准，步子迈大了容易发散；
    后期退火是为了让模型在最优解附近收敛得更细。
    """
    if step < warmup_steps:
        return base_lr * (step + 1) / warmup_steps
    if step >= max_steps:
        return base_lr * min_lr_ratio
    progress = (step - warmup_steps) / max(1, max_steps - warmup_steps)
    coeff = 0.5 * (1.0 + math.cos(math.pi * progress))
    return base_lr * (min_lr_ratio + (1 - min_lr_ratio) * coeff)
