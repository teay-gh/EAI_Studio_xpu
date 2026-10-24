"""Windows 窗口外壳：真·无边框（去掉系统白色标题栏）。

关键：overrideredirect 之后绝不能再把 WS_CAPTION 加回去，
否则系统标题栏会重现（表现为顶部白条）。
"""

from __future__ import annotations

import sys
import ctypes
from typing import Any

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_BORDER = 0x00800000
_WS_DLGFRAME = 0x00400000
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_DLGMODALFRAME = 0x00000001
_WS_EX_WINDOWEDGE = 0x00000100
_WS_EX_CLIENTEDGE = 0x00000200
_WS_EX_STATICEDGE = 0x00020000

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

# SPI_SETDRAGFULLWINDOWS：拖动时显示整窗内容，而不是橡皮筋灰框
_SPI_SETDRAGFULLWINDOWS = 37
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02


def force_full_window_drag() -> None:
    """关闭拖动橡皮筋轮廓（多层灰框的来源）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETDRAGFULLWINDOWS, 1, None,
            _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE,
        )
    except Exception:
        pass


def move_window(root: Any, x: int, y: int) -> None:
    """用 SetWindowPos 移动，避免 geometry 逐帧重绘留灰框。"""
    if sys.platform != "win32":
        try:
            root.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass
        return
    try:
        hwnd = _hwnd_of(root)
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, int(x), int(y), 0, 0,
            _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
        )
    except Exception:
        try:
            root.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def _hwnd_of(root: Any) -> int:
    root.update_idletasks()
    hwnd = root.winfo_id()
    try:
        parent = ctypes.windll.user32.GetParent(hwnd)
        if parent:
            hwnd = parent
    except Exception:
        pass
    return hwnd


def enable_custom_chrome(root: Any, rounded: bool = True) -> bool:
    """彻底去掉系统标题栏与所有边框（含 THICKFRAME）。

    若保留 WS_THICKFRAME，拖动窗口时 Windows 会画出多层灰框残留。
    缩放完全由应用自绘边缘命中检测完成。

    rounded=False（如全屏/最大化）时显式要求 DWM 不做圆角（DWMWCP_DONOTROUND）——
    无边框窗口是自己按屏幕尺寸摆的，系统不会自动判定「已最大化」，不显式关掉
    就一直是圆角，全屏时四角会露出桌面。
    """
    try:
        root.overrideredirect(True)
    except Exception:
        return False
    force_full_window_drag()
    if sys.platform != "win32":
        return True
    try:
        root.update_idletasks()
        hwnd = _hwnd_of(root)
        user32 = ctypes.windll.user32

        # 清掉全部非客户区样式：CAPTION / DIALOG / BORDER / THICKFRAME
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
        style &= ~(_WS_CAPTION | _WS_DLGFRAME | _WS_BORDER | _WS_THICKFRAME)
        user32.SetWindowLongW(hwnd, _GWL_STYLE, style)

        # 任务栏显示为应用窗口
        ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        ex |= _WS_EX_APPWINDOW
        ex &= ~_WS_EX_TOOLWINDOW
        ex &= ~(_WS_EX_DLGMODALFRAME | _WS_EX_WINDOWEDGE | _WS_EX_CLIENTEDGE | _WS_EX_STATICEDGE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex)

        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        )

        # DWM 不再外扩阴影/描边，避免拖动出现轮廓线
        try:
            class _MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(_MARGINS(0, 0, 0, 0))
            )
        except Exception:
            pass

        # Win11：DWM 合成圆角（系统级抗锯齿，无边框窗口同样生效）
        # 全屏/最大化时改 DONOTROUND，四角保持直角
        try:
            _DWMWA_WINDOW_CORNER_PREFERENCE = 33
            pref = ctypes.c_int(2 if rounded else 1)  # DWMWCP_ROUND / DWMWCP_DONOTROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref),
            )
        except Exception:
            pass
        return True
    except Exception:
        return False


def apply_window_chrome(root, theme: str, side: str, border: str) -> bool:
    """仅在使用系统边框时染色；无边框模式下为 no-op 安全调用。"""
    if sys.platform != "win32":
        return False
    try:
        hwnd = _hwnd_of(root)
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if theme == "dark" else 0)
        size = ctypes.sizeof(dark)
        hr = dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), size
        )
        if hr != 0:
            dwm.DwmSetWindowAttribute(
                hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY, ctypes.byref(dark), size
            )
        cap = ctypes.c_int(_colorref(side))
        dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_CAPTION_COLOR, ctypes.byref(cap), ctypes.sizeof(cap)
        )
        bor = ctypes.c_int(_colorref(border))
        dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_BORDER_COLOR, ctypes.byref(bor), ctypes.sizeof(bor)
        )
        return True
    except Exception:
        return False
