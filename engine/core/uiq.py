"""跨线程 UI 调度：后台线程禁止直接碰 tkinter，统一入队由主线程执行。"""

from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

_q: queue.Queue[Callable[[], None]] = queue.Queue()
_installed = False


def call_ui(fn: Callable[[], None]) -> None:
    """任意线程安全：把 UI 更新投递到主线程。"""
    _q.put(fn)


def _drain(root: tk.Misc) -> None:
    try:
        while True:
            fn = _q.get_nowait()
            try:
                fn()
            except Exception:
                pass
    except queue.Empty:
        pass
    except Exception:
        pass
    try:
        if root.winfo_exists():
            root.after(50, lambda: _drain(root))
    except tk.TclError:
        pass


def install(root: tk.Misc) -> None:
    """在主线程安装排水泵（幂等）。"""
    global _installed
    if _installed:
        return
    _installed = True
    root.after(50, lambda: _drain(root))
