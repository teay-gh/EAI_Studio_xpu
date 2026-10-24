"""bridge_api/train_prune.py —— 「模型裁切」页桥接口（动作已接真后端）。

基于 llm-compressor 的模型裁切（SparseGPT / Wanda，稀疏化）。真子进程跑
engine/tools/compress_run.py（.venv_compress 独立环境，主 venv 的 XPU torch 不受影响）。
共享作业层见同目录 `_compress.py`（参数存取 / 启动 / 状态 / 停止）。

接口：
  GET  /api/train_prune/config   参数 + 压缩环境状态 + 产物探针
  GET  /api/train_prune/status   压缩子进程状态 + 增量日志（?since=<游标>）
  POST /api/train_prune/save     保存参数（logs/compress_params_prune.json）
  POST /api/train_prune/start    自动合并校准语料 → 起压缩子进程
  POST /api/train_prune/stop     停止（写 stop.flag + 强杀压缩子进程）

铁律：只返回真实读取/执行到的数据；失败 {"ok": false, "error": "..."}，绝不编造。
"""

from __future__ import annotations

import os
import sys
from typing import Any

API_DIR = os.path.dirname(os.path.abspath(__file__))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
import _compress  # noqa: E402

MODE = "prune"


def config(_query: dict) -> dict:
    try:
        return _compress.config(MODE)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def status(query: dict) -> dict:
    try:
        return _compress.status(MODE, int(query.get("since") or 0))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def save_cfg(body: dict) -> dict:
    try:
        return _compress.save_params(MODE, body or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def start(body: dict) -> dict:
    try:
        return _compress.start(MODE, body or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def stop(_body: dict) -> dict:
    try:
        return _compress.stop(MODE)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def register(router) -> None:
    router.get("/api/train_prune/config", config)
    router.get("/api/train_prune/status", status)
    router.post("/api/train_prune/save", save_cfg)
    router.post("/api/train_prune/start", start)
    router.post("/api/train_prune/stop", stop)
