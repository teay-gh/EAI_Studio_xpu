"""bridge_api/train_pipeline.py —— 「训练流水线」页后端（用户 2026-10-17）。

按 config.json 里 `pipeline.steps` 的顺序，依次跑多步训练/压缩（预训练 → 继续预训练 → 指令微调
→ LoRA → 裁切 → 量化）。同一种训练方式可以添加多次，量化只允许一次。

**每一步自带全套参数**（用户 2026-10-17 二轮：「这里的训练和其他训练页是独立的，不要复用其他页的参数」）：
  - 步骤 = {"task": key, "name": 输出权重名, "params": {配置键: 值}}；
  - params 的键就是 config.train 里的键（预训练 pretrain_steps/pretrain_lr…、LoRA lora_r/lora_alpha…、
    裁切 method/sparsity…），缺的键一律用本文件 FIELDS 里的**引擎默认值**补齐，
    所以某一步的运行时参数绝不会从各训练页的 config 里继承；
  - 起训练走 `_host.start_training(..., persist=False)`、压缩走 `_compress.start(..., persist=False)`：
    只生成本次的 runtime/params，**不写回 config.json**，各训练页看到的仍是自己的参数。

跑在后台线程里（`_host.start_task`），页面轮询 `/status?since=` 拿增量事件；
停止 = 先给当前这一步发停止（训练写 stop.flag / 压缩写 stop.flag + 强杀），再终止后续步骤。

   GET  /api/train_pipeline/config   任务清单（含每类的参数字段表）+ 当前步骤
   POST /api/train_pipeline/save     {steps:[{task,name,params},...]}
   POST /api/train_pipeline/start    按配置顺序开跑
   POST /api/train_pipeline/stop     停当前步 + 终止后续
   GET  /api/train_pipeline/status[?since=N]
"""
from __future__ import annotations

import os
import threading
import time

import _host

TASK_NAME = "train:pipeline"
GROUP = "pipeline"

TASKS = [
    ("pretrain", "预训练"),
    ("continue", "继续预训练"),
    ("sft", "指令微调"),
    ("prune", "模型裁切"),
    ("quant", "量化"),
]
LABEL = dict(TASKS)
LABEL["lora"] = "LoRA 微调"        # 只给存量步骤显示/运行用；不再作为可添加项（用户 2026-10-20）
LABEL["import"] = "导入权重"
TRAIN = {"pretrain": ("pretrain", "full"), "continue": ("continue", "full"),
         "sft": ("sft", "full"), "lora": ("sft", "lora")}
COMPRESS = ("prune", "quant")
ONCE = ("quant",)                                        # 量化只能加一次（用户 2026-10-17）
OUT_KEY = {"pretrain": "pretrain_out", "continue": "continue_out",
           "sft": "sft_out", "lora": "lora_out"}
LOG_KEY = {"pretrain": "pretrain_log_csv", "continue": "continue_log_csv",
           "sft": "sft_log_csv", "lora": "lora_log_csv"}
# 2026-10-23：LoRA 与指令微调分家（用户要求）—— LoRA 步骤读写自己的 lora_steps / lora_init_from
STEPS_KEY = {"pretrain": "pretrain_steps", "continue": "continue_steps",
             "sft": "sft_steps", "lora": "lora_steps"}
INIT_KEY = {"continue": "continue_init_from", "sft": "sft_init_from", "lora": "lora_init_from"}

# 每类任务的参数字段：(配置键, 显示名, 类型, 默认值[, 选项])
# 默认值 = 引擎 runtime 的默认值（core/config.py / _host.runtime_cfg），**不取各训练页的当前值**。
# 2026-10-20：字段表按各训练页的「参数」卡对齐（用户：流水线的设置参数和单独页面不一致）——
#   标签、顺序、默认值都照 pages/train_{pretrain,continue,full,lora}.js 来；
#   warmup / 评估间隔 页面早就没暴露，这里也一并去掉（引擎仍用 runtime 默认值）。
F_BATCH = ("batch_size", "批量大小", "int", 32)
F_DROPOUT = ("dropout", "丢弃率", "float", 0.1)
F_CKPT = ("ckpt_count", "检查点数量", "int", 0)
F_AMP = ("amp", "混合精度 AMP", "bool", True)
F_MTP = ("mtp", "Multi-Token Prediction", "bool", False)
F_VAL = ("val_ratio", "验证集比例", "float", 0.1)
FIELDS = {
    "pretrain": [
        ("d_model", "模型维度", "int", 128),
        ("n_head", "注意力头数", "int", 4),
        ("n_layer", "网络层数", "int", 4),
        ("block_size", "上下文长度", "int", 128),
        F_BATCH, F_DROPOUT,
        ("pretrain_steps", "步数", "int", 30000),
        ("pretrain_lr", "学习率", "float", 0.002),
        F_CKPT, F_AMP, F_MTP,
    ],
    "continue": [
        F_BATCH, F_DROPOUT,
        ("continue_steps", "步数", "int", 8000),
        ("continue_lr", "学习率", "float", 0.0005),
        F_CKPT, F_AMP,
    ],
    "sft": [
        F_BATCH, F_DROPOUT,
        ("sft_steps", "步数", "int", 8000),
        ("sft_lr", "学习率", "float", 0.0005),
        F_VAL, F_CKPT, F_AMP,
    ],
    "lora": [
        ("lora_r", "LoRA 秩 r", "int", 8),
        ("lora_alpha", "缩放 α", "float", 8),
        ("lora_dropout", "LoRA 丢弃率", "float", 0.05),
        ("lora_targets", "挂载目标层", "chips", "qkv",
         ["qkv", "proj", "fc_in", "fc_out"]),
        F_BATCH,
        ("lora_steps", "步数", "int", 1200),
        ("lora_lr", "学习率", "float", 0.0002),
        F_VAL, F_CKPT,
    ],
    "prune": [
        ("method", "裁切方法", "select", "sparsegpt", ["sparsegpt", "wanda"]),
        ("sparsity", "稀疏度", "float", 0.5),
        ("calib_samples", "校准样本数", "int", 64),
        ("seq_len", "最大序列长", "int", 128),
    ],
    "quant": [
        ("scheme", "量化方案", "select", "W8A16", ["W8A16", "W4A16", "FP8_DYNAMIC"]),
        ("calib_samples", "校准样本数", "int", 64),
        ("seq_len", "最大序列长", "int", 128),
    ],
}

_STOP = threading.Event()


# ---------------------------------------------------------------- 参数字段
def _field_specs(key: str) -> list:
    out = []
    for f in FIELDS.get(key) or []:
        item = {"key": f[0], "label": f[1], "type": f[2], "default": f[3]}
        if len(f) > 4:
            item["options"] = list(f[4])
        out.append(item)
    return out


def _defaults(key: str) -> dict:
    return {f[0]: f[3] for f in FIELDS.get(key) or []}


def _coerce(key: str, ftype: str, raw):
    """按字段类型转值；空值/占位/无效 → None（调用方回落到默认值）。"""
    if raw is None:
        return None
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("", "—", "none", "null"):
            return None
        return s in ("1", "true", "yes", "on", "是", "勾选")
    s = str(raw).strip()
    if s in ("", "—", "none", "null", "None"):
        return None
    try:
        if ftype == "int":
            return int(float(s))
        if ftype == "float":
            return float(s)
    except (TypeError, ValueError):
        return None
    return s


def _clean_params(key: str, raw) -> dict:
    """只留本类任务的字段，值按类型转；无效值丢掉（后面用默认值补）。"""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for f in FIELDS.get(key) or []:
        if f[0] not in raw:
            continue
        val = _coerce(key, f[2], raw.get(f[0]))
        if val is not None:
            out[f[0]] = val
    return out


def _fill_params(key: str, params: dict) -> dict:
    """补齐默认值：这是「不继承各训练页参数」的保证。"""
    merged = _defaults(key)
    merged.update(_clean_params(key, params))
    return merged


# ---------------------------------------------------------------- 步骤
def _norm(raw) -> list:
    """规范化步骤：dict 或旧式字符串都收；量化去重；参数只留合法字段。

    2026-10-20（用户）：第一步可以是「导入权重」—— task=import，params 只留 weight，
    让流水线能从已有权重接着训，不必从 0 开始。
    """
    out, seen = [], set()
    for it in raw or []:
        if isinstance(it, dict):
            task = str(it.get("task") or "").strip()
            name = str(it.get("name") or "").strip()
            if task == "import":
                w = str((it.get("params") or {}).get("weight") or "").strip()
                if not w:
                    continue
                out.append({"task": "import", "name": "", "params": {"weight": w}})
                continue
            params = _clean_params(task, it.get("params"))
            # 2026-10-23：LoRA 与指令微调分家前，存的步骤参数叫 sft_steps/sft_lr —— 读时迁到 lora_*，
            # 免得老流水线一升级就悄悄退回默认值
            if task == "lora":
                _rp = it.get("params") or {}
                for _old, _new, _ty in (("sft_steps", "lora_steps", "int"), ("sft_lr", "lora_lr", "float")):
                    if _new not in params and _rp.get(_old) is not None:
                        _v = _coerce(task, _ty, _rp.get(_old))
                        if _v is not None:
                            params[_new] = _v
            legacy_steps = it.get("steps")
        else:
            task, name, params, legacy_steps = str(it).strip(), "", {}, None
        if task not in LABEL:
            continue
        if task in ONCE:
            if task in seen:
                continue
            seen.add(task)
        if legacy_steps not in (None, "", "—"):           # 旧格式 {steps:n} → 写进本类的步数字段
            val = _coerce(task, "int", legacy_steps)
            if val is not None:
                params[STEPS_KEY[task]] = val
        out.append({"task": task, "name": name, "params": params})
    return out


def _steps(cfg=None) -> list:
    cfg = cfg if cfg is not None else _host.load_cfg()
    return _norm((cfg.get("pipeline") or {}).get("steps") or [])


def config(_query: dict) -> dict:
    cfg = _host.load_cfg()
    return {
        "ok": True,
        "tasks": [{"key": k, "label": lb, "once": k in ONCE,
                   "fields": _field_specs(k), "defaults": _defaults(k)} for k, lb in TASKS],
        # 已移出可选项的训练方式（LoRA，用户 2026-10-20）：只给存量步骤显示/编辑/运行
        "legacy_tasks": [{"key": k, "label": lb, "once": k in ONCE,
                          "fields": _field_specs(k), "defaults": _defaults(k)}
                         for k, lb in LABEL.items()
                         if k not in dict(TASKS) and k != "import"],
        "steps": _steps(cfg),
    }


def save(body: dict) -> dict:
    steps = _norm((body or {}).get("steps") or [])
    cfg = _host.load_cfg()
    cfg.setdefault("pipeline", {})["steps"] = steps
    # 2026-10-18：步骤属于当前工程，顺手记进该工程的快照，切换工程时才不会串到下一个工程
    try:
        root = _host.project_root(cfg)
        if root:
            _host.snapshot_collect(cfg, root)
    except Exception:
        pass
    _host.save_cfg(cfg)
    return {"ok": True, "steps": steps}


# ---------------------------------------------------------------- 起停
def _step_out(step: dict) -> str:
    """这一步产出的权重绝对路径（用户 2026-10-20：下一步直接吃它，不再让用户选权重）。

    有名字 → <工程>/checkpoints/<名字>.pt；没命名 → 沿用该页配置的输出路径。
    """
    key = str(step.get("task") or "")
    name = str(step.get("name") or "").strip()
    root = _host.project_root()
    if name:
        safe = name if name.lower().endswith(".pt") else name + ".pt"
        return os.path.join(root, "checkpoints", safe)
    try:
        cfg = _host.load_cfg() or {}
    except Exception:                                    # noqa: BLE001
        cfg = {}
    out = str(((cfg.get("train") or {}).get(OUT_KEY.get(key, "")) or "")).strip()
    if out and not os.path.isabs(out):
        out = os.path.join(root, out)
    return out


def _launch(step: dict, prev_out: str = "") -> dict:
    """起一步：参数全部来自本步 params（缺的键用引擎默认值补），不读写各训练页的配置。

    prev_out：上一步产出的权重 —— 继续预训练 / 指令微调 / LoRA / 裁切 / 量化的输入一律用它，
    所以弹窗里不再出现「初始权重 / 基座权重 / 输入权重」这些选择项（用户 2026-10-20）。
    """
    key = step["task"]
    name = str(step.get("name") or "").strip()
    params = _fill_params(key, step.get("params") or {})
    root = _host.project_root()
    safe = (name if name.lower().endswith(".pt") else name + ".pt") if name else ""

    if key in TRAIN:
        stage, method = TRAIN[key]
        upd = dict(params)
        if safe:
            upd[OUT_KEY[key]] = os.path.join(root, "checkpoints", safe)
            # loss 必须写进该训练页的公共 CSV（用户 2026-10-20：「开新训练 loss 不更新」）：
            # 以前这里按权重名写 logs/<name>_loss.csv，训练页 Loss 表读的是 sft_loss.csv 这类
            # 页配置路径，永远看不到流水线这一步的曲线。去掉覆盖后走 config 的 *_log_csv；
            # 每个权重自己的曲线仍由引擎 loss_snapshot 落成 logs/<name>_loss.csv（含检查点快照）。
        init_key = INIT_KEY.get(key)
        if init_key and prev_out and os.path.isfile(prev_out):
            upd[init_key] = prev_out          # 上一步的权重就是这一步的初始权重
        return _host.start_training(stage, method, upd, persist=False) or {}

    body = dict(params)
    if safe:
        body["out"] = os.path.join(root, "checkpoints", safe)
    if prev_out and os.path.isfile(prev_out):
        body["weight"] = prev_out             # 裁切 / 量化默认吃上一步的权重
    # 压缩页的 weight 走 _resolve_weight（裸名按 <工程>/checkpoints 解析），这里先把带目录的名字
    # 接成绝对路径，避免出现 checkpoints/checkpoints/xxx.pt
    w = str(body.get("weight") or "").strip()
    if w and not os.path.isabs(w):
        body["weight"] = os.path.join(root, w.replace("/", os.sep))
    import _compress                                     # 同目录插件模块
    return _compress.start(key, body, persist=False) or {}


def _wait(key: str, res: dict, task) -> str:
    """等这一步真跑完：返回 done / failed / stopped。"""
    if key in TRAIN:
        name = str(res.get("job") or "")
        while True:
            if _STOP.is_set():
                return "stopped"
            j = _host.job(name) if name else None
            if j is None or not j.running:
                code = getattr(j, "exit_code", None)
                return "done" if code in (0, None) else "failed"
            time.sleep(1.0)
    while True:                                          # 压缩：组里没有作业 = 结束
        if _STOP.is_set():
            return "stopped"
        if not _host.running_in_group("compress"):
            return "done"
        time.sleep(1.0)


def _title(step: dict) -> str:
    lb = LABEL.get(step["task"], step["task"])
    if step.get("task") == "import":
        w = os.path.basename(str((step.get("params") or {}).get("weight") or ""))
        return "%s（%s）" % (lb, w) if w else lb
    nm = str(step.get("name") or "").strip()
    return "%s（%s）" % (lb, nm) if nm else lb


def _run(emit, task, steps: list) -> None:
    total = len(steps)
    task.set_stats(total=total, current=0, step="", state="running")
    prev_out = ""                                     # 上一步产出的权重（用户 2026-10-20：自动串）
    for i, step in enumerate(steps):
        if _STOP.is_set():
            emit("error", {"text": "[流水线] 已停止（第 %d/%d 步开始前）。" % (i + 1, total)})
            task.set_stats(state="stopped")
            return
        task.set_stats(current=i + 1, step=step["task"], state="running")
        emit("step", {"text": "[流水线] 第 %d/%d 步：%s" % (i + 1, total, _title(step))})
        if step.get("task") == "import":                 # 导入权重（用户 2026-10-20）：不从 0 开始
            w = str((step.get("params") or {}).get("weight") or "").strip()
            if w and not os.path.isabs(w):
                w = os.path.join(_host.project_root(), w.replace("/", os.sep))
            if not w or not os.path.isfile(w):
                emit("error", {"text": "[流水线] 导入权重不存在：%s" % (w or "（未选择）")})
                task.set_stats(state="failed")
                return
            prev_out = w
            emit("step", {"text": "[流水线] 已导入权重：%s" % os.path.basename(w)})
            continue
        if prev_out and step.get("task") in ("continue", "sft", "lora", "prune", "quant"):
            emit("step", {"text": "[流水线] 输入权重取上一步：%s" % os.path.basename(prev_out)})
        elif prev_out and step.get("task") == "pretrain":
            emit("step", {"text": "[流水线] 提示：预训练从 0 开始，导入的权重这一步不用（想让它在已有权重上继续，用「继续预训练」）。"})
        try:
            res = _launch(step, prev_out)
        except Exception as exc:                          # noqa: BLE001
            emit("error", {"text": "[流水线] %s 启动失败：%s: %s" % (_title(step), type(exc).__name__, exc)})
            task.set_stats(state="failed")
            return
        if not res.get("ok"):
            emit("error", {"text": "[流水线] %s 启动失败：%s" % (_title(step), res.get("error") or "未知错误")})
            task.set_stats(state="failed")
            return
        if res.get("job"):
            emit("step", {"text": "[流水线] %s 已启动（%s）" % (_title(step), res.get("job"))})
        outcome = _wait(step["task"], res, task)
        if outcome == "stopped":
            emit("error", {"text": "[流水线] 已停止（%s 中途终止）。" % _title(step)})
            task.set_stats(state="stopped")
            return
        if outcome == "failed":
            emit("error", {"text": "[流水线] %s 失败退出，流水线终止。" % _title(step)})
            task.set_stats(state="failed")
            return
        prev_out = _step_out(step) or prev_out      # 这一步的产物 = 下一步的输入
        emit("step", {"text": "[流水线] %s 完成。" % _title(step)})
    task.set_stats(state="done", current=total)
    emit("step", {"text": "[流水线] 全部 %d 步完成。" % total})


def _name_problems(steps: list) -> list:
    """起跑前命名审查（用户 2026-10-20：「点击开始训练也要审查一遍命名」）：
    未命名 / 步骤间同名 / 与仓库已有权重同名，任一存在都不放行。"""
    root = _host.project_root()
    seen: dict = {}
    probs: list = []
    for i, s in enumerate(steps):
        if str(s.get("task") or "") == "import":
            continue
        tag = "第 %d 步（%s）" % (i + 1, LABEL.get(str(s.get("task") or ""), s.get("task")))
        name = str(s.get("name") or "").strip()
        if name.lower().endswith(".pt"):
            name = name[:-3]
        if not name:
            probs.append(tag + "：还没命名")
            continue
        low = name.lower()
        if low in seen:
            probs.append(tag + "：与第 %d 步同名「%s」" % (seen[low], name))
            continue
        seen[low] = i + 1
        try:
            if os.path.isfile(os.path.join(root, "checkpoints", name + ".pt")):
                probs.append(tag + "：与仓库权重「%s.pt」同名" % name)
        except (OSError, ValueError):
            pass
    return probs


def start(_body: dict) -> dict:
    cur = _host.task(TASK_NAME)
    if cur is not None and cur.running:
        return {"ok": False, "error": "流水线已在运行，请先停止。"}
    steps = _steps()
    if not any(str(s.get("task") or "") != "import" for s in steps):
        return {"ok": False, "error": "流水线还没有训练步骤：请先添加。"}
    probs = _name_problems(steps)
    if probs:
        return {"ok": False, "error": "命名检查未通过（请进步骤里改名）：" + "；".join(probs)}
    _STOP.clear()
    try:
        _host.start_task(TASK_NAME, lambda emit, task, group: _run(emit, task, steps),
                         stop=_stop, group=GROUP)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "steps": steps}


def _stop() -> None:
    _STOP.set()
    try:
        if _host.running_in_group("training"):
            _host.stop_training()
    except Exception:                                     # noqa: BLE001
        pass
    try:
        if _host.running_in_group("compress"):
            import _compress
            for mode in COMPRESS:
                _compress.stop(mode)
    except Exception:                                     # noqa: BLE001
        pass


def stop(_body: dict) -> dict:
    return _host.stop_task(TASK_NAME)


# ---------------------------------------------------------------- 状态
def status(query: dict) -> dict:
    try:
        since = int(float(str(query.get("since") or "0") or "0"))
    except (TypeError, ValueError):
        since = 0
    st = _host.task_status(TASK_NAME, since)
    st.update({
        "step_labels": LABEL,
        "steps": _steps(),
        "busy_training": _host.running_in_group("training"),
        "busy_compress": _host.running_in_group("compress"),
    })
    return st


def register(router) -> None:
    router.get("/api/train_pipeline/config", config)
    router.get("/api/train_pipeline/status", status)
    router.post("/api/train_pipeline/save", save)
    router.post("/api/train_pipeline/start", start)
    router.post("/api/train_pipeline/stop", stop)
