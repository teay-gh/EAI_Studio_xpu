"""bridge_api/_compress.py —— 模型裁切 / 量化 共享作业层（train_prune.py / train_quant.py 共用）。

基于 llm-compressor（PyPI 项目名 `llmcompressor`）：
  - 独立压缩环境 `<repo>/.venv_compress`（Python 3.11 + llmcompressor + CPU torch）。
    **绝不能装进主 venv**：主 venv 的 torch 2.14.1+xpu 是 Arc 核显训练的命根子，
    llmcompressor 会连带装 CPU torch 顶掉它（2026-10-15 险情，已隔离）。
  - 真子进程跑 `engine/tools/compress_run.py`，作业名 compress:prune / compress:quant，
    全局互斥（group="compress"，裁切与量化不并发）。
  - 权重口径（与训练页一致）：只认仓库权重（工程 checkpoints/*.pt，外部权重先进仓库）。
  - 校准语料口径（用户 2026-10-23）：页面上可选「预训练（文本）」和/或「指令微调（对话）」，
    点「开始」时自动合并对应数据集，样本数在选中的来源之间均分 —— 两个都选就是「文本 + 对话」。
  - 输出默认落工程 checkpoints/（自动进仓库），.pt 格式与训练产物同构（含 config + 词表），
    引擎 / 推理 / 再训练照常可加载。

接口（train_prune.py / train_quant.py 薄封装到 /api/train_prune|train_quant/...）：
  GET  /config   参数（真·读写 logs/compress_params_<mode>.json）+ 宿主/环境状态
  GET  /status   压缩子进程状态 + 增量日志（?since=<游标>）
  POST /save     保存参数
  POST /start    合并语料 → 组 runtime → 起压缩子进程
  POST /stop     停止（写 stop.flag + 强杀压缩子进程）

铁律：只返回真实读取/执行到的数据；失败 {"ok": false, "error": "..."}，绝不编造。
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Optional

API_DIR = os.path.dirname(os.path.abspath(__file__))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
import _host  # noqa: E402  （下划线开头，插件加载器会跳过它）

EAI_ROOT = os.path.dirname(os.path.dirname(API_DIR))          # <项目目录>
COMPRESS_PY = os.path.join(EAI_ROOT, ".venv_compress", "Scripts", "python.exe")
RUNNER = os.path.join(_host.IDE_DIR, "tools", "compress_run.py")
GROUP = "compress"

JOB_NAME = {"prune": "compress:prune", "quant": "compress:quant"}

# 页面表单字段（['显示名', '配置键', '类型']）——两个页面共用一套存取
PRUNE_FIELDS = (
    ("权重", "weight", "text"),
    ("裁切方法", "method", "text"),          # sparsegpt | wanda
    ("稀疏度", "sparsity", "float"),
    ("校准样本数", "calib_samples", "int"),
    ("最大序列长", "seq_len", "int"),
    ("校准语料", "calib_source", "text"),       # pretrain / sft / pretrain,sft（用户 2026-10-23）
    ("输出权重", "out", "text"),
)
QUANT_FIELDS = (
    ("权重", "weight", "text"),
    ("量化方案", "scheme", "text"),          # W8A16 | W4A16 | FP8_DYNAMIC
    ("校准样本数", "calib_samples", "int"),
    ("最大序列长", "seq_len", "int"),
    ("校准语料", "calib_source", "text"),       # 同 prune：预训练 / 指令微调 可多选
    ("输出权重", "out", "text"),
)
FIELDS = {"prune": PRUNE_FIELDS, "quant": QUANT_FIELDS}

# 校准语料默认「文本 + 对话」（用户 2026-10-23）：预训练数据集出一半样本、指令微调数据集出另一半。
DEFAULTS = {
    "prune": {"method": "sparsegpt", "sparsity": 0.5, "calib_samples": 64, "seq_len": 128,
              "calib_source": "pretrain,sft"},
    "quant": {"scheme": "W8A16", "calib_samples": 64, "seq_len": 128,
              "calib_source": "pretrain,sft"},
}

# 校准语料来源（用户 2026-10-23）：预训练=文本 / 指令微调=对话，可多选，合成「文本+对话」
CALIB_KINDS = ("pretrain", "sft")
CALIB_LABEL = {"pretrain": "预训练（文本）", "sft": "指令微调（对话）"}


def calib_kinds(value: Any) -> list:
    """把表单里的 calib_source 归一成 ['pretrain', 'sft'] 的子集；空/非法 → 两种都要。"""
    raw = str(value or "").replace("，", ",").replace("/", ",").replace("+", ",")
    picked = []
    for part in raw.split(","):
        k = part.strip().lower()
        if not k:
            continue
        if k in ("sft", "lora", "finetune", "微调", "指令微调"):
            k = "sft"
        elif k in ("pretrain", "预训练", "文本", "text"):
            k = "pretrain"
        else:
            continue
        if k not in picked:
            picked.append(k)
    return picked or list(CALIB_KINDS)


# ---------------------------------------------------------------- 参数存取

def _clean(body: dict) -> dict:
    """剔除 _ 前缀键（含 bridge 注入的 _query），只留本页参数字段。"""
    keep = {f[1] for f in FIELDS["prune"]} | {f[1] for f in FIELDS["quant"]} | {"overwrite_init"}
    return {k: v for k, v in (body or {}).items() if k in keep and not str(k).startswith("_")}


def _params_path(mode: str) -> str:
    proj = _host.project(_host.load_cfg())
    return os.path.join(proj.logs_dir, f"compress_params_{mode}.json")


def load_params(mode: str) -> dict:
    """读保存的参数；缺失/损坏返回 {}。"""
    try:
        with open(_params_path(mode), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_params(mode: str, body: dict, persist: bool = True) -> dict:
    """保存参数（类型转换，占位「—」不落库——三层护栏第一层）。

    persist=False（用户 2026-10-17）：参数只用于本次压缩（训练流水线的步骤自带全套参数，
    既不读也不写 compress_params_<mode>.json，与裁切/量化页互不影响）。
    """
    clean = _clean(body)
    cur = dict(DEFAULTS[mode]) if not persist else load_params(mode)
    for f in FIELDS[mode]:
        key, typ = f[1], f[2]
        if key not in clean:
            continue
        raw = clean[key]
        if raw is None or str(raw).strip() in ("", "—", "—"):
            continue
        try:
            if typ == "int":
                cur[key] = int(float(str(raw)))
            elif typ == "float":
                cur[key] = float(str(raw))
            else:
                cur[key] = str(raw).strip()
        except (TypeError, ValueError):
            return {"ok": False, "error": f"参数 {f[0]} 取值无效：{raw!r}"}
    if "overwrite_init" in clean:   # 覆盖初始权重（用户 2026-10-16）：布尔开关单独落库
        cur["overwrite_init"] = bool(clean["overwrite_init"])
    if not persist:
        return {"ok": True, "action": "save", "saved": cur, "path": ""}
    path = _params_path(mode)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    return {"ok": True, "action": "save", "saved": cur, "path": path}


def _effective_params(mode: str, body: Optional[dict] = None) -> dict:
    """最终参数 = 默认值 < 已保存 < 本次表单。"""
    merged = dict(DEFAULTS[mode])
    merged.update(load_params(mode))
    if body:
        merged.update(_clean(body))
    return merged


# ---------------------------------------------------------------- 环境 / 路径

def env_status() -> dict:
    """压缩环境是否就绪（只看文件系统，不起解释器——快）。"""
    py = COMPRESS_PY
    llmc = os.path.join(EAI_ROOT, ".venv_compress", "Lib", "site-packages", "llmcompressor")
    ok = os.path.isfile(py) and os.path.isdir(llmc)
    return {
        "ok": ok,
        "python": py,
        "python_exists": os.path.isfile(py),
        "llmcompressor": llmc if os.path.isdir(llmc) else "",
        "note": "" if ok else "压缩环境未就绪：缺 .venv_compress/llmcompressor（需重装独立环境）",
    }


def _resolve_weight(raw: str, proj) -> str:
    """权重路径：相对 → 工程 checkpoints/；必须是存在的 .pt（仓库口径）。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    path = raw if os.path.isabs(raw) else os.path.join(proj.root, "checkpoints", raw.replace("/", os.sep))
    return path


def _auto_out(mode: str, params: dict) -> str:
    """默认输出名：源权重名 + 方法标签 + .pt，落工程 checkpoints/（自动进仓库）。"""
    weight = _resolve_weight(params.get("weight", ""), _host.project(_host.load_cfg()))
    stem = os.path.splitext(os.path.basename(weight or ""))[0] or "eproai"
    if mode == "prune":
        sp = params.get("sparsity")
        try:
            tag = f"{str(params.get('method') or 'sparsegpt').lower()}{int(round(float(sp or 0) * 100)):02d}"
        except (TypeError, ValueError):
            tag = str(params.get("method") or "sparsegpt").lower()
    else:
        tag = str(params.get("scheme") or "W8A16").lower()
    proj = _host.project(_host.load_cfg())
    base = os.path.join(proj.root, "checkpoints", f"{stem}_{tag}.pt")
    out = base
    n = 2
    while os.path.exists(out):                      # 不覆盖既有权重，顺位 _2/_3
        out = os.path.join(proj.root, "checkpoints", f"{stem}_{tag}_{n}.pt")
        n += 1
    return out


def _probe(path: str) -> dict:
    path = path or ""
    try:
        ok = bool(path) and os.path.isfile(path)
        return {"path": path, "exists": ok,
                "size_bytes": os.path.getsize(path) if ok else 0,
                "mtime": os.path.getmtime(path) if ok else None}
    except OSError:
        return {"path": path, "exists": False, "size_bytes": 0, "mtime": None}


# ---------------------------------------------------------------- 启动 / 状态 / 停止

def start(mode: str, body: Optional[dict] = None, emit: Optional[Callable[[str, dict], None]] = None,
          persist: bool = True) -> dict:
    """合并校准语料 → 组 runtime → 起压缩子进程（真·llm-compressor）。"""
    job_name = JOB_NAME[mode]
    busy = _host.running_in_group(GROUP)
    if busy:
        return {"ok": False, "error": f"已有压缩任务在运行（{busy}），请先停止。"}
    env = env_status()
    if not env["ok"]:
        return {"ok": False, "error": env["note"]}
    if not os.path.isfile(RUNNER):
        return {"ok": False, "error": f"压缩 runner 不存在：{RUNNER}"}

    saved = save_params(mode, body or {}, persist=persist)   # 参数先落库（persist=False 时只在本步生效）
    if not saved.get("ok"):
        return saved
    params = saved["saved"]

    proj = _host.project(_host.load_cfg())
    weight = _resolve_weight(params.get("weight", ""), proj)
    if not weight:
        return {"ok": False, "error": "请先点击「权重」行选择仓库权重。"}
    if not os.path.isfile(weight):
        return {"ok": False, "error": f"权重不存在：{weight}"}

    scheme = str(params.get("scheme") or "W8A16").upper()
    need_calib = mode == "prune" or scheme in ("W8A16", "W4A16")
    corpus = ""
    calib_list = []                  # [{'kind': 'pretrain'|'sft', 'path': ..., 'bytes': n}]
    merge_summary = ""
    if need_calib:
        # 校准语料（用户 2026-10-23）：按页面勾选合并「预训练」和/或「指令微调」数据集，
        # 两种都要 = 文本 + 对话；样本数在 runner 里按来源均分。
        kinds = calib_kinds(params.get("calib_source"))
        errs = []
        for k in kinds:
            try:
                import _corpus
                mres = _corpus.merge(k, proj.root, emit=lambda _a, _b: None)
            except Exception as exc:  # noqa: BLE001
                errs.append("%s：%s: %s" % (CALIB_LABEL[k], type(exc).__name__, exc))
                continue
            if not mres.get("ok"):
                errs.append("%s：%s" % (CALIB_LABEL[k], str(mres.get("error") or "未知错误")))
                continue
            path = str(mres.get("out") or "")
            if not path or not os.path.isfile(path):
                errs.append("%s：合并产物不存在" % CALIB_LABEL[k])
                continue
            calib_list.append({"kind": k, "path": path, "bytes": int(mres.get("out_bytes") or 0),
                               "reused": not mres.get("replaced", True)})
        if not calib_list:
            return {"ok": False,
                    "error": "校准语料合并失败：" + "；".join(errs)
                             + "（请先在「数据集」页给对应训练方式勾选数据集）"}
        corpus = calib_list[0]["path"]
        merge_summary = "[宿主] 校准语料（" + " + ".join(
            "%s %s %d 字节%s" % (CALIB_LABEL[c["kind"]], os.path.basename(c["path"]), c["bytes"],
                                 "（没勾数据集，沿用上次合并产物）" if c.get("reused") else "")
            for c in calib_list) + "）"
        if errs:                     # 选了两种但其中一种没勾数据集：照跑，但把原因说清楚
            merge_summary += "；未用上：" + "；".join(errs)

    out = _resolve_weight(params.get("out", ""), proj) if params.get("out") else _auto_out(mode, params)

    # 覆盖初始权重（用户 2026-10-16）：完成压缩才覆盖——产物先写临时名 __ovw__<名>，完成后才顶源权重名
    # （新模型名称与旧模型一致），旧权重更名「初始_」前缀入回收站。
    ovw_flag = params.get("overwrite_init")
    ovw_on = bool(ovw_flag) and str(ovw_flag).lower() not in ("0", "false", "") and bool(weight)
    # 锁（用户 2026-10-20）：被锁的权重不许被覆盖
    if ovw_on:
        try:
            import repo as repo_api
            if repo_api.is_locked_path(proj.root, weight):
                return {"ok": False, "error": "源权重「%s」已锁定，不能覆盖。请先解锁，或取消「覆盖初始权重」。"
                        % os.path.basename(weight)}
        except Exception:                        # noqa: BLE001
            pass
    if ovw_on:
        out = os.path.join(os.path.dirname(weight), "__ovw__" + os.path.basename(weight))
        if os.path.isfile(out):
            try:
                os.remove(out)                      # 清掉上次失败残留的临时产物
            except OSError:
                pass
    elif os.path.abspath(out) == os.path.abspath(weight):
        return {"ok": False, "error": "输出权重与源权重同名：请改权重命名，或勾选「覆盖初始权重」。"}
    os.makedirs(proj.logs_dir, exist_ok=True)
    flag = os.path.join(proj.logs_dir, "stop.flag")
    if os.path.exists(flag):
        try:
            os.remove(flag)
        except OSError:
            pass

    rt = dict(params)
    rt.update({
        "mode": mode,
        "weight": weight,
        "out": out,
        "corpus": corpus,
        "corpora": calib_list,              # 多来源校准语料（runner 按来源均分样本）
        "stop_flag": flag,
        "started_at": time.time(),
    })
    rt_path = os.path.join(proj.logs_dir, f"runtime_compress_{mode}.json")
    with open(rt_path, "w", encoding="utf-8") as f:
        json.dump(rt, f, ensure_ascii=False, indent=2)

    argv = [COMPRESS_PY, "-u", RUNNER, "--config", rt_path]
    job_obj = _host._spawn(job_name, argv, _host.IDE_DIR, group=GROUP)
    # 训练溯源（用户 2026-10-16）：产物真写盘 → 记入 _weight_hist.json（模型裁切/量化也算经历）
    hist_label = _host.WEIGHT_HIST_LABELS.get(str(mode), "其他")
    hist_started = time.time()
    hist_root = proj.root

    def _on_exit() -> None:
        cand = out
        if ovw_on:
            # 覆盖初始权重（用户 2026-10-16）：完成（退出码 0 / 优雅停止）才覆盖
            done = (job_obj.exit_code == 0) or bool(job_obj.stop_requested)
            if done:
                try:
                    if _host._ovw_swap(proj.root, weight, out, job_obj):
                        cand = weight
                        job_obj.feed("[宿主] 覆盖初始权重：新模型 → %s；旧模型已更名「初始_」前缀并移入回收站"
                                     % os.path.basename(weight))
                except Exception as exc:             # noqa: BLE001
                    job_obj.feed("[宿主] 覆盖初始权重失败：%s: %s" % (type(exc).__name__, exc))
            else:
                job_obj.feed("[宿主] 压缩未成功，未覆盖初始权重。")
        try:
            if cand and os.path.isfile(cand) and os.path.getmtime(cand) >= hist_started - 1:
                _host.record_weight_hist(hist_root, cand, hist_label, weight)   # 初始权重 = 被裁切/量化的源权重
        except OSError:
            pass

    job_obj.on_exit = _on_exit
    job_obj.feed(f"[宿主] 启动 {job_name} · pid {job_obj.pid} · "
                 f"{'裁切 ' + str(params.get('method') or 'sparsegpt') + ' 稀疏度 ' + str(params.get('sparsity')) if mode == 'prune' else '量化 ' + scheme}"
                 f" · 权重 {os.path.basename(weight)} → {os.path.basename(weight if ovw_on else out)}"
                 f"{'（覆盖初始权重，完成后顶名）' if ovw_on else ''}")
    if merge_summary:
        job_obj.feed(merge_summary)
    return {"ok": True, "job": job_name, "pid": job_obj.pid, "runtime_cfg": rt_path,
            "config": rt, "cmd": argv}


def status(mode: str, since: int = 0) -> dict:
    """压缩状态：进程状态 + 增量日志 + 真实产物/环境探针。"""
    cfg = _host.load_cfg()
    proj = _host.project(cfg)
    params = _effective_params(mode)
    weight = _resolve_weight(params.get("weight", ""), proj)
    out = _resolve_weight(params.get("out", ""), proj) if params.get("out") else ""
    j = _host.job(JOB_NAME[mode])
    other = _host.running_in_group(GROUP)

    rt_path = os.path.join(proj.logs_dir, f"runtime_compress_{mode}.json")
    rt: dict = {}
    try:
        with open(rt_path, "r", encoding="utf-8") as f:
            rt = json.load(f)
    except (OSError, ValueError):
        rt = {}
    if rt.get("out"):
        out = str(rt.get("out"))

    if j is None:
        body = {"running": bool(other), "pid": None, "exit_code": None, "elapsed": None,
                "lines": [], "total_lines": 0, "next": 0, "job_exists": False,
                "stop_requested": False, "mem_bytes": None}
    else:
        body = j.status(int(since or 0))
        body["job_exists"] = True

    body.update({
        "ok": True,
        "mode": mode,
        "project": proj.root,
        "running": body.get("running") or bool(other),
        "running_job": other,
        "stop_flag": os.path.isfile(os.path.join(proj.logs_dir, "stop.flag")),
        "config": rt or params,
        "outputs": {
            "weight": _probe(weight),
            "out": _probe(out),
        },
        "env": env_status(),
    })
    return body


def stop(mode: str) -> dict:
    """停止压缩：写 stop.flag + 强杀压缩子进程（压缩无中间态，直接终止即可）。"""
    cfg = _host.load_cfg()
    proj = _host.project(cfg)
    os.makedirs(proj.logs_dir, exist_ok=True)
    flag = os.path.join(proj.logs_dir, "stop.flag")
    with open(flag, "w", encoding="utf-8") as f:
        f.write("1")
    res = _host.stop_job(JOB_NAME[mode], force=True)
    return {"ok": True, "action": "stop", "stop_flag": flag,
            "note": "已停止压缩任务。" if res.get("stopped") else "没有正在运行的压缩任务。"}


def config(mode: str) -> dict:
    """页面取数：参数 + 环境 + 当前产物。"""
    cfg = _host.load_cfg()
    proj = _host.project(cfg)
    params = _effective_params(mode)
    weight = _resolve_weight(params.get("weight", ""), proj)
    out = _resolve_weight(params.get("out", ""), proj) if params.get("out") else ""
    return {
        "ok": True,
        "mode": mode,
        "project": proj.root,
        "params": params,
        "fields": [list(f) for f in FIELDS[mode]],
        "env": env_status(),
        "outputs": {"weight": _probe(weight), "out": _probe(out)},
        "running": bool(_host.running_in_group(GROUP)),
    }
