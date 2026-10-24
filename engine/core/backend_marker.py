#!/usr/bin/env python3
"""把 torch 后端能力落盘成 engine/torch_backend.json（EAI_Studio/engine/core/backend_marker.py）

写入方：训练子进程 `core/train.py`（解析到 device 那一刻）与桥里的推理路径 `bridge_api/chat.py`。
读取方：设置页设备卡（`bridge_api/settings.py::device`）与工作台总览（`bridge.py::device_info`）
        —— 那两处因此**完全不 import torch**（实测 torch 常驻 ≈830MB 工作集 / 1GB 私有，
        以前只要点开设置或工作台就会把 torch 拉进桥进程，设置页也要先卡 ~2s 的 import）。

torch 只在真正训练 / 对话推理时才加载；这个文件是「上次真探测的结果」，那份环境能力在本机是稳定的。
文件不存在 = 还没探测过：设置页如实显示「未知」（四个设备都可选），训练启动时按设置真解析。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = os.path.join(ENGINE_DIR, "torch_backend.json")


def read() -> dict:
    """读落盘的探测结果（没有 / 坏了都返回空 dict）。"""
    try:
        with open(MARKER, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                            # noqa: BLE001
        return {}


def note(torch_mod: Any) -> Optional[dict]:
    """探测当前 torch 的 cuda/xpu 能力并落盘；内容与上次一致时只跳过一次写盘。

    失败一律吞掉：这就是个给界面看的缓存，不能影响训练 / 推理本身。
    """
    try:
        try:
            cuda_ok = bool(torch_mod.cuda.is_available())
        except Exception:                        # noqa: BLE001
            cuda_ok = False
        try:
            xpu_ok = bool(getattr(torch_mod, "xpu", None) and torch_mod.xpu.is_available())
        except Exception:                        # noqa: BLE001
            xpu_ok = False
        ver = getattr(torch_mod, "version", None)
        data = {
            "backend": "cuda" if cuda_ok else ("xpu" if xpu_ok else "cpu"),
            "torch": str(getattr(torch_mod, "__version__", "")),
            "cuda_build": str(getattr(ver, "cuda", "") or ""),
            "xpu_available": xpu_ok,
            "cuda_available": cuda_ok,
            "xpu_name": str(torch_mod.xpu.get_device_name(0)) if xpu_ok else "",
            "cuda_name": str(torch_mod.cuda.get_device_name(0)) if cuda_ok else "",
            "threads": int(torch_mod.get_num_threads()),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        old = read()
        if old and all(old.get(k) == data[k] for k in data if k != "checked_at"):
            return data                          # 能力没变：不写盘
        with open(MARKER, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception:                            # noqa: BLE001
        return None
