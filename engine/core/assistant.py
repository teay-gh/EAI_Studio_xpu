"""右侧 AI 助手：对话 + 应用内参数操控。

后端解析顺序（resolve_backend）：
1. config.json 的 assistant 段（显式配置优先）
2. mimo CLI 的 llm-server（MimoC / 本机安装的 MiMoCode，能力 API）
3. 回退到 article.titler 现有配置（本应用已在用的 OpenAI 兼容接口）

模型通过 JSON 代码块下发工具调用（set_config / get_config / navigate / set_theme），
本模块解析并执行，结果回显在对话流中。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from . import chat_history
from . import theme as T
from . import uiq
from . import widgets as W
from .client import ChatClient, ChatError
from .tooltips import hide_all_tooltips, tip

# ============================================================
# 后端解析
# ============================================================

_ACTION_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

# ASCII 词内不可断（Tk 的 wrap="word" 对中英混排会在词中间硬断）
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./:+]*|\s+|.", re.S)


def soft_wrap(text: str, font: Any, max_px: int) -> str:
    """按 CJK 断行习惯预换行：ASCII 词内不断行，中文字后可断。"""
    import tkinter.font as tkfont

    if not isinstance(font, tkfont.Font):
        font = tkfont.Font(font=font)
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for tok in _TOKEN_RE.findall(para):
            if tok.isspace():
                tok = " "
            if line and font.measure(line + tok) > max_px:
                out.append(line.rstrip())
                line = tok.lstrip()
            else:
                line += tok
        out.append(line.rstrip())
    return "\n".join(out)


def _try_mimo_cli(base: str) -> dict[str, Any] | None:
    """尝试用 mimo CLI 铸造 llm-server token（依赖本机 MiMoCode / MimoC 安装）。"""
    exe = shutil.which("mimo")
    cmd: list[str] | None = None
    if exe:
        cmd = [exe]
    else:
        # MimoC 源码树里的 node 启动器
        cand = os.path.join(base, "..", "MimoC", "MiMo-Code-0.1.15",
                            "packages", "opencode", "bin", "mimo")
        cand = os.path.normpath(cand)
        if os.path.isfile(cand):
            node = shutil.which("node")
            if node:
                cmd = [node, cand]
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            cmd + ["llm-server", "issue", "--json", "--label", "eproai-assistant", "--ttl", "12h"],
            cwd=base, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if not data.get("base_url"):
            return None  # 没有正在服务的实例
        models = data.get("models") or []
        return {
            "base_url": data["base_url"],
            "api_key": data.get("api_key") or "",
            "model": models[0] if models else "",
        }
    except Exception:
        return None


def _read_jsonc(path: str) -> dict[str, Any] | None:
    """读 JSONC（容忍 // 注释与尾逗号）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _try_mimocode_config() -> dict[str, Any] | None:
    """从 MiMoCode（MimoC 生态）全局/项目配置里取可用的 provider key。

    读 ~/.config/mimocode/mimocode.jsonc 与项目 .mimocode/mimocode.json(.jsonc)，
    取 provider.<id>.options.apiKey；token-plan 类 provider 配已知兼容 base_url。
    """
    candidates = [
        os.path.expandvars(r"%USERPROFILE%\.config\mimocode\mimocode.jsonc"),
        os.path.expandvars(r"%USERPROFILE%\.config\mimocode\mimocode.json"),
    ]
    for cfg_path in candidates:
        obj = _read_jsonc(cfg_path)
        if not obj:
            continue
        providers = obj.get("provider") or {}
        if not isinstance(providers, dict):
            continue
        for pid, pdef in providers.items():
            if not isinstance(pdef, dict):
                continue
            opts = pdef.get("options") or {}
            key = opts.get("apiKey") or opts.get("api_key") or ""
            base = opts.get("baseURL") or opts.get("base_url") or ""
            if not key:
                continue
            if not base and "token-plan" in pid:
                base = "https://token-plan-cn.xiaomimimo.com/v1"
            if not base:
                continue
            models = list((pdef.get("models") or {}).keys())
            model = models[0] if models else ""
            if not model:
                continue
            return {"base_url": base, "api_key": key, "model": model,
                    "_source": f"mimocode · {pid}"}
    return None


def resolve_backend(app: Any) -> tuple[dict[str, Any], str]:
    """返回 (profile, 来源说明)。profile 至少含 base_url/model。"""
    cfg = app.cfg
    a = cfg.get("assistant") or {}
    if a.get("base_url") and a.get("model"):
        return dict(a), f"assistant · {a.get('model')}"
    mimo = _try_mimo_cli(app.BASE)
    if mimo and mimo.get("model"):
        merged = {**a, **mimo}
        return merged, f"mimo llm-server · {mimo['model']}"
    mc = _try_mimocode_config()
    if mc:
        merged = {**a, **mc}
        src = merged.pop("_source", "mimocode")
        return merged, f"{src} · {mc.get('model')}"
    titler = ((cfg.get("article") or {}).get("titler")) or {}
    if titler.get("base_url") and titler.get("model"):
        merged = {**a}
        merged.update({
            "base_url": titler.get("base_url", ""),
            "api_key": titler.get("api_key", ""),
            "model": titler.get("model", ""),
        })
        return merged, f"蒸馏 titler 回退 · {merged.get('model')}"
    return dict(a), "未配置"


# ============================================================
# 工具：配置读写 / 导航 / 主题
# ============================================================

_PAGE_KEYS = {"home", "article", "response", "dataset",
              "train_pretrain", "train_full", "train_lora",
              "chat", "network", "settings"}


def _mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("api_key", "apikey", "token") and isinstance(v, str) and v:
                out[k] = f"••••{v[-4:]}（已隐藏）"
            else:
                out[k] = _mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [_mask_secrets(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + f"…（共 {len(obj)} 字符）"
    return obj


def _get_path(cfg: Any, path: str) -> Any:
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(f"配置中不存在 {path}（卡在 {part}）")
    return cur


def _coerce(old: Any, value: Any) -> Any:
    """按现有值的类型转换新值。"""
    if isinstance(old, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if isinstance(old, int) and not isinstance(old, bool):
        return int(float(value))
    if isinstance(old, float):
        return float(value)
    return value if isinstance(value, str) else str(value)


def apply_actions(app: Any, actions: list[dict[str, Any]]) -> list[str]:
    """执行模型下发的动作，返回逐条人话结果。"""
    results: list[str] = []
    for act in actions:
        tool = str(act.get("tool") or "").strip()
        path = str(act.get("path") or "").strip()
        value = act.get("value")
        try:
            if tool == "get_config":
                if path:
                    val = _get_path(app.cfg, path)
                    results.append(f"get_config {path} = {json.dumps(_mask_secrets(val), ensure_ascii=False)}")
                else:
                    # 顶层只给节名清单（用户 2026-10-21：键名倾倒会以小字糊满对话）；
                    # 参数含义看《EAI 参数手册》，查具体值请带 path。
                    secs = [k for k, v in app.cfg.items() if isinstance(v, dict)]
                    plain = [k for k, v in app.cfg.items() if not isinstance(v, dict)]
                    brief = " / ".join(secs) + ("（另有 " + " / ".join(plain) + "）" if plain else "")
                    results.append(
                        "get_config（顶层）= " + brief
                        + " 共 %d 节。查具体值请带 path（如 train.device）；参数含义见《EAI 参数手册》，不必读配置。"
                        % len(secs))
            elif tool == "set_config":
                if not path:
                    raise ValueError("set_config 需要 path")
                old = _get_path(app.cfg, path)
                new = _coerce(old, value)
                parts = path.split(".")
                cur = app.cfg
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = new
                app.save_config()
                # 特殊键联动
                if path == "app.theme" and new in ("dark", "light"):
                    app.set_theme(new)
                app.on_config_changed(path)
                results.append(f"set_config {path}: {old!r} → {new!r}（已保存）")
            elif tool == "navigate":
                from .theme import NAV_ALIASES
                # key 这个字段名要认：说明书和模型都习惯写 {"tool":"navigate","key":"dataset"}，
                # 而这里原来只读 value/path → 动作里的页名被当成空串，报「未知页面 ，可选：[...]」
                raw = str(value or path or act.get("key") or "").strip()
                norm = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
                key = NAV_ALIASES.get(raw) or NAV_ALIASES.get(norm) or norm or raw   # 归一化后也可能直接就是合法 key（如 SETTINGS → settings）
                if key not in _PAGE_KEYS:
                    raise ValueError(f"未知页面 {key}，可选：{sorted(_PAGE_KEYS)}")
                app.show(key)
                results.append(f"navigate → {key}")
            elif tool == "set_theme":
                name = str(value or "").strip()
                if name not in ("dark", "light"):
                    raise ValueError("set_theme 的 value 必须是 dark 或 light")
                app.set_theme(name)
                results.append(f"set_theme → {name}")
            else:
                raise ValueError(f"未知 tool：{tool}")
        except Exception as exc:
            results.append(f"✗ {tool} 失败：{exc}")
    return results


def extract_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """从模型回复中取出 ```json 动作块，返回 (清理后的正文, actions)。"""
    actions: list[dict[str, Any]] = []
    cleaned = text
    for m in _ACTION_BLOCK_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("actions" in obj or "tool" in obj):
            raw = obj.get("actions") if "actions" in obj else [obj]
            if isinstance(raw, list):
                actions.extend(x for x in raw if isinstance(x, dict))
            cleaned = cleaned.replace(m.group(0), "")
    return cleaned.strip(), actions


# ============================================================
# 悬浮窗 UI（仿 MiMo Desktop 浮窗）
# ============================================================

class AssistantDock:
    """AI 助手悬浮面板：主窗内的固定浮层。

    - 顶栏「✦」/ Ctrl+J 开合，面板内「✕」可关
    - 单卡圆角容器：状态色点 + 消息 + 输入
    - 「新对话」开新会话；「历史」浏览/恢复
    - 有工程：会话落盘 chats/；无工程：内存缓存，退出清空
    """

    WIDTH = 420
    HEIGHT = 520
    PAD = 24

    def __init__(self, parent: tk.Widget, app: Any):
        self.app = app
        self.parent = parent  # 主窗内容容器
        # Tk 像素随 DPI 缩放：按 96DPI 设计的尺寸统一乘 k，避免高分屏下挤成一团
        try:
            self.k = max(self.app.root.winfo_fpixels("1i") / 96.0, 0.75)
        except Exception:
            self.k = 1.0
        self.WIDTH = int(self.WIDTH * self.k)
        self.HEIGHT = int(self.HEIGHT * self.k)
        self.PAD = int(self.PAD * self.k)
        self._MARGINS = {
            "user": (int(140 * self.k), int(8 * self.k)),
            "user_hdr": (int(140 * self.k), int(8 * self.k)),
            "bot": (int(8 * self.k), int(140 * self.k)),
            "bot_hdr": (int(8 * self.k), int(140 * self.k)),
            "sys": (int(8 * self.k), int(8 * self.k)),
            "tool": (int(8 * self.k), int(8 * self.k)),
            "err": (int(8 * self.k), int(8 * self.k)),
        }
        self.history: list[dict[str, str]] = []
        self.busy = False
        self._discard = False
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.backend: dict[str, Any] = {}
        self.backend_src = ""
        self._visible = False
        self._built = False
        self._status = "idle"  # idle | busy | error
        self.chats = chat_history.ChatHistory()
        self.session_id: str = ""
        proj = getattr(self.app, "project", None)
        if proj is not None:
            self.chats.bind_project(proj.root)
        conv = self.chats.new_session()
        self.session_id = conv["id"]

        self.app.root.after(100, self._poll)

    # ---------- 状态色点 ----------
    def _set_status(self, status: str) -> None:
        self._status = status
        if not self._built:
            return
        colors = {
            "idle": T.OK,       # 绿：可用
            "busy": T.WARN,     # 黄：加载
            "error": T.ERR,     # 红：错误
        }
        c = colors.get(status, T.OK)
        try:
            self.status_dot.configure(fg=c, bg=T.CARD_ALT)
        except tk.TclError:
            pass

    # ---------- 面板 ----------
    def _ensure_panel(self) -> tk.Frame:
        if self._built and getattr(self, "panel", None) is not None:
            try:
                self.panel.winfo_exists()
                return self.panel
            except tk.TclError:
                pass

        # 外层圆角卡：悬浮小窗（投影分层，MiMo Desktop 风格）
        # 玻璃层分离：面板浅一级 + 亮边细线 + 加深投影，内部聊天/输入反向内嵌
        self.panel = W.RoundedFrame(
            self.parent, radius=int(16 * self.k),
            bg=T.CARD_ALT, border=T.INK3, borderwidth=1, shadow=int(22 * self.k),
        )
        shell = self.panel.content  # 内容层
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        # 标题栏：AI 助手 + 状态点，右侧 ＋ / 时钟 / ✕ 图标
        head = tk.Frame(shell, bg=T.CARD_ALT)
        head.grid(row=0, column=0, sticky="ew",
                  padx=int(6 * self.k), pady=(int(6 * self.k), 0))
        self._head = head
        # 投影余量（与 RoundedFrame shadow 一致）；窗口贴顶固定，不支持拖动
        self._sp = int(22 * self.k)
        tk.Label(
            head, text="AI 助手", bg=T.CARD_ALT, fg=T.INK,
            font=(T.UI, T.FS_H3, "bold"),
        ).pack(side="left", padx=(int(10 * self.k), 0))
        self.status_dot = tk.Label(
            head, text="●", bg=T.CARD_ALT, fg=T.OK, font=(T.UI, 9),
        )
        self.status_dot.pack(side="left", padx=(int(6 * self.k), 0))
        tip(self.status_dot, "后端状态")

        def icon_btn(kind: str, cmd, tip_text: str) -> None:
            box = W.RoundedFrame(head, radius=int(8 * self.k), bg=T.CARD_ALT,
                                 border="", borderwidth=0)
            box.configure(width=int(40 * self.k), height=int(40 * self.k))
            box.pack_propagate(False)
            box.pack(side="right", padx=int(4 * self.k))
            ic = W.icon_canvas(box.content, kind, int(26 * self.k),
                               fg=T.INK2, bg=T.CARD_ALT)
            ic.place(relx=0.5, rely=0.5, anchor="center")

            def enter(_e: Any) -> None:
                box.set_colors(bg=T.LINE)
                ic.configure(bg=T.LINE)

            def leave(_e: Any) -> None:
                box.set_colors(bg=T.CARD_ALT)
                ic.configure(bg=T.CARD_ALT)

            for w in (box, ic):
                w.bind("<Button-1>", lambda e: cmd())
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
            tip(box, tip_text)

        icon_btn("close", lambda: self.app.toggle_ai_panel(force_open=False), "关闭")
        icon_btn("clock", self._show_history, "历史会话")
        icon_btn("plus", self.new_chat, "新对话")

        # 标题栏与对话区之间的分隔线（INK3 细线：与玻璃棱边同语言，清晰分区）
        div = tk.Frame(shell, bg=T.INK3, height=max(1, int(self.k)))
        div.grid(row=1, column=0, sticky="ew",
                 pady=(int(6 * self.k), int(2 * self.k)))

        # 消息区（同一圆角卡内）
        self._mid = tk.Frame(shell, bg=T.CARD)
        self._mid.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)
        self.chat = tk.Text(
            self._mid, wrap="word", font=(T.UI, T.FS_BODY), relief="flat",
            bg=T.CARD, fg=T.INK, highlightthickness=0,
            padx=int(12 * self.k), pady=int(8 * self.k),
            state="disabled", cursor="arrow",
        )
        W.SlimVScroll(self._mid, self.chat).pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        # 消息不做整行色块：用户右对齐、助手左对齐，靠对齐和角色小标签区分
        u_l, u_r = self._MARGINS["user"]
        b_l, b_r = self._MARGINS["bot"]
        s_l, s_r = self._MARGINS["tool"]
        self.chat.tag_configure("user_hdr", foreground=T.INK3,
                                font=(T.UI, T.FS_SMALL), justify="right",
                                lmargin1=u_l, lmargin2=u_l, rmargin=u_r, spacing1=10)
        self.chat.tag_configure("user", background=T.USER_BG, foreground="#FFFFFF",
                                font=(T.UI, T.FS_BODY), justify="right",
                                lmargin1=u_l, lmargin2=u_l, rmargin=u_r,
                                spacing1=2, spacing3=8)
        self.chat.tag_configure("bot_hdr", foreground=T.ACCENT_TEXT,
                                font=(T.UI, T.FS_SMALL),
                                lmargin1=b_l, lmargin2=b_l, rmargin=b_r, spacing1=10)
        self.chat.tag_configure("bot", background=T.BOT_BG, foreground=T.INK,
                                lmargin1=b_l, lmargin2=b_l, rmargin=b_r,
                                spacing1=2, spacing3=8)
        self.chat.tag_configure("sys", foreground=T.INK3, font=(T.UI, T.FS_SMALL),
                                lmargin1=s_l, rmargin=s_r, spacing1=4, spacing3=4)
        self.chat.tag_configure("tool", background=T.LOG_BG, foreground=T.LOG_FG,
                                font=(T.UI, T.FS_SMALL),
                                lmargin1=s_l, lmargin2=s_l, rmargin=s_r,
                                spacing1=6, spacing3=8)
        self.chat.tag_configure("err", background=T.ERR_SOFT, foreground=T.ERR,
                                font=(T.UI, T.FS_SMALL),
                                lmargin1=s_l, lmargin2=s_l, rmargin=s_r,
                                spacing1=4, spacing3=4)

        # 输入区（MiMo 风格：圆角输入卡 + 圆形发送键）——内嵌深井，与玻璃壳分层
        foot = tk.Frame(shell, bg=T.CARD_ALT)
        foot.grid(row=3, column=0, sticky="ew",
                  padx=int(10 * self.k), pady=(int(6 * self.k), int(10 * self.k)))
        self._foot = foot
        inbox = W.RoundedFrame(foot, radius=int(12 * self.k), bg=T.CARD,
                               border=T.LINE, borderwidth=1)
        inbox.pack(fill="x")
        self.input = tk.Text(
            inbox.content, height=2, wrap="word", font=(T.UI, T.FS_BODY),
            relief="flat", bg=T.CARD, fg=T.INK, insertbackground=T.INK,
            highlightthickness=0, padx=int(10 * self.k), pady=int(8 * self.k),
        )
        self.input.pack(fill="x")
        self.input.bind("<Return>", self._on_enter)
        row = tk.Frame(inbox.content, bg=T.CARD)
        row.pack(fill="x", padx=int(10 * self.k), pady=(0, int(8 * self.k)))
        self.hint = tk.Label(row, text="", bg=T.CARD, fg=T.INK3,
                             font=(T.UI, T.FS_SMALL))
        self.hint.pack(side="left")
        self.stop_btn = W.RoundBtn(row, "停止", self._stop, kind="danger",
                                   padx=int(10 * self.k), pady=int(3 * self.k))
        self.send_btn = self._make_send_circle(row)
        self.send_btn.pack(side="right")

        self._built = True
        self._append("sys", "AI 助手已就绪")
        self._set_status("idle")
        self._refresh_hint()
        return self.panel

    # ---------- 定位（主窗内 place） ----------
    def _place_panel(self) -> None:
        """悬浮小窗：紧贴内容区顶部右侧，投影与下方页面背景分层。"""
        if not getattr(self, "panel", None):
            return
        sp = self._sp  # 投影余量（RoundedFrame shadow）
        self.panel.place(
            relx=1.0, rely=0.0,
            x=-self.PAD, y=0,
            anchor="ne",
            width=self.WIDTH + sp, height=self.HEIGHT + sp,
        )
        self.panel.lift()

    def set_visible(self, visible: bool) -> None:
        """由顶栏「✦」/ Ctrl+J 控制开合；面板内「✕」同样可关。"""
        self._visible = bool(visible)
        if visible:
            self._ensure_panel()
            self._place_panel()
            self.panel.lift()
            if not self.backend_src:
                threading.Thread(target=self._reset_backend_async, daemon=True).start()
        else:
            if getattr(self, "panel", None) is not None:
                try:
                    self.panel.place_forget()
                except tk.TclError:
                    pass
        hide_all_tooltips()

    # ---------- 会话：新对话 / 过往对话 ----------
    def new_chat(self) -> None:
        """开启新会话；当前会话自动存入过往对话。"""
        self._persist_session()
        self.history.clear()
        conv = self.chats.new_session()
        self.session_id = conv["id"]
        if self._built:
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.configure(state="disabled")
            self._append("sys", "已开启新对话")

    def _persist_session(self) -> None:
        if self.session_id and self.history:
            self.chats.save_messages(self.session_id, self.history)

    def _show_history(self) -> None:
        """弹出过往对话列表，点击恢复。"""
        self._persist_session()
        items = self.chats.list_all()
        win = tk.Toplevel(self.app.root)
        win.title("历史会话")
        win.configure(bg=T.BG)
        win.geometry("420x520")
        win.transient(self.app.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(
            win, text="历史会话", bg=T.BG, fg=T.INK,
            font=(T.UI, T.FS_H2, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(
            win,
            text=("已保存到工程 chats/" if self.chats.chats_dir else "缓存于内存，关闭软件后清空"),
            bg=T.BG, fg=T.INK3, font=(T.UI, T.FS_CAPTION),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        wrap = tk.Frame(win, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        lb = tk.Listbox(
            wrap, font=(T.UI, T.FS_BODY), relief="flat",
            bg=T.CARD, fg=T.INK, highlightthickness=1,
            highlightbackground=T.LINE, selectbackground=T.ACCENT,
            selectforeground="#FFFFFF", activestyle="none",
        )
        W.SlimVScroll(wrap, lb).pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True)

        for c in items:
            title = c.get("title") or "未命名"
            updated = c.get("updated") or c.get("created") or ""
            n = len(c.get("messages") or [])
            lb.insert("end", f"{title}   ·  {n} 条  ·  {updated}")

        def open_sel(_e: Any = None) -> None:
            sel = lb.curselection()
            if not sel:
                return
            conv = items[sel[0]]
            self.history.clear()
            self.history.extend(conv.get("messages") or [])
            self.session_id = conv.get("id") or self.session_id
            if self._built:
                self.chat.configure(state="normal")
                self.chat.delete("1.0", "end")
                self.chat.configure(state="disabled")
                self._append("sys", f"已载入：{conv.get('title') or '会话'}")
                for m in self.history:
                    tag = "user" if m.get("role") == "user" else "bot"
                    self._append(tag, str(m.get("content") or ""))
            win.destroy()

        lb.bind("<Double-Button-1>", open_sel)
        bar = tk.Frame(win, bg=T.BG)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        W.btn(bar, "打开所选", open_sel, kind="accent", pady=6).pack(side="left")
        W.btn(bar, "关闭", win.destroy, kind="ghost", pady=6).pack(side="left", padx=(8, 0))

    # ---------- 消息 ----------
    def _append(self, tag: str, text: str) -> None:
        if not self._built:
            return
        lmar, rmar = self._MARGINS.get(tag, (int(8 * self.k), int(8 * self.k)))
        w = self.chat.winfo_width()
        if w < 40:  # 面板尚未排版：按面板设计宽度估列宽
            w = self.WIDTH
        cw = max(w - int(24 * self.k) - lmar - rmar, int(120 * self.k))
        fs = T.FS_SMALL if tag in ("sys", "tool", "err", "user_hdr", "bot_hdr") else T.FS_BODY
        body = soft_wrap(text.rstrip(), (T.UI, fs), cw)
        self.chat.configure(state="normal")
        if tag in ("user", "bot"):
            hdr = "user_hdr" if tag == "user" else "bot_hdr"
            name = "你" if tag == "user" else "助手"
            self.chat.insert("end", name + "\n", hdr)
        self.chat.insert("end", body + "\n", tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def _refresh_hint(self) -> None:
        """输入区左下角只显示模型名（裸标签，截断防挤走按钮）。"""
        if not self._built:
            return
        text = str(self.backend.get("model") or self.backend_src or "")
        try:
            self.hint.configure(text=text[:24] + ("…" if len(text) > 24 else ""))
        except tk.TclError:
            pass

    def reset_backend(self) -> None:
        """设置页保存后重新解析后端。"""
        self.backend, self.backend_src = resolve_backend(self.app)
        self._refresh_hint()
        if self._built:
            ok = bool(self.backend.get("base_url") and self.backend.get("model"))
            self._set_status("idle" if ok else "error")

    # ---------- 发送 ----------
    def _make_send_circle(self, parent: tk.Widget) -> tk.Canvas:
        """圆形发送键（↑ 图标，MiMo 风格）。"""
        d = int(32 * self.k)
        c = tk.Canvas(parent, width=d, height=d, bg=T.CARD,
                      highlightthickness=0, bd=0, cursor="hand2")
        lw = max(2, d // 12)

        def draw(hover: bool = False) -> None:
            c.delete("all")
            color = T.ACCENT_HOVER if hover else T.ACCENT
            # 矢量圆底（密采样弧）+ 矢量 ↑ 字形（ClearType 抗锯齿，无折线锯齿）
            W.rounded_rect(c, 1, 1, d - 1, d - 1, (d - 2) // 2, color, "")
            W.center_text(c, d / 2, d / 2, text="↑", fill="#FFFFFF",
                          font=(T.UI, int(32 * 0.55), "bold"))

        draw()
        c.bind("<Button-1>", lambda e: self._send())
        c.bind("<Enter>", lambda e: draw(True))
        c.bind("<Leave>", lambda e: draw(False))
        self._draw_send = draw
        return c

    def _on_enter(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:  # Shift
            return None
        self._send()
        return "break"

    def _stop(self) -> None:
        if self.busy:
            self._discard = True
            self._append("sys", "已请求停止")

    def _send(self) -> None:
        if self.busy:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        if not self.backend_src:
            self.reset_backend()
        if not (self.backend.get("base_url") and self.backend.get("model")):
            self._set_status("error")
            self._append("err", "未配置对话后端，请在「全局设置 → AI 助手」中配置。")
            return
        self.input.delete("1.0", "end")
        self._append("user", text)
        self.history.append({"role": "user", "content": text})
        self._persist_session()
        self._set_busy(True)
        threading.Thread(target=self._worker, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if not self._built:
            return
        self._set_status("busy" if busy else "idle")
        try:
            self.send_btn.configure(state="disabled" if busy else "normal")
        except tk.TclError:
            pass
        if busy:
            self.stop_btn.pack(side="right", padx=(0, int(6 * self.k)))
        else:
            self.stop_btn.pack_forget()
        if busy:
            self.chat.configure(state="normal")
            self._busy_mark = self.chat.index("end-1c")
            self.chat.insert("end", "思考中…\n", "sys")
            self.chat.see("end")
            self.chat.configure(state="disabled")
        elif getattr(self, "_busy_mark", None):
            try:
                self.chat.configure(state="normal")
                self.chat.delete(self._busy_mark, f"{self._busy_mark} + 1 line")
                self.chat.configure(state="disabled")
            except Exception:
                pass
            self._busy_mark = None

    def _system_message(self) -> str:
        base = str(self.backend.get("system_prompt") or "")
        app = self.app
        page = getattr(app, "current_page", "home")
        state = {
            "当前页面": page,
            "当前主题": T.current(),
            "设备": f"{app.device}（{app.device_desc}）",
            "工程": getattr(getattr(app, "project", None), "name", "") or "",
            "训练参数摘要": {k: app.cfg["train"].get(k) for k in
                             ("d_model", "n_head", "n_layer", "block_size", "batch_size",
                              "pretrain_steps", "sft_steps", "pretrain_lr", "sft_lr")},
            "采集摘要": {
                "文章数量": app.cfg["article"]["collection"].get("count"),
                "问答数量": app.cfg["response"]["collection"].get("count"),
            },
        }
        return (base + "\n\n[运行时状态，供你参考，无需复述]\n"
                + json.dumps(state, ensure_ascii=False, indent=1))

    def _worker(self) -> None:
        messages = [{"role": "system", "content": self._system_message()}]
        messages.extend(self.history[-16:])
        try:
            client = ChatClient.from_profile(self.backend, timeout=90)
            reply = client.chat(messages, max_tokens=int(self.backend.get("max_tokens") or 4096))
            self.q.put(("reply", reply))
        except ChatError as exc:
            self.q.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("error", f"未知错误：{exc}"))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "reply":
                    if self._discard:
                        self._discard = False
                        self._append("sys", "（回复已按要求忽略）")
                    else:
                        self._on_reply(str(payload))
                elif kind == "error":
                    self._set_status("error")
                    self._append("err", f"请求失败：{payload}")
                    self.history.append({"role": "assistant",
                                         "content": f"（请求失败：{payload}）"})
                    self.busy = False
                    if self._built:
                        try:
                            self.send_btn.configure(state="normal")
                        except tk.TclError:
                            pass
                    continue
                self._set_busy(False)
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.app.root.after(120, self._poll)
        except tk.TclError:
            pass

    def _on_reply(self, reply: str) -> None:
        plain, actions = extract_actions(reply)
        if plain:
            self._append("bot", plain)
            self.history.append({"role": "assistant", "content": plain})
        if actions:
            lines = apply_actions(self.app, actions)
            block = "⚙ 工具执行：\n" + "\n".join(lines)
            self._append("tool", block)
            self.history.append({"role": "assistant", "content": block})
        if not plain and not actions:
            self._set_status("error")
            self._append("err", "模型返回了无法解析的内容。")
        self._persist_session()

    def _reset_backend_async(self) -> None:
        try:
            backend, src = resolve_backend(self.app)
            self.backend, self.backend_src = backend, src
            ok = bool(backend.get("base_url") and backend.get("model"))

            def apply() -> None:
                self._refresh_hint()
                if self._built:
                    self._set_status("idle" if ok else "error")

            uiq.call_ui(apply)
        except Exception:
            pass
