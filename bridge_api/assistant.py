"""bridge_api/assistant.py —— AI 助手桥：对话 + 直接设定 EAI 内的参数。

与 Tk 版的关系（不重复实现）：
  · 逻辑全部复用 ENGINE_DIR/core/assistant.py：resolve_backend（优先级 config.assistant →
    mimo CLI 的 llm-server → mimocode 配置）、extract_actions（从回复里抠 ```json 动作块）、
    apply_actions（把动作落到 config，含 get/set/theme/show）。
  · 只重写"壳"：Tk 的 AssistantDock 是自绘悬浮窗，这里换成 HTTP + 增量事件流，
    界面由 renderer 负责（顶栏按钮 → 顶部挂载、不可拖动的面板）。

接口：
  GET  /api/assistant/state     后端可用性（来源/model/有无密钥；**绝不回显密钥值**）
  POST /api/assistant/send      {"text": "...", "page": "home"} 起一次提问（后台线程）
  GET  /api/assistant/status    ?since=N 增量事件（thinking / reply / actions / error / done）
  GET  /api/assistant/history   本次会话消息（不含 system）
  POST /api/assistant/clear     清空会话
  POST /api/assistant/stop      放弃当前这次回答（结果丢弃，不杀线程）

铁律：跑不了照实返回错误，不编造回复；密钥永不外泄。
"""
from __future__ import annotations

import io
import json
import os
import threading
from typing import Any

import _host                             # noqa: E402  同目录：load_cfg / save_cfg

ENGINE_DIR = _host.IDE_DIR                       # 2026-10-23：跟着应用中，换机器/换盘/改名都成立
HISTORY_LIMIT = 16
MAX_EVENTS = 400
_MODS: dict[str, Any] = {}


def _load():
    """core.* 必须按需 import。

    桥加载插件时 IDE 目录还没进 sys.path（bridge 是等到自己 import core.storage
    才补进去的），顶层写 `from core import ...` 会直接 No module named 'core'，
    整个插件被跳过且只在 /api/plugins 的 errors 里留一行。
    """
    if not _MODS:
        from core import assistant as A                 # noqa: PLC0415
        from core.client import ChatClient, ChatError   # noqa: PLC0415
        _MODS.update(A=A, ChatClient=ChatClient, ChatError=ChatError)
    return _MODS["A"], _MODS["ChatClient"], _MODS["ChatError"]

_LOCK = threading.Lock()
_EVENTS: list[dict[str, Any]] = []
_HISTORY: list[dict[str, str]] = []
_JOB = {"running": False, "discard": False, "seq": 0, "gen": 0}
# 会改变软件状态的动作不立刻执行，先存这儿，等前端点「执行」调 /api/assistant/apply
_PENDING: dict[str, Any] = {"actions": []}
# 面板当前会话的 id：落盘在工程 chats/<id>.json 里（与「对话验证」页共用同一套会话文件）
_SESSION: dict[str, Any] = {"conv": None}
READONLY_TOOLS = ("get_config",)


# ---------- apply_actions 需要一个"app"替身 ----------
class _Shim:
    """core.assistant.apply_actions/resolve_backend 只看这几个属性，这里给最小替身。"""

    BASE = ENGINE_DIR

    def __init__(self) -> None:
        self.cfg = _host.load_cfg()
        self.ui: dict[str, Any] = {"theme": None, "page": None}
        self.changed: list[str] = []

    # 以下三个是 apply_actions 会回调的入口
    def save_config(self) -> None:
        _host.save_cfg(self.cfg)

    def set_theme(self, name: str) -> None:
        self.ui["theme"] = name

    def on_config_changed(self, path: str) -> None:
        self.changed.append(path)

    def show(self, key: str) -> None:
        self.ui["page"] = key


_TLOCAL = threading.local()          # 每个 worker 线程记自己的「代次」


def _finish(gen: int) -> None:
    if gen == _JOB["gen"]:
        _finish(gen)


def _emit(kind: str, payload: Any) -> None:
    with _LOCK:
        gen = getattr(_TLOCAL, "gen", None)
        if gen is not None and gen != _JOB["gen"]:
            return                       # 已中止/换代：旧 worker 的迟到事件一律丢弃（用户 2026-10-15）
        _EVENTS.append({"kind": kind, "payload": payload, "t": _JOB["seq"]})
        _JOB["seq"] += 1
        if len(_EVENTS) > MAX_EVENTS:
            del _EVENTS[: len(_EVENTS) - MAX_EVENTS]


def _runtime_state(shim: _Shim, page: str) -> dict[str, Any]:
    """给模型的运行时状态（与 Tk 版同义，但只取桥拿得到的、且不 import torch）。"""
    cfg = shim.cfg
    tr = cfg.get("train") or {}
    return {
        "当前页面": page or "home",
        "当前主题": (cfg.get("app") or {}).get("theme") or "light",
        "训练设备": tr.get("device") or "auto",
        "训练参数摘要": {k: tr.get(k) for k in
                        ("d_model", "n_head", "n_layer", "block_size", "batch_size",
                         "pretrain_steps", "sft_steps", "pretrain_lr", "sft_lr")},
        "采集摘要": {
            "文章数量": ((cfg.get("article") or {}).get("collection") or {}).get("count"),
            "问答数量": ((cfg.get("response") or {}).get("collection") or {}).get("count"),
        },
    }


def _param_manual() -> str:
    """《EAI 参数手册》（bridge_api/param_manual.md）：让助手能解释 EAI 的所有参数（用户 2026-10-21）。

    手册缺失时返回空串，不影响对话。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "param_manual.md")
    try:
        with io.open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


SYSTEM_TAIL = """

你可以修改本软件的配置：在回复末尾用 ```json 代码块输出动作，例如
{"actions": [{"path": "train.device", "value": "cpu"}, {"tool": "navigate", "key": "dataset"}]}
（path 是 config.json 的点路径，value 会按原类型自动转换；切页面用 {"tool": "navigate", "key": "..."}，
可用 key：home / repo / dataset / article / response / webcurate / formatconv / train_pipeline / train_pretrain / train_continue / train_full / train_lora / train_prune / train_quant / train_lora_merge / chat / recycle_dataset / recycle_weight / settings
（中文名 → key：总览=home、仓库=repo、数据集=dataset、语料蒸馏=article、响应蒸馏=response、网页整理=webcurate、格式转换=formatconv、预训练=train_pretrain、
继续预训练=train_continue、指令微调=train_full、LoRA 微调=train_lora、模型裁切=train_prune、量化=train_quant、对话验证=chat、回收站数据集=recycle_dataset、回收站权重=recycle_weight、设置=settings）；
换主题用 {"tool": "set_theme", "name": "dark"}；读配置用 {"tool": "get_config", "path": "train.device"}）。

**不要擅自替用户做主。** 任何会改变软件状态的动作（改配置、换主题、切页面）都要先问过用户：
正文里先说明你打算做什么并问一句（例如「需要的话我先帮你打开数据集页？」），再把动作写进 ```json 块；
这些动作会挂起，直到用户点「执行」才真正生效。用户没点头就不要催、也不要自己执行。get_config 是只读的，不受限制。

**参数问题看《EAI 参数手册》**（上面的大段 Markdown）：问参数含义 / 取值建议 / 在哪个页面，直接据手册回答，
不要为此调用 get_config、更不要把配置内容或键名清单倒进回复。只有用户问「当前设成了什么 / 现在是多少」才 get_config，
且必须带具体 path（例如 train.device），不要不带 path 读顶层。
"""


def _store():
    """面板会话与「对话验证」页共用同一个 core.chat_history.ChatHistory 单例（它自己会绑定当前工程）。"""
    try:
        from bridge_api import chat as _chat                      # noqa: PLC0415  同包，复用它的单例
        return _chat._history_store()
    except Exception:                                             # noqa: BLE001
        return None


def _ensure_conv():
    """取（或新建）面板当前会话；标题由 save_messages 按首条用户消息自动生成。"""
    h = _store()
    if not h:
        return None
    cid = _SESSION.get("conv")
    if cid:
        conv = h.find(cid)
        if conv is not None:
            return conv
    try:
        conv = h.new_session()
        conv["source"] = "assistant"                              # 标记来源：重启后按它找回上一次面板会话
    except Exception:                                             # noqa: BLE001
        return None
    _SESSION["conv"] = conv.get("id")
    return conv


def reset_session() -> None:
    """切工程时清掉面板当前会话（2026-10-18）。

    现象：切到新工程后面板仍挂着上个工程的消息，_persist 还会把它写进新工程 chats/。
    这里只清内存，旧会话文件留在原工程里不动。
    """
    with _LOCK:
        _HISTORY.clear()
    _SESSION["conv"] = None


def _persist() -> None:
    """把内存里的对话写进工程 chats/<id>.json。"""
    h = _store()
    if not h:
        return
    conv = _ensure_conv()
    if not conv:
        return
    with _LOCK:
        msgs = list(_HISTORY)
    try:
        h.save_messages(conv.get("id"), msgs)
    except Exception:                                             # noqa: BLE001
        pass


def _worker(text: str, page: str, gen: int) -> None:
    _TLOCAL.gen = gen
    shim = _Shim()
    try:
        A, ChatClient, ChatError = _load()
        backend, source = A.resolve_backend(shim)
    except Exception as exc:                                     # noqa: BLE001
        _emit("error", f"解析助手后端失败：{exc}")
        _emit("done", {"ok": False})
        _finish(gen)
        return
    key = str(backend.get("api_key") or "")
    if not (backend.get("base_url") and key):
        _emit("error", "助手未配置可用的 API（请在设置页「默认 API」填 base_url 与 api_key，"
                       "或让本机 mimo CLI 提供 llm-server）")
        _emit("done", {"ok": False})
        _finish(gen)
        return

    messages = [{"role": "system",
                 "content": str(backend.get("system_prompt") or "你是 EAI Studio 内的 AI 助手。")
                 + "\n\n[运行时状态，供你参考，无需复述]\n"
                 + json.dumps(_runtime_state(shim, page), ensure_ascii=False, indent=1)
                 + "\n\n" + _param_manual()
                 + SYSTEM_TAIL}]
    with _LOCK:
        messages.extend(_HISTORY[-HISTORY_LIMIT:])
    try:
        client = ChatClient.from_profile(backend, timeout=90)
        with _LOCK:
            messages.append({"role": "user", "content": text})
        raw = client.chat(messages, max_tokens=int(backend.get("max_tokens") or 4096))
    except ChatError as exc:
        _emit("error", str(exc))
        _emit("done", {"ok": False})
        _finish(gen)
        return
    except Exception as exc:                                     # noqa: BLE001
        _emit("error", f"未知错误：{exc}")
        _emit("done", {"ok": False})
        _finish(gen)
        return

    if gen != _JOB["gen"]:                 # 已中止：本次回答丢弃、不落历史（用户 2026-10-15）
        return

    body, actions = A.extract_actions(raw)
    # 只读动作立刻执行（结果供下一轮参考）；**会改东西的动作一律挂起，等用户点「执行」**
    readonly = [a for a in actions if str(a.get("tool") or "") in READONLY_TOOLS]
    pending = [a for a in actions if str(a.get("tool") or "") not in READONLY_TOOLS]
    results: list[str] = []
    if readonly:
        try:
            results = A.apply_actions(shim, readonly)
        except Exception as exc:                                 # noqa: BLE001
            results = [f"动作执行失败：{exc}"]
    with _LOCK:
        _PENDING["actions"] = list(pending)
    with _LOCK:
        _HISTORY.append({"role": "user", "content": text})
        _HISTORY.append({"role": "assistant", "content": body or raw})
    _persist()                                                   # 落盘：面板消息也要进「本工程的历史会话」
    _emit("reply", {"text": body or raw, "source": source,
                    "model": backend.get("model") or ""})
    if actions:
        _emit("actions", {"actions": actions, "results": results,
                          "ui": shim.ui, "changed": shim.changed,
                          "pending": pending, "needs_confirm": bool(pending)})
    _emit("done", {"ok": True})
    _finish(gen)


def register(router) -> None:            # noqa: ANN001
    def state(query: dict) -> dict:                       # noqa: ARG001
        shim = _Shim()
        try:
            A, _C, _E = _load()
            backend, source = A.resolve_backend(shim)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": True, "available": False, "error": str(exc)}
        return {"ok": True,
                "available": bool(backend.get("base_url") and backend.get("api_key")),
                "source": source,
                "model": backend.get("model") or "",
                "base_url": backend.get("base_url") or "",
                "has_key": bool(backend.get("api_key"))}

    def send(body: dict) -> dict:
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "内容为空"}
        if len(text) > 8000:
            return {"ok": False, "error": "内容过长（>8000 字符）"}
        with _LOCK:
            _EVENTS.clear()
            _JOB["seq"] = 0
            _PENDING["actions"] = []                     # 新一轮提问作废上一批待确认动作
            _JOB["gen"] = _JOB.get("gen", 0) + 1         # 换代：作废在跑的旧 worker（中止后可立即接力/直接发送）
            gen = _JOB["gen"]
        _JOB["running"] = True
        _JOB["discard"] = False
        threading.Thread(target=_worker, args=(text, str(body.get("page") or "home"), gen),
                         daemon=True).start()
        return {"ok": True, "note": "已开始，轮询 /api/assistant/status?since=N"}

    def status(query: dict) -> dict:
        try:
            since = int(query.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        with _LOCK:
            events = [e for e in _EVENTS if e["t"] >= since]
            seq = _JOB["seq"]
        return {"ok": True, "events": events, "next": seq, "running": _JOB["running"]}

    def history(query: dict) -> dict:                     # noqa: ARG001
        with _LOCK:
            msgs = list(_HISTORY)
        if not msgs:                                          # 重启后内存是空的：把上一次面板会话从盘里捞回来
            try:
                h = _store()
                convs = [c for c in (h.list_all() if h else [])
                         if str(c.get("source") or "") == "assistant"]
                if convs:
                    conv = convs[0]                           # list_all 按 updated 倒序
                    msgs = [{"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
                            for m in (conv.get("messages") or []) if isinstance(m, dict)]
                    _SESSION["conv"] = conv.get("id")
                    with _LOCK:
                        _HISTORY[:] = list(msgs)
            except Exception:                                 # noqa: BLE001
                pass
        return {"ok": True, "messages": msgs}

    def convs(query: dict) -> dict:                       # noqa: ARG001
        """AI 面板自己的历史会话清单（source=assistant）。

        用户 2026-10-20：「AI 窗口的历史和对话验证的串了」—— 面板历史浮层原来直接读
        /api/chat/history（那是对话验证页的会话），点一条还会跳到对话验证页。现在两边各看各的。
        """
        h = _store()
        items: list = []
        try:
            for c in (h.list_all() if h else []):
                if str(c.get("source") or "").strip().lower() != "assistant":
                    continue
                msgs = [m for m in (c.get("messages") or []) if isinstance(m, dict)]
                cid = str(c.get("id") or "")
                items.append({
                    "id": cid,
                    "title": c.get("title") or "新会话",
                    "updated": c.get("updated") or c.get("created") or "",
                    "count": len(msgs),
                    "current": cid == str(_SESSION.get("conv") or ""),
                })
        except Exception as exc:                              # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "items": items}

    def open_conv(body: dict) -> dict:
        """切到某条面板历史：装进内存并返回消息（面板里直接重画，不再跳到对话验证页）。"""
        cid = str(body.get("id") or "").strip()
        h = _store()
        conv = h.find(cid) if (h and cid) else None
        if conv is None:
            return {"ok": False, "error": "没找到这条会话"}
        if str(conv.get("source") or "").strip().lower() != "assistant":
            return {"ok": False, "error": "这条不是 AI 助手窗口的会话"}
        msgs = [{"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
                for m in (conv.get("messages") or []) if isinstance(m, dict)]
        with _LOCK:
            _HISTORY[:] = list(msgs)
            _EVENTS.clear()
            _JOB["gen"] = _JOB.get("gen", 0) + 1              # 作废正在跑的回复，避免串进新会话
        _JOB["running"] = False
        _SESSION["conv"] = cid
        return {"ok": True, "id": cid, "messages": msgs}

    def clear(body: dict) -> dict:                        # noqa: ARG001
        with _LOCK:
            _HISTORY.clear()
            _EVENTS.clear()
            _JOB["seq"] = 0
            _PENDING["actions"] = []
        _SESSION["conv"] = None                               # 下一句另开一个会话文件（旧文件留在历史里，不删）
        return {"ok": True}

    def stop(body: dict) -> dict:                         # noqa: ARG001
        with _LOCK:
            _JOB["gen"] = _JOB.get("gen", 0) + 1         # 作废当前代 worker：迟到回复/事件/历史一律丢（用户 2026-10-15）
            _JOB["discard"] = True
        _JOB["running"] = False
        return {"ok": True, "note": "已中止"}

    def apply_pending(body: dict) -> dict:                # noqa: ARG001
        """用户点了「执行」：把挂起的动作真正落地。"""
        with _LOCK:
            acts = list(_PENDING["actions"])
            _PENDING["actions"] = []
        if not acts:
            return {"ok": True, "results": [], "ui": {}, "changed": [], "note": "没有待确认的动作"}
        try:
            A, _C, _E = _load()
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": f"载入助手模块失败：{exc}"}
        shim = _Shim()
        try:
            results = A.apply_actions(shim, acts)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": f"动作执行失败：{exc}"}
        # 单条动作失败时 apply_actions 会返回以「✗」开头的结果，这里同步把 ok 置 false，
        # 前端才会显示「执行失败」而不是「已执行」（用户 2026-10-13 报的跳转失败就是这样被吞掉的）
        failed = [r for r in results if str(r).startswith("✗")]
        return {"ok": not failed, "results": results, "ui": shim.ui, "changed": shim.changed,
                "failed": failed}

    router.get("/api/assistant/state", state)
    router.post("/api/assistant/send", send)
    router.get("/api/assistant/status", status)
    router.get("/api/assistant/history", history)
    router.get("/api/assistant/convs", convs)
    router.post("/api/assistant/open", open_conv)
    router.post("/api/assistant/clear", clear)
    router.post("/api/assistant/stop", stop)
    router.post("/api/assistant/apply", apply_pending)
