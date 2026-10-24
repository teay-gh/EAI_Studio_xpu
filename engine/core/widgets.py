"""共享 UI 组件 v2 ·「极光玻璃」设计语言。

- 真胶囊按钮：圆角 = 高/2，渐变主按钮（蓝→紫），文字直接画在 Canvas 上（永无方角）。
- 圆角卡片 14px、图标色片、英雄页头（渐变辉光）、圆角输入井、分段胶囊标签。
- 自绘控件注册进 _registry：主题热切换按「旧色→新色」映射重染重绘。
- 展开/折叠统一走 animate_height / animate_width，时长约 140ms。
"""

from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
import weakref
from typing import Any, Callable

from . import theme as T
from .tooltips import GLOSSARY, ToolTip, tip  # noqa: F401  (ToolTip 供外部引用)

# ============================================================
# 主题热切换钩子：自绘控件按映射表重染
# ============================================================

_registry: "list[weakref.ref]" = []


def _register(widget: tk.Widget) -> None:
    _registry.append(weakref.ref(widget))


def _remap_all() -> None:
    mapping = T.last_mapping()
    if not mapping:
        return
    for ref in list(_registry):
        w = ref()
        if w is None:
            _registry.remove(ref)
            continue
        # 通用兜底：建时捕获的颜色字段先按映射表重染，
        # 防止主题往返后重绘沿用旧主题色（圆角后面露白底的根因）
        for attr in ("_mask", "_bg", "_border", "_ink"):
            try:
                val = getattr(w, attr)
            except Exception:
                continue
            if isinstance(val, str) and val in mapping:
                try:
                    setattr(w, attr, mapping[val])
                except Exception:
                    pass
        try:
            w._theme_remap(mapping)  # type: ignore[attr-defined]
        except Exception:
            pass


T.register_redraw(_remap_all)


def _mapped(color: str, mapping: dict[str, str]) -> str:
    return mapping.get(color, color) if color else color


# ============================================================
# 动画工具
# ============================================================

ANIM_MS = 140
ANIM_STEPS = 8


def animate_height(widget: tk.Widget, start: int, end: int, *,
                   done: Callable[[], None] | None = None, ms: int = ANIM_MS) -> None:
    """把 widget 高度从 start 平滑改到 end（用于展开/折叠）。"""
    if start == end:
        if done:
            done()
        return
    steps = max(1, ANIM_STEPS)
    delta = (end - start) / steps
    state = {"i": 0, "cur": float(start)}

    def tick() -> None:
        state["i"] += 1
        if state["i"] >= steps:
            try:
                widget.configure(height=end)
            except tk.TclError:
                pass
            if done:
                try:
                    done()
                except Exception:
                    pass
            return
        state["cur"] += delta
        try:
            widget.configure(height=max(0, int(state["cur"])))
            widget.after(max(1, ms // steps), tick)
        except tk.TclError:
            return

    try:
        widget.configure(height=max(0, start))
    except tk.TclError:
        pass
    widget.after(max(1, ms // steps), tick)


def animate_width(widget: tk.Widget, start: int, end: int, *,
                  done: Callable[[], None] | None = None, ms: int = ANIM_MS) -> None:
    if start == end:
        if done:
            done()
        return
    steps = max(1, ANIM_STEPS)
    delta = (end - start) / steps
    state = {"i": 0, "cur": float(start)}

    def tick() -> None:
        state["i"] += 1
        if state["i"] >= steps:
            try:
                widget.configure(width=end)
            except tk.TclError:
                pass
            if done:
                try:
                    done()
                except Exception:
                    pass
            return
        state["cur"] += delta
        try:
            widget.configure(width=max(0, int(state["cur"])))
            widget.after(max(1, ms // steps), tick)
        except tk.TclError:
            return

    try:
        widget.configure(width=max(0, start))
    except tk.TclError:
        pass
    widget.after(max(1, ms // steps), tick)


# ============================================================
# 圆角 / 渐变绘制（调用方自行 delete 画布或使用独立 tag）
# ============================================================
# 圆角 / 渐变绘制（PIL 超采样渲染 → 真抗锯齿边缘；调用方自行 delete 画布或使用独立 tag）
# ============================================================

_SS = 3  # 超采样倍数（3x 渲染后 BOX 降采样 = 平滑圆角）
_ICON_SS = 4  # 图标超采样倍数（4x + LANCZOS，字形/矢量边缘最干净）

# 形状图缓存：(参数) → PhotoImage。重绘命中缓存即零渲染成本。
_SHAPE_CACHE: "dict[tuple, Any]" = {}
_SHAPE_CACHE_MAX = 512
_SHAPE_CACHE_TK: Any = None


# —— 重绘合并：N 个控件的重绘排进同一次空闲批处理，单帧完成（消除逐个冒泡） ——
_pending_redraws: "dict[tuple, tuple[tk.Widget, str]]" = {}


def queue_call(widget: tk.Widget, method: str) -> None:
    key = (id(widget), method)
    if key in _pending_redraws:
        return
    _pending_redraws[key] = (widget, method)
    try:
        widget.after_idle(_flush_redraws)
    except tk.TclError:
        pass


def _flush_redraws() -> None:
    items = list(_pending_redraws.values())
    _pending_redraws.clear()
    for widget, method in items:
        try:
            getattr(widget, method)()
        except tk.TclError:
            pass


def redraw_tree(parent: tk.Widget) -> None:
    """把 parent 子树内所有自绘控件排进重绘批（首帧绘制前完成内容重绘）。

    页面切页时用：新页面挂载前多为未布局状态（画布只有 1px），
    先按真实几何重绘完再进绘制，消除页头黑闪与逐个冒出。
    """
    pid = str(parent)
    for ref in list(_registry):
        w = ref()
        if w is None:
            continue
        try:
            name = str(w)
        except tk.TclError:
            continue
        if not (name == pid or name.startswith(pid + ".")):
            continue
        if hasattr(w, "_redraw"):
            queue_call(w, "_redraw")


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = (color or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)
    except Exception:
        return (0, 0, 0, 0)


def _shape_photo(canvas: tk.Canvas, w: int, h: int, paint: Any, key: tuple,
                 ss: int | None = None, resample: Any = None) -> Any:
    """按 paint(img, S) 在 S 倍超采样 RGBA 上作画，降采样返回 PhotoImage。

    默认 3× + BOX；图标走 4× + LANCZOS（字形边缘更干净）。
    同参数结果走 LRU 缓存：重绘（悬停/缩放/切页）不再重复渲染。
    """
    photo = _SHAPE_CACHE.get(key)
    if photo is not None and _SHAPE_CACHE_TK is canvas.tk:
        return photo
    if _SHAPE_CACHE_TK is not canvas.tk:  # 换 Tk 解释器：旧图全部失效
        _SHAPE_CACHE.clear()
        globals()["_SHAPE_CACHE_TK"] = canvas.tk
    from PIL import Image, ImageTk
    S = int(ss or _SS)
    img = Image.new("RGBA", (max(1, w) * S, max(1, h) * S), (0, 0, 0, 0))
    paint(img, S)
    img = img.resize((max(1, w), max(1, h)), resample or Image.BOX)
    photo = ImageTk.PhotoImage(img)
    if len(_SHAPE_CACHE) >= _SHAPE_CACHE_MAX:
        for old in list(_SHAPE_CACHE)[:_SHAPE_CACHE_MAX // 2]:
            del _SHAPE_CACHE[old]
    _SHAPE_CACHE[key] = photo
    refs = getattr(canvas, "_shape_imgs", None)
    if refs is None:
        refs = []
        canvas._shape_imgs = refs  # type: ignore[attr-defined]
    refs.append(photo)  # 挂住引用，防止缓存淘汰后画布图元失图
    if len(refs) > 96:
        del refs[:64]
    return photo


def rounded_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
                 r: int, fill: str, outline: str = "", width: int = 1,
                 corners: str = "tlbr", tags: str = "bg") -> None:
    """圆角矩形（抗锯齿）；corners 控制哪几角圆角（如 'tltr' 上圆下方）。"""
    from PIL import ImageDraw
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return
    r = max(0, min(int(r), w // 2, h // 2))
    if corners == "tlbr":  # 默认简写 = 四角全圆
        cor = (True, True, True, True)
    else:
        cor = ("tl" in corners, "tr" in corners, "br" in corners, "bl" in corners)
    ow = max(1, int(width)) if outline else 0

    def paint(img: Any, S: int) -> None:
        d = ImageDraw.Draw(img)
        fill_c = _rgba(fill) if fill else None
        out_c = _rgba(outline) if outline else None
        if r > 0:
            d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], radius=r * S,
                                fill=fill_c, outline=out_c,
                                width=ow * S, corners=cor)
        else:
            d.rectangle([0, 0, w * S - 1, h * S - 1],
                        fill=fill_c, outline=out_c, width=ow * S)

    photo = _shape_photo(canvas, w, h, paint,
                         ("rrect", w, h, r, fill, outline, ow, corners))
    canvas.create_image(x1, y1, image=photo, anchor="nw", tags=tags)


def _rounded_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
                  r: int, fill: str, outline: str = "", width: int = 1) -> None:
    rounded_rect(canvas, x1, y1, x2, y2, r, fill, outline, width)


def corner_bite_photo(canvas: tk.Canvas, r: int, corner: str,
                      fill: str, bg: str) -> Any:
    """四角「咬口」图：弧内=fill、弧外=bg（超采样抗锯齿）。

    盖在内容层方角之上，补出圆角——内容 Frame 是方角且不透明，
    会盖住圆角底图的缺口，用咬口把缺口补回来。
    """
    from PIL import ImageDraw
    r = max(2, int(r))

    def paint(img: Any, S: int) -> None:
        W2 = r * S
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W2 - 1, W2 - 1], fill=_rgba(bg))
        if corner == "tl":
            box = [0, 0, 2 * W2, 2 * W2]
        elif corner == "tr":
            box = [-W2, 0, W2, 2 * W2]
        elif corner == "bl":
            box = [0, -W2, 2 * W2, W2]
        else:  # br
            box = [-W2, -W2, W2, W2]
        d.ellipse(box, fill=_rgba(fill))

    return _shape_photo(canvas, r, r, paint, ("bite", r, corner, fill, bg))


def _dpi_k(widget: tk.Widget) -> float:
    try:
        return max(widget.winfo_fpixels("1i") / 96.0, 0.75)
    except Exception:
        return 1.0


def btn_radius(canvas: tk.Canvas, h: int) -> int:
    """按钮 / 导航行 / 分段行的圆角半径（物理像素）。

    取三者中的最小值：
      1) h // 2         —— 不许超过半高；
      2) RADIUS_BTN×DPI —— 设计令牌值（8 逻辑px）；
      3) h × 0.25       —— 短行防胶囊。
    行高不随 DPI 缩放的控件（侧栏导航行 34px、按字体度量算高的按钮 ≈35px）
    若直接套 8×2 = 16px，在 34px 高的行上等于 h/2 → 画出来是胶囊，不是圆角矩形。
    """
    return max(2, min(h // 2, int(T.RADIUS_BTN * _dpi_k(canvas)),
                      max(2, round(h * 0.25))))

def _mix(c1: str, c2: str, t: float) -> str:
    """把 c1 向 c2 混合 t（0..1），用于渐变 / 阴影 / 悬停色。"""
    def rgb(c: str) -> tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    try:
        a, b = rgb(c1), rgb(c2)
    except Exception:
        return c1
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient_rounded(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
                     r: int, c1: str, c2: str, mask_bg: str,
                     tags: str = "grad", horizontal: bool = True) -> None:
    """渐变圆角矩形（PIL 线性渐变 + 超采样圆角，四角真抗锯齿）。"""
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    w, h = x2 - x1, y2 - y1
    if w <= 2 or h <= 2:
        return
    r = max(0, min(int(r), h // 2, w // 2))

    def paint(img: Any, S: int) -> None:
        W2, H2 = w * S, h * S
        grad = Image.new("RGBA", (W2, H2))
        d0 = ImageDraw.Draw(grad)
        a, b = _rgba(c1), _rgba(c2)
        if horizontal:
            for xx in range(W2):
                t = xx / max(1, W2 - 1)
                d0.line([(xx, 0), (xx, H2 - 1)],
                        fill=tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(4)))
        else:
            for yy in range(H2):
                t = yy / max(1, H2 - 1)
                d0.line([(0, yy), (W2 - 1, yy)],
                        fill=tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(4)))
        mask = Image.new("L", (W2, H2), 0)
        dm = ImageDraw.Draw(mask)
        if r > 0:
            dm.rounded_rectangle([0, 0, W2 - 1, H2 - 1], radius=r * S, fill=255)
        else:
            dm.rectangle([0, 0, W2 - 1, H2 - 1], fill=255)
        img.paste(grad, (0, 0), mask)

    photo = _shape_photo(canvas, w, h, paint,
                         ("grad", w, h, r, c1, c2, horizontal))
    canvas.create_image(x1, y1, image=photo, anchor="nw", tags=tags)


class RoundedFrame(tk.Frame):
    """圆角容器：Canvas 画底 + 内容 Frame。children 放到 .content。

    shadow>0 时右下留出该像素画投影层（分层效果），卡体占 (0,0,w-sp,h-sp)。
    gradient=(c1,c2) 时卡体为渐变底（渐变的圆角遮罩色取容器自身底色）。
    """

    def __init__(self, parent: tk.Widget, radius: int = T.RADIUS_CARD,
                 bg: str | None = None, border: str | None = None,
                 borderwidth: int = 1, shadow: int = 0,
                 gradient: tuple[str, str] | None = None, **kw: Any):
        self._radius = radius
        self._bg = bg or T.CARD
        self._border = border if border is not None else T.LINE
        self._bw = borderwidth if self._border else 0
        self._shadow = max(0, int(shadow))
        self._corners = "tlbr"
        self._grad = gradient
        fbg = _bg_of(parent)
        super().__init__(parent, bg=fbg, highlightthickness=0, **kw)
        self.canvas = tk.Canvas(self, bg=fbg, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.content = tk.Frame(self, bg=self._bg)
        sp = self._shadow
        # content 用 pack：其请求尺寸会传给外框（place 不会），并为投影留出右下边距
        self.content.pack(fill="both", expand=True,
                          padx=(0, sp) if sp else 0,
                          pady=(0, sp) if sp else 0)
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        # 四角咬口小画布：盖住 content 方角，保住圆角（content 是方角不透明 Frame）
        self._bite_c: dict[str, tk.Canvas] = {}
        self._sync_bites()
        _register(self)

    def _sync_bites(self) -> None:
        r = self._radius
        sp = self._shadow
        fbg = _bg_of(self)
        for corner in ("tl", "tr", "br", "bl"):
            want = r >= 2 and (self._corners == "tlbr" or corner in self._corners)
            bc = self._bite_c.get(corner)
            if not want:
                if bc is not None:
                    bc.place_forget()
                continue
            if bc is None:
                bc = tk.Canvas(self, width=r, height=r, bg=fbg,
                               highlightthickness=0, bd=0)
                self._bite_c[corner] = bc
            bc.configure(width=r, height=r, bg=fbg)

    def _paint_bites(self, cw: int, ch: int) -> None:
        fbg = _bg_of(self)
        r = self._radius
        for corner, bc in self._bite_c.items():
            try:
                # 绝对定位（相对定位在窗口定型后不重算，会滞留在旧尺寸）
                bx = 0 if corner in ("tl", "bl") else max(0, cw - r)
                by = 0 if corner in ("tl", "tr") else max(0, ch - r)
                bc.place(x=bx, y=by)
                bc.delete("all")
                bc.configure(bg=fbg, width=r, height=r)
                photo = corner_bite_photo(bc, r, corner, self._bg, fbg)
                bc.create_image(0, 0, image=photo, anchor="nw", tags="bite")
            except tk.TclError:
                pass

    def _redraw(self, _e: Any = None) -> None:
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        sp = self._shadow
        cw, ch = (w - sp, h - sp) if sp else (w, h)
        self.canvas.delete("all")
        if sp:
            for i, t in enumerate((0.42, 0.26, 0.14)):
                off = sp * (0.28 + 0.28 * i)
                rounded_rect(self.canvas, int(off), int(off),
                             int(cw + off * 0.4), int(ch + off * 0.4),
                             self._radius, _mix(self._bg, T.SHADOW, t), "",
                             tags=f"sh{i}")
        if self._grad:
            fbg = _bg_of(self.master)
            gradient_rounded(self.canvas, 0, 0, cw, ch, self._radius,
                             self._grad[0], self._grad[1], fbg, tags="bg")
            if self._bw:
                rounded_rect(self.canvas, 0, 0, cw, ch, self._radius, "",
                             self._border, self._bw, tags="border")
        elif sp == 0:
            # 纯色、无投影：这块「全尺寸圆角底图」100% 不可见，不必栅格化。
            # 内容层 content 是不透明方角 Frame，铺满整块；四角由 _paint_bites 的
            # r×r 咬口补出（带抗锯齿），连 1px 边框也是被内容层盖住的
            # （实测边缘扫描：父底 #F5F7FC → 抗锯齿过渡 → 卡面 #FCFCFE，全程无 #DDE2F0）。
            # 于是不再生成 w×h×3× 的超采样大图（1425×1162 那张要 97.7 ms），
            # 只用一条原生矩形兜住底色，像素与优化前逐点一致。
            self.canvas.create_rectangle(0, 0, max(0, cw - 1), max(0, ch - 1),
                                         fill=self._bg, outline="", tags="bg")
        else:
            rounded_rect(self.canvas, 0, 0, cw, ch, self._radius, self._bg,
                         self._border if self._bw else "", self._bw,
                         corners=self._corners, tags="bg")
        self._paint_bites(cw, ch)

    def set_colors(self, bg: str | None = None, border: str | None = None) -> None:
        if bg:
            self._bg = bg
            self.canvas.configure(bg=bg)
            self.content.configure(bg=bg)
        if border is not None:
            self._border = border
        self._redraw()

    def set_corners(self, corners: str) -> None:
        self._corners = corners
        self._sync_bites()
        self._redraw()

    def _theme_remap(self, mapping: dict[str, str]) -> None:
        self._bg = _mapped(self._bg, mapping)
        self._border = _mapped(self._border, mapping)
        if self._grad:
            self._grad = (_mapped(self._grad[0], mapping), _mapped(self._grad[1], mapping))
        try:
            self.canvas.configure(bg=_bg_of(self.master) if self._shadow == 0 else self._bg)
        except Exception:
            pass
        self._redraw()


class Collapsible(tk.Frame):
    """可折叠区块：标题行 + 内容，展开/折叠带动画。"""

    def __init__(self, parent: tk.Widget, title: str, *,
                 expanded: bool = True, bg: str | None = None,
                 on_toggle: Callable[[bool], None] | None = None,
                 tip_text: str | None = None):
        self._bg = bg or T.CARD
        super().__init__(parent, bg=self._bg, highlightthickness=0)
        self.expanded = expanded
        self._on_toggle = on_toggle

        self.header = tk.Frame(self, bg=self._bg, cursor="hand2")
        self.header.pack(fill="x")
        self.arrow = tk.Label(
            self.header, text="▾" if expanded else "▸", bg=self._bg, fg=T.INK3,
            font=(T.UI, T.FS_CAPTION), width=2, anchor="w", cursor="hand2",
        )
        self.arrow.pack(side="left", padx=(2, 0))
        self.title_lbl = tk.Label(
            self.header, text=title, bg=self._bg, fg=T.INK,
            font=(T.UI, T.FS_H3, "bold"), anchor="w", cursor="hand2",
        )
        self.title_lbl.pack(side="left", fill="x", expand=True)

        self._wrap = tk.Frame(self, bg=self._bg)
        self.content = tk.Frame(self._wrap, bg=self._bg)
        self.content.pack(fill="both", expand=True, pady=(4, 0))
        if expanded:
            self._wrap.pack(fill="both", expand=True)

        for w in (self.header, self.arrow, self.title_lbl):
            w.bind("<Button-1>", lambda e: self.toggle())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        if tip_text:
            tip(self.header, tip_text)
        _register(self)

    def _hover(self, on: bool) -> None:
        bg = T.SIDE_HOVER if on else self._bg
        for w in (self.header, self.arrow, self.title_lbl):
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass

    def toggle(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        if self.expanded:
            return
        self.expanded = True
        self.arrow.configure(text="▾")
        self._wrap.pack(fill="both", expand=True, after=self.header)
        self._wrap.update_idletasks()
        h = self._wrap.winfo_reqheight()
        self._wrap.pack_propagate(False)
        self._wrap.configure(height=1)
        animate_height(self._wrap, 1, h, done=lambda: self._wrap.pack_propagate(True))
        if self._on_toggle:
            self._on_toggle(True)

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.expanded = False
        self.arrow.configure(text="▸")
        h = max(self._wrap.winfo_height(), 1)
        self._wrap.pack_propagate(False)
        self._wrap.configure(height=h)

        def done() -> None:
            self._wrap.pack_forget()
            self._wrap.configure(height=0)
            self._wrap.pack_propagate(True)

        animate_height(self._wrap, h, 0, done=done)
        if self._on_toggle:
            self._on_toggle(False)

    def _theme_remap(self, mapping: dict[str, str]) -> None:
        self._bg = _mapped(self._bg, mapping)
        for w in (self, self.header, self.arrow, self.title_lbl, self._wrap, self.content):
            try:
                w.configure(bg=self._bg)
            except tk.TclError:
                pass


class HelpDot(tk.Frame):
    """「?」帮助角标（自绘）：圆角小片 + 字形，与按钮/图标同一圆角语言。"""

    def __init__(self, parent: tk.Widget, key_or_text: str):
        self._bg = _bg_of(parent)
        super().__init__(parent, bg=self._bg)
        self._own_colors = True  # 被 tint 时只换遮罩底色，不动角标配色
        self._k = max(self.winfo_fpixels("1i") / 96.0, 0.75)
        s = max(int(17 * self._k), 12)
        self._s = s
        self.canvas = tk.Canvas(self, width=s, height=s, bg=self._bg,
                                highlightthickness=0, bd=0, cursor="question_arrow")
        self.canvas.pack()
        tip(self, key_or_text)
        self._redraw()
        _register(self)

    def _redraw(self, _e: Any = None) -> None:
        try:
            c = self.canvas
            c.delete("all")
            s = self._s
            r = max(int(s * 0.30), 3)
            rounded_rect(c, 0, 0, s, s, r, T.ACCENT_SOFT, None, 1,
                         self._bg, tags="bg")
            center_text(c, s / 2, s / 2, text="?",
                        font=(T.UI, -max(int(s * 0.60), 8), "bold"),
                        fill=T.ACCENT_TEXT, tags="q")
        except tk.TclError:
            pass

    def _theme_remap(self, mapping: dict[str, str]) -> None:
        self._bg = _mapped(self._bg, mapping)
        try:
            self.configure(bg=self._bg)
            self.canvas.configure(bg=self._bg)
        except tk.TclError:
            pass
        self._redraw()


def help_dot(parent: tk.Widget, key_or_text: str) -> HelpDot:
    """小圆角「?」提示角标，挂在标题旁。"""
    return HelpDot(parent, key_or_text)


# ============================================================
# 自绘线性图标（活动栏 / 侧栏 / 标题栏 / 图标色片）
# ============================================================

_ICON_KINDS = (
    "home", "layers", "play", "chat", "graph", "database", "spark",
    "gear", "ai", "folder", "nav", "theme", "sun", "moon", "plus", "minus", "clock",
    "close", "up", "search", "panel", "dot", "send", "square", "restore",
    "sparkle", "win_min", "win_max", "win_close", "win_restore",
)

# 纯矢量自绘（不需要图标字体）的图标族
_VECTOR_KINDS = ("dot", "sun", "moon", "ai", "sparkle",
                 "win_min", "win_max", "win_close", "win_restore")


def _pick_icon_font() -> str:
    """图标字体：Segoe Fluent/MDL2 = Windows 自带矢量轮廓，ClearType 抗锯齿。"""
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
            if f in fams:
                return f
    except Exception:
        pass
    return "Segoe UI Symbol"


_ICON_FONT = _pick_icon_font()

# 图标字形（经逐个视觉核对）："dot" 走实心圆点绘制。
_ICON_GLYPH = {
    "home":     "\uE80F",  # 房子
    "layers":   "\uE81E",  # 层叠
    "play":     "\uE768",  # 播放
    "chat":     "\uE90A",  # 对话气泡
    "graph":    "\uE9D2",  # 趋势图
    "database": "\uE968",  # 数据库圆柱
    "spark":    "\uE735",  # 星形
    "gear":     "\uE713",  # 齿轮
    "ai":       "\uE99A",  # 机器人
    "folder":   "\uE8B7",  # 文件夹
    "nav":      "\uE700",  # 汉堡菜单
    "theme":    "\uE706",  # 亮度
    "plus":     "\uE710",  # 加
    "minus":    "\uE738",  # 减
    "clock":    "\uE823",  # 时钟
    "close":    "\uE711",  # 关闭
    "up":       "\uE74A",  # 上箭头
    "search":   "\uE721",  # 放大镜
    "panel":    "\uE90D",  # 侧栏布局
    "send":     "\uE724",  # 发送
    "square":   "\uE922",  # 最大化
    "restore":  "\uE923",  # 还原
}


def center_text(c: tk.Canvas, x: float, y: float, **kw: Any) -> int:
    """create_text 并按墨迹 bbox 精确居中到 (x, y)。

    create_text 的锚点是版面盒（ascent+descent）中心，不是字形墨迹中心，
    直接写 w/2,h/2 会偏 1~2px；这里建文后按 bbox 把墨迹中心移到 (x, y)。
    """
    tid = c.create_text(x, y, **kw)
    try:
        bb = c.bbox(tid)
    except tk.TclError:
        bb = None
    if bb:
        c.move(tid, x - (bb[0] + bb[2]) / 2, y - (bb[1] + bb[3]) / 2)
    return tid


_ICON_TTF: "dict[str, Any]" = {}   # {"path": ttf 路径, "fonts": {px: FreeTypeFont}}


def _icon_ttf() -> str | None:
    """Segoe Fluent Icons / MDL2 的 ttf 路径（PIL 直接渲染矢量轮廓，自带抗锯齿）。"""
    if "path" not in _ICON_TTF:
        import os
        root = os.environ.get("WINDIR") or r"C:\Windows"
        path = None
        for name in ("SegoeIcons.ttf", "segmdl2.ttf"):
            p = os.path.join(root, "Fonts", name)
            if os.path.exists(p):
                path = p
                break
        _ICON_TTF["path"] = path
        _ICON_TTF["fonts"] = {}
    return _ICON_TTF["path"]  # type: ignore[return-value]


def _icon_font(px: int) -> Any:
    """按像素高取 FreeType 字体对象（缓存；环境无 ttf 时返回 None）。"""
    p = _icon_ttf()
    if not p:
        return None
    fonts = _ICON_TTF.setdefault("fonts", {})
    key = int(px)
    f = fonts.get(key)
    if f is None:
        try:
            from PIL import ImageFont
            f = ImageFont.truetype(p, key)
        except Exception:
            f = False
        fonts[key] = f
    return f or None


def _icon_glyph_paint(d: Any, kind: str, S: int, col: Any, ratio: float) -> None:
    """把图标字形画进超采样位图：字号按墨迹高度 ratio·S 自适应，再按墨迹盒居中。"""
    glyph = _ICON_GLYPH.get(kind, "")
    if not glyph:
        return
    px0 = max(4, int(S * 0.8))
    f0 = _icon_font(px0)
    if f0 is None:
        return
    bb = f0.getbbox(glyph)
    gh = max(1, bb[3] - bb[1])
    f = _icon_font(max(4, int(round(px0 * (S * ratio) / gh)))) or f0
    x0, y0, x1, y1 = f.getbbox(glyph)
    d.text(((S - (x1 - x0)) / 2 - x0, (S - (y1 - y0)) / 2 - y0),
           glyph, font=f, fill=col)


def _theme_icon_paint(img: Any, kind: str, S: int, col: Any, ss: int = 3) -> None:
    """主题图标（PIL 矢量绘制，超采样抗锯齿）。

    sun  = 实心圆 + 8 道圆头射线（PIL 线段是平头，两端补圆点）；
    moon = 双圆相减的月牙，用「遮罩相减」而不是拿底色圆去盖——
           图标位图是透明底，盖底色会拖出一块色斑。
    """
    from PIL import Image, ImageDraw
    cx = cy = S / 2
    d = ImageDraw.Draw(img)
    if kind == "sun":
        r = S * 0.20
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
        w = max(2.0, S * 0.075)
        r0, r1 = S * 0.33, S * 0.44
        for i in range(8):
            a = math.radians(i * 45.0)
            ca, sa = math.cos(a), math.sin(a)
            x0, y0 = cx + r0 * ca, cy + r0 * sa
            x1, y1 = cx + r1 * ca, cy + r1 * sa
            d.line([x0, y0, x1, y1], fill=col, width=int(round(w)))
            for qx, qy in ((x0, y0), (x1, y1)):  # 圆头线帽
                d.ellipse([qx - w / 2, qy - w / 2, qx + w / 2, qy + w / 2], fill=col)
        return
    if kind != "moon":
        return
    r = S * 0.46
    off = r * 0.70
    cx += (r - off / 2) / 2      # 墨迹 x ∈ [cx-r, cx+off/2] → 右移补偿使墨迹居中
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    md.ellipse([cx + off - r, cy - r, cx + off + r, cy + r], fill=0)
    layer = Image.new("RGBA", (S, S), col)
    img.paste(layer, (0, 0), mask)


def _star4(d: Any, cx: float, cy: float, R: float, col: Any) -> None:
    """四角星（EProject 的 AI 图标 = 三颗这种星）：8 边形，内外半径比 0.384。"""
    pts = []
    for i in range(8):
        a = math.radians(i * 45.0 - 90.0)
        rr = R if i % 2 == 0 else R * 0.384
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=col)


def _sparkle_paint(img: Any, S: float, col: Any) -> None:
    """AI 助手图标 = EProject 活动栏那颗：大四角星 + 左右两颗小四角星。

    直接照搬 EProject index.html 里 <svg viewBox="0 0 24 24"> 的路径几何：
    大星 圆心(12,9) R=7，小星 圆心(18,17.5)/(6,17) R=3.5，内外半径比 0.384。
    """
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    u = 0.04 * S                                     # 24 单位坐标系 → 墨迹约 0.74·S
    ox, oy = S / 2 - 12.25 * u, S / 2 - 11.5 * u     # 按整簇墨迹盒居中
    _star4(d, 12 * u + ox, 9 * u + oy, 7 * u, col)
    _star4(d, 18 * u + ox, 17.5 * u + oy, 3.5 * u, col)
    _star4(d, 6 * u + ox, 17 * u + oy, 3.5 * u, col)


def _win_icon_paint(img: Any, kind: str, S: float, col: Any) -> None:
    """标题栏窗口按钮，完全按 EProject 实测几何：细线 + 圆角方框 + 长横线。

    S 是超采样后的画布边长，按 13 逻辑 px 画布换算：u = 1 逻辑 px。
    EProject 实测（200% DPI 物理px→逻辑px）：最小化 24×2→12×1、
    最大化 圆角方框 22×21 线宽 ~1.5→11×10.5 线宽 0.75、关闭 X 16×16 线宽 ~2→8×8 线宽 1。
    """
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    u = S / 13.0
    cx = cy = S / 2.0
    w = max(2, int(round(1.1 * u)))
    if kind == "win_min":
        L, T = 12.6 * u, 1.05 * u
        d.rounded_rectangle([cx - L / 2, cy - T / 2, cx + L / 2, cy + T / 2],
                            radius=T / 2, fill=col)
        return
    if kind == "win_max":
        h = 10.4 * u / 2
        d.rounded_rectangle([cx - h, cy - h, cx + h, cy + h],
                            radius=2.2 * u, outline=col, width=w)
        return
    if kind == "win_restore":
        # 「还原」= 一个**完整**前圆角方框 + 后框的开放路径（顶边 + 右上圆角 + 右边）。
        # 后框圆角与「前框右上角」**同心**：同一个圆心 (f1-rad_f, g0+rad_f)，
        #   半径 = 前框半径 + 两框间距(=2·off) —— 即外侧圆角按同心圆算。
        # 这样弧的外沿正好落在后框顶边 / 右边上，线宽天然对齐（PIL 的 arc 线宽往内画、
        # line 线宽居中画，所以顶边/右边中线都要让半个线宽）。
        from PIL import Image as _PILImage
        sd_f = 7.8 * u                       # 前框（四边形）边长
        off = 1.0 * u                        # 后框相对前框的右上偏移
        wpx = max(2, int(round(1.25 * u)))
        rad_f = 1.8 * u                      # 前框圆角半径（维持不变）
        rad_b = rad_f + 2 * off              # 后框圆角半径（同心 → + 两框间距）
        half = wpx / 2.0
        f0, f1 = cx - off - sd_f / 2, cx - off + sd_f / 2     # 前框 左/右
        g0, g1 = cy + off - sd_f / 2, cy + off + sd_f / 2     # 前框 上/下
        bl, bt = f0 + 2 * off, g0 - 2 * off                   # 后框 左上角
        br, bb = f1 + 2 * off, g1 - 2 * off                   # 后框 右下角
        ccx, ccy = f1 - rad_f, g0 + rad_f                     # 与前框右上角同心
        mask = _PILImage.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        # 顶边：后框左端 → 圆角圆心 x
        md.line([bl + wpx, bt + half, ccx, bt + half], fill=255, width=wpx)
        md.ellipse([bl + wpx - half, bt, bl + wpx + half, bt + wpx], fill=255)
        # 右上圆角：以 (ccx, ccy) 为圆心、半径 rad_b 的四分之一弧（外沿 = 后框外缘）
        md.arc([ccx - rad_b, ccy - rad_b, ccx + rad_b, ccy + rad_b],
               270, 360, fill=255, width=wpx)
        # 右边：圆角圆心 y → 后框底部略上
        y_end = bb - wpx
        md.line([br - half, ccy, br - half, y_end], fill=255, width=wpx)
        md.ellipse([br - wpx, y_end - half, br, y_end + half], fill=255)
        # 完整前框
        md.rounded_rectangle([f0, g0, f1, g1], radius=rad_f,
                             outline=255, width=wpx)
        img.paste(_PILImage.new("RGBA", img.size, col), (0, 0), mask)
        return
    # win_close：两条对角线（PIL 线段是平头，两端补圆点做圆头）
    # 实测 EProject 的 ✕ 字形只有 8 逻辑 px（比如它的 ▢ 是 11px），这里跟随它的比例。
    e = 7.6 * u / 2
    for x0, y0, x1, y1 in ((cx - e, cy - e, cx + e, cy + e),
                           (cx - e, cy + e, cx + e, cy - e)):
        d.line([x0, y0, x1, y1], fill=col, width=w)
        for qx, qy in ((x0, y0), (x1, y1)):
            d.ellipse([qx - w / 2, qy - w / 2, qx + w / 2, qy + w / 2], fill=col)


def icon_canvas(parent: tk.Widget, kind: str, size: int = 16,
                fg: str | None = None, bg: str | None = None,
                chip: str | None = None) -> tk.Canvas:
    """画简洁线性图标：PIL 矢量渲染（3× 超采样 → 降采样），任何尺寸都无锯齿。

    chip 给出时先画圆角色片底；图标位图是透明底，边缘由降采样抗锯齿。
    """
    color = fg or T.SIDE_TEXT
    back = bg or T.SIDE
    c = tk.Canvas(parent, width=size, height=size, bg=back,
                  highlightthickness=0, bd=0)
    c._icon = {"kind": kind, "fg": color, "bg": back, "size": size, "chip": chip}  # type: ignore[attr-defined]

    def draw() -> None:
        ic = c._icon  # type: ignore[attr-defined]
        kind = ic["kind"]
        c.delete("all")
        s = size
        chip_c = ic["chip"]
        col = _rgba(ic["fg"])
        chip_rgba = _rgba(chip_c) if chip_c else None
        if kind not in _VECTOR_KINDS and not _icon_ttf():
            # 兜底：找不到图标字体（异常环境）时退回 Tk 字形
            gl = _ICON_GLYPH.get(kind, "")
            if gl:
                c.create_text(s / 2, s / 2, text=gl, fill=ic["fg"],
                              font=(_ICON_FONT, max(5, int(s * 0.7))), tags="glyph")
            return

        def paint(img: Any, S: int) -> None:
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            if chip_rgba:
                d.rounded_rectangle([0, 0, s * S - 1, s * S - 1],
                                    radius=max(2, s // 4) * S, fill=chip_rgba)
            if kind == "dot":
                r = max(1.5, s * 0.16) * S
                d.ellipse([s * S / 2 - r, s * S / 2 - r,
                           s * S / 2 + r, s * S / 2 + r], fill=col)
            elif kind in ("sun", "moon"):
                _theme_icon_paint(img, kind, s * S, col, S)
            elif kind in ("win_min", "win_max", "win_close", "win_restore"):
                _win_icon_paint(img, kind, s * S, col)
            elif kind in ("ai", "sparkle"):
                _sparkle_paint(img, s * S, col)
            else:
                _icon_glyph_paint(d, kind, s * S, col, 0.58 if chip_rgba else 0.72)

        try:
            from PIL import Image as _PILImage
            photo = _shape_photo(c, s, s, paint,
                                 ("icon", kind, s, ic["fg"], chip_c),
                                 ss=_ICON_SS, resample=_PILImage.LANCZOS)
            c.create_image(0, 0, image=photo, anchor="nw", tags="glyph")
        except Exception:
            pass

    draw()
    c._icon_draw = draw  # type: ignore[attr-defined]
    _register(c)
    return c


def _icon_remap(self: tk.Canvas, mapping: dict[str, str]) -> None:
    icon = getattr(self, "_icon", None)
    if not icon:
        return
    icon["fg"] = _mapped(icon["fg"], mapping)
    icon["bg"] = _mapped(icon["bg"], mapping)
    if icon.get("chip"):
        icon["chip"] = _mapped(icon["chip"], mapping)
    try:
        self.configure(bg=icon["bg"])
        self._icon_draw()  # type: ignore[attr-defined]
    except tk.TclError:
        pass


# 把重绘钩子挂到 tk.Canvas 类（仅 icon_canvas 创建的实例带 _icon）
tk.Canvas._theme_remap = _icon_remap  # type: ignore[attr-defined]


def set_icon_kind(c: tk.Canvas, kind: str) -> None:
    icon = getattr(c, "_icon", None)
    if not icon:
        return
    icon["kind"] = kind
    try:
        c._icon_draw()  # type: ignore[attr-defined]
    except Exception:
        pass


def set_icon_colors(c: tk.Canvas, fg: str | None = None, bg: str | None = None,
                    chip: str | None = None) -> None:
    icon = getattr(c, "_icon", None)
    if not icon:
        return
    if fg:
        icon["fg"] = fg
    if bg:
        icon["bg"] = bg
    if chip is not None:
        icon["chip"] = chip
    try:
        c.configure(bg=icon["bg"])
        c._icon_draw()  # type: ignore[attr-defined]
    except tk.TclError:
        pass


def _bg_of(parent: tk.Widget) -> str:
    try:
        return parent.cget("bg")
    except Exception:
        return T.BG


def icon_chip(parent: tk.Widget, kind: str, size: int = 36,
              tone: str = "accent") -> tk.Canvas:
    """图标色片：浅色圆角方块 + 线性图标（页头 / 统计瓷片用）。"""
    tones = {
        "accent": (T.CARD_ALT, T.INK2),
        "grad": (T.CARD_ALT, T.INK2),
        "ok": (T.OK_SOFT, T.OK),
        "warn": (T.WARN_SOFT, T.WARN),
        "err": (T.ERR_SOFT, T.ERR),
        "info": (T.INFO_SOFT, T.INFO),
        "muted": (T.CARD_ALT, T.INK2),
    }
    chip_bg, chip_fg = tones.get(tone, tones["accent"])
    return icon_canvas(parent, kind, size, fg=chip_fg, bg=_bg_of(parent), chip=chip_bg)


class Card(RoundedFrame):
    """圆角玻璃卡。title_tip / sub_tip 可挂悬停说明。"""

    def __init__(
        self,
        parent: tk.Widget,
        title: str | None = None,
        subtitle: str | None = None,
        *,
        title_tip: str | None = None,
        sub_tip: str | None = None,
        icon: str | None = None,
        **kw: Any,
    ):
        super().__init__(parent, radius=T.RADIUS_CARD, bg=T.CARD, border=T.LINE, **kw)
        self.header: tk.Frame | None = None
        self._title_labels: list[tk.Label] = []
        self.body = tk.Frame(self.content, bg=T.CARD)
        if title:
            self.header = tk.Frame(self.content, bg=T.CARD)
            self.header.pack(fill="x", padx=T.PAD_CARD, pady=(T.PAD_CARD, 10))
            if icon:
                try:
                    icon_chip(self.header, icon, int(26), "muted").pack(side="left", padx=(0, 10))
                except Exception:
                    pass
            title_lbl = tk.Label(
                self.header, text=title, bg=T.CARD, fg=T.INK,
                font=(T.UI, T.FS_H3, "bold"), anchor="w",
            )
            title_lbl.pack(side="left")
            self._title_labels.append(title_lbl)
            if title_tip:
                tip(title_lbl, title_tip)
                help_dot(self.header, title_tip).pack(side="left", padx=(6, 0))
            if subtitle:
                sub_lbl = tk.Label(
                    self.header, text=subtitle, bg=T.CARD, fg=T.INK3,
                    font=(T.UI, T.FS_CAPTION), anchor="w",
                )
                sub_lbl.pack(side="left", padx=(10, 0))
                if sub_tip:
                    tip(sub_lbl, sub_tip)
        self.body.pack(fill="both", expand=True, padx=T.PAD_CARD,
                       pady=(8 if title else T.PAD_CARD, T.PAD_CARD))


class SectionLabel(tk.Frame):
    """侧栏分组标题。"""

    def __init__(self, parent: tk.Widget, text: str):
        super().__init__(parent, bg=T.SIDE)
        tk.Label(
            self, text=text.upper(), bg=T.SIDE, fg=T.SIDE_MUTED,
            font=(T.UI, T.FS_CAPTION, "bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 6))


class NavButton(tk.Frame):
    """侧栏导航项：圆角胶囊行 + 图标 + 文案，激活态渐变指示条。"""

    def __init__(self, parent: tk.Widget, icon: str, label: str, command: Callable[[], None],
                 tip_text: str | None = None):
        super().__init__(parent, bg=T.SIDE, cursor="hand2")
        self._command = command
        self._active = False
        self._hover = False
        self._icon = icon
        self._label = label
        self._paint_state: tuple | None = None
        self.canvas = tk.Canvas(self, bg=T.SIDE, highlightthickness=0, bd=0, height=54)
        self.canvas.pack(fill="both", expand=True)
        row = tk.Frame(self.canvas, bg=T.SIDE)
        self._row = row
        # 激活指示条：做成行内控件才看得见（画在画布上会被行内容压住）
        self.bar = tk.Frame(row, bg=T.SIDE, width=3, height=24)
        self.bar.pack(side="left", fill="y", padx=(9, 0), pady=0)
        self.bar.pack_propagate(False)
        if icon:                # icon="" → 这一行不要图标，文字与其它行对齐
            self.icon_w = icon_canvas(row, icon, 18, fg=T.SIDE_TEXT, bg=T.SIDE)
            self.icon_w.pack(side="left", padx=(12, 12))
        else:
            self.icon_w = None
        self.lbl = tk.Label(
            row, text=label, bg=T.SIDE, fg=T.SIDE_TEXT,
            font=(T.UI, T.FS_NAV_ROW), anchor="w", cursor="hand2",
        )
        self.lbl.pack(side="left", fill="x", expand=True,
                      padx=(10 if not icon else 0, 0))

        for w in (x for x in (self, self.canvas, self.lbl, row, self.icon_w)
                  if x is not None):
            w.bind("<Button-1>", lambda e: self._command())
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        if tip_text:
            tip(self, tip_text)
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    def _row_bg(self) -> str:
        if self._active:
            return T.SIDE_ACTIVE
        return T.SIDE_HOVER if self._hover else T.SIDE

    def _redraw(self) -> None:
        try:
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            bg = self._row_bg()
            # 状态没变就不重画：点击导航时只重绘真正变色的两行，
            # 避免整列逐行重绘造成的「逐个渲染」闪烁
            state = (w, h, bg, self._active)
            if state == self._paint_state:
                return
            self._paint_state = state
            r = btn_radius(self.canvas, h)
            self.canvas.delete("all")
            rounded_rect(self.canvas, 0, 0, w, h,
                         r, bg, "", tags="bg")
            # 行内容按行高垂直居中（原先贴顶，导航行图标/文字偏上 4.5px）
            rh = max(self._row.winfo_reqheight(), 1)
            self.canvas.create_window(0, max(0, (h - rh) // 2),
                                      window=self._row, anchor="nw", tags="row")
            self.configure(bg=T.SIDE)
            self.canvas.configure(bg=T.SIDE)
            self._row.configure(bg=bg)
            fg = T.SIDE_ACTIVE_FG if self._active else T.SIDE_TEXT
            self.lbl.configure(bg=bg, fg=fg)
            self.bar.configure(bg=T.ACCENT if self._active else bg)
            if self.icon_w is not None:
                set_icon_colors(self.icon_w, bg=bg, fg=fg)
            # 四角咬口：行内容是方角不透明 Frame，会盖住圆角缺口
            self._bite_paint(bg, w, h, r)
        except tk.TclError:
            pass

    def _bite_paint(self, row_bg: str, w: int, h: int, r: int) -> None:
        if not hasattr(self, "_bite_c"):
            self._bite_c = {}
        for corner in ("tl", "tr", "br", "bl"):
            bc = self._bite_c.get(corner)
            if bc is None:
                bc = tk.Canvas(self, width=r, height=r, bg=T.SIDE,
                               highlightthickness=0, bd=0)
                self._bite_c[corner] = bc
            bx = 0 if corner in ("tl", "bl") else max(0, w - r)
            by = 0 if corner in ("tl", "tr") else max(0, h - r)
            bc.place(x=bx, y=by)
            bc.configure(width=r, height=r, bg=T.SIDE)
            bc.delete("all")
            photo = corner_bite_photo(bc, r, corner, row_bg, T.SIDE)
            bc.create_image(0, 0, image=photo, anchor="nw", tags="bite")

    def _on_enter(self, _e: Any = None) -> None:
        if self._hover:
            return
        self._hover = True
        self._redraw()

    def _on_leave(self, _e: Any = None) -> None:
        if not self._hover:
            return
        self._hover = False
        self._redraw()

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._redraw()

    def _theme_remap(self, _mapping: dict[str, str]) -> None:
        self._redraw()


class PageHeader(tk.Frame):
    """英雄页头：渐变辉光横幅 + 图标色片 + 大标题 + 右侧操作区。"""

    def __init__(self, parent: tk.Widget, title: str, subtitle: str = "",
                 title_tip: str | None = None, icon: str = ""):
        self._bg = T.BG
        super().__init__(parent, bg=self._bg)
        self._k = max(self.winfo_fpixels("1i") / 96.0, 0.75)
        h = int(T.HERO_H * self._k)
        self.canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0,
                                height=h)
        self.canvas.pack(fill="x")
        self.canvas.pack_propagate(False)

        inner = tk.Frame(self.canvas, bg=T.CARD)
        self._inner = inner
        self.canvas.create_window(0, 0, window=inner, anchor="nw", tags="row")

        chip = None
        if icon:                # icon="" → 不要图标色片
            try:
                chip = icon_chip(inner, icon, int(42 * self._k), "muted")
                chip.pack(side="left", padx=(int(18 * self._k), int(14 * self._k)))
            except Exception:
                chip = None
        self._chip = chip
        col = tk.Frame(inner, bg=T.CARD)
        # 没图标色片时标题自己留左边距，别贴着卡片边
        col.pack(side="left", fill="y", pady=int(14 * self._k),
                 padx=(0 if chip is not None else int(18 * self._k), 0))
        self.title = tk.Label(
            col, text=title, bg=T.CARD, fg=T.INK,
            font=(T.UI, T.FS_HERO, "bold"), anchor="w",
        )
        self.title.pack(anchor="w")
        self._sub = None
        if subtitle:
            self._sub = tk.Label(
                col, text=subtitle, bg=T.CARD, fg=T.INK3,
                font=(T.UI, T.FS_CAPTION), anchor="w",
            )
            self._sub.pack(anchor="w", pady=(2, 0))
        self.actions = tk.Frame(inner, bg=T.CARD)
        self.actions.pack(side="right", padx=(0, int(18 * self._k)))

        if title_tip:
            tip(self.title, title_tip)
            help_dot(self, title_tip)  # tooltip 载体（不布局）
            if self._sub is not None:
                tip(self._sub, title_tip)

        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    def _redraw(self, _e: Any = None) -> None:
        try:
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            r = int(14 * self._k)
            self.canvas.delete("all")
            # 平色页头（原为蓝→紫渐变辉光横幅 + 底部极光线）：与卡片同底 + 1px 描边
            rounded_rect(self.canvas, 0, 0, w, h, r, T.CARD, T.LINE, 1, tags="bg")
            self.canvas.create_window(0, 0, window=self._inner, anchor="nw", tags="row")
            self._tint(self._inner, T.CARD)
            # 四角咬口（控件层）：内框方角盖住了底图圆角缺口，补回（大标题框四角全圆）
            self._bite_paint(T.CARD, r)
        except tk.TclError:
            pass

    def _bite_paint(self, band: str, r: int) -> None:
        """四角咬口小画布（必须是控件：Tk 画布窗口项永远盖住图像项）。"""
        if not hasattr(self, "_bite_c"):
            self._bite_c = {}
        w = max(self.canvas.winfo_width(), 2)
        h = max(self.canvas.winfo_height(), 2)
        for corner in ("tl", "tr", "br", "bl"):
            bc = self._bite_c.get(corner)
            if bc is None:
                bc = tk.Canvas(self, width=r, height=r, bg=self._bg,
                               highlightthickness=0, bd=0)
                self._bite_c[corner] = bc
            # 绝对定位（相对定位在窗口定型后不重算）
            bx = 0 if corner in ("tl", "bl") else max(0, w - r)
            by = 0 if corner in ("tl", "tr") else max(0, h - r)
            bc.place(x=bx, y=by)
            bc.configure(width=r, height=r, bg=self._bg)
            bc.delete("all")
            photo = corner_bite_photo(bc, r, corner, band, self._bg)
            bc.create_image(0, 0, image=photo, anchor="nw", tags="bite")

    def _tint(self, w: tk.Widget, bg: str) -> None:
        if getattr(w, "_own_colors", False):
            # 自绘按钮：只同步遮罩底色，不覆盖其自身配色
            try:
                w._mask = bg  # type: ignore[attr-defined]
                w.canvas.configure(bg=bg)  # type: ignore[attr-defined]
                w._redraw()  # type: ignore[attr-defined]
            except (tk.TclError, AttributeError):
                pass
            return
        icon = getattr(w, "_icon", None)
        if icon is not None:
            try:
                set_icon_colors(w, bg=bg)  # type: ignore[arg-type]
                return
            except Exception:
                pass
        try:
            w.configure(bg=bg)
        except tk.TclError:
            pass
        for c in w.winfo_children():
            self._tint(c, bg)

    def _theme_remap(self, _mapping: dict[str, str]) -> None:
        self._bg = T.BG
        self._redraw()


class Badge(tk.Label):
    """状态徽章（胶囊）。"""

    def __init__(self, parent: tk.Widget, text: str, tone: str = "info"):
        palette = {
            "ok": (T.OK_SOFT, T.OK),
            "warn": (T.WARN_SOFT, T.WARN),
            "err": (T.ERR_SOFT, T.ERR),
            "info": (T.ACCENT_SOFT, T.ACCENT_TEXT),
            "muted": (T.CARD_ALT, T.INK3),
        }
        bg, fg = palette.get(tone, palette["info"])
        super().__init__(
            parent, text=f" {text} ", bg=bg, fg=fg,
            font=(T.UI, T.FS_SMALL, "bold"), padx=10, pady=3,
        )


def btn(
    parent: tk.Widget,
    text: str,
    command: Callable[[], None] | None = None,
    *,
    kind: str = "ghost",
    padx: int = 14,
    pady: int = 6,
    tip_text: str | None = None,
) -> Any:
    """统一胶囊按钮。kind: accent(渐变) | primary | ghost | danger | ok | soft"""
    return RoundBtn(parent, text, command, kind=kind, padx=padx, pady=pady,
                    tip_text=tip_text)


class RoundBtn(tk.Frame):
    """真胶囊按钮：圆角 = 高/2，文字画在 Canvas 上（永无方角）。

    accent = 蓝→紫渐变主按钮；primary = 反白；ghost = 浅底；
    danger / ok / soft 各自语义色。hover 亮 12%。
    """

    def __init__(self, parent: tk.Widget, text: str, command: Callable[[], None] | None = None,
                 *, kind: str = "accent", padx: int = 18, pady: int = 8,
                 tip_text: str | None = None, font: Any = None):
        styles = {
            "accent": (T.GRAD_A, "#FFFFFF"),
            "primary": (T.BTN2_BG, T.BTN2_FG),
            "ghost": (T.CARD_ALT, T.INK2),
            "danger": (T.ERR, "#FFFFFF"),
            "ok": (T.OK, "#FFFFFF"),
            "soft": (T.ACCENT_SOFT, T.ACCENT_TEXT),
        }
        self._kind = "accent" if kind == "accent" else ("custom" if kind not in styles else kind)
        self.bg, self.fg = styles.get(kind, styles["accent"])
        self._grad = kind == "accent"
        self._text = text
        self._command = command
        self._padx, self._pady = padx, pady
        self._font = font or (T.UI, T.FS_BODY)
        self._state = "normal"
        self._hover = False
        self._own_colors = True  # 底色由自绘管理：染色/主题遍历不得覆盖
        self._mask = _bg_of(parent)
        super().__init__(parent, bg=self._mask)
        f = tkfont.Font(font=self._font)
        w = f.measure(text) + padx * 2
        h = f.metrics("linespace") + pady * 2
        self.canvas = tk.Canvas(self, width=max(w, 10), height=max(h, 10),
                                bg=self._mask, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        for wdg in (self, self.canvas):
            wdg.bind("<Button-1>", lambda e: self._on_click())
            wdg.bind("<Enter>", self._hover_on)
            wdg.bind("<Leave>", self._hover_off)
        if tip_text:
            tip(self, tip_text)
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    def _colors(self) -> tuple[str, str, bool]:
        """返回 (填充色或渐变左色, 文字色, 是否渐变)。"""
        fill, fg = self.bg, self.fg
        grad = self._grad and self._kind == "accent"
        if grad:
            c1, c2 = T.GRAD_A, T.GRAD_B
            if self._hover:
                c1, c2 = _mix(c1, "#FFFFFF", 0.10), _mix(c2, "#FFFFFF", 0.10)
            return c2, fg, True
        if self._hover:
            fill = _mix(fill, "#FFFFFF", 0.12)
        return fill, fg, False

    def _redraw(self, _e: Any = None) -> None:
        try:
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            fill, fg, grad = self._colors()
            dim = self._state != "normal"
            self.canvas.delete("all")
            r = btn_radius(self.canvas, h)
            if grad:
                c1, c2 = T.GRAD_A, T.GRAD_B
                if self._hover:
                    c1, c2 = _mix(c1, "#FFFFFF", 0.10), _mix(c2, "#FFFFFF", 0.10)
                if dim:
                    c1, c2 = _mix(c1, self._mask, 0.55), _mix(c2, self._mask, 0.55)
                gradient_rounded(self.canvas, 0, 0, w, h, r, c1, c2, self._mask, tags="bg")
            else:
                if dim:
                    fill = _mix(fill, self._mask, 0.5)
                rounded_rect(self.canvas, 0, 0, w, h, r, fill, "", tags="bg")
            if dim:
                fg = _mix(fg, self._mask, 0.5)
            center_text(self.canvas, w / 2, h / 2, text=self._text, fill=fg,
                        font=self._font, tags="txt")
        except tk.TclError:
            pass

    def _hover_on(self, _e: Any = None) -> None:
        if self._state == "normal":
            self._hover = True
            self._redraw()

    def _hover_off(self, _e: Any = None) -> None:
        self._hover = False
        self._redraw()

    def _on_click(self) -> None:
        if self._state == "normal" and self._command:
            self._command()

    # —— tk.Button 兼容 ——
    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        if cnf:
            kw = {**dict(cnf), **kw}
        if "text" in kw:
            self._text = str(kw.pop("text"))
        if "state" in kw:
            self._state = str(kw.pop("state"))
        if "bg" in kw:
            self.bg = str(kw.pop("bg"))
            self._kind = "custom"
            self._grad = False
        if "fg" in kw:
            self.fg = str(kw.pop("fg"))
        for opt in ("activebackground", "activeforeground", "padx", "pady",
                    "font", "anchor", "justify", "wraplength", "compound",
                    "command", "disabledforeground", "overrelief", "bitmap",
                    "image", "default", "takefocus", "bd", "relief",
                    "highlightthickness", "highlightbackground", "highlightcolor"):
            kw.pop(opt, None)
        self._redraw()
        return super().configure(**kw)

    def cget(self, key: str) -> Any:  # type: ignore[override]
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key in ("bg", "background"):
            return self.bg
        if key in ("fg", "foreground"):
            return self.fg
        try:
            return super().cget(key)
        except tk.TclError:
            return None

    def invoke(self) -> Any:
        return self._command() if self._command else None

    def _theme_remap(self, mapping: dict[str, str]) -> None:
        self.bg = _mapped(self.bg, mapping)
        self.fg = _mapped(self.fg, mapping)
        self._redraw()


class ProgressBar(tk.Frame):
    """渐变圆角进度条。"""

    def __init__(self, parent: tk.Widget):
        self._mask = _bg_of(parent)
        super().__init__(parent, bg=self._mask, height=8)
        self.canvas = tk.Canvas(self, bg=self._mask, highlightthickness=0, bd=0, height=8)
        self.canvas.pack(fill="both", expand=True)
        self._ratio = 0.0
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    def set_ratio(self, r: float) -> None:
        self._ratio = max(0.0, min(1.0, r))
        self._redraw()

    def _redraw(self, _e: Any = None) -> None:
        try:
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            self.canvas.delete("all")
            rounded_rect(self.canvas, 0, 0, w, h, h // 2, T.CARD_ALT, "", tags="bg")
            fw = int(w * self._ratio)
            if fw > h:
                gradient_rounded(self.canvas, 0, 0, fw, h, h // 2,
                                 T.GRAD_A, T.GRAD_B, T.CARD_ALT, tags="fill")
        except tk.TclError:
            pass

    def _theme_remap(self, _mapping: dict[str, str]) -> None:
        self._redraw()


def progress(parent: tk.Widget) -> tk.Frame:
    """进度条（保留旧 API：返回带 set_ratio 的 Frame）。"""
    return ProgressBar(parent)


def field_row(
    parent: tk.Widget,
    label: str,
    var: tk.Variable,
    width: int = 28,
    show: str | None = None,
    tip_text: str | None = None,
) -> tk.Entry:
    """圆角输入井：小字标签在上，Entry 与井底同色（方角不可见）。"""
    bg = _bg_of(parent)
    wrap = tk.Frame(parent, bg=bg)
    wrap.pack(fill="x", pady=5)
    key = tip_text or (label if label in GLOSSARY else None)
    alias = {
        "base_url": "base_url",
        "api_key": "api_key",
        "model": "model",
        "temperature": "temperature",
        "max_tokens": "max_tokens",
        "system_prompt": "system_prompt",
    }
    if not key and label in alias:
        key = alias[label]
    lbl = tk.Label(wrap, text=label, bg=bg, fg=T.INK2,
                   font=(T.UI, T.FS_CAPTION), anchor="w")
    lbl.pack(fill="x", pady=(0, 3))
    well = RoundedFrame(wrap, radius=T.RADIUS_INPUT, bg=T.CARD_ALT,
                        border=T.LINE, borderwidth=1)
    well.pack(fill="x")
    entry = tk.Entry(
        well.content, textvariable=var, font=(T.UI, T.FS_BODY), width=width,
        relief="flat", bg=T.CARD_ALT, fg=T.INK, insertbackground=T.INK,
        highlightthickness=1,
        highlightbackground=T.CARD_ALT, highlightcolor=T.ACCENT_LINE,
    )
    if show:
        entry.configure(show=show)
    entry.pack(fill="x", padx=int(10), ipady=6)
    if key:
        tip(lbl, key)
        tip(entry, key)
    return entry


def labeled_text(parent: tk.Widget, label: str, var: tk.StringVar, height: int = 4,
                 tip_text: str | None = None) -> tk.Text:
    bg = _bg_of(parent)
    wrap = tk.Frame(parent, bg=bg)
    wrap.pack(fill="x", pady=5)
    key = tip_text or (label if label in GLOSSARY else None)
    if label == "system_prompt":
        key = key or "system_prompt"
    lbl = tk.Label(wrap, text=label, bg=bg, fg=T.INK2, font=(T.UI, T.FS_CAPTION), anchor="w")
    lbl.pack(fill="x", pady=(0, 3))
    if key:
        tip(lbl, key)
    well = RoundedFrame(wrap, radius=T.RADIUS_INPUT, bg=T.CARD_ALT,
                        border=T.LINE, borderwidth=1)
    well.pack(fill="x")
    text = tk.Text(
        well.content, height=height, font=(T.UI, T.FS_BODY), wrap="word",
        relief="flat", bg=T.CARD_ALT, fg=T.INK, insertbackground=T.INK,
        highlightthickness=1,
        highlightbackground=T.CARD_ALT, highlightcolor=T.ACCENT_LINE,
        padx=12, pady=8,
    )
    text.pack(fill="x")
    if key:
        tip(text, key)
    if var.get():
        text.insert("1.0", var.get())

    def sync(*_a: Any) -> None:
        var.set(text.get("1.0", "end-1c"))

    text.bind("<KeyRelease>", sync)
    text.bind("<FocusOut>", sync)
    return text


def log_box(parent: tk.Widget, height: int = 12) -> tk.Text:
    """暗色终端风日志框（两主题统一）。"""
    box = tk.Text(
        parent, height=height, font=(T.MONO, T.FS_CAPTION), wrap="word",
        relief="flat", bg=T.LOG_BG, fg=T.LOG_FG,
        insertbackground=T.LOG_INSERT, padx=12, pady=10,
        highlightthickness=1, highlightbackground=T.LINE,
    )
    box.tag_configure("err", foreground="#F14C4C")
    box.tag_configure("ok", foreground="#2EA043")
    box.tag_configure("dim", foreground="#7A7A7A")
    return box


def stat_card(
    parent: tk.Widget,
    label: str,
    value: str,
    hint: str = "",
    tone: str = "accent",
    tip_text: str | None = None,
    icon: str | None = "",          # "" = 不要图标（默认）
) -> tk.Frame:
    """统计瓷片：大数字 + 提示（可选图标色片）。"""
    tones = {
        "accent": ("muted", T.INK),
        "ok": ("muted", T.INK),
        "warn": ("muted", T.WARN),
        "err": ("muted", T.ERR),
        "muted": ("muted", T.INK),
    }
    tone_name, ink = tones.get(tone, tones["accent"])
    icons = {"accent": "graph", "ok": "dot", "warn": "clock",
             "err": "close", "muted": "dot"}
    card = RoundedFrame(parent, radius=T.RADIUS_CARD, bg=T.CARD,
                        border=T.LINE, borderwidth=1)
    body = tk.Frame(card.content, bg=T.CARD)
    body.pack(fill="both", expand=True, padx=16, pady=(14, 14))
    top = tk.Frame(body, bg=T.CARD)
    top.pack(fill="x")
    if icon != "":          # icon="" → 不要图标色片
        try:
            icon_chip(top, icon or icons.get(tone, "dot"), int(28), tone_name).pack(
                side="left", padx=(0, 10))
        except Exception:
            pass
    lbl = tk.Label(top, text=label, bg=T.CARD, fg=T.INK3,
                   font=(T.UI, T.FS_CAPTION), anchor="w")
    lbl.pack(side="left", fill="x", expand=True)
    val_lbl = tk.Label(body, text=value, bg=T.CARD, fg=ink,
                       font=(T.UI, 24, "bold"), anchor="w")
    val_lbl.pack(fill="x", pady=(10, 0))
    hint_lbl = tk.Label(body, text=hint, bg=T.CARD, fg=T.INK3,
                        font=(T.UI, T.FS_SMALL), anchor="w")
    if hint:
        hint_lbl.pack(fill="x", pady=(2, 0))

    def set_stat(v: str, h: str = "", color: str | None = None) -> None:
        val_lbl.configure(text=v)
        if color:
            val_lbl.configure(fg=color)
        if h:
            hint_lbl.configure(text=h)
            if not hint_lbl.winfo_ismapped():
                hint_lbl.pack(fill="x", pady=(2, 0))

    card.set_stat = set_stat  # type: ignore[attr-defined]
    if tip_text:
        tip(card, tip_text)
        tip(lbl, tip_text)
    return card


def hint_banner(parent: tk.Widget, text: str, tone: str = "info") -> tk.Label:
    tones = {
        "info": (T.INFO_SOFT, T.INFO),
        "ok": (T.OK_SOFT, T.OK),
        "warn": (T.WARN_SOFT, T.WARN),
        "err": (T.ERR_SOFT, T.ERR),
        "accent": (T.ACCENT_SOFT, T.ACCENT_TEXT),
    }
    bg, fg = tones.get(tone, tones["info"])
    return tk.Label(
        parent, text=text, bg=bg, fg=fg, font=(T.UI, T.FS_CAPTION),
        anchor="w", justify="left", padx=16, pady=10,
    )


class Check(tk.Frame):
    """圆角复选框（自绘矢量）：方形+小圆角；选中渐变填充 + 矢量对勾。"""

    def __init__(self, parent: tk.Widget, text: str, var: tk.Variable,
                 *, bg: str | None = None, tip_text: str | None = None):
        self._bg = bg or T.CARD
        super().__init__(parent, bg=self._bg)
        self._own_colors = True  # 被 tint 时只换遮罩底色，不动方块配色
        self.var = var
        self._hover = False
        self._k = max(self.winfo_fpixels("1i") / 96.0, 0.75)
        s = max(int(18 * self._k), 13)
        self._s = s
        self.canvas = tk.Canvas(self, width=s, height=s, bg=self._bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack(side="left")
        self.label = tk.Label(self, text=text, bg=self._bg, fg=T.INK2,
                              font=(T.UI, T.FS_CAPTION), cursor="hand2")
        self.label.pack(side="left", padx=(8 * self._k, 0))
        for w in (self.canvas, self.label):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._toggle)
        self.configure(takefocus=1, cursor="hand2", highlightthickness=0)
        self.bind("<space>", lambda e: self._toggle())
        self.bind("<Return>", lambda e: self._toggle())
        try:
            var.trace_add("write", lambda *_: self._redraw())
        except Exception:
            pass
        if tip_text:
            tip(self, tip_text)
        self._redraw()
        _register(self)

    def _toggle(self, _e: Any = None) -> None:
        try:
            self.var.set(not bool(self.var.get()))
        except tk.TclError:
            pass
        self._redraw()

    def _enter(self, _e: Any = None) -> None:
        self._hover = True
        self._redraw()

    def _leave(self, _e: Any = None) -> None:
        self._hover = False
        self._redraw()

    def _redraw(self, _e: Any = None) -> None:
        try:
            c = self.canvas
            c.delete("all")
            s = self._s
            r = max(int(s * 0.28), 3)
            checked = bool(self.var.get())
            if checked:
                gradient_rounded(c, 0, 0, s, s, r, T.GRAD_A, T.GRAD_B,
                                 self._bg, tags="box")
                center_text(c, s / 2, s / 2, text="\uE73E",
                            font=(_pick_icon_font(), -max(int(s * 0.58), 8)),
                            fill="#FFFFFF", tags="tick")
            else:
                fill = T.CARD_ALT
                outline = T.INK3 if self._hover else T.ACCENT_LINE
                rounded_rect(c, 0, 0, s, s, r, fill, outline, 1,
                             self._bg, tags="box")
        except tk.TclError:
            pass

    def _theme_remap(self, mapping: dict[str, str]) -> None:
        self._bg = _mapped(self._bg, mapping)
        try:
            self.configure(bg=self._bg)
            self.canvas.configure(bg=self._bg)
            self.label.configure(bg=self._bg)
        except tk.TclError:
            pass
        self._redraw()


def check(
    parent: tk.Widget,
    text: str,
    var: tk.Variable,
    *,
    bg: str | None = None,
    tip_text: str | None = None,
) -> Check:
    """圆角复选框（自绘矢量）。"""
    return Check(parent, text, var, bg=bg, tip_text=tip_text)


class SegTabs(tk.Frame):
    """分段胶囊标签（页内切换）：圆角容器 + 激活段实心胶囊。"""

    def __init__(self, parent: tk.Widget, items: list[tuple[str, str]],
                 initial: str, on_select: Callable[[str], None],
                 tip_map: dict[str, str] | None = None):
        self._mask = _bg_of(parent)
        super().__init__(parent, bg=self._mask)
        self._on_select = on_select
        self._active = initial
        self._tip_map = tip_map or {}
        self._k = max(self.winfo_fpixels("1i") / 96.0, 0.75)
        self.canvas = tk.Canvas(self, bg=self._mask, highlightthickness=0, bd=0,
                                height=int(40 * self._k))
        self.canvas.pack(fill="x")
        self.canvas.pack_propagate(False)
        self._segs: dict[str, RoundBtn] = {}
        self._labels: dict[str, Any] = {}
        row = tk.Frame(self.canvas, bg=T.CARD_ALT)
        self._row = row
        for key, label in items:
            seg = RoundBtn(row, label, lambda k=key: self._pick(k), kind="custom",
                           padx=16, pady=5, tip_text=self._tip_map.get(key))
            seg.configure(bg=T.CARD_ALT, fg=T.INK2)
            seg.pack(side="left", padx=2, pady=4)
            self._segs[key] = seg
            self._labels[key] = seg  # 兼容旧引用
        self.canvas.create_window(2, int(20 * self._k), window=row,
                                  anchor="w", tags="row")
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.canvas.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    def _pick(self, key: str) -> None:
        if key == self._active:
            return
        self.set_active(key)
        self._on_select(key)

    def set_active(self, key: str) -> None:
        self._active = key
        self._redraw()

    def _redraw(self, _e: Any = None) -> None:
        try:
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            self.canvas.delete("all")
            rounded_rect(self.canvas, 0, 0, w, h,
                         btn_radius(self.canvas, h), T.CARD_ALT, T.LINE, 1, tags="bg")
            # 段行在容器内两轴居中（原先贴左上角）
            rh = max(self._row.winfo_reqheight(), 1)
            rw = max(self._row.winfo_reqwidth(), 1)
            x = 2 if rw + 4 >= w else max(2, (w - rw) // 2)
            self.canvas.create_window(x, max(0, (h - rh) // 2),
                                      window=self._row, anchor="nw", tags="row")
            self._row.configure(bg=T.CARD_ALT)
            for key, seg in self._segs.items():
                active = key == self._active
                bg = T.GRAD_A if active else T.CARD_ALT
                fg = "#FFFFFF" if active else T.INK2
                seg.configure(bg=bg, fg=fg)
        except tk.TclError:
            pass

    def _theme_remap(self, _mapping: dict[str, str]) -> None:
        self._redraw()


def flat_button(parent: tk.Widget, icon: str, command: Callable[[], None],
                tip_text: str | None = None, size: int = 16,
                fg: str | None = None, hover_bg: str | None = None,
                bg: str | None = None) -> tk.Frame:
    """图标按钮（活动栏 / 标题栏 / 浮窗用）：方块 + 悬停底色。"""
    base = bg or _bg_of(parent)
    box = tk.Frame(parent, bg=base, cursor="hand2")
    ic = icon_canvas(box, icon, size, fg=fg or T.SIDE_TEXT, bg=base)

    def center() -> None:
        for w in box.winfo_children():
            w.place_forget()
        ic.place(relx=0.5, rely=0.5, anchor="center")

    box.bind("<Configure>", lambda e: center())
    box.after_idle(center)

    hbg = hover_bg or T.SIDE_HOVER

    def enter(_e: Any) -> None:
        box.configure(bg=hbg)
        set_icon_colors(ic, bg=hbg)

    def leave(_e: Any) -> None:
        box.configure(bg=base)
        set_icon_colors(ic, bg=base)

    for w in (box, ic):
        w.bind("<Button-1>", lambda e: command())
        w.bind("<Enter>", enter)
        w.bind("<Leave>", leave)
    if tip_text:
        tip(box, tip_text)
    return box


class SlimVScroll(tk.Canvas):
    """极简竖向滚动条：细圆角拇指，无箭头，矢量绘制。

    SlimVScroll(parent, widget).pack(side="right", fill="y")
    自动接管 widget 的 yscrollcommand；内容不满一屏时拇指自动隐藏。
    """

    def __init__(self, parent: tk.Widget, widget: tk.Widget, width: int = 12):
        self._k = max(widget.winfo_fpixels("1i") / 96.0, 0.75)
        self._mask = _bg_of(parent)
        super().__init__(parent, width=max(6, int(width * self._k)), bg=self._mask,
                         highlightthickness=0, bd=0, cursor="arrow")
        self._widget = widget
        self._first, self._last = 0.0, 1.0
        self._hovered = False
        widget.configure(yscrollcommand=self._set)
        self.bind("<Configure>", lambda e: queue_call(self, "_redraw"))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<Enter>", lambda e: self._on_hover(True))
        self.bind("<Leave>", lambda e: self._on_hover(False))
        self.after_idle(lambda: queue_call(self, "_redraw"))
        _register(self)

    # —— yview 回调 ——
    def _set(self, first: str, last: str) -> None:
        try:
            self._first, self._last = float(first), float(last)
        except ValueError:
            self._first, self._last = 0.0, 1.0
        self._redraw()

    def _thumb_rect(self) -> tuple[int, int, int, int] | None:
        h = max(self.winfo_height(), 2)
        w = max(self.winfo_width(), 2)
        span = self._last - self._first
        if span >= 0.999 or h < 40:
            return None
        thumb_h = max(int(h * span), int(24 * self._k))
        travel = max(1, h - thumb_h)
        y1 = int(travel * (self._first / max(1e-6, 1.0 - span)))
        return 1, y1, w - 1, min(h - 1, y1 + thumb_h)

    def _redraw(self, _e: Any = None) -> None:
        try:
            self.delete("all")
            rect = self._thumb_rect()
            if rect:
                x1, y1, x2, y2 = rect
                if self._hovered:
                    color = _mix(T.INK2, "#FFFFFF", 0.12)
                else:
                    color = _mix(T.CARD_ALT, T.INK2, 0.55)
                rounded_rect(self, x1, y1, x2, y2,
                             max(1, (x2 - x1) // 2), color, "", tags="thumb")
        except tk.TclError:
            pass

    def _on_hover(self, on: bool) -> None:
        self._hovered = on
        self._redraw()

    def _press(self, e: tk.Event) -> None:
        rect = self._thumb_rect()
        if rect is None:
            return
        _, y1, _, y2 = rect
        h = max(self.winfo_height(), 2)
        thumb_h = max(1, y2 - y1)
        travel = max(1, h - thumb_h)
        span = self._last - self._first
        frac = max(0.0, min(1.0, (e.y - thumb_h / 2) / travel))
        try:
            self._widget.yview_moveto(frac * max(0.0, 1.0 - span))
        except tk.TclError:
            pass

    def _theme_remap(self, _mapping: dict[str, str]) -> None:
        self._redraw()
