"""bridge_api/train_lora.py —— 「LoRA 微调」页桥接口（读真数据 + 动作接真后端）。

对应 Tk 版页面：ENGINE_DIR/pages/train.py（LoraPage，TrainPageBase 子类）。

数据来源（只读）：
  - LoRA 超参：config.json 的 train 段（lora_r / lora_alpha / lora_dropout /
    lora_targets 与 batch_size / lora_steps / lora_lr / lora_init_from / amp / sample / device），
    2026-10-23 起步数/学习率/基座独立成 lora_* 键，不再和指令微调共用 sft_*。
    取值口径与 Tk LoraPage._refresh_lora_info 一致（α/r 缩放、逗号切目标层数）。
  - 语料 / 权重 / 导出目录：磁盘真实文件的存在性与大小。
  - 日志尾部：lora_log_csv（core/train.py 写的表头 step,train_loss,val_loss,lr,elapsed）。

动作（接真 → 走引擎宿主 _host，与 Tk 版同一套引擎）：
  - save  → _host.save_train（写 config.json 的 train 段，类型按 Tk _save 规则转换）
  - start → _host.start_training("sft", "lora", body)
            （子进程 core/train.py --config runtime_sft_lora.json；全局互斥；
             缺基座权重时由 _host.validate_training 拦下并返回中文文案）
  - stop  → _host.stop_training（写 <工程>/logs/stop.flag，训练保存权重后优雅退出）

接口：
  GET  /api/train_lora/config      LoRA 超参（真）+ 宿主字段（running/pid/lines/next/outputs）
  GET  /api/train_lora/status      ?since=<游标> 训练状态增量（宿主真相；页面每秒轮询）
  GET  /api/train_lora/artifacts   权重 / 导出目录 / 语料（真）
  GET  /api/train_lora/log         训练日志尾部（真 CSV）
  POST /api/train_lora/save        保存配置（真·写 config.json）
  POST /api/train_lora/start       开始 LoRA 微调（真·起 core/train.py 子进程）
  POST /api/train_lora/stop        停止训练（真·写 stop.flag）

铁律：只返回真实读取到的数据；读不到就返回 {"error": "..."}，绝不编造。
"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))            # ...\EAI_Studio\bridge_api
if HERE not in sys.path:                                     # 让 `import _host` 稳定可用
    sys.path.insert(0, HERE)
import _host  # noqa: E402

ENGINE_DIR = _host.IDE_DIR
CONFIG_PATH = _host.CONFIG_PATH


# Tk LoraPage 默认基座 / 目标层（core/lora.py DEFAULT_TARGETS 同一口径）
DEFAULT_TARGETS = "qkv"          # 2026-10-23：默认只挂 qkv（基座多已完成 SFT，少挂更稳）
DEFAULT_LORA_OUT = "checkpoints/eproai_lora.pt"
DEFAULT_LOG_CSV = "logs/lora_loss.csv"

STAGE = "sft"
METHOD = "lora"


# ---------------------------------------------------------------- 基础读取

def _read_config() -> dict:
    import json

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_project(override: str = "") -> str:
    """当前工程根：显式 override（须是目录） > recent_projects[0]。

    与 bridge.py 的 resolve_project / dataset.py 同一套规则。2026-10-21 撤兜底：没有工程直接报「请先创建工程」。
    """
    if override:
        # 2026-10-23（用户）：?project= 必须是带 .eproj 的工程目录
        return _host.require_project(override)
    recent = (_read_config().get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and _host.is_project_dir(item):
                return os.path.abspath(item)
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）：不再回落默认工程


def _abs(path: Any, proj: str) -> str:
    if path is None:
        return ""
    s = str(path).strip()
    if not s:
        return ""
    return s if os.path.isabs(s) else os.path.join(proj, s)


def _as_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cfg_train(cfg: dict) -> dict:
    t = cfg.get("train")
    return t if isinstance(t, dict) else {}


# ---------------------------------------------------------------- 磁盘事实

def _human(n: int) -> str:
    """体积文案（用户 2026-10-18）：到三位数就不带小数，269.0 KB → 269 KB，瓷片不再越界。"""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % n
            return ("%.0f %s" if f >= 100 else "%.1f %s") % (f, unit)
        f /= 1024.0
    return "%d B" % n


def _file_info(label: str, key: str, path: str) -> dict:
    d = {"label": label, "key": key, "path": path, "exists": False,
         "size_bytes": 0, "size_human": "—", "is_file": False}
    if not path:
        return d
    try:
        if os.path.isfile(path):
            size = os.path.getsize(path)
            d.update(exists=True, is_file=True, size_bytes=size, size_human=_human(size))
    except OSError:
        pass
    return d


def _line_count(path: str, cap: int = 0) -> int:
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                n += 1
                if cap and n >= cap:
                    break
    except OSError:
        return 0
    return n


def _dir_info(label: str, path: str) -> dict:
    d = {"label": label, "path": path, "exists": False, "count": 0, "files": []}
    if not path or not os.path.isdir(path):
        return d
    files = []
    try:
        for name in sorted(os.listdir(path)):
            fp = os.path.join(path, name)
            if os.path.isfile(fp):
                size = os.path.getsize(fp)
                files.append({"name": name, "size_bytes": size, "size_human": _human(size)})
            else:
                files.append({"name": name + "/", "size_bytes": 0, "size_human": "目录"})
    except OSError:
        return d
    d.update(exists=True, count=len(files), files=files)
    return d


def _lora_paths(cfg: dict, proj: str) -> dict:
    train = _cfg_train(cfg)
    out = _abs(train.get("lora_out") or DEFAULT_LORA_OUT, proj)
    init_from = _abs(train.get("lora_init_from") or train.get("pretrain_out") or "", proj)
    adapter = (os.path.splitext(out)[0] + ".lora.pt") if out else ""
    corpus = _abs(train.get("sft_corpus") or "", proj)
    log_csv = _abs(train.get("lora_log_csv") or DEFAULT_LOG_CSV, proj)
    return {"out": out, "init_from": init_from, "adapter": adapter,
            "corpus": corpus, "log_csv": log_csv}


# ---------------------------------------------------------------- 宿主字段

def _host_view(since: int = 0) -> dict:
    """引擎宿主的训练真相：running / pid / 增量 lines / next 游标 / outputs。

    宿主不可用（缺 torch / core 导入失败等）时不抛，返回 host_error 供页面照实显示。
    """
    try:
        st = _host.training_status(STAGE, METHOD, int(since or 0))
    except Exception as exc:  # noqa: BLE001
        return {
            "running": False, "pid": None, "exit_code": None, "stop_flag": False,
            "lines": [], "total_lines": 0, "next": int(since or 0),
            "outputs": None, "job_exists": False,
            "host_error": "%s: %s" % (type(exc).__name__, exc),
        }
    return {
        "running": st.get("running"),
        "running_job": st.get("running_job"),
        "pid": st.get("pid"),
        "exit_code": st.get("exit_code"),
        "elapsed": st.get("elapsed"),
        "stop_flag": st.get("stop_flag"),
        "lines": st.get("lines") or [],
        "total_lines": st.get("total_lines", 0),
        "next": st.get("next", int(since or 0)),
        "job_exists": st.get("job_exists"),
        "mem_bytes": st.get("mem_bytes"),   # 占用内存瓷片（用户 2026-10-17：右侧对齐预训练）
        "outputs": st.get("outputs"),
        "runtime_config": st.get("config"),
        "project": st.get("project"),
    }


# ---------------------------------------------------------------- 接口

def config(query: dict) -> dict:
    """LoRA 超参（真·来自 config.json 的 train 段）+ 宿主字段（running/pid/lines/next/outputs）。

    旧字段（r/alpha/dropout/scale/targets/... ）全部保留，仅新增宿主与路径字段。
    """
    try:
        cfg = _read_config()
        proj = _resolve_project(str(query.get("project") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"error": "读取配置失败：%s" % exc}
    train = _cfg_train(cfg)
    # 训练运行中（含流水线 persist=False 的步骤）以子进程真吃的 runtime 为准
    # （用户 2026-10-20：「使用流水线进入某个训练页，左侧参数应和流水线设置的一致」）
    ov = _host.run_config_overlay("sft", "lora")
    if ov:
        train = {**train, **{k: v for k, v in ov.items() if k in train}}
    paths = _lora_paths(cfg, proj)
    r = _as_int(train.get("lora_r"), 8)
    alpha = _as_float(train.get("lora_alpha"), 16.0)
    dropout = _as_float(train.get("lora_dropout"), 0.05)
    targets = str(train.get("lora_targets") or DEFAULT_TARGETS)
    tlist = [t.strip() for t in targets.replace("，", ",").split(",") if t.strip()]
    scale = alpha / max(1, r)
    out = {
        "project": proj,
        "config_path": CONFIG_PATH,
        "device": str(train.get("device") or "") or "自动探测",
        "method_in_config": str(train.get("method") or "full"),
        "r": r,
        "alpha": alpha,
        "dropout": dropout,
        "scale": scale,
        "targets": targets,
        "targets_list": tlist,
        "targets_count": len(tlist),
        "batch_size": _as_int(train.get("batch_size"), 32),
        # 2026-10-23：LoRA 独立成键（用户要求）—— 原来和指令微调共用 sft_steps/sft_lr/sft_init_from
        "lora_steps": _as_int(train.get("lora_steps"), 1200),
        "lora_lr": _as_float(train.get("lora_lr"), 2e-4),
        "amp": bool(train.get("amp", True)),
        "sample": bool(train.get("sample", True)),
        "val_ratio": _as_float(train.get("val_ratio"), 0.1),   # 验证集比例（用户 2026-10-19）
        "ckpt_count": (ov.get("ckpt_count") if ov.get("ckpt_count") is not None
                       else _host.get_ckpt_count(_read_config(), "sft", "lora")),   # 每页独立（2026-10-20）
        # 表单用路径（解析到工程根的绝对路径，便于直接起训练）
        "sft_corpus": paths["corpus"],
        # 2026-10-23：基座也独立（原来和指令微调共用 sft_init_from）
        "lora_init_from": paths["init_from"],
        "lora_init_from_raw": str(train.get("lora_init_from") or ""),
        "lora_out": paths["out"],
        "lora_log_csv": paths["log_csv"],
        # 宿主字段：running / pid / lines / next / outputs
        "stage": STAGE,
        "method": METHOD,
    }
    out.update(_host_view(0))
    return out


def status(query: dict) -> dict:
    """/api/train_lora/status?since=<游标> —— 训练状态增量（宿主真相）。

    页面每秒轮询：新行（lines）追到日志框，next 是下一次的游标；running/pid/outputs
    用于切换按钮状态、刷新权重清单与 Loss 曲线。
    """
    since = _as_int(query.get("since"), 0)
    out = {"ok": True, "since": since, "stage": STAGE, "method": METHOD}
    out.update(_host_view(since))
    return out


def artifacts(query: dict) -> dict:
    """权重 / 导出目录 / 语料的真实存在性与大小。"""
    try:
        cfg = _read_config()
        proj = _resolve_project(str(query.get("project") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"error": "读取工程失败：%s" % exc}
    p = _lora_paths(cfg, proj)

    items = [
        _file_info("基座权重", "base", p["init_from"]),
        _file_info("LoRA 适配器", "adapter", p["adapter"]),
        _file_info("微调语料", "corpus", p["corpus"]),
        _file_info("训练日志 CSV", "log_csv", p["log_csv"]),
    ]
    # 语料附带真实行数
    for it in items:
        if it["key"] == "corpus" and it["is_file"]:
            it["lines"] = _line_count(it["path"])

    ckpt_dir = os.path.dirname(p["out"]) if p["out"] else os.path.join(proj, "checkpoints")
    log_dir = os.path.dirname(p["log_csv"]) if p["log_csv"] else os.path.join(proj, "logs")
    dirs = [
        _dir_info("checkpoints", ckpt_dir),
        _dir_info("logs", log_dir),
    ]
    return {
        "project": proj,
        "items": items,
        "dirs": dirs,
        "adapter_note": "训练只存适配器（命名 = 输出权重同名 + .lora.pt）；合并放到「导出模型」页可选做",
    }


def log(query: dict) -> dict:
    """训练日志尾部（真·lora_loss.csv）。文件不存在 → exists=False（照实）。"""
    try:
        cfg = _read_config()
        proj = _resolve_project(str(query.get("project") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"error": "读取工程失败：%s" % exc}
    path = _lora_paths(cfg, proj)["log_csv"]
    if not path:
        return {"error": "配置里没有 lora_log_csv"}
    if not os.path.isfile(path):
        return {"path": path, "exists": False, "header": [], "rows": [],
                "points": [], "tail_text": "", "total": 0}

    try:
        tail_n = _as_int(query.get("tail"), 200) or 200
    except Exception:  # noqa: BLE001
        tail_n = 200

    header: list = []
    rows: list = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if i == 0:
                    header = [h.strip() for h in line.split(",")]
                    continue
                parts = line.split(",")
                rec: dict = {}
                for j, col in enumerate(header):
                    rec[col] = parts[j].strip() if j < len(parts) else ""
                rows.append(rec)
    except OSError as exc:
        return {"error": "读取日志失败：%s" % exc, "path": path}

    points = []
    for rec in rows:
        step = _as_int(rec.get("step"), -1)
        train_v = _as_float(rec.get("train_loss"), None)  # type: ignore[arg-type]
        val_v = _as_float(rec.get("val_loss"), None)       # type: ignore[arg-type]
        if step >= 0 and train_v is not None:
            points.append({"step": step, "train": train_v, "val": val_v})

    tail = rows[-tail_n:] if tail_n > 0 else rows[-200:]
    tail_lines = []
    for rec in tail:
        tail_lines.append(
            "step %-6s | train %-9s | val %-9s | lr %s"
            % (rec.get("step", ""), rec.get("train_loss", ""),
               rec.get("val_loss", ""), rec.get("lr", ""))
        )

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0
    return {
        "path": path,
        "exists": True,
        "size_bytes": size_bytes,
        "size_human": _human(size_bytes),
        "header": header,
        "rows": tail,
        "points": points[-tail_n:] if tail_n > 0 else points,
        "tail_text": "\n".join(tail_lines),
        "total": len(rows),
    }


# ---------------------------------------------------------------- 动作（接真）

def _updates(body: dict) -> dict:
    """从 POST body 里挑出要写进 config.train 的表单字段（去掉 _query 等内部键）。"""
    return {k: v for k, v in (body or {}).items()
            if not str(k).startswith("_") and v is not None}


def save(body: dict) -> dict:
    """保存配置：把表单值写进 config.json 的 train 段（_host.save_train，与 Tk _save 同口径）。"""
    updates = _updates(body)
    try:
        cfg = _host.load_cfg()
        updates = _host.apply_ckpt_count(cfg, "sft", "lora", updates)   # 2026-10-20
        train = _host.save_train(cfg, updates)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "save", "error": "保存配置失败：%s" % exc}
    return {
        "ok": True,
        "action": "save",
        "config_path": CONFIG_PATH,
        "saved": {k: train.get(k) for k in updates},
    }


def start(body: dict) -> dict:
    """开始 LoRA 微调：保存表单 → 组装 runtime 配置 → 起 core/train.py 子进程。

    失败（缺语料 / 缺基座权重 / 已有训练在跑）时返回 {"ok": False, "error": "<中文文案>"}，
    页面把文案原样显示。
    """
    updates = _updates(body)
    try:
        res = dict(_host.start_training(STAGE, METHOD, updates))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "start",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    res.setdefault("action", "start")
    return res


def stop(body: dict) -> dict:
    """停止训练：写 <工程>/logs/stop.flag，训练会保存权重后优雅退出（_host.stop_training）。"""
    try:
        res = dict(_host.stop_training())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "stop",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    res.setdefault("action", "stop")
    return res


def register(router) -> None:
    router.get("/api/train_lora/config", config)
    router.get("/api/train_lora/status", status)
    router.get("/api/train_lora/artifacts", artifacts)
    router.get("/api/train_lora/log", log)
    router.post("/api/train_lora/save", save)
    router.post("/api/train_lora/start", start)
    router.post("/api/train_lora/stop", stop)
