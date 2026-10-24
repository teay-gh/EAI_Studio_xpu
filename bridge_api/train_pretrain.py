"""bridge_api/train_pretrain.py —— 「预训练」页桥接口（save/start/stop 已接真引擎）。

对应 Tk 版页面：ENGINE_DIR/pages/train.py 的 PretrainPage（TrainPageBase 子类，
title=「预训练」，subtitle=「阶段一 · 纯文本从零训练语言模型」）。

真实数据来源（读：config.json / 工程文件；写：仅 save 改 config.json、start 起子进程、
stop 写 stop.flag，全部复用 ENGINE_DIR 自己的引擎，见 bridge_api/_host.py）：
  * 训练超参  → EAI_Studio/engine\\config.json 的 train 段（config.json 是唯一权威配置）；
  * 当前工程  → config.app.recent_projects[0]（存在且是目录才用）；没有就报「请先创建工程」
                （2026-10-21 撤兜底，不再回落默认工程）；
  * 输出权重  → train.pretrain_out（如 checkpoints\\eproai_pretrain.pt）的大小 / 修改时间；
  * 占用内存  → 训练子进程工作集内存（_host.proc_mem_bytes，Windows psapi / Linux VmRSS）；
  * 训练日志  → train.pretrain_log_csv（如 logs\\pretrain_loss.csv）的尾部与解析出的
                (step, train, val, lr, elapsed) 数据点；
  * 运行时目录 → <工程>\\logs 下的 runtime_pretrain_full.json 与 stop.flag（存在性）。
  * 训练进程 → 由宿主（_host）以子进程托管 core/train.py，桥接口只读其实时状态与增量日志。

接口：
  GET  /api/train_pretrain/status[?since=N]       真实配置 + 文件状态 + 宿主进程增量日志
  GET  /api/train_pretrain/log[?tail=N]           真实日志文件尾部 + 数据点（只读）
  POST /api/train_pretrain/save                   真写 config.json（_host.save_train）
  POST /api/train_pretrain/start                  真起 core/train.py 子进程（_host.start_training）
  POST /api/train_pretrain/stop                   真写 stop.flag 优雅停止（_host.stop_training）
  POST /api/train_pretrain/export                 未接入（不打包导出权重）

铁律：只返回真实读取到的数据；读不到（缺 config / 缺 train 段 / IO 失败）就返回
      {"error": "..."}，由页面照实显示。绝不返回编造的假数据。
      启动 / 停止 / 保存一律复用 ENGINE_DIR 自己的引擎（bridge_api/_host.py），不另写一套。
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime
from typing import Any

import _host                                     # 引擎宿主（同目录，bridge_api 已在 sys.path）

HERE = os.path.dirname(os.path.abspath(__file__))            # ...\EAI_Studio\bridge_api
WEB_DIR = os.path.dirname(HERE)                              # ...\EAI_Studio
EAI_ROOT = os.path.dirname(WEB_DIR)                          # ...\EAI
ENGINE_DIR = os.path.join(WEB_DIR, "engine")                # 2026-10-23：不再假设目录名叫 EAI_Studio，改名/换盘都成立                  # Tk subprocess 的 cwd
CONFIG_PATH = os.path.join(ENGINE_DIR, "config.json")


# Tk PretrainPage 关心的 train 配置键（表单 + 运行所需），原样透出
CONFIG_KEYS = (
    "device", "method", "seed", "d_model", "n_head", "n_layer", "block_size",
    "batch_size", "dropout", "grad_clip", "grad_accum", "weight_decay",
    "amp", "amp_dtype", "mtp", "mtp_k", "sample", "cosine_lr",
    "pretrain_corpus", "pretrain_steps", "pretrain_lr", "pretrain_warmup",
    "pretrain_eval_every", "pretrain_out", "pretrain_log_csv",
    "ckpt_count",          # 检查点数量（用户 2026-10-19）
)

LOG_COLUMNS = "step,train_loss,val_loss,lr,elapsed"

_NOT_IMPL_EXPORT = "迁移期不导出/打包权重文件"


# ---------------------------------------------------------------- 基础

def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_project(override: str = "") -> str:
    """当前工程根：显式 override（须是目录） > recent_projects[0]。2026-10-21 撤兜底：没有工程直接报「请先创建工程」。"""
    if override:
        # 2026-10-23（用户）：?project= 必须是带 .eproj 的工程目录
        return _host.require_project(override)
    recent = (_read_config().get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and _host.is_project_dir(item):
                return os.path.abspath(item)
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）：不再回落默认工程


def _resolve_path(value: Any) -> str:
    """相对路径按当前工程根解析（用户 2026-10-18）。
    以前按 ENGINE_DIR 解析，会把 checkpoints/his.pt 显示成
    EAI_Studio/engine/checkpoints/his.pt —— 那是软件本体目录，不该出现工程产物。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if not os.path.isabs(v):
        try:
            v = os.path.join(_resolve_project(), v)
        except Exception:                            # noqa: BLE001
            v = os.path.join(ENGINE_DIR, v)
    return os.path.abspath(v)


def _file_fact(path: str) -> dict:
    """文件的真实存在性 / 大小 / 修改时间。只读。"""
    fact = {
        "path": path or "",
        "name": os.path.basename(path) if path else "",
        "exists": False,
        "size_bytes": 0,
        "size_mb": None,
        "mtime": None,
    }
    try:
        if path and os.path.isfile(path):
            st = os.stat(path)
            fact["exists"] = True
            fact["size_bytes"] = int(st.st_size)
            fact["size_mb"] = round(st.st_size / (1024.0 * 1024.0), 2)
            fact["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        pass
    return fact


def _count_lines(path: str) -> int:
    """数文本行数（只读，逐块读，避免整文件进内存）。"""
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                n += 1
    except OSError:
        return 0
    return n


# ---------------------------------------------------------------- 接口

def status(query: dict) -> dict:
    """真实配置 + 文件状态 + 宿主进程增量日志。刷新/轮询用。

    ?since=<游标> 取宿主子进程自该游标起的新增日志行（返回的 next 作新游标）。
    """
    train = _read_config().get("train")
    if not isinstance(train, dict):
        return {"error": "config.json 缺少 train 段（%s）" % CONFIG_PATH}
    # 训练运行中（含流水线 persist=False 的步骤）以子进程真吃的 runtime 为准
    # （用户 2026-10-20：「使用流水线进入某个训练页，左侧参数应和流水线设置的一致」）
    ov = _host.run_config_overlay("pretrain", "full")
    if ov:
        train = {**train, **{k: v for k, v in ov.items() if k in train}}

    proj = _resolve_project(str(query.get("project") or ""))
    out_path = _resolve_path(train.get("pretrain_out"))
    log_path = _resolve_path(train.get("pretrain_log_csv"))
    corpus_path = _resolve_path(train.get("pretrain_corpus"))

    rt_dir = os.path.join(proj, "logs")
    rt_json = os.path.join(rt_dir, "runtime_pretrain_full.json")
    stop_flag = os.path.join(rt_dir, "stop.flag")

    config_view = {k: train.get(k) for k in CONFIG_KEYS if k in train}
    # 检查点数量按页独立（用户 2026-10-20）：四页不再共用 config.train.ckpt_count
    config_view["ckpt_count"] = (ov.get("ckpt_count") if ov.get("ckpt_count") is not None
                                else _host.get_ckpt_count({"train": train}, "pretrain", "full"))

    out = {
        "stage": "pretrain",
        "project": proj,
        "config_path": CONFIG_PATH,
        "source": "config.json#train",
        "running": False,                      # 由宿主进程状态覆盖（见下）
        "running_note": "训练进程由桥宿主托管（core/train.py 子进程）；"
                        "running / pid / lines / next / outputs 为其实时状态。",
        "config": config_view,
        "resolved": {
            "out": out_path,
            "log_csv": log_path,
            "corpus": corpus_path,
        },
        "corpus": _file_fact(corpus_path),
        "checkpoint": _file_fact(out_path),
        "log_file": dict(
            _file_fact(log_path),
            lines=_count_lines(log_path) if os.path.isfile(log_path) else 0,
        ),
        "runtime": {
            "dir": rt_dir,
            "dir_exists": os.path.isdir(rt_dir),
            "runtime_json": rt_json,
            "runtime_json_exists": os.path.isfile(rt_json),
            "stop_flag": stop_flag,
            "stop_flag_exists": os.path.isfile(stop_flag),
        },
        # ---- 宿主进程增量字段（新；页面用 since 游标增量取日志，next 作新游标）----
        "pid": None,
        "mem_bytes": None,                       # 训练子进程工作集内存（未托管 = None）
        "exit_code": None,
        "lines": [],
        "next": 0,
        "total_lines": 0,
        "elapsed": None,
        "stop_requested": False,
        "job_exists": False,
        "running_job": None,
        "host_stop_flag": None,
        "outputs": None,
    }

    try:
        since = int(float(str(query.get("since") or "0") or "0"))
    except (TypeError, ValueError):
        since = 0
    try:
        st = _host.training_status("pretrain", "full", since)
        out["running"] = bool(st.get("running"))
        out["running_job"] = st.get("running_job")
        out["pid"] = st.get("pid")
        out["mem_bytes"] = st.get("mem_bytes")
        out["exit_code"] = st.get("exit_code")
        out["lines"] = st.get("lines") or []
        out["next"] = st.get("next")
        out["total_lines"] = st.get("total_lines")
        out["elapsed"] = st.get("elapsed")
        out["stop_requested"] = st.get("stop_requested")
        out["job_exists"] = st.get("job_exists")
        out["host_stop_flag"] = st.get("stop_flag")
        out["outputs"] = st.get("outputs")
        out["host_config"] = st.get("config")   # 子进程吃的那份 runtime 配置
        if st.get("project"):
            out["host_project"] = st.get("project")
        if st.get("error"):
            out["host_error"] = st.get("error")
    except Exception as exc:                   # noqa: BLE001  宿主异常不拖垮只读状态
        out["host_error"] = "%s: %s" % (type(exc).__name__, exc)

    # ---- 内存/显存水位（用户 2026-10-14）：XPU 查本机剩余内存 + EAI 进程占用；CUDA 查剩余显存 ----
    want = str(train.get("device") or "auto").strip().lower()
    if want in ("cpu", "cuda", "xpu", "mps"):
        dev = want
    else:
        dev = "cpu"
        try:                                    # auto → 读最近一次真探测的标记，不重复 import torch
            with open(os.path.join(ENGINE_DIR, "torch_backend.json"), "r", encoding="utf-8") as fh:
                dev = str(json.load(fh).get("backend") or "cpu").strip().lower()
        except Exception:                      # noqa: BLE001
            pass
    sysm = _host.sys_mem_bytes()
    gpum = _host.gpu_mem_bytes(dev) if dev == "cuda" else None
    out["mem"] = {
        "device": dev,
        "proc_bytes": out.get("mem_bytes"),    # EAI 训练子进程工作集（未托管 = None）
        "sys_total": sysm.get("total", 0),
        "sys_avail": sysm.get("avail", 0),
        "gpu_total": (gpum or {}).get("total"),
        "gpu_free": (gpum or {}).get("free"),
        "gpu_used": (gpum or {}).get("used"),
    }
    return out


def log(query: dict) -> dict:
    """真实训练日志文件尾部 + 解析出的 (step, train, val, lr, elapsed) 点。只读。

    可选 ?path=<abs> 覆盖日志文件（调试用，与 network.py 的 ?checkpoint= 同套路）。
    """
    train = _read_config().get("train") or {}
    if not isinstance(train, dict):
        train = {}
    override = str(query.get("path") or "").strip()
    log_path = _resolve_path(override or train.get("pretrain_log_csv"))

    try:
        tail_n = int(str(query.get("tail") or "200"))
    except (TypeError, ValueError):
        tail_n = 200
    tail_n = max(1, min(tail_n, 5000))

    if not log_path:
        return {"error": "config.json 未配置 train.pretrain_log_csv，无法定位日志文件。"}
    if not os.path.isfile(log_path):
        return {
            "path": log_path,
            "exists": False,
            "columns": LOG_COLUMNS,
            "total_lines": 0,
            "total_rows": 0,
            "points": [],
            "tail": "",
            "note": "日志文件尚不存在（该工程还没有产生预训练日志）。",
        }

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as exc:
        return {"error": "读取日志失败：%s" % exc}

    lines = raw.splitlines()
    points: list[dict] = []
    rows = 0
    reader = csv.reader(io.StringIO(raw))
    header_seen = False
    for row in reader:
        if not row:
            continue
        if not header_seen and row[0].strip().lower() == "step":
            header_seen = True
            continue
        if len(row) < 3:
            continue
        try:
            pt = {
                "step": int(float(row[0])),
                "train": float(row[1]),
                "val": float(row[2]),
                "lr": float(row[3]) if len(row) > 3 and row[3] != "" else None,
                "elapsed": float(row[4]) if len(row) > 4 and row[4] != "" else None,
            }
        except (ValueError, IndexError):
            continue
        points.append(pt)
        rows += 1

    tail_text = "\n".join(lines[-tail_n:])
    return {
        "path": log_path,
        "exists": True,
        "columns": LOG_COLUMNS,
        "total_lines": len(lines),
        "total_rows": rows,
        "points": points[-2000:],
        "tail": tail_text,
    }


# ---------------------------------------------------------------- 真动作（复用引擎宿主 _host）

def save(body: dict) -> dict:
    """保存配置：把表单值写进 config.json 的 train 段。

    _host.save_train 与 App / Tk 版 pages/train.py::_save 同一套默认值合并与类型转换。
    """
    try:
        cfg = _host.load_cfg()
        body = _host.apply_ckpt_count(cfg, "pretrain", "full", body or {})
        train = _host.save_train(cfg, body)
        return {"ok": True, "config_path": _host.CONFIG_PATH, "saved": train}
    except Exception as exc:                   # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def start(body: dict) -> dict:
    """开始预训练：复用宿主的 start_training（真起 core/train.py 子进程，全局互斥）。

    body 里带 train.* 的表单值会被先保存，再按 stage/method 组装 runtime 配置。
    缺语料等校验失败返回 {"ok": false, "error": "..."}，由页面照实显示。
    """
    try:
        return _host.start_training("pretrain", "full", body or {})
    except Exception as exc:                   # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def stop(body: dict) -> dict:
    """停止训练：写 <工程>/logs/stop.flag，core/train.py 会保存权重后优雅退出。"""
    try:
        return _host.stop_training()
    except Exception as exc:                   # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def export(_body: dict) -> dict:
    """导出权重：会打包/复制工程文件，未接入。"""
    return {"not_implemented": True, "reason": _NOT_IMPL_EXPORT}


def register(router) -> None:
    router.get("/api/train_pretrain/status", status)
    router.get("/api/train_pretrain/log", log)
    router.post("/api/train_pretrain/save", save)
    router.post("/api/train_pretrain/start", start)
    router.post("/api/train_pretrain/stop", stop)
    router.post("/api/train_pretrain/export", export)
