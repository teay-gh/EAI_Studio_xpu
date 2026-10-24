#!/usr/bin/env python3
"""EAI Studio · Python 数据桥（外壳层）

只用 Python 标准库：一个只绑 127.0.0.1 的只读 HTTP 服务，把 ENGINE_DIR 的真实
数据（工程目录、样本数、设备、模型权重）以 JSON 交给 Electron 主进程。

数据来源（全部只读，不写 ENGINE_DIR 下任何文件）：
  - 配置：<应用目录>\\engine\\config.json
  - 当前工程：config.json 的 app.recent_projects[0]（存在才用），否则
    <应用目录>\\engine\\projects\\default
  - 文章/问答样本数：工程 data/article、data/response 下的 *.jsonl
    （与 core/storage.py 的 DatasetStore 落盘格式一致：一行一条记录，
     排除 *_rejected.jsonl）。优先用 core.storage.DatasetStore.import_jsonl
     统计；import 失败或超过 5s 就退回按行统计（实测 import 只要 ~0.18s，
     不拖入 torch）。
  - 模型：<当前工程>/checkpoints/eproai_model.pt

启动后 stdout 第一行固定是 `PORT <实际端口>`（随后 flush），供外壳握手。
命令行加 --selftest 时直接打印一份 overview JSON 后退出。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# ---------------------------------------------------------------- 路径 / 常量

HERE = os.path.dirname(os.path.abspath(__file__))            # ...\EAI\EAI_Studio
EAI_ROOT = os.path.dirname(HERE)                             # ...\EAI
IDE_DIR = os.path.join(HERE, "engine")                       # 2026-10-23：由本文件位置推导，不假设盘符/目录名
DEFAULT_CONFIG_PATH = os.path.join(IDE_DIR, "config.json")
DEFAULT_PROJECT_ROOT = os.path.join(IDE_DIR, "projects", "default")
MODEL_FILENAME = "eproai_model.pt"

_IMPORT_BUDGET_S = 5.0


def _errlog():
    """兜底异常落盘（2026-10-24，排查报告第 2 条）：core/errlog.py。

    engine 目录要到 :112 才进 sys.path，启动早期（读配置）就可能要记日志，所以这里自己挂一次。
    """
    for _ in range(2):
        try:
            from core import errlog                    # noqa: PLC0415
            return errlog
        except Exception:                              # noqa: BLE001
            if IDE_DIR in sys.path:
                return None
            sys.path.insert(0, IDE_DIR)
    return None


def _log_exc(tag: str, exc: Any, **fields: Any) -> None:
    mod = _errlog()
    if mod is not None:
        mod.log_exc(tag, exc, **fields)


# 「还没有工程」是首启的正常状态，不是 500（2026-10-24 开源首启实测：全新副本里 16 条全是它）
_NO_PROJECT_HINTS = ("请先创建工程", "还没有工程", "当前工程文件夹不存在")


def _is_no_project(exc: BaseException) -> bool:
    """渲染层 req() 认得 error 里的这几句并渲染空态；但 HTTP 500 会让 Chromium 控制台
    每页刷一串红字、bridge_error.log 也被灌满。所以统一降级成 200 + no_project。"""
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc)
    return any(h in msg for h in _NO_PROJECT_HINTS)


# ---------------------------------------------------------------- 配置读取

def load_config(path: str) -> dict[str, Any]:
    """读 config.json；缺失/损坏都返回 {}（不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                     # noqa: BLE001
        _log_exc("config.load(bridge)", exc, path=path)   # 坏配置也要留堆栈（2026-10-24）
        return {}


def has_project_file(root: str) -> bool:
    """工程文件审查（用户 2026-10-23）：根目录里必须有 *.eproj（旧名 project.eproj 也算）。"""
    try:
        for name in os.listdir(root):
            if name.lower().endswith(".eproj") and os.path.isfile(os.path.join(root, name)):
                return True
    except OSError:
        return False
    return False


def resolve_project(cfg: dict[str, Any], override: Optional[str] = None) -> str:
    """当前工程根目录：命令行 override > config.app.recent_projects[0] > 默认工程。

    2026-10-23（用户）：override 与 recent 都要过 .eproj 审查，不是工程就不许当工程用。
    """
    if override:
        ap = os.path.abspath(override)
        if not has_project_file(ap):
            raise RuntimeError("「%s」不是 EAI 工程：没有 .eproj 工程文件"
                               % (os.path.basename(ap.rstrip("\\/")) or ap))
        return ap
    app_cfg = cfg.get("app") or {}
    recent = app_cfg.get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and item and os.path.isdir(item) and has_project_file(item):
                return os.path.abspath(item)
    return DEFAULT_PROJECT_ROOT


def data_dir(cfg: dict[str, Any], project_root: str, kind: str, use_project: bool) -> str:
    """样本目录：优先工程里固定的 data/<kind>（与 app 的 proj.article_dir 一致）；
    工程被显式覆盖时也走工程目录，否则退到 config 的 collection.output_dir。"""
    if use_project:
        return os.path.join(project_root, "data", kind)
    coll = ((cfg.get(kind) or {}).get("collection") or {})
    out = coll.get("output_dir")
    if isinstance(out, str) and out.strip():
        return out
    return os.path.join(project_root, "data", kind)


# ---------------------------------------------------------------- 样本计数

_storage_cache: Any = None  # None=未探测, False=不可用, 否则是 DatasetStore 类


def _try_storage():
    """尝试 import core.storage（可能连带 torch）。实测很快；慢/失败就弃用。"""
    global _storage_cache
    if _storage_cache is not None:
        return _storage_cache
    t0 = time.perf_counter()
    try:
        if IDE_DIR not in sys.path:
            sys.path.insert(0, IDE_DIR)
        from core.storage import DatasetStore  # type: ignore
        dt = time.perf_counter() - t0
        if dt > _IMPORT_BUDGET_S:
            print(f"[bridge] core.storage 导入耗时 {dt:.1f}s 超过预算，改用按行统计",
                  file=sys.stderr)
            _storage_cache = False
        else:
            _storage_cache = DatasetStore
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge] core.storage 不可用（{exc}），改用按行统计", file=sys.stderr)
        _storage_cache = False
    return _storage_cache


def _count_lines(path: str) -> int:
    """按 storage.py 的落盘语义数条数：非空行且是可接受的记录对象。"""
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and (obj.get("article") or obj.get("question") or obj.get("title")):
                    n += 1
    except Exception:
        return 0
    return n


def count_records(dirpath: str) -> tuple[int, str]:
    """返回 (样本数, 统计方式)。只读，目录不存在 → (0, 'no-dir')。"""
    if not dirpath or not os.path.isdir(dirpath):
        return 0, "no-dir"
    files = sorted(
        f for f in os.listdir(dirpath)
        if f.endswith(".jsonl") and "rejected" not in f
    )
    store = _try_storage()
    total = 0
    if store:
        for name in files:
            try:
                total += len(store.import_jsonl(os.path.join(dirpath, name)))
            except Exception:
                total += _count_lines(os.path.join(dirpath, name))
        return total, "core.storage.import_jsonl"
    for name in files:
        total += _count_lines(os.path.join(dirpath, name))
    return total, "linecount"


# ---------------------------------------------------------------- 设备 / 模型

def device_info(cfg: dict[str, Any]) -> tuple[str, str]:
    """总览的训练设备：与设置页/训练引擎**同口径**，但**不 import torch**。

    可用性读 engine/torch_backend.json（训练 / 对话第一次 import torch 时由 _host.pick_device 刷新）。
    以前这里直接调 _host.pick_device，等于每开一次工作台就把 torch（实测 830MB 工作集）拉进桥进程。
    """
    train = cfg.get("train") or {}
    raw = str(train.get("device") or "auto").strip().lower()
    if raw not in ("auto", "cpu", "cuda", "xpu"):
        raw = "auto"
    try:
        with open(os.path.join(IDE_DIR, "torch_backend.json"), "r", encoding="utf-8") as f:
            cached = json.load(f) or {}
    except Exception:                            # noqa: BLE001
        cached = {}
    if not isinstance(cached, dict) or not cached:   # 还没探测过：如实报设置原值，不猜
        return raw.upper(), "未探测"
    cuda = bool(cached.get("cuda_available"))
    xpu = bool(cached.get("xpu_available"))
    if raw in ("cuda", "xpu") and (cuda if raw == "cuda" else xpu):
        pick = raw
    elif raw == "cpu":
        pick = "cpu"
    else:
        pick = "cuda" if cuda else ("xpu" if xpu else "cpu")
    return pick.upper(), ("自动探测" if raw == "auto" else "配置指定")


def model_info(project_root: str) -> dict[str, Any]:
    path = os.path.join(project_root, "checkpoints", MODEL_FILENAME)
    try:
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / 1024.0 / 1024.0
            return {
                "ready": True,
                "label": f"{MODEL_FILENAME} · {size_mb:.1f} MB",
                "size_mb": round(size_mb, 1),
            }
    except Exception:
        pass
    return {"ready": False, "label": "请先完成训练", "size_mb": None}


# ---------------------------------------------------------------- 总览：文件占用 + 操作日志

def human_size(n: int) -> str:
    """字节 → 人看的字符串（1 位小数）。"""
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return ("%d B" % int(round(f))) if unit == "B" else ("%.1f %s" % (f, unit))
        f /= 1024.0
    return "%.1f GB" % f


def dir_size(root: str) -> int:
    """工程目录真实占用（跳过 .git / __pycache__ / node_modules 这类不该算进去的）。"""
    total = 0
    if not root or not os.path.isdir(root):
        return 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


_USE_LABELS = {"pretrain": "预训练", "continue": "继续预训练",
               "sft": "指令微调", "lora": "LoRA 微调"}


def _collect_use_map(project_root: str) -> dict:
    """蒸馏产物 → 训练用途（采集启动时记进 data/_collect_use.json；老产物没记录就走默认）。"""
    p = os.path.join(project_root, "data", "_collect_use.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _distill_logs(project_root: str) -> list:
    """蒸馏产物 → 日志行：左标签=「语料蒸馏/响应蒸馏」（同页面名），右侧只写文件名（用户 2026-10-21）。"""
    rows = []
    for kind, label in (("article", "语料蒸馏"),
                        ("response", "响应蒸馏")):
        base = os.path.join(project_root, "data", kind)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in sorted(files):
                if fn.startswith(".") or "rejected" in fn.lower():
                    continue
                if os.path.splitext(fn)[1].lower() not in (".txt", ".json", ".jsonl", ".ndjson"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                rows.append({"op": label, "detail": fn, "path": p, "mtime": mt})
    return rows


def _repo_logs(project_root: str) -> list:
    """仓库手动添加（权重/数据集）→ 日志行（用户 2026-10-15：在仓库添加权重或数据集时写日志）。

    落库 <工程>/data/_repo_adds.json（repo.add_files 添加时 append）；行形如既有日志：
    op=「添加」、detail=「权重 · 文件名」/「数据集 · 文件名」。
    """
    p = os.path.join(project_root, "data", "_repo_adds.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception:  # noqa: BLE001  没有/坏档 = 没有手动添加记录
        return []
    if not isinstance(items, list):
        return []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        if not name:
            continue
        kind = "权重" if str(it.get("kind") or "") == "weight" else "数据集"
        ok = it.get("ok")
        reason = str(it.get("reason") or "")
        if ok is False:
            # 失败/拒收也留日志（用户 2026-10-16）
            rows.append({
                "op": "失败",
                "detail": "%s · %s：%s" % (kind, name, reason or "未添加"),
                "path": str(it.get("path") or ""),
                "mtime": float(it.get("mtime") or 0),
            })
        else:
            rows.append({
                "op": "添加",
                "detail": "%s · %s" % (kind, name),
                "path": str(it.get("path") or ""),
                "mtime": float(it.get("mtime") or 0),
            })
    return rows


def _conv_logs(project_root: str) -> list:
    """模型操作日志（用户 2026-10-21）：转换导入 / 转换导出 / 转换 / 导出（导出模型=「导出」）。

    落库 <工程>/data/_model_logs.json（_host.model_log 写：model_convert.py 转换成功、
    export.py 导出成功后各记一笔）；旧名 _modelconv_logs.json 一并兼容读。
    """
    items = []
    for fn in ("_model_logs.json", "_modelconv_logs.json"):
        p = os.path.join(project_root, "data", fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                items += d
        except Exception:  # noqa: BLE001  没有/坏档 = 没有该账
            pass
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        if not name:
            continue
        rows.append({
            "op": str(it.get("op") or "转换"),
            "detail": name,
            "path": str(it.get("path") or ""),
            "mtime": float(it.get("mtime") or 0),
        })
    return rows


def _artifact_stem(fn: str) -> str:
    """产物文件名 → 基座名：TCLS_Check2.pt / TCLS.progress.json / TCLS.lora.pt 都归到 TCLS
    （用户 2026-10-20：检查点、进度文件要跟着它那次训练的方式走）。"""
    low = fn.lower()
    for suf in (".lora.progress.json", ".progress.json", ".lora.pt", ".pt", ".json"):
        if low.endswith(suf):
            fn = fn[: -len(suf)]
            break
    import re as _re
    return _re.sub(r"(?i)_check\d+$", "", fn)


def _train_logs(cfg: dict[str, Any], project_root: str) -> list:
    """训练产物 → 日志行：先按配置的输出路径认（预训练 / 微调 / LoRA），再扫当前工程的 checkpoints/。"""
    train = cfg.get("train") or {}
    ck_dir = os.path.join(project_root, "checkpoints")
    rows, seen = [], set()

    def _key(p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    def _add(op: str, p: str) -> None:
        if not p:
            return
        # 配置里常写相对路径（checkpoints/X.pt）：按工程根解析，否则 isfile 相对 bridge 的 CWD
        # 永远失败 → 该产物掉到下面的名字猜测里变成笼统的「训练」（用户 2026-10-20）
        if not os.path.isabs(p):
            p = os.path.join(project_root, p)
        if not os.path.isfile(p):
            return
        k = _key(p)
        if k in seen:
            return
        try:
            mt = os.path.getmtime(p)
        except OSError:
            return
        seen.add(k)
        rows.append({"op": op, "detail": os.path.basename(p), "path": p, "mtime": mt})

    guesses = (
        ("预训练", train.get("pretrain_out"), "eproai_pretrain.pt"),
        ("继续预训练", train.get("continue_out"), "eproai_continue.pt"),
        ("指令微调", train.get("sft_out"), "eproai_model.pt"),
        ("LoRA 微调", train.get("lora_out"), "eproai_lora.pt"),
    )
    for op, configured, fallback in guesses:
        _add(op, configured)
        _add(op, os.path.join(ck_dir, fallback))

    # 基座名 → 训练方式：配置里的产物名优先（TCLSZ.pt → 指令微调），
    # 再叠训练结束时引擎记的 logs/_train_methods.json（检查点 / 进度文件都在里面）
    by_stem: dict[str, str] = {}
    for op, configured, _fb in guesses:
        if configured:
            by_stem[_artifact_stem(os.path.basename(str(configured)))] = op
    try:
        with open(os.path.join(project_root, "logs", "_train_methods.json"), "r", encoding="utf-8") as f:
            noted = json.load(f)
        if isinstance(noted, dict):
            for name, op in noted.items():
                if isinstance(op, str) and op:
                    by_stem[_artifact_stem(os.path.basename(str(name)))] = op
    except (OSError, ValueError):
        pass

    if os.path.isdir(ck_dir):
        for fn in sorted(os.listdir(ck_dir)):
            p = os.path.join(ck_dir, fn)
            if not os.path.isfile(p):
                continue
            op = by_stem.get(_artifact_stem(fn))
            if not op:
                low = fn.lower()
                # LoRA 合并产物（<基座>_Merge.pt）名字里也带 lora，先判合并，否则日志里
                # 会写成「LoRA 微调」（用户 2026-10-22）
                if "merge" in low:
                    op = "LoRA 合并"
                elif "lora" in low:
                    op = "LoRA 微调"
                elif "continue" in low:
                    op = "继续预训练"
                elif "pretrain" in low:
                    op = "预训练"
                elif "sft" in low or "model" in low:
                    op = "指令微调"
                else:
                    op = "训练"
            _add(op, p)
    return rows


CONFIG_PATH_DBG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "config.json")


def _log_hidden_state(project_root: str) -> dict:
    """总览日志删除/全部清除状态（用户 2026-10-16）：
    data/_log_hidden.json = {keys: [op|detail], cleared_before: ts}。"""
    p = os.path.join(project_root, "data", "_log_hidden.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {
                "keys": [str(x) for x in (d.get("keys") or [])],
                "cleared_before": float(d.get("cleared_before") or 0),
            }
    except Exception:  # noqa: BLE001  没有/坏档 = 没删过
        pass
    return {"keys": [], "cleared_before": 0.0}


def _log_key(r: dict) -> str:
    return "%s|%s" % (r.get("op") or "", r.get("detail") or "")


def _filter_hidden_logs(project_root: str, rows: list) -> list:
    st = _log_hidden_state(project_root)
    if not st["keys"] and not st["cleared_before"]:
        return rows
    keys = set(st["keys"])
    cb = st["cleared_before"]
    out = []
    for r in rows:
        if _log_key(r) in keys:
            continue                      # 单条删除
        if cb and float(r.get("mtime") or 0) <= cb:
            continue                      # 全部清除：清空前生成的行都不再显示
        out.append(r)
    return out


def build_overview(cfg: dict[str, Any], project_override: Optional[str] = None) -> dict[str, Any]:
    project = resolve_project(cfg, project_override)
    use_project = bool(project_override)
    article_dir = data_dir(cfg, project, "article", use_project)
    response_dir = data_dir(cfg, project, "response", use_project)
    article_n, article_src = count_records(article_dir)
    response_n, response_src = count_records(response_dir)
    device, hint = device_info(cfg)
    size_bytes = dir_size(project)
    # 仓库手动添加单独成行（op=「添加」）、模型操作单独成行（转换导入/转换导出/转换/导出，2026-10-21）；
    # 同一文件的派生扫描行（蒸馏/训练，含 checkpoints 扫描猜的笼统「训练」）让位，避免一条文件两行
    repo_rows = _repo_logs(project)
    conv_rows = _conv_logs(project)
    keep_paths = {os.path.normcase(os.path.abspath(r["path"])) for r in (repo_rows + conv_rows) if r["path"]}
    logs = [r for r in (_distill_logs(project) + _train_logs(cfg, project))
            if os.path.normcase(os.path.abspath(str(r.get("path") or ""))) not in keep_paths]
    logs = conv_rows + repo_rows + logs
    logs = _filter_hidden_logs(project, logs)   # 单条删除/全部清除（2026-10-16）
    logs.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    logs = logs[:30]
    for r in logs:
        r["time"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("mtime") or 0))
        r["path"] = str(r.get("path") or "")
        r["mtime"] = int(r.get("mtime") or 0)
    return {
        "article": int(article_n),
        "response": int(response_n),
        "device": device,
        "device_hint": hint,
        "model": model_info(project),
        "storage": {"bytes": int(size_bytes), "text": human_size(size_bytes), "path": project},
        "logs": logs,
        "_meta": {
            "project": project,
            "article_dir": article_dir,
            "response_dir": response_dir,
            "count_source": {"article": article_src, "response": response_src},
            # 诊断用：总览这次用的 device 原值 + 配置文件 mtime（证明每次重读盘）
            "device_raw": (cfg.get("train") or {}).get("device"),
            "cfg_mtime": (os.path.getmtime(CONFIG_PATH_DBG) if os.path.isfile(CONFIG_PATH_DBG) else None),
        },
    }


# ---------------------------------------------------------------- HTTP 服务

class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, cfg: dict[str, Any], project_override: Optional[str]):
        super().__init__(addr, handler)
        self.cfg = cfg
        self.project_override = project_override

    def overview(self) -> dict[str, Any]:
        # 每次读盘取配置：设置页改了 train.device 之后总览要立刻跟上，
        # 不能吃 self.cfg（那是桥启动时的快照 → 设置选 CPU、总览还显示 XPU 的根源）。
        try:
            api_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_api")
            if api_dir not in sys.path:
                sys.path.insert(0, api_dir)
            from _host import load_cfg as _load_cfg      # _host 的唯一口径（同 config.json 路径）
            cfg = _load_cfg()
        except Exception as exc:             # noqa: BLE001  读不到就退回启动快照
            sys.stderr.write("[overview] 重读 config 失败，用启动快照：%r\n" % (exc,))
            cfg = self.cfg
        return build_overview(cfg, self.project_override)


# ------------------------------------------------ 页面接口插件（bridge_api/*.py）
# 每个页面把自己的后端接口写成一个模块放进 bridge_api/，模块提供 register(router)：
#     def register(router):
#         router.get("/api/article/list", lambda q: {...})
#         router.post("/api/article/distill", lambda body: {...})
# 加载失败只记一笔，不让主服务起不来。
PLUGIN_GET: dict = {}
PLUGIN_POST: dict = {}
PLUGIN_ERRORS: list = []


# 2026-10-23（用户）：桥接只认自己人 ----
# ① 启动时生成一次性随机 token，随 `PORT <n> <token>` 握手行交给主进程，渲染层每个请求带 X-EAI-Token；
# ② Host 只认 127.0.0.1/localhost/[::1] + 本端口（挡 DNS rebinding）；
# ③ Origin 只放行 null / file://（Electron 渲染层），CORS 头不再发 `*`。
AUTH_TOKEN = ""

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")


class SimpleRouter:
    """插件的极简路由表：get(path, fn) / post(path, fn)。"""

    def __init__(self) -> None:
        self.gets: dict = {}
        self.posts: dict = {}

    def get(self, path: str, fn) -> None:
        self.gets[path] = fn

    def post(self, path: str, fn) -> None:
        self.posts[path] = fn


def load_plugins() -> None:
    import importlib
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_api")
    if not os.path.isdir(d):
        return
    if d not in sys.path:
        sys.path.insert(0, d)
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        try:
            mod = importlib.import_module(fn[:-3])
            router = SimpleRouter()
            mod.register(router)
            PLUGIN_GET.update(router.gets)
            PLUGIN_POST.update(router.posts)
        except Exception as exc:  # noqa: BLE001
            PLUGIN_ERRORS.append("%s: %s" % (fn, exc))
            print("[bridge] 插件 %s 加载失败: %s" % (fn, exc), file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "EAIWebBridge/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _send(self, code: int, obj: Any) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": f"序列化失败: {exc}"}).encode("utf-8")
            code = 500
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端先走了，忽略

    # ---- 鉴权 ----
    def _send_cors(self) -> None:
        """只给本应用回 CORS 头（file:// 页面的 Origin 是 `null`）；没有 Origin 的调用（主进程、
        命令行）本来就不需要 CORS，一个头也不发 —— 再也不发 `*`（用户 2026-10-23）。"""
        if getattr(self, "_no_cors", False):   # 拒绝请求时不回任何 CORS 头
            return
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return
        allow = origin if origin.lower().startswith("file://") else "null"
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-EAI-Token")

    def _host_ok(self) -> bool:
        """Host 必须是本机回环 + 本服务端口，否则 DNS rebinding 把外站域名指到 127.0.0.1 就能读。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        port = str(self.server.server_address[1])   # type: ignore[attr-defined]
        return host in tuple("%s:%s" % (h, port) for h in _ALLOWED_HOSTNAMES)

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        return (origin == "" or origin == "null" or origin.startswith("file://"))

    def _deny(self, why: str) -> None:
        self._no_cors = True
        self._send(403, {"error": "forbidden: " + why})

    def _authorize(self) -> bool:
        """三道门：Host → Origin → token。任一不过直接 403 并断掉这次请求。"""
        if not self._host_ok():
            self._deny("bad host")
            return False
        if not self._origin_ok():
            self._deny("bad origin")
            return False
        tok = (self.headers.get("X-EAI-Token") or "").strip()
        if (not AUTH_TOKEN) or tok != AUTH_TOKEN:
            self._deny("bad token")
            return False
        return True

    def _query(self) -> dict:
        from urllib.parse import parse_qs, urlparse
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        path = self.path.split("?", 1)[0]
        if path in PLUGIN_GET:                       # 页面插件优先
            try:
                self._send(200, PLUGIN_GET[path](self._query()))
            except Exception as exc:  # noqa: BLE001
                if _is_no_project(exc):
                    self._send(200, {"ok": False, "error": str(exc), "no_project": True})
                else:
                    _log_exc("GET " + path, exc, plugin="get")
                    self._send(500, {"error": str(exc), "type": type(exc).__name__})
            return
        try:
            if path == "/api/health":
                self._send(200, {"ok": True, "pid": os.getpid()})
            elif path == "/api/overview":
                self._send(200, self.server.overview())  # type: ignore[attr-defined]
            elif path == "/api/plugins":
                self._send(200, {"get": sorted(PLUGIN_GET), "post": sorted(PLUGIN_POST),
                                 "errors": PLUGIN_ERRORS})
            elif path == "/api/info":
                ov = self.server.overview()  # type: ignore[attr-defined]
                self._send(200, {
                    "pid": os.getpid(),
                    "config_path": DEFAULT_CONFIG_PATH,
                    "project": ov["_meta"]["project"],
                    "article_dir": ov["_meta"]["article_dir"],
                    "response_dir": ov["_meta"]["response_dir"],
                    "count_source": ov["_meta"]["count_source"],
                    "python": sys.version.split()[0],
                })
            else:
                self._send(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001  任何异常都变成 500 JSON，不让服务崩
            if _is_no_project(exc):                      # 首启没工程：200 + 空态，不刷红字
                self._send(200, {"ok": False, "error": str(exc), "no_project": True})
            else:
                _log_exc("GET " + path, exc)
                self._send(500, {"error": str(exc), "type": type(exc).__name__})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        path = self.path.split("?", 1)[0]
        fn = PLUGIN_POST.get(path)
        if fn is None:
            self._send(404, {"error": "not found", "path": path})
            return
        nbytes = 0
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            nbytes = len(raw)
            if not raw:
                body = {}
            else:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception as exc:   # 别静默吞：坏 body 一律 400。
                    # 否则页面参数会「莫名不生效」—— 实测有人因此误起了 count=200 的采集。
                    self._send(400, {"error": "请求体不是合法 JSON: %s" % exc,
                                     "bytes": len(raw)})
                    return
            if not isinstance(body, dict):
                body = {"value": body}
            body["_query"] = self._query()
            self._send(200, fn(body))
        except Exception as exc:  # noqa: BLE001
            if _is_no_project(exc):
                self._send(200, {"ok": False, "error": str(exc), "no_project": True})
            else:
                _log_exc("POST " + path, exc, body_bytes=nbytes)   # 只记长度，不记请求体（含 api_key）
                self._send(500, {"error": str(exc), "type": type(exc).__name__})

    def do_OPTIONS(self) -> None:  # noqa: N802
        """只回本应用（用户 2026-10-23）：Host/Origin 不过就 403，过才给 204 + 具体头。"""
        if not self._host_ok():
            self._deny("bad host")
            return
        if not self._origin_ok():
            self._deny("bad origin")
            return
        self.send_response(204)
        self._send_cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # 静默：stdout 只留握手行
        pass


# ---------------------------------------------------------------- 入口

def _install_error_hooks() -> None:
    """未捕获异常也落盘（2026-10-24）：主线程 sys.excepthook + 子线程 threading.excepthook。

    兜底 try/except 覆盖不到的地方（线程里、回调里、事件循环外）也能在
    logs/bridge_error.log 里留下堆栈，而不是只朝 stderr 喷一行就没了。
    """
    mod = _errlog()
    if mod is None:
        return
    prev = sys.excepthook

    def _hook(exc_type, exc, tb):                  # noqa: ANN001
        try:
            mod.log_exc("uncaught", exc, tb=tb)
        except Exception:                          # noqa: BLE001
            pass
        prev(exc_type, exc, tb)

    sys.excepthook = _hook
    try:
        import threading as _th                    # noqa: PLC0415

        def _th_hook(args):                        # noqa: ANN001
            try:
                name = getattr(getattr(args, "thread", None), "name", "?")
                mod.log_exc("thread:" + str(name), args.exc_value, tb=args.exc_traceback)
            except Exception:                      # noqa: BLE001
                pass

        _th.excepthook = _th_hook                  # type: ignore[assignment]
    except Exception:                              # noqa: BLE001
        pass


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="EAI Studio Python 数据桥")
    ap.add_argument("--port", type=int, default=0, help="监听端口，0=系统分配")
    ap.add_argument("--host", default="127.0.0.1", help="只绑本机")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="config.json 路径")
    ap.add_argument("--project", default="", help="覆盖当前工程目录（调试/测试用）")
    ap.add_argument("--selftest", action="store_true", help="打印 overview JSON 后退出")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    override = args.project or None

    if args.selftest:
        print(json.dumps(build_overview(cfg, override), ensure_ascii=False, indent=2))
        return 0

    load_plugins()
    # 2026-10-18：旧版把工程数据写在 EAI_Studio/engine 里，启动时搬到 <用户目录>/EAI Projects/default
    try:
        from bridge_api import _host as _host_boot          # noqa: PLC0415
        moved = _host_boot.migrate_engine_data()
        if moved:
            sys.stderr.write("MIGRATE %s\n" % json.dumps(moved, ensure_ascii=False))
        # 2026-10-19（用户要求）：.eproj 用 EAI 应用同款图标（HKCU 级关联，不改系统）
        _host_boot.migrate_corpus_paths()      # 2026-10-19：合并语料挪到 <工程>/corpora
        _host_boot.register_project_icon(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "eai.ico"))
    except Exception as _exc:                               # noqa: BLE001
        sys.stderr.write("MIGRATE skipped: %s\n" % _exc)
    _install_error_hooks()
    global AUTH_TOKEN
    AUTH_TOKEN = secrets.token_urlsafe(24)      # 一次性随机 token（用户 2026-10-23）
    server = BridgeServer((args.host, args.port), Handler, cfg, override)
    port = server.server_address[1]
    # 握手：stdout 第一行必须是 PORT <n> <token>
    sys.stdout.write(f"PORT {port} {AUTH_TOKEN}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
