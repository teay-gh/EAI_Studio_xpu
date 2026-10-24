"""EAI Studio 设计系统 v2 ·「极光玻璃」— 黑 / 白双主题。

取向（美观优先）：
- 深空墨蓝底 + 浮层玻璃卡 + 蓝→紫极光渐变主按钮（真胶囊圆角）。
- 页面头部为「英雄横幅」：大标题 + 图标色片 + 渐变辉光。
- 图标色片（圆角方块 + 线性图标）、胶囊按钮、圆角输入井、分段胶囊标签。

约定：
- 所有颜色只允许从本模块读取（T.BG / T.CARD …），页面不得硬编码色值。
- 两个主题的色值在同一主题内必须唯一：热切换靠「旧色值 → 新色值」映射遍历控件树。
  例外：两主题取同一值的键（成对镜像）会被映射跳过。
- 任何主题的色值都不得等于代码里的字面色（#FFFFFF / #000000 等），否则字面色会被换掉。
- LOG_* 与 CLOSE_HOVER 两主题同值；TT_* 不参与映射。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable

# ---------- 品牌 ----------
BRAND = "EAI Studio"
BRAND_SUB = "数据工厂 · 模型训练 · 对话验证"
VERSION = "2.2.0"

# ---------- 黑主题（深空墨蓝 · 极光） ----------
_DARK: dict[str, str] = {
    # 画布与表面
    "BG": "#0F1117",
    "CARD": "#171A23",
    "CARD_ALT": "#1F2330",
    "LINE": "#2A2F3E",
    "LINE_SOFT": "#212634",
    # 侧栏 / 活动栏
    "SIDE": "#12141C",
    "SIDE_HOVER": "#1D2130",
    "SIDE_ACTIVE": "#232A3D",
    "SIDE_ACTIVE_FG": "#F2F5FF",
    "SIDE_TEXT": "#B9C0D4",
    "SIDE_MUTED": "#727B94",
    "SIDE_DIVIDER": "#232838",
    # 文字
    "INK": "#EAEDF6",
    "INK2": "#A7AFC4",
    "INK3": "#6E768C",
    "INK_INV": "#FAFBFF",
    # 强调色（单一静蓝；GRAD 与 ACCENT 同值 → 平色，不再有蓝→紫渐变）
    "ACCENT": "#7C93EE",
    "ACCENT_HOVER": "#93A8F5",
    "ACCENT_SOFT": "#242A3F",
    "ACCENT_LINE": "#5F79E0",
    "ACCENT_TEXT": "#A6B7F2",
    "GRAD_A": "#7C93EE",
    "GRAD_B": "#7C93EE",
    # 次级按钮
    "BTN2_BG": "#262C3E",
    "BTN2_FG": "#E4E9F7",
    "BTN2_ACTIVE": "#303850",
    # 状态（降饱和：只在语义处出现，不再抢眼）
    "OK": "#57B79B",
    "OK_SOFT": "#1B2A28",
    "OK_HOVER": "#6BC7AC",
    "WARN": "#C9A961",
    "WARN_SOFT": "#2E2A1F",
    "WARN_HOVER": "#D8BC7E",
    "ERR": "#D0706C",
    "ERR_SOFT": "#33232A",
    "ERR_HOVER": "#DE8380",
    "INFO": "#77AECD",
    "INFO_SOFT": "#1B2634",
    # 聊天气泡
    "USER_BG": "#4A5C9E",
    "BOT_BG": "#202635",
    "SHADOW": "#06070C",
    # 日志终端（两主题同值）
    "LOG_BG": "#0B0D14",
    "LOG_FG": "#C9D2E8",
    "LOG_INSERT": "#AAB6D4",
    # 关闭键悬停（两主题同值）
    "CLOSE_HOVER": "#C42B1C",
    # tooltip（不参与映射）
    "TT_BG": "#232838",
    "TT_FG": "#EAEDF6",
    "TT_TITLE": "#9DB4FF",
}

# ---------- 白主题（清透玻璃 · 极光） ----------
_LIGHT: dict[str, str] = {
    "BG": "#F5F7FC",
    "CARD": "#FCFCFE",
    "CARD_ALT": "#EDEFF7",
    "LINE": "#DDE2F0",
    "LINE_SOFT": "#E9ECF6",
    "SIDE": "#FAFBFE",
    "SIDE_HOVER": "#EAEDF7",
    "SIDE_ACTIVE": "#E0E7F8",
    "SIDE_ACTIVE_FG": "#1A2140",
    "SIDE_TEXT": "#3A4258",
    "SIDE_MUTED": "#7C8499",
    "SIDE_DIVIDER": "#E6EAF4",
    "INK": "#1B2130",
    "INK2": "#5A6379",
    "INK3": "#8B93A8",
    "INK_INV": "#FAFBFF",
    "ACCENT": "#4C63D8",
    "ACCENT_HOVER": "#3F55C4",
    "ACCENT_SOFT": "#E8EBF7",
    "ACCENT_LINE": "#5971D6",
    "ACCENT_TEXT": "#3C4FA8",
    "GRAD_A": "#4C63D8",
    "GRAD_B": "#4C63D8",
    "BTN2_BG": "#E7EAF5",
    "BTN2_FG": "#2A3247",
    "BTN2_ACTIVE": "#D8DDEE",
    "OK": "#3D9077",
    "OK_SOFT": "#E4F0EB",
    "OK_HOVER": "#337A64",
    "WARN": "#94743A",
    "WARN_SOFT": "#F5F0E4",
    "WARN_HOVER": "#7E6230",
    "ERR": "#B95C5C",
    "ERR_SOFT": "#F6E9E9",
    "ERR_HOVER": "#A55050",
    "INFO": "#4A88B0",
    "INFO_SOFT": "#E8F0F7",
    "USER_BG": "#4A5CB4",
    "BOT_BG": "#E9EDF7",
    "SHADOW": "#C3C9DC",
    "LOG_BG": "#0B0D14",
    "LOG_FG": "#C9D2E8",
    "LOG_INSERT": "#AAB6D4",
    "CLOSE_HOVER": "#C42B1C",
    "TT_BG": "#232838",
    "TT_FG": "#EAEDF6",
    "TT_TITLE": "#9DB4FF",
}

THEMES: dict[str, dict[str, str]] = {"dark": _DARK, "light": _LIGHT}
THEME_LABELS: dict[str, str] = {"dark": "黑 · Dark", "light": "白 · Light"}

# 不参与热切换映射的 key（临时控件或刻意跨主题同色）
_NO_REMAP = {"TT_BG", "TT_FG", "TT_TITLE"}

_current: str = "dark"
_last_mapping: dict[str, str] = {}


def current() -> str:
    return _current


def palette() -> dict[str, str]:
    return THEMES[_current]


def last_mapping() -> dict[str, str]:
    """最近一次热切换的「旧色值 → 新色值」映射（供自绘控件重染）。"""
    return dict(_last_mapping)


def _export(p: dict[str, str]) -> None:
    globals().update(p)


_export(THEMES[_current])


def set_theme_globals(name: str) -> None:
    """仅更新模块级颜色变量（供新建控件读取）。"""
    global _current
    if name not in THEMES:
        raise ValueError(f"unknown theme: {name}")
    _current = name
    _export(THEMES[name])


# ---------- 运行时热切换 ----------
_REMAP_OPTS = (
    "bg", "fg",
    "background", "foreground",
    "activebackground", "activeforeground",
    "highlightbackground", "highlightcolor",
    "insertbackground",
    "selectbackground", "selectforeground",
    "troughcolor",
    "selectcolor",
)

_redraw_callbacks: list[Callable[[], None]] = []


def register_redraw(fn: Callable[[], None]) -> None:
    """注册重绘回调：主题切换后调用（自绘 canvas / 图表）。"""
    if fn not in _redraw_callbacks:
        _redraw_callbacks.append(fn)


def _build_map(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    by_old: dict[str, str] = {}
    for key, new_val in new.items():
        if key in _NO_REMAP:
            continue
        old_val = old.get(key)
        if not old_val or old_val == new_val:
            continue
        prev = by_old.get(old_val)
        if prev is not None and prev != new_val:
            raise RuntimeError(
                f"theme remap conflict: {old_val} -> {prev} vs {new_val} (key={key})"
            )
        by_old[old_val] = new_val
    return by_old


def _walk(widget: tk.Widget, fn: Callable[[tk.Widget], None]) -> None:
    fn(widget)
    for child in widget.winfo_children():
        _walk(child, fn)


def apply_theme(root: tk.Tk, name: str) -> None:
    """把 root 下所有常驻控件从当前主题热切换到 name（不重建 UI，保留状态）。"""
    global _last_mapping
    if name == _current or name not in THEMES:
        return
    mapping = _build_map(THEMES[_current], THEMES[name])
    _last_mapping = mapping
    set_theme_globals(name)

    def fix(widget: tk.Widget) -> None:
        # 自绘控件（如胶囊按钮）自行管理配色：跳过按值映射，由其 _theme_remap 重绘
        if getattr(widget, "_own_colors", False):
            return
        # Text / Entry 的标签与选项
        if isinstance(widget, (tk.Text, tk.Entry)):
            try:
                tags = widget.tag_names()  # type: ignore[union-attr]
            except Exception:
                tags = ()
            for tag in tags:
                for opt in ("background", "foreground"):
                    try:
                        val = widget.tag_cget(tag, opt)  # type: ignore[union-attr]
                    except Exception:
                        continue
                    if val in mapping:
                        try:
                            widget.tag_configure(tag, **{opt: mapping[val]})  # type: ignore[union-attr]
                        except Exception:
                            pass
        # 先快照再改：bg/background 是同一选项，边读边改会二次映射
        vals: dict[str, Any] = {}
        for opt in _REMAP_OPTS:
            try:
                vals[opt] = widget.cget(opt)
            except Exception:
                continue
        for opt, val in vals.items():
            if isinstance(val, str) and val in mapping:
                try:
                    widget.configure(**{opt: mapping[val]})
                except Exception:
                    pass

    _walk(root, fix)

    # 自绘内容（圆角 / 渐变 / 图标）重绘
    for fn in list(_redraw_callbacks):
        try:
            fn()
        except Exception:
            pass


# ---------- 字体 ----------
UI = "Microsoft YaHei UI"
MONO = "Cascadia Mono"

# 字号层级（英雄标题突出，正文收敛）
FS_HERO = 20
FS_H1 = 15
FS_H2 = 12
FS_H3 = 11
FS_NAV = 12
FS_NAV_ROW = 13            # 侧栏导航行专字号（原与标签页/工程名共用 12）
FS_BODY = 10
FS_CAPTION = 9
FS_SMALL = 9

# 间距 / 圆角（96DPI 设计值，app 层乘 k 缩放）——放宽留白，界面不再拥挤
PAD_PAGE = 30
PAD_CARD = 20
PAD_CARD_IN = 14
RADIUS_CARD = 14
RADIUS_BTN = 8        # 按钮 = 带圆角的长方形（r=8，不做胶囊）
RADIUS_INPUT = 10
RADIUS_PILL = 999
RADIUS_HINT = 10

# ---------- 外壳布局（96DPI 设计值） ----------
TITLEBAR_H = 40
ACTIVITY_W = 56
SIDEBAR_W = 252
TAB_H = 40
HERO_H = 92

# ---------- 导航 ----------
# (group, icon_kind, [(key, label, icon_kind, tip?)])
NAV_GROUPS: list[tuple[str, str, list[tuple]]] = [
    ("工作台", "home", [
        ("home", "总览", "", "home"),
    ]),
    ("数据工厂", "layers", [
        ("article", "文章蒸馏", "", "article_distill"),
        ("response", "响应蒸馏", "", "response_distill"),
        ("dataset", "数据集", "", "dataset"),
    ]),
    ("训练中心", "play", [
        ("train_pretrain", "预训练", "", "train_pretrain"),
        ("train_full", "全量微调", "", "train_full"),
        ("train_lora", "LoRA 微调", "", "train_lora"),
    ]),
    ("模型", "chat", [
        ("chat", "对话验证", "", "chat"),
        ("network", "网络结构", "", "network"),
    ]),
    ("系统", "gear", [
        ("settings", "全局设置", "", "全局路径、默认 API 与设备选项。配置保存在本机 config.json。"),
    ]),
]

# 导航键 → 别名（AI 助手 navigate 兼容）
# 模型经常直接发界面上的中文名（「数据集」「文章蒸馏」…）或分组名（「数据中心」…），
# 这里全部兜住，避免 apply_actions 报「未知页面」。键统一小写、去空格/下划线/连字符后再查（见 assistant.navigate）。
NAV_ALIASES = {
    "train": "train_pretrain",
    "train_center": "train_pretrain",
    # 中文页名
    "总览": "home", "首页": "home", "主页": "home", "工作台": "home", "概览": "home",
    "文章蒸馏": "article", "文章": "article",
    "响应蒸馏": "response", "响应": "response", "问答": "response",
    "数据集": "dataset", "数据": "dataset", "数据管理": "dataset", "数据中心": "dataset",
    "预训练": "train_pretrain", "训练": "train_pretrain", "训练中心": "train_pretrain",
    "全量微调": "train_full", "全量训练": "train_full", "全量": "train_full",
    "微调": "train_lora", "lora微调": "train_lora", "lora": "train_lora",
    "对话验证": "chat", "对话": "chat", "聊天": "chat", "模型": "chat",
    "设置": "settings", "全局设置": "settings", "系统设置": "settings", "系统": "settings",
    "网络": "network", "网络检测": "network",
    # 英文别名/常见写法
    "overview": "home", "dashboard": "home", "index": "home",
    "train_pretrain": "train_pretrain", "pretrain": "train_pretrain",
    "train_full": "train_full", "trainfull": "train_full", "trainpretrain": "train_pretrain",
    "trainlora": "train_lora", "train_lora": "train_lora", "settings": "settings",
    "conversation": "chat", "chats": "chat", "chat": "chat",
    "config": "settings", "setting": "settings", "preference": "settings",
}
