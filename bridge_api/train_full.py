"""bridge_api/train_full.py —— 「指令微调」页桥接口（动作已接真后端）。

对应 Tk 版页面：ENGINE_DIR/pages/train.py（FullFTPage，stage=sft / method=full）。

引擎宿主：同目录的 `_host.py`（下划线开头 → 插件加载器自动跳过）。本模块 import _host，
真正复用 ENGINE_DIR 自己的 engine（core.config / core.project / 子进程 core/train.py），
不重写一套。**训练是真子进程**，全局互斥，优雅停止 = 写工程 logs/stop.flag。

数据来源：
  - 配置：<应用目录>\\engine\\config.json 的 train 段
  - 当前工程：config.app.recent_projects[0]（须存在且是目录）> projects/default（与 bridge.py 同规则）
  - 语料 / 检查点 / 日志 / 导出：读磁盘真实文件
  - 训练状态：_host 里那个真子进程（running/pid/增量日志/outputs）

接口：
  GET  /api/train_full/config   真实 train 配置 + 运行时预览 + 路径存在性 + 宿主状态
  GET  /api/train_full/assets   工程内语料/检查点/日志/导出文件清单（真实磁盘扫描）
  GET  /api/train_full/log      训练日志尾部（默认取配置的 sft_log_csv）
  GET  /api/train_full/loss     从训练日志 CSV 解析出的 loss 数据点（供曲线）
  GET  /api/train_full/status   训练子进程状态 + 增量日志（?since=<游标>）
  POST /api/train_full/save     把表单写回 config.json（_host.save_train）
  POST /api/train_full/start    起真训练子进程（_host.start_training("sft","full",body)）
  POST /api/train_full/stop     优雅停止（写 stop.flag，_host.stop_training）
  POST /api/train_full/export   仍未接入（本页没有导出按钮）

铁律：只返回真实读取/执行到的数据；失败就返回 {"ok": false, "error": "..."}，由页面照实显示。
      绝不返回编造的假数据。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

API_DIR = os.path.dirname(os.path.abspath(__file__))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
import _host  # noqa: E402  （下划线开头，插件加载器会跳过它）

ENGINE_DIR = _host.IDE_DIR                       # 2026-10-23：由 _host 推导，不再写死盘符
CONFIG_PATH = os.path.join(ENGINE_DIR, "config.json")


# FullFTPage 关心、页面要回填的字段
TRAIN_FIELDS = ("batch_size", "dropout", "sft_steps", "sft_lr", "val_ratio", "ckpt_count")
TRAIN_BOOLS = ("amp", "sample", "overwrite_init")   # overwrite_init=覆盖初始权重（用户 2026-10-16）
CORPUS_KEYS = ("sft_corpus", "sft_init_from")

_TAIL_LINES = 40
_CHAR_READ_LIMIT = 64 * 1024 * 1024  # 超过 64MB 的语料不整读字符量

STAGE = "sft"
METHOD = "full"

# 页面 POST 里非表单字段（bridge 会塞 _query；页面自定义的 _ 前缀键也一并忽略）
def _clean(body: dict) -> dict:
    """剔除 _ 前缀键（含 bridge 注入的 _query），只留可写 config.train 的字段。"""
    return {k: v for k, v in (body or {}).items() if not str(k).startswith("_")}


# ---------------------------------------------------------------- 配置 / 路径

def _read_config() -> dict:
    """读 config.json；缺失/损坏都返回 {}（不抛）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_project(cfg: dict) -> str:
    """当前工程根：recent_projects[0]（须是目录）。与 bridge.py 同规则。2026-10-21 撤兜底：没有工程直接报「请先创建工程」。"""
    recent = (cfg.get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and item and os.path.isdir(item):
                return os.path.abspath(item)
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）：不再回落默认工程


def _abs(p: str) -> str:
    """配置里的路径 → 绝对路径。

    相对路径以**当前工程根**为基（2026-10-18 改；原来按 ENGINE_DIR，会把权重解析到软件目录里）。
    正/反斜杠混合的配置值也照常归一化。
    """
    p = (p or "").strip()
    if not p:
        return ""
    p = p.replace("/", os.sep).replace("\\", os.sep)
    if not os.path.isabs(p):
        try:
            p = os.path.join(_host.project_root(), p)
        except Exception:                            # noqa: BLE001
            p = os.path.join(ENGINE_DIR, p)
    return os.path.normpath(os.path.abspath(p))


def _file_info(raw: str) -> dict:
    ap = _abs(raw)
    info = {"raw": raw or "", "abs": ap, "exists": False,
            "size_bytes": None, "mtime": None}
    try:
        if ap and os.path.isfile(ap):
            st = os.stat(ap)
            info["exists"] = True
            info["size_bytes"] = int(st.st_size)
            info["mtime"] = int(st.st_mtime)
    except Exception:
        pass
    return info


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


def _runtime_sft(train: dict) -> dict:
    """复刻 Tk FullFTPage._runtime_stage_cfg()（stage=sft / method=full，只读预览用）。"""
    init_from = train.get("sft_init_from") or train.get("pretrain_out") or ""
    return {
        "stage": "sft",
        "method": "full",
        "corpus": train.get("sft_corpus") or "",
        "steps": _as_int(train.get("sft_steps"), 8000),
        "lr": _as_float(train.get("sft_lr"), 5e-4),
        "warmup": _as_int(train.get("sft_warmup"), 200),
        "eval_every": _as_int(train.get("sft_eval_every"), 200),
        "init_from": init_from,
        "out": train.get("sft_out", "checkpoints/eproai_model.pt"),
        "log_csv": train.get("sft_log_csv", "logs/sft_loss.csv"),
    }


def _dirs(project: str) -> dict:
    return {
        "corpora": os.path.join(project, "data", "corpora"),
        "checkpoints": os.path.join(project, "checkpoints"),
        "logs": os.path.join(project, "logs"),
        "exports": os.path.join(project, "exports"),
    }


# ---------------------------------------------------------------- 磁盘扫描

def _count_chars(path: str) -> Any:
    """字符量：整读文本计数（UTF-8, errors=replace）；过大或读失败 → None。"""
    try:
        if os.path.getsize(path) > _CHAR_READ_LIMIT:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return len(f.read())
    except Exception:
        return None


def _scan_dir(d: str, with_chars: bool = False) -> Any:
    """列目录内文件（只文件、按名排序）；目录不存在 → None。"""
    if not os.path.isdir(d):
        return None
    rows: list = []
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        row = {
            "name": name,
            "path": full,
            "ext": (os.path.splitext(name)[1] or "").lstrip(".").lower() or "?",
            "size_bytes": int(st.st_size),
            "mtime": int(st.st_mtime),
        }
        if with_chars:
            row["chars"] = _count_chars(full)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- GET 接口

def _host_status(since: int = 0) -> dict:
    """真训练子进程状态（running/pid/增量日志/outputs/exit_code/elapsed/config）。"""
    return _host.training_status(STAGE, METHOD, int(since or 0))


def _host_scalars(since: int = 0) -> dict:
    """从宿主状态里摘出给 config 用的标量字段（不带 lines）。"""
    st = _host_status(since)
    keys = ("running", "running_job", "pid", "exit_code", "elapsed", "started_at",
            "stop_requested", "stop_flag", "next", "job_exists", "project", "outputs")
    return {k: st.get(k) for k in keys}


def _mem_block(train: dict, proc_bytes) -> dict:
    """内存/显存水位（与 train_pretrain.status 同口径；auto 读 torch_backend.json，不 import torch）。"""
    want = str((train or {}).get("device") or "auto").strip().lower()
    if want in ("cpu", "cuda", "xpu", "mps"):
        dev = want
    else:
        dev = "cpu"
        try:
            with open(os.path.join(ENGINE_DIR, "torch_backend.json"), "r", encoding="utf-8") as fh:
                dev = str(json.load(fh).get("backend") or "cpu").strip().lower()
        except Exception:  # noqa: BLE001
            pass
    sysm = _host.sys_mem_bytes()
    gpum = _host.gpu_mem_bytes(dev) if dev == "cuda" else None
    return {
        "device": dev,
        "proc_bytes": proc_bytes,
        "sys_total": sysm.get("total", 0),
        "sys_avail": sysm.get("avail", 0),
        "gpu_total": (gpum or {}).get("total"),
        "gpu_free": (gpum or {}).get("free"),
        "gpu_used": (gpum or {}).get("used"),
    }


def config(query: dict) -> dict:
    """真实 train 配置 + 运行时预览 + 关键路径存在性 + 宿主状态（running/pid/outputs…）。"""
    cfg = _read_config()
    if not cfg:
        return {"error": "读取 config.json 失败：%s" % CONFIG_PATH}
    train = cfg.get("train")
    if not isinstance(train, dict):
        return {"error": "config.json 缺少 train 段：%s" % CONFIG_PATH}
    # 训练运行中（含流水线 persist=False 的步骤）以子进程真吃的 runtime 为准
    # （用户 2026-10-20：「使用流水线进入某个训练页，左侧参数应和流水线设置的一致」）
    ov = _host.run_config_overlay("sft", "full")
    if ov:
        train = {**train, **{k: v for k, v in ov.items() if k in train}}

    project = _resolve_project(cfg)
    rt = _runtime_sft(train)
    paths = {
        "corpus": _file_info(rt["corpus"]),
        "init_from": _file_info(rt["init_from"]),
        "out": _file_info(rt["out"]),
        "log_csv": _file_info(rt["log_csv"]),
    }

    def pick(keys) -> dict:
        return {k: train.get(k) for k in keys}

    result = {
        "ok": True,
        "config_path": CONFIG_PATH,
        "base_dir": ENGINE_DIR,
        "project": project,
        "device": train.get("device"),
        # 检查点数量按页独立（用户 2026-10-20）
        "fields": dict(pick(TRAIN_FIELDS),
                       ckpt_count=(ov.get("ckpt_count") if ov.get("ckpt_count") is not None
                                   else _host.get_ckpt_count(cfg, "sft", "full"))),
        "bools": pick(TRAIN_BOOLS),
        "corpus": pick(CORPUS_KEYS),
        "runtime": rt,
        "paths": paths,
        "dirs": _dirs(project),
    }

    # 宿主字段：running / pid / lines(next) / outputs（旧字段全部保留）
    try:
        result.update(_host_scalars(0))
        result["host"] = _host_status(0)
        result["host"].pop("lines", None)          # config 不带日志正文
    except Exception as exc:  # noqa: BLE001  引擎不可用时也不让 config 挂掉
        result["running"] = None
        result["host_error"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def status(query: dict) -> dict:
    """训练子进程状态 + 增量日志（?since=<游标>）。页面每 1 秒拿一次。"""
    raw = query.get("since") or 0
    try:
        since = int(float(raw))
    except (TypeError, ValueError):
        since = 0
    try:
        st = _host_status(since)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "since": since, "running": False, "lines": [], "next": since,
                "pid": None, "exit_code": None, "outputs": {}}
    st["config_path"] = CONFIG_PATH
    # 瓷片事实（与预训练页同口径）：内存/显存水位 —— outputs 里已有 out/corpus 文件探针
    try:
        train = _read_config().get("train") or {}
        st["mem"] = _mem_block(train, st.get("mem_bytes"))
    except Exception:  # noqa: BLE001
        st["mem"] = {"device": "cpu", "proc_bytes": st.get("mem_bytes"),
                     "sys_total": 0, "sys_avail": 0}
    return st


def assets(query: dict) -> dict:
    """工程内语料 / 检查点 / 日志 / 导出清单（真实磁盘扫描）。"""
    cfg = _read_config()
    project = _resolve_project(cfg or {})   # 2026-10-21 撤兜底：没配置/没工程都直接报「请先创建工程」
    dirs = _dirs(project)
    out: dict = {"ok": True, "project": project, "dirs": dirs, "dirs_missing": []}
    for key, d in dirs.items():
        rows = _scan_dir(d, with_chars=(key == "corpora"))
        if rows is None:
            out["dirs_missing"].append(d)
            out[key] = []
        else:
            out[key] = rows
    return out


def log(query: dict) -> dict:
    """训练日志尾部（默认取配置的 sft_log_csv；可用 ?file=<abs> 覆盖）。"""
    cfg = _read_config()
    train = (cfg.get("train") or {}) if cfg else {}
    rt = _runtime_sft(train) if isinstance(train, dict) else {"log_csv": ""}
    target = _abs(query.get("file") or rt.get("log_csv") or "")
    res = {"ok": True, "path": target, "exists": False, "total_lines": 0, "tail": []}
    if not target or not os.path.isfile(target):
        return res
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception as exc:  # noqa: BLE001
        return {"error": "读取日志失败：%s" % exc, "path": target}
    res["exists"] = True
    res["total_lines"] = len(lines)
    res["tail"] = lines[-_TAIL_LINES:]
    return res


def loss(query: dict) -> dict:
    """从训练日志 CSV 解析 loss 数据点（表头 step,train_loss,val_loss,lr,elapsed）。"""
    cfg = _read_config()
    train = (cfg.get("train") or {}) if cfg else {}
    rt = _runtime_sft(train) if isinstance(train, dict) else {"log_csv": ""}
    target = _abs(query.get("file") or rt.get("log_csv") or "")
    if not target or not os.path.isfile(target):
        return {"ok": True, "path": target, "exists": False, "points": []}
    points: list = []
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            f.readline()  # 跳过表头
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                try:
                    points.append({
                        "step": int(float(parts[0])),
                        "train": float(parts[1]),
                        "val": float(parts[2]),
                        "lr": float(parts[3]) if len(parts) > 3 and parts[3] else None,
                        "elapsed": float(parts[4]) if len(parts) > 4 and parts[4] else None,
                    })
                except ValueError:
                    continue
    except Exception as exc:  # noqa: BLE001
        return {"error": "读取日志失败：%s" % exc, "path": target}
    return {"ok": True, "path": target, "exists": True, "points": points}


# ---------------------------------------------------------------- POST 接口

def save_cfg(body: dict) -> dict:
    """把页面表单写回 config.json（类型按 Tk 版 _save 的规则转换）。"""
    updates = _clean(body)
    if not updates:
        return {"ok": False, "error": "没有要保存的字段。"}
    try:
        cfg = _host.load_cfg()
        updates = _host.apply_ckpt_count(cfg, "sft", "full", updates)   # 2026-10-20
        train = _host.save_train(cfg, updates)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "ok": True,
        "config_path": CONFIG_PATH,
        "saved": updates,
        "train": {k: train.get(k) for k in updates},
    }


def start(body: dict) -> dict:
    """起真训练子进程：stage=sft / method=full（_host 负责保存表单、组装 runtime、全局互斥）。"""
    try:
        return _host.start_training(STAGE, METHOD, _clean(body))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def stop(body: dict) -> dict:
    """优雅停止：写工程 logs/stop.flag（core/train.py 会保存权重后退出）。"""
    try:
        return _host.stop_training()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def export_weights(body: dict) -> dict:
    """本页没有导出按钮，保持未接入。"""
    return {"ok": False, "not_implemented": True,
            "reason": "本页未提供导出按钮：导出权重会写入工程 exports，尚未接入。"}


def register(router) -> None:
    router.get("/api/train_full/config", config)
    router.get("/api/train_full/assets", assets)
    router.get("/api/train_full/log", log)
    router.get("/api/train_full/loss", loss)
    router.get("/api/train_full/status", status)
    router.post("/api/train_full/start", start)
    router.post("/api/train_full/stop", stop)
    router.post("/api/train_full/save", save_cfg)
    router.post("/api/train_full/export", export_weights)
