/* 中英文切换（用户 2026-10-18）
   - 词典式翻译：精确匹配「整段中文」→ 英文，未收录的中文原样保留（不会翻坏日志/文件名/用户输入）
   - 动态节点：MutationObserver 盯着新增/改动的文本，切到 en 时自动跟进
   - 设置页的「语言」卡片由本模块自绘自管，语言落盘 config.app.lang
   用法：I18N.t('保存') / I18N.setLang('en') / I18N.lang()            */
(function () {
  'use strict';

  var EN = {
    /* ---------- 弹窗（用户 2026-10-22：弹窗标题/正文/按钮全都没翻） ---------- */
    '提示': 'Notice',
    '请确认': 'Confirm',
    '操作失败': 'Action failed',
    '复制失败': 'Copy failed',
    '创建失败': 'Create failed',
    '移动失败': 'Move failed',
    '删除失败': 'Delete failed',
    '保存失败': 'Save failed',
    '打开失败': 'Open failed',
    '添加失败': 'Add failed',
    '批量删除': 'Bulk delete',
    '删除文件': 'Delete file',
    '删除文件夹': 'Delete folder',
    '删除所选文件': 'Delete selected files',
    '删除所选样本': 'Delete selected samples',
    '删除所选产物': 'Delete selected outputs',
    '训练方式保存失败': 'Saving training method failed',
    '备注保存失败': 'Saving note failed',
    '未通过格式审查，未添加': 'Failed the format check, nothing added',
    '全部清除': 'Clear all',
    '清空对话': 'Clear conversations',
    '命名检查': 'Name check',
    '显存不足': 'Not enough VRAM',
    '内存不足': 'Not enough memory',
    '清空失败': 'Clear failed',
    '重命名失败': 'Rename failed',
    '文件名不能为空': 'File name cannot be empty',
    '文件夹名不能为空': 'Folder name cannot be empty',
    '未选择任何文件。': 'No files selected.',
    '当前环境不支持选择文件': 'This environment cannot open a file picker',
    '当前环境不支持选择文件夹': 'This environment cannot open a folder picker',
    '会移入回收站，可恢复。': 'Moves to the Recycle Bin; it can be restored.',
    '确定清除全部日志？清除后不可恢复。': 'Clear all logs? This cannot be undone.',
    '根目录（不放进文件夹）': 'Root (not inside a folder)',
    '重新加载后生效': 'Takes effect after reload',
    '未知错误': 'Unknown error',
    '这些步骤的输出命名需要先改：': 'These steps need their output names fixed first:',
    '清空全部对话和历史？': 'Clear all conversations and history?',
    '当前工程里这一页的所有会话记录都会被删除，删除后无法恢复。': 'Every conversation on this page in the current project is deleted and cannot be recovered.',
    '当前工程里这一页的所有会话记录都会被删除，删除后无法恢复（AI 助手窗口的会话不受影响）。': 'Every conversation on this page in the current project is deleted and cannot be recovered.',
    '已停止训练。': 'Training stopped.',
    '已暂停训练（进度已保存）。': 'Training paused (progress saved).',
    '请及时关闭占内存的程序，以免训练中断。': 'Close memory-hungry programs soon so training is not interrupted.',
    '确定': 'OK',
    '取消': 'Cancel',
    '删除': 'Delete',
    '锁定': 'Lock',
    '解锁': 'Unlock',
    '清除': 'Clear',
    '保存': 'Save',
    '知道了': 'Got it',
    '关闭': 'Close',
    '移动': 'Move',
    '新建': 'Create',
    /* ---------- 参数说明弹窗：训练各页 PARAM_HELP 的标题与逐行正文 ---------- */
    '权重': 'Weight',
    '裁切方法': 'Prune method',
    '量化方案': 'Quantization scheme',
    '稀疏度': 'Sparsity',
    '校准样本数': 'Calibration samples',
    '最大序列长': 'Max sequence length',
    '校准语料': 'Calibration corpus',
    '批量大小': 'Batch size',
    '丢弃率': 'Dropout',
    '步数': 'Steps',
    '学习率': 'Learning rate',
    '混合精度 AMP': 'Mixed precision (AMP)',
    '初始权重': 'Initial weights',
    'LoRA 秩 r': 'LoRA rank r',
    '缩放 α': 'Scaling α',
    'LoRA 丢弃率': 'LoRA dropout',
    '挂载目标层': 'Target modules',
    'batch_size': 'batch_size',
    '基座权重': 'Base weights',
    '模型维度': 'Model width (d_model)',
    '注意力头数': 'Attention heads',
    '网络层数': 'Layers',
    '上下文长度': 'Context length',
    'Multi-Token Prediction': 'Multi-Token Prediction',
    'LoRA 的学习率，可以比全参微调大一些（只更新适配矩阵）。': 'LoRA learning rate — can be a bit higher than full fine-tuning, since only the adapters are updated.',
    'LoRA 训练总步数。': 'Total LoRA training steps.',
    'SparseGPT：逐层按权重重要性剪枝，慢一些但精度保持好；Wanda：按权重×激活幅度排序剪，快且几乎不用校准。': 'SparseGPT prunes layer by layer by weight importance — slower but keeps accuracy. Wanda ranks by weight × activation magnitude — fast and needs almost no calibration.',
    'Transformer 层数：堆几层，决定模型的深度和能建模的抽象层级。': 'Number of Transformer layers — sets the depth and the level of abstraction the model can capture.',
    'W8A16：权重 8bit、激活 16bit，精度损失最小；W4A16：权重 4bit，省一半空间、精度略降；FP8_DYNAMIC：权重与激活都 FP8、动态缩放，需要较新的硬件。': 'W8A16: weights 8-bit, activations 16-bit — smallest accuracy loss. W4A16: weights 4-bit — half the size, slightly lower accuracy. FP8_DYNAMIC: both FP8 with dynamic scaling — needs newer hardware.',
    'r 翻倍 ≈ 可训练参数翻倍，显存和每步耗时也会跟着涨。': 'Doubling r roughly doubles trainable parameters, plus memory and time per step.',
    '一次预测后面 k 个 token 而不是只预测下一个：表征学得更充分、收敛更快。': 'Predict the next k tokens instead of only the next one: richer representations and faster convergence.',
    '上下文长度：一条样本一次最多看多少个 token，超出的部分会被切开。': 'Context length: the most tokens one sample can cover in one pass; anything longer is split.',
    '丢弃率：训练时按比例随机把激活置零，用来抑制过拟合。': 'Dropout: randomly zeroes a share of activations during training to curb overfitting.',
    '低秩增量按 α/r 缩放，决定 LoRA 分支的相对强度。': 'The low-rank delta is scaled by α/r, which sets the strength of the LoRA branch.',
    '低秩适配矩阵的秩：只训练两个 r 很窄的矩阵，参数量随 r 线性增长。': 'Rank of the low-rank adapters: only two narrow r-sized matrices are trained, so parameters grow linearly with r.',
    '例：10MB 语料约 500 万 token，batch 32 × block 128 ≈ 4096 → 1 epoch ≈ 1200 步，2~4 epoch 就是 2400~4800 步。Loss 进入平台期就可以停，硬堆步数只会过拟合。': 'Example: 10MB of text is about 5M tokens; batch 32 × block 128 ≈ 4096 → 1 epoch ≈ 1200 steps, so 2–4 epochs is 2400–4800 steps. Stop once the loss plateaus — piling on steps only overfits.',
    '判断信号：Loss 抖动上翘或出 NaN → 减半；曲线几乎水平 → 乘 1.5~2 再试。配上 warmup 和余弦衰减更稳。': 'Signals: loss jitters upward or hits NaN → halve it; the curve is flat → multiply by 1.5–2. Warmup plus cosine decay keeps it steadier.',
    '判断信号：Loss 进入平缓段就停；新语料上不再下降、同时旧任务效果变差，说明已经训过头。': 'Signals: stop once the loss flattens; if it stops dropping on new data while old tasks get worse, you have trained too long.',
    '判断信号：训练 Loss 很低但验证 Loss 高（在背答案）→ 加到 0.15~0.2；两边都高就是欠拟合，减小它并加步数。': 'Signals: very low training loss but high validation loss (memorising) → raise to 0.15–0.2; if both are high it is underfitting — lower it and add steps.',
    '判断信号：训练 Loss 很快接近 0、验证 Loss 却升高 = 参数量相对语料过大，减层或加数据；两边都降不下去再加层。': 'Signals: training loss falls fast while validation loss rises means the model is too large for the corpus — drop layers or add data; if neither goes down, add layers.',
    '判断信号：验证 Loss 明显高于训练 Loss 就加到 0.15~0.2；两边都高说明欠拟合，反而该减小。': 'Signals: validation loss clearly above training loss → raise to 0.15–0.2; if both are high it is underfitting, so lower it instead.',
    '判断信号：验证 Loss 明显高于训练 Loss（差 20% 以上）就往 0.15→0.2 加；两边都高说明欠拟合，应该减小 dropout。': 'Signals: validation loss well above training loss (over 20%) → move from 0.15 to 0.2; if both are high it is underfitting, so reduce dropout.',
    '剪掉的权重比例，0~1：0.5 表示剪掉一半。': 'Share of weights to prune, 0–1: 0.5 prunes half of them.',
    '只在 LoRA 分支上做 dropout，基座权重不受影响。': 'Dropout runs only on the LoRA branch; base weights are untouched.',
    '太小几乎学不进新语料，太大容易把原来学到的分布冲掉。': 'Too small barely learns the new corpus; too large washes out what was already learned.',
    '微调总步数，别太多，否则会遗忘基座能力。': 'Total fine-tuning steps — keep it modest or the base model\'s ability fades.',
    '微调数据少时学习率偏大很容易过拟合。': 'With little fine-tuning data, too high a learning rate overfits quickly.',
    '微调的更新幅度，通常是从零预训练的 1/4~1/10。': 'Update size for fine-tuning, usually 1/4 to 1/10 of pretraining from scratch.',
    '微调的起点权重。': 'Starting weights for fine-tuning.',
    '必须是 d_model 的约数；每头低于 16 维基本学不到长程关系。改 d_model 后要重新算它。': 'Must divide d_model; below 16 dimensions per head the model barely learns long-range relations. Recompute it after changing d_model.',
    '怎么定值：0.05 是常用默认；数据少或明显过拟合（训练/验证 Loss 差距大）加到 0.1~0.15。': 'Choosing a value: 0.05 is the usual default; with little data or clear overfitting (a large train/validation gap) go to 0.1–0.15.',
    '怎么定值：0.3 起步看效果，0.5 是常用档；准确率掉得多就退到 0.3~0.4。超过 0.7 通常会明显掉点。': 'Choosing a value: start at 0.3 and look at the result; 0.5 is common. If accuracy drops a lot, back off to 0.3–0.4; above 0.7 usually costs several points.',
    '怎么定值：1 个 epoch = 样本数 ÷ batch_size，一般跑 2~3 个 epoch；先跑 1 个 epoch 看回答质量，不够再加。': 'Choosing a value: 1 epoch = samples ÷ batch_size, usually 2–3 epochs. Run one epoch first and judge the answers before adding more.',
    '怎么定值：1 个 epoch = 样本数 ÷ batch_size，通常 2~3 个 epoch 就够。LoRA 收敛比全参微调快，先跑 1 个 epoch 看输出效果再决定加不加。': 'Choosing a value: 1 epoch = samples ÷ batch_size and 2–3 epochs is usually enough. LoRA converges faster than full fine-tuning, so run one epoch, check the output, then decide.',
    '怎么定值：1 个 epoch 的步数 = 语料 token 数 ÷（batch_size × block_size），一般训练 2~4 个 epoch 的步数。': 'Choosing a value: steps per epoch = corpus tokens ÷ (batch_size × block_size); usually train for 2–4 epochs worth of steps.',
    '怎么定值：2e-4~8e-4，先用 5e-4：回答开始像样就保持；答非所问或重复就加到 8e-4；Loss 抖动、输出格式崩坏就减到 2e-4。': 'Choosing a value: 2e-4 to 8e-4; start at 5e-4 and keep it while answers look right. Off-topic or repetitive answers → 8e-4; jittery loss or broken output format → 2e-4.',
    '怎么定值：64~128 足够；时间和显存紧张就 32。量化后困惑度偏高、而样本数又很少（<32）时加到 128 重测。': 'Choosing a value: 64–128 is plenty; use 32 when time or memory is tight. If perplexity is high after quantization and you have very few samples (<32), retry with 128.',
    '怎么定值：LoRA 只训练少量参数，显存比全参微调省，batch 8~32 一般够；等效批量用梯度累积补到 32~64。': 'Choosing a value: LoRA trains few parameters and uses less memory than full fine-tuning, so batch 8–32 usually suffices; use gradient accumulation to reach an effective batch of 32–64.',
    '怎么定值：XPU / CUDA 都建议开，优先 bf16（不需要 loss scale）。只有 fp16 的老卡若出现 Loss = NaN，就关掉 AMP 或换 bf16。': 'Choosing a value: turn it on for both XPU and CUDA, preferring bf16 (no loss scaling). On older fp16-only cards, if the loss becomes NaN, turn AMP off or switch to bf16.',
    '怎么定值：q_proj,v_proj 最省参数、效果最稳，先只挂这两个；效果不够再加 k_proj,o_proj，最后才考虑全部线性层。': 'Choosing a value: q_proj,v_proj use the fewest parameters and behave most reliably — start with just those; add k_proj,o_proj if needed, and consider all linear layers last.',
    '怎么定值：r=8 起步。任务离基座越远（新领域、新说话风格）越要加：数据 >5k 条或 r=8 效果不够就上 16、32；只调口吻这类小改动 r=4 也够。': 'Choosing a value: start at r=8. The further the task is from the base model (new domain, new voice) the more you need: go to 16 or 32 when you have over 5k samples or r=8 is not enough; r=4 is fine for small tweaks such as tone.',
    '怎么定值：从 4 层起步；128 维模型 4~6 层，256 维 6~12 层。': 'Choosing a value: start at 4 layers; 4–6 layers for a 128-wide model, 6–12 for 256.',
    '怎么定值：从 8~16 起、翻倍试到显存接近满；等效批量 = batch_size × 梯度累积，续训同样落在 32~128。': 'Choosing a value: start at 8–16 and double until memory is nearly full; effective batch = batch_size × accumulation, and for continuation it also lands in 32–128.',
    '怎么定值：从 8~16 起，逐步翻倍到显存接近满。等效批量 = batch_size × 梯度累积，想让梯度稳定一般落在 32~128。': 'Choosing a value: start at 8–16 and double until memory is nearly full. Effective batch = batch_size × accumulation; for stable gradients it usually sits in 32–128.',
    '怎么定值：从零预训练常用 1e-3~3e-3（当前 0.002 就在这个档）；续训/微调取它的 1/5~1/10。': 'Choosing a value: pretraining from scratch commonly uses 1e-3 to 3e-3 (0.002 is in that range); continuation and fine-tuning take 1/5 to 1/10 of it.',
    '怎么定值：优先用预训练 / 续训产物（eproai_pretrain.pt 等），从零做指令微调效果通常很差；留空表示从零训练，只用于调试。': 'Choosing a value: prefer a pretrained or continued checkpoint (e.g. eproai_pretrain.pt); instruction tuning from scratch usually goes badly. Leave it empty to train from scratch — for debugging only.',
    '怎么定值：先定 d_model，再按每头 32~64 维反推（128→2~4 头，256→4~8 头，384→6~12 头）。': 'Choosing a value: fix d_model first, then work back from 32–64 dimensions per head (128→2–4 heads, 256→4–8, 384→6–12).',
    '怎么定值：先按显存定档——8GB 用 128，12~16GB 用 256，24GB 以上 384~512。语料小（<10MB）先别超过 256。': 'Choosing a value: size it to memory — 128 for 8GB, 256 for 12–16GB, 384–512 above 24GB. With a small corpus (<10MB) stay at 256 or below.',
    '怎么定值：先用 Wanda 快速看效果；精度掉得多再换 SparseGPT 重跑对比。': 'Choosing a value: start with Wanda for a quick read; if accuracy drops too far, rerun with SparseGPT and compare.',
    '怎么定值：先算 1 个 epoch = 新语料 token ÷（batch_size × block_size），一般跑 1~3 个 epoch；经验上取从零预训练步数的 1/10~1/5。': 'Choosing a value: compute 1 epoch = new-corpus tokens ÷ (batch_size × block_size) and run 1–3 epochs; as a rule of thumb use 1/10 to 1/5 of pretraining steps.',
    '怎么定值：先跑 W8A16 把流程走通，再降到 W4A16 看困惑度；掉得明显就回 W8A16，或换 FP8_DYNAMIC 对比。': 'Choosing a value: run W8A16 first to validate the pipeline, then drop to W4A16 and compare perplexity; if it degrades noticeably, go back to W8A16 or try FP8_DYNAMIC.',
    '怎么定值：取常用推理长度（128 / 256 / 512），至少覆盖语料 95% 的长度；更长只增加耗时，效果提升很小。': 'Choosing a value: pick a common inference length (128 / 256 / 512) that covers about 95% of the corpus; longer only costs time with little gain.',
    '怎么定值：小语料 0.1~0.2，大语料 0~0.1。': 'Choosing a value: 0.1–0.2 for a small corpus, 0–0.1 for a large one.',
    '怎么定值：小语料 0.1~0.2，大语料或大模型 0~0.1。': 'Choosing a value: 0.1–0.2 for a small corpus, 0–0.1 for a large corpus or model.',
    '怎么定值：常用 2e-4~8e-4（约为预训练的 1/5~1/10）。先用 5e-4 试：Loss 不降就乘 2，振荡或上翘就除以 2。': 'Choosing a value: 2e-4 to 8e-4 (about 1/5 to 1/10 of pretraining). Start at 5e-4: if the loss stalls, double it; if it oscillates or climbs, halve it.',
    '怎么定值：常用 α = r（比例 1）或 α = 2r（比例 2）。效果偏弱先把比例提到 2；训练不稳、Loss 抖动就把比例降回 1。': 'Choosing a value: α = r (ratio 1) or α = 2r (ratio 2) are common. If the effect is weak, raise the ratio to 2; if training is unstable or the loss jitters, drop back to 1.',
    '怎么定值：常见 1e-3~3e-3（页里默认 2e-3 就在这个档）。输出几乎没变化 → 乘 2；Loss 抖动或输出崩坏 → 除以 2。': 'Choosing a value: 1e-3 to 3e-3 (the page default 2e-3 sits here). Output barely changes → double it; jittery loss or broken output → halve it.',
    '怎么定值：建议开启，优先 bf16；出现 NaN 就关掉。': 'Choosing a value: keep it on, preferring bf16; turn it off if NaNs appear.',
    '怎么定值：建议开启，优先 bf16；只有 fp16 的老卡若出现 NaN 就关掉。': 'Choosing a value: keep it on, preferring bf16; on older fp16-only cards turn it off if NaNs appear.',
    '怎么定值：指令数据通常几千条，batch 8~32 就够；显存不够先减它，再用梯度累积把等效批量补到 32~64。': 'Choosing a value: instruction data is usually a few thousand rows, so batch 8–32 is enough; if memory is short, lower it and use accumulation to reach an effective batch of 32–64.',
    '怎么定值：数据量小，0.1 是安全默认。': 'Choosing a value: with little data, 0.1 is a safe default.',
    '怎么定值：统计语料句子长度，取能覆盖约 95% 样本的长度（常见 128 / 256 / 512）。比最长句短会把句子截断，学不到完整语义。': 'Choosing a value: measure sentence lengths and pick a length covering about 95% of samples (commonly 128 / 256 / 512). Shorter than the longest sentence truncates it and loses meaning.',
    '怎么定值：选仓库里最新的预训练或续训产物；行里显示「—」代表没选到文件，先去仓库确认。': 'Choosing a value: pick the newest pretrained or continued checkpoint in the Repository; a “—” in the row means no file is selected — check the Repository first.',
    '怎么定值：选仓库里最近一次预训练产物（如 eproai_pretrain.pt）；行里显示「—」代表没选到文件，先去仓库确认它存在。': 'Choosing a value: pick the latest pretrained checkpoint from the Repository (e.g. eproai_pretrain.pt); a “—” in the row means no file is selected — confirm it exists first.',
    '怎么定值：选后续推理想用的同一个基座（如 eproai_pretrain.pt）；行里显示「—」代表没选到文件，先去仓库确认。': 'Choosing a value: use the same base you plan to run inference with (e.g. eproai_pretrain.pt); a “—” in the row means no file is selected — check the Repository.',
    '怎么定值：默认开启、k=3。语料很小（<1MB）或任务对顺序极敏感时可以关掉，避免被迫预测过远反而干扰。': 'Choosing a value: on by default with k=3. With a very small corpus (<1MB) or order-sensitive tasks you can turn it off so distant predictions do not interfere.',
    '总优化步数：跑满后自动保存权重并结束。': 'Total optimisation steps: the run saves the weights and stops when it reaches this.',
    '批量大小：每一步同时喂进多少条序列，影响梯度噪声和显存占用。': 'Batch size: how many sequences go through each step — affects gradient noise and memory use.',
    '把低秩适配器挂到哪些线性层上，用逗号分隔。': 'Which linear layers the adapters attach to, comma separated.',
    '把向量切成几份：把 d_model 切成几份并行做注意力，每头维度 = d_model ÷ n_head。': 'Splits the vector: d_model is divided into parallel attention heads, each of size d_model ÷ n_head.',
    '挂得越多参数越多、越接近全参数微调；显存吃紧时优先减少挂载层。': 'More modules means more parameters and a result closer to full fine-tuning; when memory is tight, attach fewer.',
    '换了基座相当于换了起点，步数和学习率都要重新看 Loss 再定。': 'A different base is a different starting point — re-read the loss before setting steps and learning rate.',
    '换基座后旧适配器不能复用，必须重新训练。': 'Adapters cannot be reused with a different base and must be retrained.',
    '改 r 时要同步改 α，否则强度会跟着漂。': 'Change α together with r, otherwise the strength drifts.',
    '显存不够先减它，再用梯度累积补回等效批量——这样 Loss 曲线还能和上一轮直接对比。': 'If memory runs short, lower it and use accumulation to restore the effective batch — then the loss curve still compares directly with the previous run.',
    '显存不够时优先减 batch_size 再补梯度累积——等效批量不变，Loss 曲线还能直接对比。': 'When memory is short, cut batch_size first and add accumulation — the effective batch stays the same and the loss curve remains comparable.',
    '显存占用约与 block_size × batch_size 成正比：先定 block_size，再用 batch_size 把显存填满。': 'Memory scales roughly with block_size × batch_size: fix block_size first, then raise batch_size until memory is full.',
    '校准 / 量化时每条样本截多长。': 'How long each sample is cut to during calibration / quantization.',
    '校准用的语料：点「开始」时自动合并预训练数据集，不需要单独挑文件。': 'Corpus for calibration: the pretraining dataset is merged automatically when you press Start — no need to pick files.',
    '每一步同时喂进多少条序列，决定显存占用和梯度噪声。': 'How many sequences go in per step — sets memory use and gradient noise.',
    '每一步同时喂进多少条指令样本。': 'How many instruction samples go in per step.',
    '每一步喂进多少条序列。': 'How many sequences go in per step.',
    '每步更新权重的幅度，最影响成败的一个数。': 'How far weights move each step — the single number that decides success most often.',
    '用 bf16/fp16 做前向反向、fp32 更新权重：省显存、提速。': 'Forward/backward in bf16/fp16 with fp32 weight updates: less memory, more speed.',
    '用 bf16/fp16 做前向反向、fp32 更新权重：省显存、提速，精度基本无损。': 'Forward/backward in bf16/fp16 with fp32 weight updates: less memory, more speed, essentially no accuracy loss.',
    '用多少条语料样本估计权重重要性 / 量化范围。': 'How many corpus samples estimate weight importance / quantization ranges.',
    '约束：d_model 必须等于 n_head × 每头维度，每头 32~64 维最稳（128 配 2~4 头，256 配 4~8 头）。': 'Constraint: d_model must equal n_head × head size, and 32–64 dimensions per head is safest (128 with 2–4 heads, 256 with 4–8).',
    '续训的更新幅度，必须比从零预训练小。': 'Update size for continuation — it must be smaller than pretraining from scratch.',
    '续训的起点权重，必须指向真实存在的 .pt 文件。': 'Starting weights for the continuation; it must point to an existing .pt file.',
    '要挂 LoRA 的基座模型，必填，且必须指向存在的 .pt。': 'Base model to attach LoRA to — required, and it must point to an existing .pt.',
    '要被裁切/量化的权重文件，必须指向真实存在的 .pt。': 'The weights file to prune / quantize; it must point to an existing .pt.',
    '超过 5 个 epoch 往往开始照抄训练集答案，泛化反而变差。': 'Beyond 5 epochs the model often starts copying training answers and generalises worse.',
    '这次续训要跑多少步：越少越不容易冲掉原有能力。': 'How many steps this continuation runs: fewer steps are less likely to wash out existing ability.',
    '随机置零激活的比例。': 'Share of activations randomly zeroed.',
    '随机置零激活的比例，用来抑制过拟合。': 'Share of activations randomly zeroed, used to curb overfitting.',
    '隐藏层宽度：每个 token 表示成这么长的向量，各层矩阵的宽都由它决定。越大表达力越强，参数量按平方级涨。': 'Hidden width: each token is a vector this long and every layer matrix is sized by it. Larger means more capacity, while parameters grow quadratically.',
    '训练中途额外保存几份权重：设 1 就在一半处存一次，设 2 就在 1/3、2/3 处各存一次（k/(N+1) 等分点）。': 'Extra checkpoints saved mid-training: set 1 to save once at the halfway point, set 2 to save at the 1/3 and 2/3 points (k/(N+1) divisions).',
    '用途：训练后期过拟合（验证 Loss 往上翘）时，可以回到中途那份继续用。': 'Use: when validation loss starts climbing late in training, resume from that mid-run checkpoint.',
    '文件存 checkpoints/，名字是「权重名-ck步数.pt」，仓库权重弹窗里能直接选。': 'Files land in checkpoints/ named “weights-ckSTEP.pt” and can be picked straight from the Repository weight dialog.',
    '从语料里留出多少做验证集：越大越能看出过拟合，但可用于训练的样本更少。常用 10%。': 'How much of the corpus is held out for validation: larger shows overfitting more clearly but leaves less data to train on. 10% is common.',
    '这个比例是全局的：预训练 / 继续预训练 / 指令微调 / LoRA 微调都按它切（config 里的 train.val_ratio）。': 'This ratio is global: pretraining, continuation, instruction tuning and LoRA tuning all split by it (train.val_ratio in the config).',
    '确定清除全部日志？': 'Clear all logs?',
    '删除所选产物': 'Delete selected outputs',
    '删除所选样本': 'Delete selected samples',
    '删除所选文件': 'Delete selected files',
    '新建文件夹': 'New folder',
    '复制文件夹': 'Copy folder',
    '复制权重': 'Copy weights',
    '复制数据集': 'Copy dataset',
    '合并产物': 'Merge outputs',
    '合并数据集': 'Merge datasets',
    '文件夹名': 'Folder name',
    '新文件名': 'New name',
    '新文件夹名': 'New folder name',
    '重命名文件夹': 'Rename folder',
    '名称': 'Name',
    '创建': 'Create',
    '合并': 'Merge',
    '复制': 'Copy',
    '重命名': 'Rename',
    '备注': 'Note',
    '文件名': 'File name',
    '路径': 'Path',
    '选择权重': 'Select weights',
    '搜索权重名称 / 备注': 'Search weight name / note',
    '备注': 'Note',
    '大小': 'Size',
    '更新时间': 'Updated',
    '文件夹': 'Folder',
    '读取中…': 'Loading…',
    '（未命名）': '(untitled)',
    '双击打开文件夹': 'Double-click a folder to open',
    '仓库里还没有权重。': 'No weights in the Repository yet.',
    '仓库里还没有 LoRA 适配器。': 'No LoRA adapters in the Repository yet.',
    '仓库里还没有可用作基座的权重。': 'No base weights in the Repository yet.',
    '个可用权重': 'available weights',
    '‹ 返回': '‹ Back',
    '训练中途额外保存几份权重：设 1 就在一半处存一次，设 2 就在 1/3、2/3 处各存一次。': 'Extra checkpoints saved mid-training: set 1 to save once at halfway, set 2 to save at the 1/3 and 2/3 points.',
    '取消置顶': 'Unpin',
    '导入文件': 'Import files',
    '导入文件夹': 'Import folder',
    '打开文件夹': 'Open folder',
    '移出文件夹': 'Move out of folder',
    '新建文件夹…': 'New folder…',
    '解锁（随文件夹锁定）': 'Unlock (follows the folder lock)',
    '重复惩罚': 'Repetition penalty',
    '查看内容': 'View content',
    '微调数据集': 'Fine-tune dataset',
    '移动到文件夹': 'Move to folder',
    '批量移动到文件夹': 'Bulk move to folder',     /* 用户 2026-10-22 */
    '批量移出': 'Bulk move out',
    '预训练数据集': 'Pretrain dataset',
    '数据集格式示例': 'Dataset format example',
    '文件夹里没有可添加的文件': 'No files to add in this folder',
    '文件不存在': 'File not found',
    '找不到文件': 'File not found',
    '不是权重文件': 'Not a weights file',
    '不是数据集文件': 'Not a dataset file',
    '已存在同名权重，不能添加': 'A weight with this name already exists',
    '已存在同名数据集，不能添加': 'A dataset with this name already exists',
    '已在仓库中': 'Already in the Repository',
    '只能回收工程目录内的文件': 'Only files inside the project can be recycled',
    '不是有效的文件夹': 'Not a valid folder',
    '暂无历史会话': 'No conversations yet',
    '换一批': 'More',
    /* ---------- 导航分组 / 页面 ---------- */
    '工作台': 'Workbench', '数据中心': 'Data Center', '训练中心': 'Training', '回收站': 'Recycle Bin',
    '系统': 'System', '模型': 'Models',
    '总览': 'Overview', '仓库': 'Repository', '托管': 'Autopilot',
    '数据集': 'Datasets', '语料蒸馏': 'Corpus Distill', '响应蒸馏': 'Response Distill',
    '网页整理': 'Web Curation', '格式转换': 'Format Convert',
    '训练流水线': 'Pipeline', '预训练': 'Pretrain', '继续预训练': 'Continue Pretrain',
    '指令微调': 'Instruction Tune', 'LoRA 微调': 'LoRA Tune', 'LoRA 合并': 'LoRA Merge',
    '模型裁切': 'Prune', '量化': 'Quantize', '对话验证': 'Chat Test', '导出模型': 'Export',
    '权重': 'Weights', '全局设置': 'Settings', '设置': 'Settings', '页面迁移中': 'Page in progress',

    /* ---------- 设置页 ---------- */
    '外观与渲染性能': 'Appearance and rendering', '外观': 'Appearance', '主题': 'Theme',
    '浅色': 'Light', '深色': 'Dark', '核显加速': 'iGPU acceleration',
    '该设置需要重启应用后生效。': 'This setting takes effect after a restart.',
    '立即重启': 'Restart now', '蒸馏数据集默认引入': 'Auto-import distilled datasets',
    '强制托管训练完成': 'Force autopilot training to finish', '默认 API': 'Default API',
    '保存': 'Save', '测试': 'Test', '训练设备': 'Training device',
    '语言': 'Language', '界面语言': 'Interface language', '中文': '中文', '英文': 'English',

    /* ---------- 回收站（用户 2026-10-20 补翻） ---------- */
    '单条数据': 'Individual records', '恢复所选': 'Restore selected',
    '彻底删除所选': 'Delete selected', '清空回收站': 'Empty recycle bin',
    '查看内容': 'View content', '查看详细信息': 'View details', '详细信息': 'Details',
    '清空': 'Clear', '删除时间': 'Deleted', '响应样本': 'Response sample',
    '隔离样本': 'Quarantined samples', '问': 'Q', '答': 'A',
    '请先勾选要恢复的条目': 'Select the items to restore first',
    '请先勾选要彻底删除的条目': 'Select the items to delete first',
    '这个文件夹里没有文件': 'No files in this folder',
    '（文件夹里没有文件）': '(no files in this folder)',
    '这条权重还没有 Loss 曲线': 'No loss curve for this weight yet',
    '确定执行该操作？': 'Run this action?',
    '导出数据集': 'Export dataset',
    '模型转换': 'Model Convert', '内存占用': 'Memory usage',

    /* ---------- 训练页 / 设置页（用户 2026-10-20 补翻） ---------- */
    '检查点数量': 'Checkpoints', '未选择': 'Not selected', '未完成功能': 'Unfinished features',
    '开启': 'On', '暂无内容': 'Nothing here yet',
    '压缩环境未就绪': 'Compression environment is not ready',
    '没有符合的数据集。': 'No matching datasets.',
    '仓库里还没有权重。': 'No weights in the repository yet.',
    '仓库里还没有可用作基座的权重。': 'No base weights in the repository yet.',
    '仓库里还没有 LoRA 适配器。': 'No LoRA adapters in the repository yet.',

    /* ---------- 输入框占位提示（用户 2026-10-20） ---------- */
    '搜索权重名称 / 备注': 'Search weight name / note', '搜索权重名称': 'Search weight name',
    '搜索': 'Search', '搜索数据集': 'Search datasets', '搜索文章标题 / 主题': 'Search title / theme',
    '输出权重名': 'Output weight name', '输出文件夹': 'Output folder',
    '请选择.safetensors/.pt': 'Select .safetensors/.pt',
    '输入问题，回车或点「发送」': 'Type a question, then press Enter or Send',
    '粘贴新密钥': 'Paste new key', '输入新密钥': 'Enter new key',
    '每行一个，\n不填入表示随机主题。': 'One per line; leave empty for random topics.',
    '要查什么？一句话或几个关键词，\n例如：光合作用的原理':
      'What do you want to look up? A sentence or a few keywords, e.g. how photosynthesis works',

    /* ---------- 通用按钮 / 动作 ---------- */
    '取消': 'Cancel', '关闭': 'Close', '确定': 'OK', '知道了': 'Got it', '确认': 'Confirm',
    '删除': 'Delete', '刷新': 'Refresh', '重试': 'Retry', '选择': 'Select', '搜索': 'Search',
    '新建': 'New', '导入': 'Import', '导出': 'Export', '添加': 'Add', '开始': 'Start',
    '停止': 'Stop', '暂停': 'Pause', '继续': 'Resume', '中止': 'Abort', '复制': 'Copy',
    '重命名': 'Rename', '恢复': 'Restore', '彻底删除': 'Delete permanently', '全选': 'Select all',
    '删除所选': 'Delete selected', '批量删除': 'Delete selected', '明细': 'Details',
    '查看方案': 'View plan', '生成方案': 'Generate plan', '填充示例': 'Fill example',
    '保存配置': 'Save config', '重置': 'Reset', '重新加载': 'Reload', '发送': 'Send', '收起': 'Collapse',
    '展开': 'Expand', '置顶': 'Pin', '更多': 'More', '返回': 'Back', '下一页': 'Next',
    '上一页': 'Prev', '条': 'rows', '字': 'chars',

    /* ---------- 表头 / 字段 ---------- */
    '名称': 'Name', '文件名': 'File', '大小': 'Size', '日期': 'Date', '更新时间': 'Updated',
    '状态': 'Status', '进度': 'Progress', '参数': 'Params', '备注': 'Note', '路径': 'Path',
    '来源': 'Source', '类型': 'Type', '方式': 'Method', '训练方式': 'Training method',
    '外部导入': 'External import',
    '数据类型': 'Data type', '字符数': 'Characters', '格式审查': 'Format check',
    '训练方式 1': 'Training method 1', '训练方式 2': 'Training method 2', '浏览': 'Browse',
    '条数': 'Records', '并发': 'Concurrency', '数据集名': 'Dataset name', '体量': 'Volume',
    '步数': 'Steps', '学习率': 'Learning rate', '批量大小': 'Batch size', '最大序列长': 'Max seq len',
    '混合精度 AMP': 'AMP', '丢弃率': 'Dropout', '缩放 α': 'Scale α', '目标字数': 'Target chars',
    /* 预训练页参数中文化（用户 2026-10-19）：英文模式仍显示技术名 */
    '模型维度': 'd_model', '注意力头数': 'n_head', '网络层数': 'n_layer',
    'LoRA 丢弃率': 'LoRA dropout',
    '验证集比例': 'Validation split', '重复惩罚': 'Repetition penalty',
    '轮次': 'Rounds', '每次检索': 'Queries / round', '每词页数': 'Pages / query',
    '输出名': 'Output name', '启用': 'Enabled', '基座': 'Base', '基座权重': 'Base weights',
    '初始权重': 'Initial weights', '输出权重': 'Output weights', '预训练权重': 'Pretrained weights',
    '模型权重': 'Model weights', '语料': 'Corpus', '语料体量': 'Corpus size',
    '训练输出': 'Training output', '运行日志': 'Runtime log', '训练状态': 'Training status',
    'Loss 曲线': 'Loss curve', 'Loss 日志表': 'Loss log', '暂无训练日志': 'No training log yet',
    '一键清空': 'Clear all',
    '暂无足够数据点': 'Not enough data points', '权重命名': 'Weight name',
    '验证样本': 'Validation samples', '校准样本数': 'Calibration samples',
    '覆盖初始权重': 'Overwrite initial weights', '从仓库选择权重': 'Pick weights from repository',
    '选择权重': 'Select weights', '未命名': 'Unnamed', '（未命名）': '(unnamed)',

    /* ---------- 状态 ---------- */
    '未启动': 'Idle', '未生成': 'None', '尚未开始': 'Not started yet',
    '未加载': 'Not loaded', '已加载': 'Loaded', '未开始': 'Not started',
    '已完成': 'Completed', '完成': 'Done', '失败': 'Failed', '运行中': 'Running',
    '已停止': 'Stopped', '缺失': 'Missing', '不存在': 'Missing', '未知': 'Unknown',
    '合格': 'Valid', '重复': 'Duplicate', '纯文本': 'Text', '文本': 'Text', '问答': 'Q&A',
    '空闲': 'Idle', '就绪': 'Ready', '正常': 'OK', '异常': 'Error', '有': 'Yes', '无': 'None',
    '共': 'Total', '正在': 'Running', '成功': 'OK', '警告': 'Warning', '错误': 'Error',
    '训练中': 'Training', '已暂停': 'Paused', '加载中': 'Loading', '读取中…': 'Loading…',
    '暂无数据': 'No data', '空的': 'Empty', '回收站是空的': 'Recycle bin is empty',

    /* ---------- 本机状态 / 托管 ---------- */
    '占用内存': 'Memory used', '剩余内存': 'Free memory', '占用存储': 'Storage used', '本工程占用': 'Project size',
    '本机状态': 'Machine', '目标模型': 'Target model', '方案与预估': 'Plan & estimate',
    '托管进度': 'Progress', '结论': 'Conclusion', '风险提示': 'Risks', 'AI 方案': 'AI plan',
    '数据蒸馏': 'Data distillation', '训练': 'Training', '验证': 'Validation',
    '预估': 'Estimate', '仓库体检': 'Repository audit', '可用': 'Available',
    '条数不足': 'Too few records', 'token 消耗': 'Tokens used', '生成方案说明': 'Plan summary',
    '方案说明': 'Summary', '目标模型描述': 'Model brief', '风险与对策': 'Risks & mitigations',

    /* ---------- 对话验证 ---------- */
    '新对话': 'New chat', '历史': 'History', '系统提示词': 'System prompt',
    '请输入问题': 'Type a question', '发送中…': 'Sending…', '上下文': 'Context',

    /* ---------- 数据集 / 回收站 ---------- */
    '会移入回收站，可恢复。': 'Moves to Recycle Bin, recoverable.',
    '确定删除选中的': 'Delete selected', '采集中': 'Collecting', '采集中心': 'Collector',
    '数据管理': 'Data', '采集参数': 'Collector params', '审阅模型': 'Reviewer model',
    '模型配置': 'Model config', '数据集名称': 'Dataset name',

    /* ---------- 训练页 ---------- */
    '开始训练': 'Start training', '停止训练': 'Stop training', '已停止训练。': 'Training stopped.',
    '训练进程未运行': 'Training process is not running', '训练输出为空': 'No training output yet',
    '保存失败': 'Save failed', '读取失败': 'Read failed', '启动失败': 'Start failed',
    '停止失败': 'Stop failed', '删除失败': 'Delete failed', '合并失败': 'Merge failed',
    '生成失败': 'Generate failed', '导出失败': 'Export failed', '操作失败': 'Action failed',
    '未知错误': 'Unknown error', '桥未就绪': 'Bridge not ready', '无响应': 'Not responding',
    '尚未测试': 'Not tested', '未配置': 'Not configured', '（未配置）': '(not configured)',

    /* ---------- 格式转换 ---------- */
    '转换': 'Convert', '输入': 'Input', '输出': 'Output', '格式': 'Format',
    '预览': 'Preview', '样本预览': 'Sample preview', '文本': 'Text', '对话': 'Chat',

    /* ---------- 提示 ---------- */
    '暂无': 'None', '共有': 'Total', '更新于': 'Updated', '大小': 'Size',
    '本机剩余可用内存': 'Free memory', '显存': 'VRAM', '内存': 'RAM',
    '剩余显存': 'Free VRAM', '占用显存': 'Used VRAM', '显存不足': 'Out of VRAM',
    '内存不足': 'Out of memory', '其他': 'Other', '设备': 'Device', '问题': 'Question',
    '提示': 'Notice', '一致': 'Match', '已选': 'Selected', '样本': 'samples',
    '文本长度': 'Text length', '字符': 'chars',
    /* ---------- 第二轮补齐（2026-10-18 巡检） ---------- */
    '蒸馏': 'Distill', '未标注': 'Unlabeled', 'AI 助手': 'AI Assistant', '切换工程': 'Switch project',
    '新建工程': 'New project', '文章样本': 'Article samples', '问答样本': 'Q&A samples',
    '占用存储': 'Storage used', '文件占用': 'File size', '日志': 'Log', '内存：': 'Memory:', '关于': 'About', '应用': 'App',
    '版本': 'Version', '该页正在迁移到新渲染管线': 'This page is being migrated',
    '还没有工程': 'No project yet', '创建工程': 'Create project', '导入工程': 'Import project',
    '明细 ›': 'Details ›', '暂无产物': 'No output yet', '没有匹配的产物': 'No matching output',
    /* 蒸馏页（语料 / 响应）数据管理的空态与删除弹窗（用户 2026-10-20） */
    '没有匹配的文件。': 'No matching files.',
    '共 0 个文件：输出目录没有产物文件。': '0 files: the output folder has no artifacts.',
    '没有匹配的样本。': 'No matching samples.',
    '该文件没有可读的逐条数据。': 'This file has no readable records.',
    '共 0 条：输出目录没有可读的 *.jsonl（排除 *_rejected）。':
      '0 rows: no readable *.jsonl in the output folder (excluding *_rejected).',
    '…（文件过大，已截断显示）': '… (file too large, truncated)',
    '文件读取失败：': 'Failed to read file: ',
    '删除所选文件': 'Delete selected files', '删除所选样本': 'Delete selected records',
    '会移入回收站，可恢复。': 'Moves to the Recycle Bin; it can be restored.',
 '▶ 开始': '▶ Start', '文件夹': 'Folder', '行数': 'Rows', '全部': 'All',
    '微调权重': 'Tuned weights', '加载基座权重': 'Load base weights',
    'LoRA权重': 'LoRA weights', '基座权重': 'Base weights',
    '加载 LoRA 权重': 'Load LoRA weights', '清空对话': 'Clear chat', '未加载模型': 'No model loaded',
    '资源占用': 'Resources', '参数量': 'Parameters', '生成参数': 'Generation params',
    '来自 config.json（只读）': 'From config.json (read-only)', '当前工程': 'Current project',
    '推理设备': 'Inference device', '生成长度': 'Max tokens', '上下文长度': 'Context length',
    '训练提示词': 'Training prompt', '检查点文件': 'Checkpoint file', '采集数量': 'Collect count',
    '并发数': 'Concurrency', '输出目录': 'Output dir', '开始采集': 'Start collecting',
    '采集篇数': 'Collect pieces', '最大字数': 'Max chars', '高级设置': 'Advanced',
    '保存并隔离被拒文本': 'Save & quarantine rejected text', '填充示例主题': 'Fill example topics',
    '合并': 'Merge', '输出文件名': 'Output file name', 'LoRA 适配器': 'LoRA adapter',
    '校准语料': 'Calibration corpus', '开始时自动合并预训练数据集': 'Auto-merge datasets on start',
    '示例': 'Example', '起': 'From', '止': 'To', '托管 AI 的决策': 'Autopilot AI decisions',
    /* 训练页右侧筛选：英文别带 datasets，否则三个选项换行（用户 2026-10-20） */
    '可用数据集': 'Available', '标注数据集': 'Labeled',
    '已选数据集': 'Selected', '未选数据集': 'Unselected',
    '题目模型': 'Question model', '写作模型': 'Writer model', '最少字数': 'Min chars',
    '最多字数': 'Max chars', '标题': 'Title', '出题模型': 'Question model', '答题模型': 'Answer model',
    '微调方式': 'Tuning method', '规划模型': 'Planner model', '整理模型': 'Curator model',
    '搜索引擎网址': 'Search engine URL', '重复率阈值': 'Duplicate threshold',
    '每轮检索式': 'Queries / round', '每条检索抓几篇': 'Pages / query', '最多几轮': 'Max rounds',
    '开始整理': 'Start curating', '字数': 'Chars', '重复率': 'Duplicate rate',
    '源文件': 'Source file', '选择文件…': 'Choose file…', '识别结果': 'Detected',
    '尚未选择文件': 'No file selected', '转换设置': 'Conversion settings',
    '目标形状与产物命名': 'Target shape and naming', '目标格式': 'Target format',
    '自动识别': 'Auto', '最多转换条数': 'Max records',
    '只在交给模型时生效': 'Applies only when sent to the model', '每批条数': 'Batch size',
    '运行状态': 'Runtime status',
    /* ---------- 第三轮补齐（2026-10-18：英文模式残留排查） ---------- */
    '输出权重名': 'Weight name',
    '描述任务，输入 / 调用技能': 'Describe the task, type / for skills',
    '例如：一个懂基础物理的中文对话模型，回答简洁、公式与单位准确，能在本机上跑':
      'e.g. a Chinese chat model that knows basic physics, brief answers with accurate formulas and units, runs locally',
    'LoRA 秩 r': 'LoRA rank r', '挂载目标层': 'Target modules', '▶ 合并': '▶ Merge',
    '合并状态': 'Merge status', '裁切方法': 'Prune method', '稀疏度': 'Sparsity',
    '量化方案': 'Quant scheme', '暂无明细': 'No details yet', '暂无训练日志': 'No training log yet',
    '最小化': 'Minimize', '最大化 / 还原': 'Maximize / Restore', '切换窗口大小': 'Toggle window size',
    '本工程的对话历史': 'Chat history of this project', '对话历史': 'Chat history',
    '收起/展开': 'Collapse / expand', '拖动调整输入框高度': 'Drag to resize the input box',
    '拖动调整窗口大小': 'Drag to resize the window', '活动栏': 'Activity bar', '侧栏': 'Sidebar',
    '导航': 'Navigation', '关闭工程': 'Close project', '选择工程文件夹并切换': 'Pick a project folder to switch',
    '选择文件夹作为新工程': 'Pick a folder as a new project', '删除该条': 'Delete this item',
    '主题即时生效，随配置持久化': 'Applies instantly and is saved to config',
    '已保存（点一下可输入新密钥）': 'Saved (click to enter a new key)',
    '本机未识别到cuda 设备': 'No CUDA device detected', '放大查看': 'Open larger view',
    '数据来源筛选': 'Data source filter', '选择状态筛选': 'Selection filter',
    /* 输入框提示（含换行的多行提示要整串匹配，2026-10-18） */
    '每行一个，\n不填入表示随机主题。': 'One per line; leave empty for random topics.',
    '要查什么？一句话或几个关键词，\n例如：光合作用的原理': 'What to search? A sentence or keywords,\ne.g. how photosynthesis works',
    '点右侧「选择文件…」挑一个下载来的数据集': 'Use “Choose file…” on the right to pick a downloaded dataset',
    '使用源文件名': 'Use source file name',
    '输入问题，回车或点「发送」': 'Type a question, then press Enter or Send',
    '（未选择）': '(none selected)',
    '模型文件': 'Model files', '重新扫描': 'Rescan', '导出到': 'Export to', '选择文件夹': 'Choose folder', '选择文件': 'Choose file',
    '筛选训练方式': 'Filter by method', '搜索权重名称': 'Search weights',
    '导出日志': 'Export log', '记录数': 'Records', '已转换': 'Converted', '产物': 'Output',
    '耗时': 'Elapsed', '开始转换': 'Start conversion', '保存参数': 'Save params',
    '流水线步骤': 'Pipeline steps', '当前步骤': 'Current step', '已用时间': 'Elapsed',
    /* ---------- 模型转换页 / 导出页漏翻（用户 2026-10-23） ---------- */
    '源权重': 'Source weights',
    '张量': 'Tensors',
    '词表': 'Vocabulary',
    '保存至工程': 'Save to project',
    '转换状态': 'Conversion status',
    '方向': 'Direction',
    '转换日志': 'Conversion log',
    '权重文件': 'Weight files',
    '全部文件': 'All files',
    '选择要转换的权重': 'Select weights to convert',
    '选择输出文件夹': 'Choose output folder',
    '待转换': 'Idle',
    '转换中…': 'Converting…',
    '已结束': 'Finished',
    '转换失败': 'Conversion failed',
    '保存到 EAI 权重': 'Save into EAI weights',
    '另存到文件夹': 'Save to a folder',
    '已请求停止…': 'Stop requested…',
    '尚无此文件': 'File missing',
    '选择导出文件夹': 'Choose export folder',
    '导出到此': 'Export here',
    '继续预训练权重': 'Continue-pretrain weights',
    '指令微调权重': 'Instruction-tuned weights',
    'LoRA 权重': 'LoRA weights',
    'LoRA 合并权重': 'LoRA merged weights',
    '其他权重': 'Other weights',
    'LoRA 微调权重': 'LoRA-tuned weights',
    'LoRA 适配器': 'LoRA adapter',
    '（工程 checkpoints/）': ' (project checkpoints/)',
    /* ---------- 悬停 / aria 漏翻（用户 2026-10-24） ---------- */
    '已锁定：EAI 内不能删除': 'Locked: cannot be deleted inside EAI',
    '留空即保留原值；已保存时显示为点，点一下可输入新密钥': 'Leave empty to keep the current key; when saved it shows as dots, click to enter a new one',
    '添加训练步骤': 'Add training step',
    'loss 曲线': 'Loss curve',
    '历史会话': 'History',
    '删除这条会话': 'Delete this conversation',
    '未选择权重，不能勾选覆盖初始权重': 'No weight selected: cannot enable overwrite initial weights',
    '不可用': 'Unavailable',
    '有风险': 'Risky',
    '未填写': 'Empty',
    'auto = 有 xpu/cuda 就用，否则 cpu': 'auto = use xpu/cuda when available, otherwise cpu',
    '自建': 'User-made',
  };

  /* 带数字的短句：整句匹配词典覆盖不到，这里按模式翻（2026-10-18） */
  var RULES = [
    /* 悬停 / aria 里带变量或拼出来的句子（用户 2026-10-24）：整串查不到词典，按模式换 */
    [/^「(.+)」已锁定，不能勾选覆盖初始权重$/, '“$1” is locked: cannot enable overwrite initial weights'],
    [/^随文件夹「(.+)」锁定：文件夹不解锁就不能单独解锁$/, 'Locked with folder “$1”: unlock the folder first'],
    [/^当前工程：(.+)（来源：(.+)） · 配置：(.+)$/, 'Project: $1 (source: $2) · Config: $3'],
    [/^未测试连通性 · 点「测试」验证（(\d+) 个模型配置(都已有密钥|部分缺密钥)）$/, function (a) {
      return 'Not tested: click “Test” (' + a[1] + ' model profiles, '
        + (a[2] === '都已有密钥' ? 'all have keys' : 'some missing keys') + ')';
    }],
    /* 数据集卡「训练方式」是多个标注拼起来的：预训练、继续预训练 → Pretrain, Continue Pretrain */
    [/^(?:预训练|继续预训练|指令微调|LoRA 微调|未标注)(?:、(?:预训练|继续预训练|指令微调|LoRA 微调|未标注))+$/, function (a) {
      return String(a[0]).split('、').map(function (x) { return EN[x] || x; }).join(', ');
    }],
    /* 弹窗里带变量/名字的句子（用户 2026-10-22）：整段查不到词典，只能按模式换 */
    [/^确定解锁「(.+)」？解锁后可被删除。$/, 'Unlock “$1”? It can be deleted once unlocked.'],
    [/^确定删除「(.+)」？此操作不可恢复。$/, 'Delete “$1”? This cannot be undone.'],
    [/^确定删除「(.+)」？里面的文件一起删，进回收站。$/, 'Delete folder “$1”? Its files are deleted with it and go to the Recycle Bin.'],
    [/^确定删除所选 (\d+) 个文件？此操作不可恢复。$/, 'Delete the $1 selected file(s)? This cannot be undone.'],
    [/^确定删除选中的 (\d+) 个文件？$/, 'Delete the $1 selected file(s)?'],
    [/^确定删除选中的 (\d+) 条样本？$/, 'Delete the $1 selected record(s)?'],
    [/^确定删除选中的 (\d+) 个产物？$/, 'Delete the $1 selected output(s)?'],
    [/^共 (\d+) 个可用权重$/, '$1 available weight(s)'],
    [/^「(.+)」文件夹已存在$/, 'Folder “$1” already exists'],
    [/^已有同名文件「(.+)」，文件夹不能与文件同名$/, 'A file named “$1” already exists — a folder cannot share its name'],
    [/^没有匹配「(.+)」的权重。$/, 'No weights matching “$1”.'],
    [/^仓库里还没有可用作基座的权重。$/, 'No base weights in the Repository yet.'],
    [/^文件存(?:在)? checkpoints\/，名字是「权重名-ck步数\.pt」，仓库权重弹窗里能直接选。$/,
      'Files land in checkpoints/ named “weights-ckSTEP.pt” and can be picked straight from the Repository weight dialog.'],
    [/^(.+)：所在文件夹「(.+)」已锁定，先解锁文件夹再删除$/, '$1: its folder “$2” is locked — unlock the folder before deleting'],
    [/^(.+)：(不是权重文件|不是数据集文件)$/, function (m) { return m[1] + ': not a ' + (m[2] === '不是权重文件' ? 'weights' : 'dataset') + ' file'; }],
    [/^(.+)：已在仓库中$/, '$1: already in the Repository'],
    [/^(.+)：已存在同名权重，不能添加$/, '$1: a weight with this name already exists'],
    [/^(.+)：已存在同名数据集，不能添加$/, '$1: a dataset with this name already exists'],
    [/^(.+)：已存在同名数据集，不能合并$/, '$1: a dataset with this name already exists'],
    [/^(.+)：(找不到文件|文件不存在)$/, '$1: file not found'],
    [/^(.+)：文件夹里没有可添加的文件$/, '$1: no files to add in this folder'],
    [/^(.+)：已锁定，先解锁再删除$/, '$1: locked — unlock it before deleting'],
    [/^(.+)：不在当前工程目录内，已拒绝删除$/, '$1: outside the current project, deletion refused'],
    [/^已清空全部对话和历史（(\d+) 条）[。]?$/, 'Cleared all conversations and history ($1 record(s))'],
    [/^⚠ 自动停止失败：(.+)$/, '⚠ Auto-stop failed: $1'],
    [/^⚠ (显存|内存)不足 1GB，已自动(停止|暂停)训练（进度保存后退出）。$/,
      function (m) { return '⚠ Only 1 GB of ' + (m[1] === '显存' ? 'VRAM' : 'memory') + ' left — training ' + (m[2] === '停止' ? 'stopped' : 'paused') + ' automatically (progress saved).'; }],
    [/^删除失败：(.+)$/, 'Delete failed: $1'],
    [/^合并失败：(.+)$/, 'Merge failed: $1'],
    [/^保存失败：(.+)$/, 'Save failed: $1'],
    [/^读取失败：(.+)$/, 'Read failed: $1'],
    [/^本机剩余可用内存 (.+)，已低于 (\d+) GB。$/, 'Only $1 of memory left on this machine, below $2 GB.'],
    [/^剩余显存 (.+)，已低于 (\d+) GB。$/, 'Only $1 of VRAM left, below $2 GB.'],
    [/^(\d[\d,.]*)\s*条排队$/, '$1 queued'],
    [/^(\d[\d,.]*)\s*条$/, '$1 rows'],
    [/^(\d[\d,.]*)\s*个文件$/, '$1 files'],
    [/^(\d[\d,.]*)\s*个$/, '$1'],
    [/^(\d[\d,.]*)\s*字符$/, '$1 chars'],
    [/^(\d[\d,.]*)\s*字$/, '$1 chars'],
    [/^共\s*(\d[\d,.]*)\s*条$/, '$1 total'],
    [/^(\d[\d,.]*)\s*项$/, '$1'],
    [/^(\d[\d,.]*)\s*份$/, '$1']
  ];

  /* 启动语言（用户 2026-10-20）：先用本地记忆的值，进软件那一刻就是对的；
     之后设置页保存 / 切换时会再写回，服务端的 config.app.lang 只做兜底校准。 */
  var hadLocal = false;
  var lang = (function () {
    try {
      var v = localStorage.getItem('eaiLang');
      if (v === 'en' || v === 'zh') { hadLocal = true; return v; }
    } catch (e) { /* 隐私模式 */ }
    return 'zh';
  })();
  var store = new WeakMap();          /* 文本节点 → 中文原文 */
  var attrStore = new WeakMap();      /* 元素 → { attr: 原文 } */
  var ATTRS = ['placeholder', 'title', 'aria-label'];
  var observer = null;
  var queued = false;

  function hasCJK(s) { return /[\u4e00-\u9fa5]/.test(s); }

  /* 文本 / 属性的统一翻译入口（2026-10-24）：先整串查词典，再认「语料 · 方式 · 文件名」前缀，
     最后按 RULES 模式换。悬停 title / aria-label 以前只查词典，拼出来的句子永远翻不到，
     现在属性也走这里 —— 加规则即可，不用在每处调用点手写 bi()。 */
  function matchText(core) {
    var hit = EN[core];
    if (hit === undefined) {
      /* 日志行是桥层拼的「语料/响应 · 训练方式 · 文件名」（用户 2026-10-18）：只换前缀，文件名原样保留 */
      var m = /^(语料|响应)\s*·\s*(预训练|继续预训练|指令微调|LoRA 微调)\s*·\s*/.exec(core);
      if (m) {
        var head = m[1] === '语料' ? 'Corpus' : 'Response';
        var use = { '预训练': 'Pretrain', '继续预训练': 'Continue Pretrain',
                    '指令微调': 'Instruction Tune', 'LoRA 微调': 'LoRA Tune' }[m[2]];
        hit = head + ' · ' + use + ' · ' + core.slice(m[0].length);
      }
    }
    if (hit === undefined) {
      for (var ri = 0; ri < RULES.length; ri++) {
        if (RULES[ri][0].test(core)) {
          var rre = RULES[ri][0], rrep = RULES[ri][1];
          hit = (typeof rrep === 'function')
            ? core.replace(rre, function () { return rrep(Array.prototype.slice.call(arguments)); })
            : core.replace(rre, rrep);
          break;
        }
      }
    }
    return hit;
  }

  function translateNode(node) {
    if (!node || node.nodeType !== 3) return;
    var raw = node.nodeValue;
    if (!raw) return;
    var core = raw.trim();
    if (!core || !hasCJK(core)) return;
    var parent = node.parentNode;
    if (parent && parent.nodeType === 1) {
      /* 日志/报告/代码块整块不翻；但 pre 里的空态短句（「暂无训练日志」）要翻，靠长度区分 */
      var skip = parent.closest && parent.closest('[data-no-i18n], textarea, .ho-log, .ho-logbox, .detail-body, .ho-sub-pre, .ho-report, .rs-preview, .hljs');
      if (skip) return;
      if (parent.closest && parent.closest('pre, code') && (core.length > 12 || core.indexOf('\n') >= 0)) return;
    }
    var hit = matchText(core);
    if (hit === undefined) return;
    if (!store.has(node)) store.set(node, raw);
    node.nodeValue = raw.replace(core, hit);
  }

  function translateAttrs(el) {
    if (!el || el.nodeType !== 1) return;
    if (el.closest && el.closest('[data-no-i18n]')) return;
    var saved = null;
    ATTRS.forEach(function (a) {
      var v = el.getAttribute && el.getAttribute(a);
      if (!v || !hasCJK(v)) return;
      var hit = matchText(v.trim());
      if (hit === undefined) return;
      saved = saved || {};
      saved[a] = v;
      el.setAttribute(a, hit);
    });
    if (saved) attrStore.set(el, saved);
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    translateAttrs(root.nodeType === 1 ? root : null);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, null);
    var n = walker.nextNode();
    while (n) {
      if (n.nodeType === 3) translateNode(n);
      else translateAttrs(n);
      n = walker.nextNode();
    }
  }

  function restore() {
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      var saved = attrStore.get(el);
      if (saved) {
        Object.keys(saved).forEach(function (a) { el.setAttribute(a, saved[a]); });
        attrStore.delete(el);
      }
    }
    var tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var n = tw.nextNode();
    while (n) {
      if (store.has(n)) { n.nodeValue = store.get(n); store.delete(n); }
      n = tw.nextNode();
    }
    if (document.title) document.title = EN[document.title] || document.title;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    var run = function () {
      queued = false;
      if (lang === 'en') walk(document.body);
      syncSegThumbs();   /* 翻译完再对一次滑块（用户 2026-10-20） */
    };
    /* 用微任务而不是 requestAnimationFrame（用户 2026-10-20）：rAF 是在下一帧绘制时才翻，
       新页面会先画出一帧中文再被替换。微任务在同一次渲染前跑完，看到的第一眼就是英文。 */
    if (typeof Promise !== 'undefined') Promise.resolve().then(run);
    else setTimeout(run, 0);
  }

  function observe() {
    if (observer || typeof MutationObserver === 'undefined') return;
    observer = new MutationObserver(function (muts) {
      if (lang !== 'en') return;
      /* 只由属性变化触发的这一轮，如果新值里已经没有中文就不用再走一遍（避免自己改自己无限循环） */
      var onlyAttr = true, need = false;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type !== 'attributes') { onlyAttr = false; need = true; break; }
        var v = m.target && m.target.getAttribute ? m.target.getAttribute(m.attributeName) : '';
        /* 2026-10-24：拼出来的句子（「X」已锁定…、当前工程：…）词典里没有，但 RULES 认；
           这里只看词典会漏掉「挂载后才异步设上的 title」。走 matchText 一并判断。 */
        if (v && hasCJK(v) && matchText(v.trim()) !== undefined) need = true;
      }
      if (!need && onlyAttr) return;
      schedule();
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS });   /* 用户 2026-10-20：placeholder 后设置的也要翻 */
  }

  /* 语言切换的订阅（用户 2026-10-20）：页面里跟语言走的文案（如蒸馏提示词）注册回调，
     切换语言时同步刷新，不必等切页重挂。 */
  var SUBS = [];
  function on(fn) {
    if (typeof fn === 'function' && SUBS.indexOf(fn) < 0) SUBS.push(fn);
  }
  function fire() {
    SUBS.forEach(function (f) { try { f(lang); } catch (e) { /* 单个订阅出错不影响切换 */ } });
  }

  function setLang(next) {
    lang = next === 'en' ? 'en' : 'zh';
    try { localStorage.setItem('eaiLang', lang); } catch (e) { /* 隐私模式 */ }
    document.documentElement.setAttribute('lang', lang === 'en' ? 'en' : 'zh-CN');
    if (lang === 'en') { observe(); walk(document.body); }
    else restore();
    paintLangSeg();
    /* 语言一换，上一门语言量出来的宽度一律作废：还没重新量到的横条先不显示滑块，
       免得「先按英文宽度画一帧、再缩回中文宽度」（用户 2026-10-20）。 */
    Array.prototype.forEach.call(document.querySelectorAll('.seg'), function (s) {
      s.removeAttribute('data-seg-fit');
    });
    /* 段选控件（.seg）的滑块是按文字宽度算出来的：语言一换，文字宽度变了，
       滑块必须重新对位，否则位置还停在中文时的宽度上（用户 2026-10-20：横条位置不变，点一下才对） */
    reflowSegThumbs();
    fire();
  }

  /* 段选滑块对位（用户 2026-10-20）：滑块宽度/位移是按当前选中项的文字宽度算的，
     语言一换、页面重建、字体加载完都会变，所以几个时机都要重算：
     setLang 当场、下一帧、页面重建（walk 之后）、稍等一下（翻译是异步套上去的）、字体就绪后。 */
  function syncSegThumbs() {
    var thumbs = document.querySelectorAll('.seg > .seg-thumb');
    Array.prototype.forEach.call(thumbs, function (th) {
      var seg = th.parentElement;
      if (!seg) return;
      var on = seg.querySelector('.seg-opt.on') || seg.querySelector('.seg-opt[aria-checked="true"]');
      if (!on) return;
      var w = on.offsetWidth, x = on.offsetLeft;
      if (!w) return;
      var tf = 'translateX(' + x + 'px)';
      seg.setAttribute('data-seg-fit', lang);   /* 这一门语言的宽度已经量准 → 允许显示滑块 */
      if (th.style.width === w + 'px' && th.style.transform === tf) return;
      /* 这里只做「纠正」：页面刚建好 / 文案刚被翻译时宽度还是旧的，直接落位、不参与过渡，
         否则会从左边「弹」进来、还越过横条左缘（用户 2026-10-20）。
         用户点选项时的滑动由 api.segThumb 负责，不受影响。 */
      th.classList.add('seg-thumb-init');
      th.style.width = w + 'px';
      th.style.transform = tf;
      void th.offsetWidth;                         /* 强制布局，让这次赋值不参与过渡 */
      requestAnimationFrame(function () { th.classList.remove('seg-thumb-init'); });
    });
  }
  function reflowSegThumbs() {
    syncSegThumbs();
    var later = function () { if (lang === 'en' || lang === 'zh') syncSegThumbs(); };
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(later);
      requestAnimationFrame(function () { requestAnimationFrame(later); });
    } else setTimeout(later, 16);
    setTimeout(later, 120);
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
      document.fonts.ready.then(function () { setTimeout(later, 0); });
    }
  }

  /* 保险：任何点击/键操作之后补量一次（用户 2026-10-20）。
     切语言时会给所有横条作废 data-seg-fit，藏在别的页签/面板里的横条当场量不到宽度，
     滑块先不显示；等它被切出来，下一次点击就补上，不会留下没滑块的横条。 */
  function hookReflow() {
    if (hookReflow.once) return;
    hookReflow.once = 1;
    var kick = function () { setTimeout(syncSegThumbs, 0); };
    document.addEventListener('click', kick, true);
    document.addEventListener('keydown', kick, true);
  }

  /* ---------- 设置页「语言」卡片（自绘自管） ---------- */
  function paintLangSeg() {
    var ttl = document.querySelector('#langCard .card-title');
    if (ttl) ttl.textContent = lang === 'en' ? 'Language' : '语言';
    var seg = document.getElementById('langSeg');
    if (!seg) return;
    Array.prototype.forEach.call(seg.querySelectorAll('.seg-opt'), function (b) {
      var on = b.dataset.value === lang;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  function mountLangCard() {
    var page = document.querySelector('[data-page="settings"] .stack');
    if (!page || document.getElementById('langCard')) return;
    var card = document.createElement('div');
    card.className = 'card';
    card.id = 'langCard';
    card.innerHTML =
      '<div class="card-head"><h2 class="card-title">' + (lang === 'en' ? 'Language' : '语言') + '</h2></div>' +
      '<div class="card-body">' +
      '  <div class="seg" id="langSeg" role="radiogroup" aria-label="语言">' +
      '    <button class="seg-opt" type="button" role="radio" data-value="zh" data-no-i18n>中文</button>' +
      '    <button class="seg-opt" type="button" role="radio" data-value="en" data-no-i18n>English</button>' +
      '  </div>' +
      '</div>';
    page.insertBefore(card, page.firstChild);
    var seg = card.querySelector('#langSeg');
    seg.addEventListener('click', function (e) {
      var b = e.target.closest('.seg-opt');
      if (!b) return;
      var next = b.dataset.value === 'en' ? 'en' : 'zh';
      if (next === lang) return;
      setLang(next);
      var api = window.__EAI_API;
      if (api && api.base && api.post) { try { api.post('/api/settings/app', { lang: next }); } catch (err) {} }
    });
    paintLangSeg();
  }

  function boot() {
    window.__I18N_BOOT = 'start';
    mountLangCard();
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      var api = window.__EAI_API;
      if (!api || !api.get) {          // api.base 要先调一次 get 才会握手，不能拿它当就绪条件
        window.__I18N_BOOT = 'wait:' + tries;
        if (tries > 150) clearInterval(timer);
        return;
      }
      clearInterval(timer);
      var p = api.get('/api/settings/state');
      if (!p || typeof p.then !== 'function') { window.__I18N_BOOT = 'bad-promise'; return; }
      p.then(function (s) {
        var want = ((s || {}).app || {}).lang;
        window.__I18N_BOOT = 'ok:' + String(want);
        /* 本机记忆优先（用户 2026-10-20）：本地已经有选择就照本地的来，
           服务端值只在「这台机器第一次跑」（没有本地记忆）时用一次 ——
           否则刚在设置里切成中文、重启又被服务端的旧值改回英文。 */
        if (!hadLocal && (want === 'en' || want === 'zh')) setLang(want);
        mountLangCard();
      }).then(null, function (e) { window.__I18N_BOOT = 'err:' + String(e); });
    }, 400);
  }

  /* 2026-10-23：动态拼接文案的双语/单复数助手。
     翻译器是整串查词典，拼出来的句子查不到，所以这类串必须自己带两份。
     · window.bi(zh, en)          —— 英文界面取 en（各页原有的本地 bi() 仍在作用域内优先）
     · window.enPlural(n, 单, 复数) —— 英文单复数：1 用单数，其余用复数 */
  if (!window.bi) {
    window.bi = function (zh, en) { return lang === 'en' ? en : zh; };
  }
  if (!window.enPlural) {
    window.enPlural = function (n, one, many) { return Number(n) === 1 ? one : many; };
  }

  window.I18N = {
    t: function (zh) { return lang === 'en' && EN[zh] !== undefined ? EN[zh] : zh; },
    lang: function () { return lang; },
    setLang: setLang,
    on: on,
    apply: function () { if (lang === 'en') walk(document.body); },
    /* 切页/切页签后用：把刚显示出来的横条按当前语言重新落位（用户 2026-10-20） */
    reflowSegThumbs: reflowSegThumbs,
    mount: mountLangCard
  };

  /* 启动：DOM 一就绪就按本地语言翻一遍（同步、绘制前完成），再异步跟服务端校准。 */
  function startup() {
    try {
      if (lang === 'en') {
        document.documentElement.setAttribute('lang', 'en');
        observe();
        walk(document.body);
        paintLangSeg();
        reflowSegThumbs();
      } else {
        document.documentElement.setAttribute('lang', 'zh-CN');
      }
      hookReflow();
    } finally {
      /* 翻完就把首帧遮罩撤掉（用户 2026-10-20），此时画出来的第一眼就是英文 */
      document.documentElement.removeAttribute('data-lang-pending');
    }
    boot();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startup);
  else startup();
})();
