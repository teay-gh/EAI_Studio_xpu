# EAI 参数手册

> **AI 助手使用须知**：回答「参数是什么 / 什么意思 / 怎么定值 / 有什么用 / 在哪」时直接用本手册作答，**不要为此调用 get_config**。只有用户问「当前设成了什么 / 现在是多少」才读配置，且必须带具体 path（如 train.device）；不要不带 path 读顶层。本手册覆盖 EAI 全部参数：界面参数 + config.json 全部键。

## 一、全局机制（不是参数，但解释参数时常相关）

- **重复率守卫**（语料蒸馏 / 响应蒸馏 / 网页整理共用）：按段落统计 4-gram 字符相似度。重复率 ≥20% 弹窗「重复率警告」一次；≥40% 弹窗「重复率过高，已中止」并停止采集（已收集的保留）。阈值写死，不可配置。
- **换问法机制**：重复率 >20%、>25%、>30%、>35% 四档，自动在出题/规划提示词尾部追加「换一种问法」指令（逐级加重，>35% 为最重档），用户无感知；重复率回落后自动降档或消失。
- **保存并隔离**：勾选后，审稿 AI 拒收的样本写入 `<数据集名>_rejected.jsonl`（进回收站可查）；不勾选则拒收样本直接丢弃（discard_on_reject）。
- **Check 文件夹**：权重检查（check）产生的权重放进 `<基名>_Check<k>` 文件夹，训练方式标注沿用基座的历史。
- **回收站**：删除先入回收站（可恢复）；「彻底删除」永久删除不可恢复；「清空回收站」按当前页条目逐个彻底删除。
- **权重锁定**：被锁的文件/文件夹不能删除、批量删除不可勾选。
- **产物目录**：格式转换 → `<工程>\data\corpora`；语料蒸馏 → `<工程>\data\article`；响应蒸馏 → `<工程>\data\response`；网页整理 → `<工程>\data\webcurate`（元数据在 `<工程>\data\_web_curate.json`）。数据集页扫描 `<工程>\data` 下的目录。

## 二、设置页

| 界面项 | config 路径 | 含义 |
|---|---|---|
| 软件名 / 版本 | app.name / app.version | 软件显示名与版本号 |
| 主题 | app.theme | 界面主题：light（浅色）/ dark（深色） |
| 界面语言 | app.lang | zh（中文）/ en（英文） |
| 侧栏 | app.sidebar | 侧栏是否展开 |
| AI 面板 | app.ai_panel | AI 助手面板是否打开 |
| 导航折叠 | app.nav_collapsed | 收起的侧栏分组名列表 |
| 最近工程 | app.recent_projects | 最近打开过的工程路径列表 |
| 当前分组 | app.activity_group | 侧栏当前展开的分组 |
| 蒸馏数据集默认引入 | app.auto_distill_dirs | 开=蒸馏产物自动进「数据集」扫描 |
| 工程关闭标记 | app.project_closed | 上次退出是否正常关闭工程（异常退出会提示） |
| 默认 API | assistant.base_url / assistant.api_key / assistant.model | AI 助手后端（OpenAI 兼容）。api_key 绝不回显 |
| 强制托管训练完成 | （设置页开关，host_force_complete） | 开=托管 AI 只提示风险不中止训练；卡死照样停 |

## 三、AI 助手（assistant）

| 键 | 含义 |
|---|---|
| assistant.base_url | AI 后端接口地址（OpenAI 兼容；也自动探测 mimo CLI / MiMoCode） |
| assistant.api_key | 密钥（永不回显、永不进对话） |
| assistant.model | 模型名 |
| assistant.temperature | 生成温度（0~2，越高越发散） |
| assistant.max_tokens | 单次回复最大 token 数 |
| assistant.system_prompt | 用户自定义系统提示词（留空用内置） |

机制：改配置 / 换主题 / 切页面这类动作**先挂确认卡**，用户点「执行」才生效；get_config 是只读的可直接用。会话历史保留最近 16 条。

## 四、语料蒸馏（article）

三个 AI 角色，每个都有 `base_url / api_key / model / temperature / max_tokens / system_prompt` 六个键：

| 角色 | config 段 | 职责 |
|---|---|---|
| 出题 AI（titler） | article.titler | 按主题出题目/选题 |
| 写作 AI（writer） | article.writer | 按题写作正文 |
| 审稿 AI（reviewer） | article.reviewer | 审稿：通过 / 拒收（拒收理由会进弹窗回执） |

采集参数（article.collection）：

| 界面项 | 键 | 含义 |
|---|---|---|
| 主题 | themes | 主题清单，每行一个；留空=随机主题 |
| 采集条数 | count | 收满多少条停 |
| 并发数 | concurrency | 同时向 AI 发几个请求（1~8） |
| 每篇字数 | min_words / max_words | 单篇字数区间 |
| 拒收即弃 | discard_on_reject | 拒收样本直接丢弃 |
| 保存并隔离 | save_rejected | 拒收样本写 `<数据集名>_rejected.jsonl` |
| 输出目录 | output_dir | 产物目录（默认 data\article） |
| 数据集名 | dataset_name | 产物文件名 |

## 五、响应蒸馏（response）

三个 AI 角色（各六键同上）：出题 AI（generator，出问题）、回答 AI（responder，作答）、审稿 AI（reviewer，审答案）。

采集参数（response.collection）：

| 界面项 | 键 | 含义 |
|---|---|---|
| 主题 | topics | 主题清单，每行一个 |
| 采集条数 | count | 收满多少条停 |
| 并发数 | concurrency | 并发请求数 |
| 模式 | mode | 出题方式（主题出题 / 随机） |
| 随机主题 | auto_topic | 开=不填主题也能跑（自动生成） |
| 拒收即弃 / 保存并隔离 | discard_on_reject / save_rejected | 同语料蒸馏 |
| 输出目录 / 数据集名 | output_dir / dataset_name | 同语料蒸馏 |

## 六、网页整理（webcurate）

主面板：主题、搜索引擎网址、采集篇数、并发数、最大字数、输出文件名、输出目录（只读）。
「选择」→ 选择文件 / 选择文件夹（文件夹=逐个转换其中数据文件）。

| 界面项 | config 键 | 含义 |
|---|---|---|
| 主题 | topic | 要查什么（一句话或关键词） |
| 搜索引擎网址 | engine_url | 检索入口 URL（可换搜索引擎） |
| 采集篇数 | pieces | 收满多少篇停（1~500，默认 20；一篇=一个网页正文） |
| 并发数 | concurrency | 同时抓几个网页（1~8，默认 3） |
| 最大字数 | max_chars | 单篇正文上限（200~20000，默认 2000；段落边界截断、短段不丢） |
| 输出文件名 | out_name | 产物文件名 |
| 高级设置：每轮检索式 | queries_per_round | 每轮生成几条检索词（默认 4） |
| 高级设置：每条检索抓几篇 | pages_per_query | 每条检索词抓几个结果页（默认 3） |
| 高级设置：最多几轮 | max_rounds | 检索规划最多循环几轮（默认 3） |

产物：UTF-8 纯文本 .txt（LF），篇间空行分隔、篇内段落空行分隔；元数据（主题/引擎/字数/篇数/重复率/轮数/来源数/时间）在 `data\_web_curate.json`。
遗留键：webcurate.dup_threshold、webcurate.target_chars（已废弃，新逻辑按篇收口，勿再解释为生效参数）。

## 七、格式转换（formatconv）

| 界面项 | config 键 | 含义 |
|---|---|---|
| 源文件（选择） | src | 单个数据文件，或文件夹（文件夹里 .json/.jsonl/.ndjson/.txt 逐个转换，每个出一个产物） |
| 目标格式 | target | auto（自动识别，本地能读就不走模型）/ sft（问答 .jsonl）/ pretrain（纯文本 .txt） |
| 输出文件名 | out_name | 产物名；勾「使用源文件名」时忽略此项 |
| 使用源文件名 | use_src_name | 1=产物名=源文件名（.jsonl/.txt 后缀由结果定）；勾选时查重名（产物已存在或本批同名→弹窗警告并自动取消勾选）。不勾且名字空白会被拦：「请填输出文件名，或勾选「使用源文件名」」 |
| 最多转换条数 | max_records | 读源文件最多取多少条 |
| 每批条数 | batch | 交给模型转换时每批几条（只在走模型时生效） |

本地识别：JSON/JSONL 问答对、纯文本都直接读（模式=本地转）；认不出的才发给模型（走模型转）。

## 八、训练（train）

公共键：

| 键 | 含义 |
|---|---|
| train.device | 训练设备：cpu / cuda / xpu（本机 Intel Arc 核显=xpu） |
| train.method | 当前训练方式（pretrain / continue / sft / lora） |
| train.data_roots | 数据根目录列表（数据集扫描范围） |
| train.seed | 随机种子（固定它=可复现） |
| train.amp / train.amp_dtype | 混合精度开关 / 精度类型（bf16 优先；fp16 出 NaN 就换 bf16 或关） |
| train.grad_clip | 梯度裁剪：梯度范数超过它就缩回（防爆炸，常用 1.0） |
| train.grad_accum | 梯度累积步数：等效批量 = batch_size × grad_accum |
| train.val_ratio | 验证集比例（0~0.5；按它切出验证集算验证 Loss） |
| train.ckpt_count 系列 | 检查点数量：训练中途额外保存几份权重（设 1 在一半处存一次，设 2 在 1/3、2/3 处各存一次，k/(N+1) 等分点）。四个键分别管 pretrain/continue/sft/lora |
| train.persist、train.updates | **遗留键，当前版本无任何读取方**（保留兼容，勿解释为生效参数） |

### 8.1 预训练（从零）——模型结构

| 界面项（键） | 怎么定值（页面「点参数名看说明」原文） |
|---|---|
| 模型维度（d_model） | 隐藏层宽度：每个 token 表示成这么长的向量，各层矩阵的宽都由它决定。越大表达力越强，参数量按平方级涨。怎么定值：先按显存定档——8GB 用 128，12~16GB 用 256，24GB 以上 384~512。语料小（<10MB）先别超过 256。约束：d_model 必须等于 n_head × 每头维度，每头 32~64 维最稳（128 配 2~4 头，256 配 4~8 头）。 |
| 注意力头数（n_head） | 把 d_model 切成几份并行做注意力，每头维度 = d_model ÷ n_head。怎么定值：先定 d_model，再按每头 32~64 维反推（128→2~4 头，256→4~8 头，384→6~12 头）。必须是 d_model 的约数；每头低于 16 维基本学不到长程关系。 |
| 网络层数（n_layer） | Transformer 层数，决定深度。怎么定值：4 层起步；128 维 4~6 层，256 维 6~12 层。判断信号：训练 Loss 很快接近 0、验证 Loss 却升高 = 参数量相对语料过大，减层或加数据。 |
| 前馈宽度（d_ff） | 每层中间的升维宽度，通常 = 4 × d_model |
| 上下文长度（block_size） | 一条样本一次最多看多少 token，超出切开。怎么定值：取覆盖约 95% 样本的长度（常见 128/256/512）。显存约与 block_size × batch_size 成正比：先定 block_size，再用 batch_size 填显存。 |
| 批量大小（batch_size） | 每步同时喂多少条序列。怎么定值：8~16 起翻倍到显存接近满；等效批量 = batch_size × 梯度累积，稳定落在 32~128。显存不够先减它、再补梯度累积，Loss 曲线仍可对比。 |
| 丢弃率（dropout） | 随机置零激活防过拟合。怎么定值：小语料 0.1~0.2，大语料 0~0.1。验证 Loss 明显高于训练 Loss（差 20%+）就往 0.15→0.2 加；两边都高是欠拟合，应减小。 |
| 共享词表权重（tie_weights） | 输入词表与输出词表共享权重：省参数，小模型常用 |
| 步数（pretrain_steps） | 总优化步数，跑满自动保存。怎么定值：1 epoch = 语料 token 数 ÷（batch_size × block_size），一般跑 2~4 个 epoch 的步数。例：10MB 语料约 500 万 token，batch 32 × block 128 ≈ 4096 → 1 epoch ≈ 1200 步。Loss 进平台期就停。 |
| 学习率（pretrain_lr） | 每步更新幅度。怎么定值：从零预训练常用 1e-3~3e-3；Loss 抖动上翘或 NaN → 减半；曲线几乎水平 → 乘 1.5~2。配 warmup + 余弦衰减更稳。 |
| 预热步数（pretrain_warmup） | 前多少步学习率从 0 线性升到目标值（防开局震荡，一般 5%~10% 总步数） |
| 评估间隔（pretrain_eval_every） | 每多少步在验证集上算一次 Loss（越小越细但越慢） |
| 权重衰减（weight_decay） | L2 正则强度（常用 0.01~0.1，防过拟合） |
| 余弦衰减（cosine_lr） | 开=学习率按余弦曲线从峰值降到 0（更稳、终点更优） |
| 采样开关（sample）/ 采样提示词（prompt）/ 采样字数（sample_chars） | 训练中每隔一段用当前权重试生成一段文本看效果（prompt 是试生成的开头） |
| 混合精度 AMP | 用 bf16/fp16 前向反向、fp32 更新权重：省显存提速。怎么定值：XPU/CUDA 都建议开，优先 bf16；fp16 老卡出 NaN 就关掉或换 bf16。 |
| Multi-Token Prediction（mtp / mtp_k） | 一次预测后面 k 个 token：表征更充分、收敛更快。默认开、k=3；语料很小（<1MB）或对顺序极敏感可关。 |
| 预训练语料 / 输出 / 日志 | pretrain_corpus（语料路径）/ pretrain_out（输出权重）/ pretrain_log_csv（Loss 日志 CSV） |

### 8.2 继续预训练（continue_*）

| 界面项（键） | 怎么定值 |
|---|---|
| 初始权重（continue_init_from） | 续训起点，必须是存在的 .pt；选仓库最近预训练产物。换基座=换起点，步数和学习率要重看 Loss 定。 |
| 步数（continue_steps） | 越少越不容易冲掉原有能力。1 epoch = 新语料 token ÷（batch_size × block_size），一般 1~3 个 epoch；约为从零预训练的 1/10~1/5。 |
| 学习率（continue_lr） | 必须比从零预训练小：常用 2e-4~8e-4（1/5~1/10）。5e-4 起步：不降乘 2，振荡除 2。 |
| 覆盖初始权重（overwrite_init） | 开=输出权重可以直接盖在初始权重文件上（危险，确认再开） |
| 输出 / 日志 | continue_out / continue_log_csv（= pretrain_log_csv 同目录口径） |
| 批量大小 / 丢弃率 / AMP | 同预训练（怎么定值原文见预训练表） |

### 8.3 指令微调（sft_*）

| 界面项（键） | 怎么定值 |
|---|---|
| 初始权重（sft_init_from） | 优先用预训练/续训产物（eproai_pretrain.pt 等），从零做指令微调效果通常很差；留空=从零，仅调试用。 |
| 语料（sft_corpus） | 指令微调数据集（问答 jsonl） |
| 步数（sft_steps） | 别太多否则遗忘基座。1 epoch = 样本数 ÷ batch_size，一般 2~3 个 epoch；超 5 个 epoch 常开始照抄训练集。 |
| 学习率（sft_lr） | 2e-4~8e-4（预训练的 1/4~1/10），5e-4 起步。回答像样就保持；答非所问/重复→8e-4；Loss 抖动/格式崩→2e-4。数据少时偏大易过拟合。 |
| 批量大小（batch_size） | 指令数据通常几千条，batch 8~32 够；显存不够先减它再用梯度累积补到 32~64。 |
| 输出 / 日志 | sft_out / sft_log_csv |

### 8.4 LoRA（lora_*）

| 界面项（键） | 怎么定值 |
|---|---|
| 基座权重 | 要挂 LoRA 的基座 .pt，必填；换基座后旧适配器不能复用。 |
| LoRA 秩 r（lora_r） | 低秩矩阵的秩，参数量随 r 线性涨。r=8 起步；任务离基座越远越要加（数据 >5k 或不够→16、32）；只调口吻 r=4 够。 |
| 缩放 α（lora_alpha） | 增量按 α/r 缩放。常用 α=r（比例 1）或 2r（比例 2）：效果弱提比例，Loss 抖降比例。改 r 同步改 α。 |
| LoRA 丢弃率（lora_dropout） | 只作用在 LoRA 分支。0.05 常用；数据少/过拟合加到 0.1~0.15。 |
| 挂载目标层（lora_targets） | 挂到哪些线性层（逗号分隔）。q_proj,v_proj 最省最稳先用这两个；不够再加 k_proj,o_proj，最后才全挂。 |
| batch_size / 步数 / 学习率 | batch 8~32（LoRA 显存省）；2~3 epoch 起步；学习率可比全参大：1e-3~3e-3（默认 2e-3），没变化乘 2、崩坏除 2。 |
| 输出 / 日志 | lora_out / lora_log_csv |

### 8.5 LoRA 合并

把训练好的 LoRA 适配器与基座权重合并成一份完整 .pt（推理不再需要适配器）。参数=基座权重 + 适配器 + 输出名。

### 8.6 模型裁切 / 量化（train_compress 页）

| 界面项 | 怎么定值（页面说明原文） |
|---|---|
| 权重 | 要被裁切/量化的权重文件，必须指向真实存在的 .pt。选仓库里最新的预训练或续训产物。 |
| 裁切方法 | SparseGPT：逐层按权重重要性剪枝，慢一些但精度保持好；Wanda：按权重×激活幅度排序剪，快且几乎不用校准。先用 Wanda 快速看效果；精度掉得多再换 SparseGPT 对比。 |
| 量化方案 | W8A16：权重 8bit、激活 16bit，精度损失最小；W4A16：权重 4bit，省一半空间、精度略降；FP8_DYNAMIC：权重与激活都 FP8、动态缩放，需较新硬件。先跑 W8A16 走通流程，再降 W4A16 看困惑度。 |
| 稀疏度 | 剪掉的权重比例 0~1（0.5=剪一半）。0.3 起步，0.5 常用；掉点多退 0.3~0.4；超 0.7 通常明显掉点。 |
| 校准样本数 | 用多少条样本估计重要性/量化范围。64~128 足够，紧张就 32；量化后困惑度偏高且样本 <32 时加到 128 重测。 |
| 最大序列长 | 校准/量化时每条样本截多长。取常用推理长度（128/256/512），覆盖语料 95% 长度即可。 |
| 校准语料 | 校准用的语料：点「开始」时自动合并预训练数据集，不需要单独挑文件。 |

### 8.7 训练流水线（pipeline）

- pipeline.steps：流水线步骤列表（每步=一个训练任务：方式 + 权重命名 + 该方式全部参数）。
- 输出命名规则：各步输出名不能互相重复、不能为空（不合法会在开始前弹「命名检查」列出问题）。
- 上一步产物自动作为下一步初始权重（链条跑通：预训练→续训→微调→LoRA）。

### 8.8 对话验证（chat 页）

| 界面项 | 键 | 含义 |
|---|---|---|
| 生成长度 | train.sample_chars | 一次生成最多多少字（16~4096） |
| 上下文长度 | train.chat_max_len | 对话时喂给模型的最大上下文（超了截断旧消息） |
| 重复惩罚 | chat.repetition_penalty | >1 抑制复读（常用 1.05~1.2）；=1 不惩罚 |
| 采样提示词 | train.prompt | 采样/试生成的开头 |

## 九、其它页面

- **仓库**：权重/数据集文件管理。文件夹=虚拟分组（data\_repo_meta.json）；check 按钮自动建 `<基名>_Check` 文件夹；锁定后删除禁用；右键菜单=查看内容/恢复/彻底删除。
- **数据集**：扫描 `<工程>\data` 各子目录；文件表可删除（进回收站）；单条数据可预览全文。
- **模型转换**：不同格式权重互转（probe 报格式/张量/参数量）。
- **导出模型**：把权重拷到目标文件夹。参数=导出到（可编辑路径框）+ 筛选训练方式（下拉）+ 搜索权重名称；文件标注走「训练溯源→_Check<k> 沿用基座→文件名推断」真值链。
- **托管**：训练交给托管 AI 看管；「强制托管训练完成」开=只提示风险不中止（卡死照样停）。
- **回收站（数据集 / 权重 两页）**：恢复所选 / 彻底删除所选 / 清空回收站（都按页分开）。

## 十、config.json 全键速查（get_config 的 path 对照）

app.name、app.version、app.theme、app.sidebar、app.ai_panel、app.nav_collapsed、app.recent_projects、app.activity_group、app.auto_distill_dirs、app.project_closed、app.lang；
assistant.base_url、assistant.api_key、assistant.model、assistant.temperature、assistant.max_tokens、assistant.system_prompt；
article.titler|writer|reviewer 各 6 键（base_url/api_key/model/temperature/max_tokens/system_prompt）；
article.collection.themes、.count、.concurrency、.min_words、.max_words、.discard_on_reject、.save_rejected、.output_dir、.dataset_name；
response.generator|responder|reviewer 各 6 键；response.collection.topics、.count、.concurrency、.mode、.auto_topic、.discard_on_reject、.save_rejected、.output_dir、.dataset_name；
train.device、.method、.lora_r、.lora_alpha、.lora_dropout、.lora_targets、.data_roots、.seed、.d_model、.n_head、.n_layer、.d_ff、.block_size、.batch_size、.dropout、.tie_weights、.grad_clip、.grad_accum、.mtp、.mtp_k、.amp、.amp_dtype、.sample、.cosine_lr、.weight_decay、.eval_iters、.pretrain_corpus、.pretrain_steps、.pretrain_lr、.pretrain_warmup、.pretrain_eval_every、.pretrain_out、.pretrain_log_csv、.sft_corpus、.sft_steps、.sft_lr、.sft_warmup、.sft_eval_every、.sft_out、.sft_log_csv、.sft_init_from、.lora_out、.lora_log_csv、.prompt、.sample_chars、.continue_init_from、.continue_out、.overwrite_init、.chat_max_len、.persist（遗留）、.updates（遗留）、.continue_steps、.continue_lr、.val_ratio、.pretrain_ckpt_count、.continue_ckpt_count、.sft_ckpt_count、.lora_ckpt_count；
pipeline.steps；
formatconv.src、.target、.out_name、.use_src_name、.max_records、.batch；
webcurate.topic、.engine_url、.out_name、.queries_per_round、.pages_per_query、.max_rounds、.dup_threshold（废弃）、.target_chars（废弃）、.pieces、.concurrency、.max_chars；
chat.repetition_penalty。
